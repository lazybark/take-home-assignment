"""Undoing a charge: stop, reverse or refund (all need the customer's tap)."""

from uuid import uuid4

import structlog

from payments.application.context import Context
from payments.application.errors import TerminalBusy
from payments.application.recovery import Recovery
from payments.application.store_policy import STORE_RETRY_SECONDS, StorePolicy
from payments.domain.budget import CLEANUP_RESERVE_SECONDS, RESPONSE_MARGIN_SECONDS
from payments.domain.cancel import CANCEL_DEADLINE_SECONDS, resolve_cancel_outcome, resolve_refund
from payments.domain.marketpay.currency import to_numeric
from payments.domain.marketpay.models import (
    CancelTransactionRequest,
    ProcessTransactionRequest,
    TransactionType,
)
from payments.domain.marketpay.outcomes import NotSent
from payments.domain.models import Operation, Payment
from payments.domain.recovery import lease_for, take_over
from payments.domain.repository import StoreUnavailable
from payments.domain.terminal_lock import UpdateKind
from payments.domain.transitions import (
    apply_purchase,
    apply_undo,
    claim_undo,
    record_undo_baseline,
    request_cancel,
)

log = structlog.get_logger(__name__)


class Undo:
    """Undoing a charge: stop a purchase still running, reverse an approved one, or
    refund one that completed despite an abort. Each needs the customer's tap."""

    def __init__(self, ctx: Context, store: StorePolicy, recovery: Recovery) -> None:
        self._repo = ctx.repo
        self._clock = ctx.clock
        self._ops = ctx.ops
        self._me = ctx.me
        self._ctx = ctx
        self._store = store
        self._recovery = recovery

    def stop_purchase(self, payment: Payment) -> Payment:
        """The purchase is still in flight: abort it, then let it settle."""

        until = self._cancel_deadline()
        payment = self._store.change(payment.id, request_cancel)  # nothing sent yet: may 503
        if payment.operation is not Operation.PURCHASE:
            # It settled between our read and the cancel: nothing of ours runs any more. An
            # abort now could stop the NEXT payment on this terminal (an abort names none).
            return payment
        abort = self._ops.abort(payment.terminal_id, until=until)

        if not self._recovery.orphaned(payment):
            # The request that created it is still driving it and will record the outcome
            # (the recorded cancel request turns "stopped" into "cancelled"). Leave time
            # to confirm ourselves if it disappears.
            payment = self._store.wait_while(
                payment,
                until - CLEANUP_RESERVE_SECONDS,
                still=lambda p: (
                    p.operation is Operation.PURCHASE and not self._recovery.orphaned(p)
                ),
            )

        if payment.operation is Operation.PURCHASE:
            # Nobody settles it (it was UNKNOWN, or its request died): take it over.
            claim_id = uuid4().hex
            claim = self._store.run(
                lambda: self._repo.update(
                    payment.id,
                    lambda cur: take_over(
                        cur,
                        self._me,
                        self._clock.now(),
                        until - self._clock.monotonic(),
                        claim_id=claim_id,
                    ),
                ),
                what="take_over",
            )

            if not self._store.won(claim, claim_id):
                return claim.payment  # its live request still has it
            resolution = self._ops.confirm_after_abort(
                payment.terminal_id, payment.reference, abort, until=until
            )

            payment = self._store.record(
                claim.payment, lambda cur, now: apply_purchase(cur, resolution, now), until=until
            )

        return payment

    def undo(self, payment: Payment, until: float | None = None) -> Payment:
        """Reverse (cancel-transaction) or refund a payment: a POS cancel, or a PARTIAL."""
        until = until if until is not None else self._cancel_deadline()
        claim_id = uuid4().hex
        # Nothing sent yet: if the store stays down, this raises (the caller answers 503).
        claim = self._store.run(
            lambda: self._repo.update(
                payment.id,
                lambda cur: claim_undo(
                    cur,
                    self._clock.now(),
                    self._me,
                    lease_for(self._clock.now(), CANCEL_DEADLINE_SECONDS),
                    claim_id=claim_id,
                ),
            ),
            what="claim_undo",
            until=min(until, self._clock.monotonic() + STORE_RETRY_SECONDS),
        )
        if claim.kind is UpdateKind.TERMINAL_BUSY:
            raise TerminalBusy(claim.blocking_lock)
        if not self._store.won(claim, claim_id):
            # Someone else claimed it first (a concurrent cancel): wait for theirs.
            return self.await_undo(claim.payment)

        payment = claim.payment

        log.info("undo_started", operation=payment.operation, attempt=payment.undo_attempts)

        # Both branches start a card-present transaction: the customer must tap again.
        # For a POS cancel the waiter is there; for a PARTIAL the customer has just tapped.
        # Neither is ever started by recovery.
        if payment.operation is Operation.REVERSAL:
            # By terminalTransactionId, with the original amount. The only undo for a
            # PARTIAL: we never learn how much of it was approved, so a REFUND (which must
            # name an amount) could pay out more than was charged.
            baseline = self._ops.last_record_id(payment.terminal_id, until=until)
            if self._baseline_recorded(payment, baseline):
                result = self._ops.reverse(
                    payment.terminal_id,
                    CancelTransactionRequest(
                        terminal_transaction_id=payment.provider_transaction_id,
                        ecr_transaction_id=payment.reference,
                        amount=str(payment.amount),
                        currency=to_numeric(payment.currency),
                        ecr_params=self._ctx.ecr_params(payment.id, Operation.REVERSAL),
                    ),
                    until=until,
                    baseline_transaction_id=baseline,
                )
            else:
                result = resolve_cancel_outcome(
                    NotSent(reason="repeated reversal without a stored baseline: not sent")
                )
        else:
            # REFUND: required after an abort was sent (MarketPay), or without a
            # terminalTransactionId. A NEW ecrTransactionId per attempt (rf01…, rf02…):
            # MarketPay treats ecrTransactionId as the idempotency key.
            started = self._clock.monotonic()
            refund = self._ops.run_transaction(
                payment.terminal_id,
                ProcessTransactionRequest(
                    ecr_transaction_id=payment.refund_reference,
                    amount=str(payment.amount),
                    currency=to_numeric(payment.currency),
                    transaction_type=TransactionType.REFUND,
                    ecr_params=self._ctx.ecr_params(payment.id, Operation.REFUND),
                ),
                started=started,
                deadline_seconds=max(1, int(until - started)),
                before_abort=lambda: None,
                on_late=lambda late: self._store.record_late(
                    payment.id, lambda cur, now: apply_undo(cur, resolve_refund(late), now)
                ),
            )
            result = resolve_refund(refund)

        # MarketPay has spoken: record it, or (store down) still answer with it.
        return self._store.record(
            payment, lambda cur, now: apply_undo(cur, result, now), until=until
        )

    def _baseline_recorded(self, payment: Payment, baseline: str | None) -> bool:
        """May this reversal be sent? Its record is our ecrTransactionId with a NEW
        terminalTransactionId, "new" meaning neither the purchase's id nor the baseline.

        A first attempt can be told apart by the purchase's id alone, so its baseline is
        a best-effort write-ahead note. A repeated attempt can't: the previous attempt's
        record also carries our ecrTransactionId and a new id. So it is sent only once its
        baseline is known *and* stored (a crash must not leave recovery unable to tell)."""

        if payment.undo_attempts <= 1:
            self._store.note(payment.id, lambda cur, now: record_undo_baseline(cur, baseline, now))
            return True
        if baseline is None:
            log.warning("reversal_not_sent", reason="no baseline for a repeated reversal")
            return False
        try:
            self._store.change(
                payment.id, lambda cur, now: record_undo_baseline(cur, baseline, now)
            )
        except StoreUnavailable:
            log.warning("reversal_not_sent", reason="baseline not stored")
            return False
        return True

    def await_undo(self, payment: Payment) -> Payment:
        """A reversal/refund was already sent: wait for the request driving it, or take
        it over if that request is gone."""
        until = self._cancel_deadline()
        in_flight = (Operation.REVERSAL, Operation.REFUND)
        payment = self._store.wait_while(
            payment,
            until,
            still=lambda p: p.operation in in_flight and not self._recovery.orphaned(p),
        )

        if payment.operation in in_flight and self._recovery.orphaned(payment):
            poll_until = until - CLEANUP_RESERVE_SECONDS
            payment = self._recovery.take_over_and_recover(payment, poll_until, until)

        return self._recovery.recheck(payment)

    def _cancel_deadline(self) -> float:
        return self._clock.monotonic() + CANCEL_DEADLINE_SECONDS - RESPONSE_MARGIN_SECONDS
