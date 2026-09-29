"""Recovery: settling operations that nobody is driving, and re-checking inferred outcomes."""

from uuid import UUID, uuid4

import structlog

from payments.application.context import Context, diagnostics
from payments.application.store_policy import StorePolicy
from payments.domain.cancel import UndoOutcome, UndoResult, observe_reversal, resolve_refund
from payments.domain.marketpay.models import LastTransactionResult, LastTransactionState
from payments.domain.models import (
    Operation,
    Payment,
    PaymentState,
    Resolution,
    ResolvedVia,
    StateReason,
    TerminalLock,
    UndoReason,
)
from payments.domain.outcomes import Sighting, observe_last_transaction
from payments.domain.recovery import (
    conclude_never_recorded,
    conclude_refund,
    conclude_reversal,
    is_orphaned,
    take_over,
)
from payments.domain.repository import StoreUnavailable
from payments.domain.transitions import (
    apply_purchase,
    apply_undo,
    mark_abort_requested,
    release_partial_due,
    settle_without_operation,
)
from payments.domain.verification import needs_verification, verify_against

log = structlog.get_logger(__name__)


def _purchase_is_last(last: LastTransactionResult, reference: str) -> bool:
    tx = last.transaction_result
    echoed = tx.final_transaction_params if tx else None

    return (
        last.last_transaction_state is LastTransactionState.FINISHED
        and last.cancellation_result is None
        and echoed is not None
        and echoed.ecr_transaction_id == reference
    )


def partial_reversal_due(payment: Payment) -> bool:
    return (
        payment.operation is Operation.REVERSAL
        and payment.undo_reason is UndoReason.PARTIAL_APPROVAL
        and payment.undo_started_at is None
    )


def _undo_unclear() -> UndoResult:
    return UndoResult(
        outcome=UndoOutcome.UNCLEAR,
        reason=StateReason.AWAITING_UNDO,
        via=ResolvedVia.LAST_TRANSACTION,
        detail="reversal outcome not visible after recovery",
    )


class Recovery:
    """Settling operations nobody is driving: one look (`recheck`), a take-over after a
    crash (`take_over_and_recover`), and the re-check of outcomes we only inferred."""

    def __init__(self, ctx: Context, store: StorePolicy) -> None:
        self._repo = ctx.repo
        self._clock = ctx.clock
        self._ops = ctx.ops
        self._me = ctx.me
        self._store = store

    # --- Re-checking inferred outcomes ------------------------------------------------

    def verify_previous(self, terminal_id: str, payment_id: UUID, *, final: bool) -> None:
        """One look at the terminal for an earlier payment whose outcome we only inferred.
        If its transaction turned up after all, correct the record to what MarketPay holds
        (never reversed automatically: that needs a customer's tap, domain.verification).

        `final`: the caller is about to send a transaction that overwrites the evidence, so
        this is the last chance — the flag is cleared either way. A reconcile (not final)
        keeps the flag when it finds nothing: a late request may still land.

        If MarketPay can't be asked, the caller goes ahead (one look is all we spend on a
        flagged terminal). But in `final` mode a store failure raises StoreUnavailable: a
        correction we found, or couldn't check, must not be overwritten by the caller."""

        with structlog.contextvars.bound_contextvars(verified_payment_id=str(payment_id)):
            try:
                self._verify(terminal_id, payment_id, final=final)
            except StoreUnavailable:
                log.error("verification_failed", reason="store unavailable", final=final)
                if final:
                    raise

    def _verify(self, terminal_id: str, payment_id: UUID, *, final: bool) -> None:
        previous = self._store.run(lambda: self._repo.get(payment_id), what="get")
        settled = previous is None or not needs_verification(previous)
        if not settled:
            last = self._ops.observe_once(terminal_id, lambda last: last)
            if last is None:
                log.warning("verification_skipped", reason="no last-transaction answer")
            else:
                # `change`, not `record`: a correction that isn't stored must not be lost
                # quietly (the evidence is about to be overwritten).
                corrected = self._store.change(
                    previous.id, lambda cur, now: verify_against(cur, last, now)
                )
                settled = corrected != previous

                if settled:
                    log.error(
                        "late_outcome_found",
                        reference=previous.reference,
                        was=previous.state,
                        now=corrected.state,
                        state_reason=corrected.state_reason,
                    )
                else:
                    log.info("verification_found_nothing", reference=previous.reference)

        if settled or final:
            self._store.run(
                lambda: self._repo.clear_verification(terminal_id, payment_id),
                what="clear_verification",
            )

    def verify_flagged_terminals(self, terminal_id: str | None) -> None:
        try:
            flagged = self._store.run(lambda: self._repo.terminals_to_verify(), what="to_verify")
        except StoreUnavailable:
            return

        for terminal, payment_id in flagged.items():
            if terminal_id is None or terminal == terminal_id:
                self.verify_previous(terminal, payment_id, final=False)

    # --- Recovery ---------------------------------------------------------------------

    def orphaned(self, payment: Payment) -> bool:
        return is_orphaned(payment, self._me, self._clock.now())

    def take_over_and_recover(
        self, payment: Payment, poll_until: float, answer_by: float
    ) -> Payment:
        lease_seconds = max(1.0, answer_by - self._clock.monotonic())
        claim_id = uuid4().hex
        claim = self._store.run(
            lambda: self._repo.update(
                payment.id,
                lambda cur: take_over(
                    cur, self._me, self._clock.now(), lease_seconds, claim_id=claim_id
                ),
            ),
            what="take_over",
        )

        if not self._store.won(claim, claim_id):
            return claim.payment  # someone alive (another request, a reconcile) has it

        log.info("recovery_started", operation=payment.operation, previous_owner=payment.owner)

        return self._recover(claim.payment, poll_until, answer_by)

    def _recover(self, payment: Payment, poll_until: float, answer_by: float) -> Payment:
        """Settle an operation this process now owns. Never re-sends it."""

        match payment.operation:
            case Operation.PURCHASE:
                resolution = self._ops.settle_transaction(
                    payment.terminal_id,
                    payment.reference,
                    poll_until=poll_until,
                    answer_by=answer_by,
                    before_abort=lambda: self._store.note(payment.id, mark_abort_requested),
                )
                resolution = self._unless_never_recorded(payment, payment.reference, resolution)

                return self._store.record(
                    payment, lambda cur, now: apply_purchase(cur, resolution, now), until=answer_by
                )

            case Operation.REFUND if payment.undo_started_at is not None:
                refund = self._ops.settle_transaction(
                    payment.terminal_id,
                    payment.refund_reference,
                    poll_until=poll_until,
                    answer_by=answer_by,
                    before_abort=lambda: None,
                )
                refund = self._unless_never_recorded(payment, payment.refund_reference, refund)
                result = resolve_refund(refund)

                return self._store.record(
                    payment, lambda cur, now: apply_undo(cur, result, now), until=answer_by
                )

            case Operation.REVERSAL if payment.undo_started_at is not None:
                result = (
                    self._ops.watch_reversal(
                        payment.terminal_id,
                        payment.reference,
                        payment.provider_transaction_id,
                        payment.undo_baseline_transaction_id,
                        until=answer_by,
                    )
                    or self._look_at_reversal(payment)
                    or _undo_unclear()
                )

                return self._store.record(
                    payment, lambda cur, now: apply_undo(cur, result, now), until=answer_by
                )

        return payment  # a REFUND / PARTIAL reversal due but never sent: the caller decides

    # --- Rechecking unresolved payments ------------------------------------------------

    def recheck(self, payment: Payment) -> Payment:
        """One last-transaction look at an operation nobody is driving (UNKNOWN, or its
        request died). A terminal that finished in the meantime settles here, and frees
        itself. Cheap and side-effect free on the terminal: used by POS retries, cancels, and a
        new payment that meets the lock (reads never ask MarketPay)."""

        if payment.operation is None:
            return payment
        if payment.state is not PaymentState.UNKNOWN and not self.orphaned(payment):
            return payment  # a live request is driving it
        match payment.operation:
            case Operation.PURCHASE:
                observation = self._ops.observe_once(
                    payment.terminal_id,
                    lambda last: observe_last_transaction(last, payment.reference),
                )

                if observation is not None and observation.sighting is Sighting.OURS_FINISHED:
                    resolution = observation.resolution
                else:
                    resolution = conclude_never_recorded(payment, observation, self._clock.now())

                if resolution is None:
                    return payment
                updated = self._store.record(
                    payment, lambda cur, now: apply_purchase(cur, resolution, now)
                )

            case Operation.REVERSAL if payment.undo_started_at is not None:
                result = self._look_at_reversal(payment)
                if result is None:
                    return payment

                updated = self._store.record(payment, lambda cur, now: apply_undo(cur, result, now))
            case Operation.REFUND if payment.undo_started_at is not None:
                result = self._ops.observe_once(
                    payment.terminal_id,
                    lambda last: conclude_refund(
                        observe_last_transaction(last, payment.refund_reference),
                        _purchase_is_last(last, payment.reference),
                        payment,
                        self._clock.now(),
                    ),
                )

                if result is None:
                    return payment

                updated = self._store.record(payment, lambda cur, now: apply_undo(cur, result, now))
            case _:
                return payment  # an undo due but never sent: nothing to look for yet
        # `checked_*`: the payment looked at (may differ from the request's own payment).
        log.info(
            "payment_rechecked",
            checked_payment_id=str(updated.id),
            checked_reference=updated.reference,
            **diagnostics(updated),
        )

        return updated

    def _partial_left_behind(self, payment: Payment) -> bool:
        """A PARTIAL whose reversal is due while nobody drives it: learned after its request
        ended (a late answer, a look). Recovery never starts a card transaction, so only a
        POS cancel would still send it."""
        return partial_reversal_due(payment) and self.orphaned(payment)

    def _release_partial(self, payment: Payment) -> Payment:
        """Free the terminal now (not only at the next reconcile); a person settles it."""
        released = self._store.record(payment, release_partial_due)
        log.warning("partial_left_to_a_person", **diagnostics(released))
        return released

    def _unless_never_recorded(
        self, payment: Payment, reference: str, resolution: Resolution
    ) -> Resolution:
        """Recovery ended without an answer: one more look, and apply the time rule."""

        if resolution.state is not PaymentState.UNKNOWN:
            return resolution

        observation = self._ops.observe_once(
            payment.terminal_id, lambda last: observe_last_transaction(last, reference)
        )

        if observation is not None and observation.sighting is Sighting.OURS_FINISHED:
            return observation.resolution

        return conclude_never_recorded(payment, observation, self._clock.now()) or resolution

    def _look_at_reversal(self, payment: Payment) -> UndoResult | None:
        return self._ops.observe_once(
            payment.terminal_id,
            lambda last: conclude_reversal(
                observe_reversal(
                    last,
                    payment.reference,
                    payment.provider_transaction_id,
                    payment.undo_baseline_transaction_id,
                ),
                payment,
                self._clock.now(),
            ),
        )

    def look_once_without_operation(self, payment: Payment) -> Payment:
        """An open payment holding no operation (a partial reversal, or a record from
        before operations existed): one look; it can settle but never takes the lock."""
        if payment.state_reason is StateReason.PARTIAL_REVERSAL:
            return payment  # needs a person

        observation = self._ops.observe_once(
            payment.terminal_id,
            lambda last: observe_last_transaction(last, payment.reference),
        )

        if observation is None or observation.sighting is not Sighting.OURS_FINISHED:
            return payment

        resolution = observation.resolution

        return self._store.record(
            payment, lambda cur, now: settle_without_operation(cur, resolution, now)
        )

    def settle_blocker(self, lock: TerminalLock | None) -> bool:
        """If the terminal is held by a payment nobody drives, one quick look may settle
        it. True if that freed the terminal. (Full recovery: POST /reconcile.)"""

        if lock is None:
            return False
        try:
            blocker = self._store.run(lambda: self._repo.get(lock.payment_id), what="get")
            if blocker is None:
                return False
            blocker = self.recheck(blocker)
            if self._partial_left_behind(blocker):
                # Another payment needs the terminal, and nobody will send this PARTIAL's
                # reversal (a POS cancel would, while the customer is there: not now).
                blocker = self._release_partial(blocker)

            return blocker.operation is None

        except StoreUnavailable:
            return False  # can't tell: the new payment gets 409 terminal_busy
