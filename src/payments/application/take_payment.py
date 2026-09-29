"""Take a payment: POST /payments."""

from datetime import timedelta
from uuid import uuid4

import structlog
from pydantic import BaseModel, ConfigDict

from payments.application.context import Context, diagnostics
from payments.application.errors import IdempotencyMismatch, TerminalBusy
from payments.application.recovery import Recovery, partial_reversal_due
from payments.application.store_policy import StorePolicy
from payments.application.undo import Undo
from payments.domain.budget import RESPONSE_MARGIN_SECONDS, process_budget
from payments.domain.marketpay.currency import to_numeric
from payments.domain.marketpay.models import ProcessTransactionRequest
from payments.domain.models import Operation, Payment, PaymentState, payment_id_for
from payments.domain.recovery import lease_for
from payments.domain.repository import StoreUnavailable
from payments.domain.terminal_lock import BeginKind, idempotency_mismatch
from payments.domain.transitions import abandon_unsent, apply_purchase, mark_abort_requested

log = structlog.get_logger(__name__)


class NewPayment(BaseModel):
    """A validated request to take a payment (the service's input, not the HTTP body)."""

    model_config = ConfigDict(frozen=True)

    terminal_id: str
    amount: int
    currency: str
    reference: str
    deadline_seconds: int = 60


class CreatePaymentResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    payment: Payment
    created: bool  # False: an existing payment was returned for a repeated reference


class TakePayment:
    """Use case: take a payment (POST /payments), synchronous and within its deadline."""

    def __init__(self, ctx: Context, store: StorePolicy, recovery: Recovery, undo: Undo) -> None:
        self._repo = ctx.repo
        self._clock = ctx.clock
        self._ops = ctx.ops
        self._me = ctx.me
        self._ctx = ctx
        self._store = store
        self._recovery = recovery
        self._undo = undo

    def take(self, new: NewPayment) -> CreatePaymentResult:
        started = self._clock.monotonic()  # the deadline counts from here
        payment_id = payment_id_for(new.reference)
        structlog.contextvars.bind_contextvars(
            payment_id=str(payment_id), reference=new.reference, terminal_id=new.terminal_id
        )

        # 1. Atomically: record the intent and lock the terminal before touching it.
        # The id derived from the reference makes this the idempotency check too.
        now = self._clock.now()
        candidate = Payment(
            id=payment_id,
            terminal_id=new.terminal_id,
            amount=new.amount,
            currency=new.currency,
            reference=new.reference,
            state=PaymentState.PENDING,
            operation=Operation.PURCHASE,
            owner=self._me,
            lease_until=lease_for(now, new.deadline_seconds),
            deadline_at=now + timedelta(seconds=new.deadline_seconds),
            created_by=uuid4().hex,
            created_at=now,
            updated_at=now,
        )

        # Nothing has been sent yet: if the store stays down, fail (503) — nothing charged.
        decision = self._store.run(lambda: self._repo.begin(candidate), what="begin")
        if decision.kind is BeginKind.TERMINAL_BUSY and self._recovery.settle_blocker(
            decision.blocking_lock
        ):
            # The blocker just resolved: try once more.
            decision = self._store.run(lambda: self._repo.begin(candidate), what="begin")

        match decision.kind:
            case BeginKind.TERMINAL_BUSY:
                log.info("terminal_busy", blocked_by=decision.blocking_lock.reference)

                raise TerminalBusy(decision.blocking_lock)

            case BeginKind.DUPLICATE:
                log.info("payment_duplicate_reference", state=decision.payment.state)

                # Same reference, different order: refuse rather than return (or re-charge)
                # the wrong payment. The terminal is not touched.
                if mismatch := idempotency_mismatch(decision.payment, candidate):
                    log.info("payment_idempotency_mismatch", fields=mismatch)

                    raise IdempotencyMismatch(decision.payment, mismatch, candidate)

                payment = self._resume(decision.payment, started, new.deadline_seconds)

                return CreatePaymentResult(payment=payment, created=False)

        log.info("payment_created", amount=new.amount, currency=new.currency)

        # 1b. The previous payment on this terminal ended on an inference ("no record of it"):
        # before our transaction overwrites the evidence, check once.
        if decision.verify_payment_id is not None:
            try:
                self._recovery.verify_previous(
                    new.terminal_id, decision.verify_payment_id, final=True
                )
            except StoreUnavailable:
                # We couldn't store (or check) the earlier payment's correction, and our
                # transaction would overwrite its evidence: don't send. Nothing is charged;
                # release the terminal if the store lets us, and answer 503.
                self._store.note(payment_id, abandon_unsent)
                raise

        # 2. Run it on the terminal: blocks while the customer taps and the bank answers.
        resolution = self._ops.run_transaction(
            new.terminal_id,
            ProcessTransactionRequest(
                ecr_transaction_id=new.reference,
                amount=str(new.amount),
                currency=to_numeric(new.currency),
                ecr_params=self._ctx.ecr_params(payment_id, Operation.PURCHASE),
            ),
            started=started,
            deadline_seconds=new.deadline_seconds,
            before_abort=lambda: self._store.note(payment_id, mark_abort_requested),
            # If MarketPay answers only after we've replied "unknown", record it then.
            on_late=lambda late: self._store.record_late(
                payment_id, lambda cur, now: apply_purchase(cur, late, now)
            ),
        )

        # 3. Record it; a confirmed outcome also releases the terminal (same transaction).
        #    MarketPay has spoken: if the store stays down we still answer with its outcome
        #    (never a 500 for a charge that happened). The stored record keeps the lock and
        #    converges later through a recheck or reconcile.
        payment = self._store.record(
            candidate,
            lambda cur, now: apply_purchase(cur, resolution, now),
            until=started + new.deadline_seconds - RESPONSE_MARGIN_SECONDS / 2,
        )

        log.info("payment_resolved", **diagnostics(payment))

        # 4. A PARTIAL must not stand: reverse it now, while the customer is still at
        #    the terminal (the reversal needs their tap), within this request's deadline.
        if partial_reversal_due(payment):
            budget = process_budget(new.deadline_seconds)
            try:
                payment = self._undo.undo(payment, until=started + budget.answer_within)
            except StoreUnavailable:
                # Couldn't claim it, so it was never sent: recovery hands it to a person.
                log.error("partial_reversal_not_started", reason="store unavailable")

        return CreatePaymentResult(payment=payment, created=True)

    def _resume(self, payment: Payment, started: float, deadline_seconds: int) -> Payment:
        """A repeated submit of an order already in flight (e.g. the POS retrying after
        our crash). Give the waiter the real outcome, within *this* request's deadline:
        join the live request driving it, or take over and recover an orphaned one."""

        if payment.operation is None:
            return payment
        budget = process_budget(deadline_seconds)
        answer_by = started + budget.answer_within

        if not self._recovery.orphaned(payment):
            payment = self._store.wait_while(
                payment,
                answer_by,
                still=lambda p: p.operation is not None and not self._recovery.orphaned(p),
            )

        if payment.operation is not None and self._recovery.orphaned(payment):
            payment = self._recovery.take_over_and_recover(
                payment, poll_until=started + budget.resolve_within, answer_by=answer_by
            )

        return self._recovery.recheck(payment)
