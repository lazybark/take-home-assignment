"""Cancel a payment: POST /payments/{id}/cancel."""

from enum import StrEnum
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

from payments.application.context import Context, diagnostics
from payments.application.recovery import Recovery
from payments.application.store_policy import StorePolicy
from payments.application.undo import Undo
from payments.domain.cancel import CancelPlan, plan_cancel
from payments.domain.models import Payment, PaymentState
from payments.domain.verification import needs_verification

log = structlog.get_logger(__name__)


class CancelKind(StrEnum):
    CANCELLED = "cancelled"  # 200: nothing is charged (or it was already cancelled)
    SETTLED_WITHOUT_CHARGE = "settled_without_charge"  # 200: it ended declined/failed
    UNRESOLVED = "unresolved"  # 200: outcome not visible yet (unknown / still pending)
    NOT_CANCELLABLE = "not_cancellable"  # 409: declined/failed before we did anything
    NEEDS_ATTENTION = "needs_attention"  # 409: e.g. partial reversal, manual follow-up
    UNDO_FAILED = "undo_failed"  # 409: reversal/refund definitively not done; still charged


class CancelResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: CancelKind
    payment: Payment


def _cancel_kind(payment: Payment) -> CancelKind:
    match payment.state:
        case PaymentState.CANCELLED:
            return CancelKind.CANCELLED
        case PaymentState.APPROVED if payment.operation is None:
            return CancelKind.UNDO_FAILED
        case PaymentState.DECLINED | PaymentState.FAILED:
            return CancelKind.SETTLED_WITHOUT_CHARGE
        case PaymentState.UNKNOWN if payment.operation is None:
            return CancelKind.NEEDS_ATTENTION
        case _:
            return CancelKind.UNRESOLVED


class CancelPayment:
    """Use case: cancel a payment (POST /payments/{id}/cancel)."""

    def __init__(self, ctx: Context, store: StorePolicy, recovery: Recovery, undo: Undo) -> None:
        self._repo = ctx.repo
        self._store = store
        self._recovery = recovery
        self._undo = undo

    def cancel(self, payment_id: UUID) -> CancelResult | None:
        """POS cancel: abort a purchase in flight, or reverse/refund an approved one."""

        payment = self._store.run(lambda: self._repo.get(payment_id), what="get")
        if payment is None:
            return None

        structlog.contextvars.bind_contextvars(
            payment_id=str(payment_id), reference=payment.reference, terminal_id=payment.terminal_id
        )

        payment = self._recovery.recheck(payment)
        if needs_verification(payment):
            # We only inferred its outcome (e.g. "the reversal never landed"). A new undo
            # would overwrite the evidence, so look first: it may have landed after all.
            self._recovery.verify_previous(payment.terminal_id, payment.id, final=True)
            payment = self._store.run(lambda: self._repo.get(payment_id), what="get")
        plan = plan_cancel(payment)

        log.info("cancel_requested", plan=plan, state=payment.state)

        match plan:
            case CancelPlan.ALREADY_CANCELLED:
                return CancelResult(kind=CancelKind.CANCELLED, payment=payment)
            case CancelPlan.NOT_CANCELLABLE:
                return CancelResult(kind=CancelKind.NOT_CANCELLABLE, payment=payment)
            case CancelPlan.NEEDS_ATTENTION:
                return CancelResult(kind=CancelKind.NEEDS_ATTENTION, payment=payment)
            case CancelPlan.STOP_PURCHASE:
                payment = self._undo.stop_purchase(payment)
            case CancelPlan.AWAIT_UNDO:
                payment = self._undo.await_undo(payment)

        # Approved (from the start, or completed despite our abort): undo it.
        if plan is CancelPlan.UNDO or (
            plan is CancelPlan.STOP_PURCHASE and plan_cancel(payment) is CancelPlan.UNDO
        ):
            payment = self._undo.undo(payment)

        result = CancelResult(kind=_cancel_kind(payment), payment=payment)

        log.info("cancel_finished", kind=result.kind, **diagnostics(payment))

        return result
