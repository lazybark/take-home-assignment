"""Pure rules for POS cancellation: what to do, and how to read a reversal's outcome."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from payments.domain.marketpay.models import (
    CancellationResult,
    LastTransactionResult,
    LastTransactionState,
    TransactionStatus,
)
from payments.domain.marketpay.outcomes import (
    Accepted,
    Ambiguous,
    CancelCompleted,
    CancelOutcome,
    NotSent,
    Rejected,
)
from payments.domain.models import (
    FINAL_STATES,
    Operation,
    Payment,
    PaymentState,
    Resolution,
    ResolvedVia,
    StateReason,
)

# Total time a cancel request may take (the contract gives cancel no deadline parameter).
CANCEL_DEADLINE_SECONDS = 60
# How often a request checks Firestore while another request drives the payment.
SETTLE_POLL_SECONDS = 0.5
# A reversal that never left us is re-sent; one that may have arrived never is.
MAX_REVERSAL_SENDS = 3


# --- What should this cancel request do? ----------------------------------------------


class CancelPlan(StrEnum):
    ALREADY_CANCELLED = "already_cancelled"  # 200, idempotent
    NOT_CANCELLABLE = "not_cancellable"  # 409: declined/failed — nothing was charged
    NEEDS_ATTENTION = "needs_attention"  # 409: e.g. a partial reversal; manual follow-up
    STOP_PURCHASE = "stop_purchase"  # purchase still in flight: abort it, let it settle
    UNDO = "undo"  # approved: reverse / refund it
    AWAIT_UNDO = "await_undo"  # a reversal/refund is already in flight: wait for it


def plan_cancel(payment: Payment) -> CancelPlan:
    if payment.state is PaymentState.CANCELLED:
        return CancelPlan.ALREADY_CANCELLED

    match payment.operation:
        case Operation.PURCHASE:
            return CancelPlan.STOP_PURCHASE
        case Operation.REVERSAL | Operation.REFUND if payment.undo_started_at is not None:
            return CancelPlan.AWAIT_UNDO
        case Operation.REFUND | Operation.REVERSAL:
            return CancelPlan.UNDO  # due, not sent yet: this request may claim it

    if payment.state is PaymentState.APPROVED:
        return CancelPlan.UNDO

    if payment.state in (PaymentState.DECLINED, PaymentState.FAILED):
        return CancelPlan.NOT_CANCELLABLE

    return CancelPlan.NEEDS_ATTENTION  # UNKNOWN with nothing in flight (e.g. a PARTIAL)


# --- Undo outcomes ---------------------------------------------------------------------


class UndoOutcome(StrEnum):
    DONE = "done"  # the charge was reversed / refunded
    NOT_DONE = "not_done"  # definitively not undone: the charge stands
    PARTIAL = "partial"  # MarketPay says PARTIAL: may be partly undone
    UNCLEAR = "unclear"  # not visible yet: must not guess


class UndoResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    outcome: UndoOutcome
    reason: StateReason
    via: ResolvedVia
    detail: str | None = None


def resolve_cancellation(result: CancellationResult, via: ResolvedVia) -> UndoResult:
    match result.status:
        case TransactionStatus.OK:
            return UndoResult(outcome=UndoOutcome.DONE, reason=StateReason.REVERSED, via=via)

        case TransactionStatus.NOK:
            return UndoResult(
                outcome=UndoOutcome.NOT_DONE,
                reason=StateReason.UNDO_REFUSED,
                via=via,
                detail="cancel-transaction: NOK",
            )

        case TransactionStatus.PARTIAL:
            return UndoResult(
                outcome=UndoOutcome.PARTIAL, reason=StateReason.PARTIAL_REVERSAL, via=via
            )

        case _:
            return UndoResult(
                outcome=UndoOutcome.UNCLEAR,
                reason=StateReason.AWAITING_UNDO,
                via=via,
                detail="cancellation result without status",
            )


def resolve_cancel_outcome(outcome: CancelOutcome) -> UndoResult:
    via = ResolvedVia.CANCEL_RESPONSE

    match outcome:
        case CancelCompleted(result=result):
            return resolve_cancellation(result, via)

        case NotSent(reason=reason):
            return UndoResult(
                outcome=UndoOutcome.NOT_DONE,
                reason=StateReason.UNDO_NOT_SENT,
                via=via,
                detail=reason,
            )

        case Rejected(status_code=status_code):
            return UndoResult(
                outcome=UndoOutcome.NOT_DONE,
                reason=StateReason.UNDO_REFUSED,
                via=via,
                detail=f"cancel-transaction: HTTP {status_code}",
            )

        case Accepted():
            return UndoResult(
                outcome=UndoOutcome.UNCLEAR,
                reason=StateReason.AWAITING_UNDO,
                via=via,
                detail="202 Accepted",
            )

        case Ambiguous(reason=reason):
            return UndoResult(
                outcome=UndoOutcome.UNCLEAR,
                reason=StateReason.AWAITING_UNDO,
                via=via,
                detail=reason,
            )


def resolve_refund(refund: Resolution) -> UndoResult:
    """Read a REFUND transaction's resolution as an undo of the original payment."""
    via = refund.resolved_via
    if refund.state is PaymentState.APPROVED:
        return UndoResult(outcome=UndoOutcome.DONE, reason=StateReason.REFUNDED, via=via)

    if refund.state in FINAL_STATES:
        if refund.state_reason is StateReason.NOT_SENT:
            reason = StateReason.UNDO_NOT_SENT
        elif _inferred(refund):
            # No record of the refund, not a refusal: a delayed one could still land, so
            # this is re-checked like any inferred outcome (domain.verification).
            reason = StateReason.UNDO_NOT_RECORDED
        else:
            reason = StateReason.UNDO_REFUSED

        return UndoResult(
            outcome=UndoOutcome.NOT_DONE,
            reason=reason,
            via=via,
            detail=f"refund {refund.state.value} ({refund.state_reason.value})",
        )

    return UndoResult(
        outcome=UndoOutcome.UNCLEAR,
        reason=StateReason.AWAITING_UNDO,
        via=via,
        detail=f"refund {refund.state_reason.value}",
    )


def _inferred(resolution: Resolution) -> bool:
    """A failure we concluded from the absence of a record, not from MarketPay's answer."""
    return resolution.state_reason is StateReason.NEVER_RECORDED or (
        resolution.state_reason is StateReason.ABORTED
        and resolution.resolved_via is ResolvedVia.ABORT_RESPONSE
    )


# --- Reading a reversal from last-transaction -------------------------------------------


class ReversalSighting(StrEnum):
    REVERSAL_FINISHED = "reversal_finished"  # this reversal's own record, with its status
    NO_NEW_RECORD = "no_new_record"  # still the purchase (or an earlier attempt) as last
    IN_PROGRESS = "in_progress"
    OTHER = "other"


class ReversalObservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    sighting: ReversalSighting
    result: UndoResult | None = None


def observe_reversal(
    last: LastTransactionResult,
    reference: str,
    purchase_transaction_id: str | None,
    baseline_transaction_id: str | None = None,
) -> ReversalObservation:
    """Interpret last-transaction for a reversal of the payment `reference`.

    Two shapes are recognised:
    - the spec's: a `cancellationResult` for our payment;
    - staging's: a `transactionResult` echoing *our* ecrTransactionId (a reversal
      reuses it) with a *new* terminalTransactionId — one that is neither the purchase's
      nor the baseline taken just before this reversal was sent (an earlier attempt's).
    """

    cancellation = last.cancellation_result
    params = cancellation.cancellation_params if cancellation else None
    if (
        cancellation is not None
        and params is not None
        and params.ecr_transaction_id == reference
        and (
            params.terminal_transaction_id is None
            or purchase_transaction_id is None
            or params.terminal_transaction_id == purchase_transaction_id
        )
    ):
        return ReversalObservation(
            sighting=ReversalSighting.REVERSAL_FINISHED,
            result=resolve_cancellation(cancellation, ResolvedVia.LAST_TRANSACTION),
        )

    if last.last_transaction_state is LastTransactionState.IN_PROGRESS:
        return ReversalObservation(sighting=ReversalSighting.IN_PROGRESS)

    tx = last.transaction_result
    echoed = tx.final_transaction_params if tx else None
    if last.last_transaction_state is not LastTransactionState.FINISHED or tx is None:
        return ReversalObservation(sighting=ReversalSighting.OTHER)

    # While we hold the terminal nothing else runs on it, so a landed reversal would be
    # its last record. The record we saw just before sending (the baseline), or another
    # payment's record, still being last means this reversal has not landed.
    if (
        baseline_transaction_id is not None
        and tx.terminal_transaction_id == baseline_transaction_id
    ):
        return ReversalObservation(sighting=ReversalSighting.NO_NEW_RECORD)
    if echoed is None:
        return ReversalObservation(sighting=ReversalSighting.OTHER)
    if echoed.ecr_transaction_id != reference:
        return ReversalObservation(sighting=ReversalSighting.NO_NEW_RECORD)

    known = {t for t in (purchase_transaction_id, baseline_transaction_id) if t}

    if not known or tx.terminal_transaction_id is None:
        return ReversalObservation(sighting=ReversalSighting.OTHER)  # can't tell them apart
    if tx.terminal_transaction_id in known:
        return ReversalObservation(sighting=ReversalSighting.NO_NEW_RECORD)

    return ReversalObservation(
        sighting=ReversalSighting.REVERSAL_FINISHED,
        result=resolve_reversal_record(tx.status, ResolvedVia.LAST_TRANSACTION),
    )


def resolve_reversal_record(status: TransactionStatus | None, via: ResolvedVia) -> UndoResult:
    """A reversal recorded as a transaction (staging): its status is the undo's."""

    match status:
        case TransactionStatus.OK:
            return UndoResult(outcome=UndoOutcome.DONE, reason=StateReason.REVERSED, via=via)

        case TransactionStatus.NOK:
            return UndoResult(
                outcome=UndoOutcome.NOT_DONE,
                reason=StateReason.UNDO_REFUSED,
                via=via,
                detail="reversal recorded as NOK",
            )

        case TransactionStatus.PARTIAL:
            return UndoResult(
                outcome=UndoOutcome.PARTIAL, reason=StateReason.PARTIAL_REVERSAL, via=via
            )

    return UndoResult(
        outcome=UndoOutcome.UNCLEAR,
        reason=StateReason.AWAITING_UNDO,
        via=via,
        detail="reversal record without status",
    )
