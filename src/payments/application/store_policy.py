"""How the use cases read and write the store, and retry it."""

from collections.abc import Callable
from uuid import UUID

import structlog

from payments.application.context import Context, diagnostics
from payments.domain.cancel import SETTLE_POLL_SECONDS
from payments.domain.models import Payment
from payments.domain.repository import Change, StoreUnavailable
from payments.domain.terminal_lock import UpdateKind, UpdateResult

log = structlog.get_logger(__name__)


# Retrying the store: backoff from 0.1 s, doubling, capped at 1 s. Calls made before
# anything was sent to MarketPay give up after STORE_RETRY_SECONDS (then 503).
STORE_RETRY_SECONDS = 5
STORE_RETRY_FIRST_SLEEP = 0.1
STORE_RETRY_MAX_SLEEP = 1.0


class StorePolicy:
    """How every use case talks to the store, depending on what MarketPay already knows.

    - Before anything was sent (`run`): retry transient failures briefly, then give up
      (the caller answers 503: nothing was charged).
    - After MarketPay answered (`record`): retry until the deadline, then return MarketPay's
      answer anyway (never a 500 for a charge that happened); the record converges later.
    - Write-ahead notes (`note`): best effort; their loss only weakens crash recovery.
    """

    def __init__(self, ctx: Context) -> None:
        self._repo = ctx.repo
        self._clock = ctx.clock
        self._me = ctx.me

    def run[R](self, call: Callable[[], R], *, what: str, until: float | None = None) -> R:
        """Run a store call, retrying transient failures with backoff until `until`."""
        until = until if until is not None else self._clock.monotonic() + STORE_RETRY_SECONDS
        delay, attempt = STORE_RETRY_FIRST_SLEEP, 0

        while True:
            attempt += 1
            try:
                return call()
            except StoreUnavailable as exc:
                if not exc.transient or self._clock.monotonic() + delay >= until:
                    log.error("store_unavailable", what=what, attempts=attempt, error=str(exc))
                    raise

                log.warning("store_retry", what=what, attempt=attempt, error=str(exc))

                self._clock.sleep(delay)
                delay = min(delay * 2, STORE_RETRY_MAX_SLEEP)

    def change(
        self, payment_id: UUID, transition: Callable, *, until: float | None = None
    ) -> Payment:
        """Apply a pure transition `(current, now) -> next` to the stored payment."""

        now = self._clock.now()
        change: Change = lambda current: transition(current, now)  # noqa: E731

        return self.run(
            lambda: self._repo.update(payment_id, change),
            what=getattr(transition, "__name__", "change"),
            until=until,
        ).payment

    def record(
        self, known: Payment, transition: Callable, *, until: float | None = None
    ) -> Payment:
        """Record what MarketPay said. If the store stays down, return that truth anyway
        (applied to the version we know); the stored record, still holding the terminal,
        converges through a later recheck or reconcile."""

        try:
            return self.change(known.id, transition, until=until)
        except StoreUnavailable:
            truth = transition(known, self._clock.now())

            log.error(
                "store_write_failed_returning_marketpay_outcome",
                state=truth.state,
                state_reason=truth.state_reason,
            )

            return truth

    def record_late(self, payment_id: UUID, transition: Callable) -> None:
        """A MarketPay answer that arrived after the request already replied (worker
        thread). Same pure transition as on time: if a recheck or reconcile settled the
        payment meanwhile, it changes nothing."""

        try:
            updated = self.change(payment_id, transition)

            log.info("late_answer_recorded", **diagnostics(updated))
        except Exception:
            log.exception("late_answer_not_recorded")  # a later look settles it instead

    def note(self, payment_id: UUID, transition: Callable) -> None:
        """Best-effort write-ahead note: its loss only weakens crash recovery."""

        try:
            self.change(payment_id, transition, until=self._clock.monotonic() + 1)
        except StoreUnavailable:
            log.error("store_note_failed", what=getattr(transition, "__name__", "note"))

    def won(self, claim: UpdateResult, claim_id: str) -> bool:
        """Did this request get the claim? UNCHANGED can still mean yes: our own earlier
        attempt landed and only its response was lost — the stored claim is then ours.
        (The owner alone can't tell: it names a process, and two requests of one process,
        e.g. a double-tapped cancel, must not both win.)"""

        if claim.kind is UpdateKind.UPDATED:
            return True

        p = claim.payment

        return (
            claim.kind is UpdateKind.UNCHANGED
            and p.claim_id == claim_id
            and p.operation is not None
        )

    def wait_while(
        self, payment: Payment, until: float, still: Callable[[Payment], bool]
    ) -> Payment:
        """Re-read the payment until `still(payment)` is false or time runs out.
        A failed read keeps the last version seen and tries again."""

        while True:
            if not still(payment) or self._clock.monotonic() >= until:
                return payment

            self._clock.sleep(SETTLE_POLL_SECONDS)
            try:
                payment = self._repo.get(payment.id) or payment
            except StoreUnavailable as exc:
                log.warning("store_read_failed_while_waiting", error=str(exc))
