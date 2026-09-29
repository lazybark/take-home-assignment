"""Driving MarketPay operations to a resolution, within a time budget.

No storage here: these methods talk to the terminal and return what it means. The caller
records the result. Every loop is bounded by an absolute deadline on the monotonic clock.

Card-present calls (process-transaction, cancel-transaction) run on a worker thread, so the
request can act while MarketPay still holds the call open: that is the only moment an
abort can stop the terminal. If a call outlives the request's deadline, its eventual
answer is handed to an `on_late` callback, so the payment still settles promptly.
"""

import contextvars
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

import structlog

from payments.application.clock import Clock
from payments.application.notification_inbox import NotificationInbox
from payments.domain.abort import (
    MAX_ABORT_ATTEMPTS,
    abort_answered,
    confirmation_complete,
    label_after_abort,
    resolve_after_abort,
)
from payments.domain.budget import (
    ABORT_TIMEOUT_SECONDS,
    CLEANUP_RESERVE_SECONDS,
    LOOKUP_TIMEOUT_SECONDS,
    POLL_INTERVAL_SECONDS,
    process_budget,
)
from payments.domain.cancel import (
    MAX_REVERSAL_SENDS,
    ReversalSighting,
    UndoOutcome,
    UndoResult,
    observe_reversal,
    resolve_cancel_outcome,
)
from payments.domain.marketpay.gateway import MarketPayGateway
from payments.domain.marketpay.models import (
    CancelTransactionRequest,
    LastTransactionResult,
    ProcessTransactionRequest,
)
from payments.domain.marketpay.outcomes import (
    AbortOutcome,
    AbortUnconfirmed,
    CancelOutcome,
    Found,
    NotSent,
    ProcessOutcome,
)
from payments.domain.models import FINAL_STATES, PaymentState, Resolution, ResolvedVia, StateReason
from payments.domain.outcomes import (
    Sighting,
    is_definitive_rejection,
    needs_lookup,
    observe_last_transaction,
    resolve_process_outcome,
)

log = structlog.get_logger(__name__)

# Lookups tried to confirm a 400/404 before calling the payment failed.
REJECTION_LOOK_ATTEMPTS = 2
# Worker threads for card-present calls held open by MarketPay (one per payment in flight).
CALL_WORKERS = 64
# Re-sending a request that never left us: backoff 0.5 s doubling to 4 s, for at most
# 15 s (a DNS/network blip), never past the abort point. A permanent failure — e.g. a
# rejected client certificate — then ends as failed / not_sent without a long wait.
RESEND_WINDOW_SECONDS = 15
RESEND_FIRST_DELAY_SECONDS = 0.5
RESEND_MAX_DELAY_SECONDS = 4.0

T = TypeVar("T")

# Receives the answer of a call that outlived its request's deadline (worker thread).
LateCallback = Callable[[Resolution], None]


class TerminalOperations:
    def __init__(
        self, marketpay: MarketPayGateway, clock: Clock, inbox: NotificationInbox | None = None
    ) -> None:
        self._marketpay = marketpay
        self._clock = clock
        self._inbox = inbox  # notified records (the bonus webhook); None: polling only
        self._calls = ThreadPoolExecutor(max_workers=CALL_WORKERS, thread_name_prefix="marketpay")

    def _start(self, call: Callable[[], T]) -> Future:
        """Run a MarketPay call on a worker thread, keeping this request's log context."""

        return self._calls.submit(contextvars.copy_context().run, call)

    # --- Transactions (purchase / refund) -------------------------------------------

    def run_transaction(
        self,
        terminal_id: str,
        request: ProcessTransactionRequest,
        *,
        started: float,
        deadline_seconds: int,
        before_abort: Callable[[], None],
        on_late: LateCallback | None = None,
    ) -> Resolution:
        """process-transaction, held open past our deadline:

        - answered before the abort point -> that answer (the happy path: one call);
        - still open at the abort point  -> abort WHILE it is open (only then does it
          work), then wait for the call to return the terminal's result;
        - no answer even by the deadline -> UNKNOWN, and the late answer goes to `on_late`.
        A 202, a lost reply or an undocumented 4xx falls back to poll -> abort -> confirm.

        `before_abort` runs just before the abort is sent (write-ahead record).
        """

        reference = request.ecr_transaction_id
        budget = process_budget(deadline_seconds)
        abort_at, answer_by = started + budget.resolve_within, started + budget.answer_within
        resend_until = min(abort_at, started + RESEND_WINDOW_SECONDS)
        resend_delay, sends = RESEND_FIRST_DELAY_SECONDS, 0

        abort: AbortOutcome | None = None
        while True:
            sends += 1
            call = self._start(
                lambda: self._marketpay.process_transaction(
                    terminal_id,
                    request,
                    wait_time=budget.wait_time,
                    read_timeout=budget.read_timeout,
                )
            )
            if not self._clock.wait_for(call, abort_at - self._clock.monotonic()):
                # Nobody has finished by now (the customer hasn't tapped): stop the
                # terminal while MarketPay still holds our request, so the open call
                # returns its NOK.
                before_abort()
                abort = self.abort(terminal_id, until=answer_by)

                if not self._clock.wait_for(call, answer_by - self._clock.monotonic()):
                    # 409 (the bank is still deciding) or no answer: say so, and let the
                    # late answer settle the payment the moment it arrives.
                    self._on_late(call, reference, abort, on_late, resolve_process_outcome)
                    log.warning("transaction_open_at_deadline", abort=abort.kind)
                    return _open_at_deadline(abort)
                break

            outcome_so_far = call.result()
            # The request never left us (DNS, TCP/TLS connect) — MarketPay never saw
            # it, so sending it again, with the SAME ecrTransactionId, can't start a
            # second charge. Absorb a short blip; give up well before the abort point.
            # (Never re-send for any other reason: a lost reply may hide a running
            # transaction, which last-transaction doesn't show.)
            if isinstance(outcome_so_far, NotSent) and (
                self._clock.monotonic() + resend_delay < resend_until
            ):
                log.warning(
                    "transaction_not_sent_resending", send=sends, reason=outcome_so_far.reason
                )
                self._clock.sleep(resend_delay)
                resend_delay = min(resend_delay * 2, RESEND_MAX_DELAY_SECONDS)

                continue

            break

        outcome: ProcessOutcome = call.result()
        log.info("transaction_answered", ecr_transaction_id=reference, outcome=outcome.kind)
        resolution = resolve_process_outcome(outcome, reference)
        if abort is not None:
            resolution = label_after_abort(abort, resolution)

        if is_definitive_rejection(outcome):
            return self._confirm_rejection(terminal_id, reference, resolution)

        if not needs_lookup(outcome):
            return resolution

        # A 202, a lost reply, an undocumented 4xx: the call proves nothing.
        if abort is not None:
            return self.confirm_after_abort(terminal_id, reference, abort, until=answer_by)

        return self.settle_transaction(
            terminal_id,
            reference,
            poll_until=abort_at,
            answer_by=answer_by,
            before_abort=before_abort,
        )

    def _on_late(
        self,
        call: Future,
        reference: str,
        abort: AbortOutcome,
        on_late: LateCallback | None,
        resolve: Callable,
    ) -> None:
        """When a call that outlived its request finally answers, pass on a *final* result
        (a late 202 or a lost reply says nothing: a later look settles those)."""
        if on_late is None:
            return

        def deliver(done: Future) -> None:
            try:
                if done.exception() is not None:
                    return

                resolution = resolve(done.result(), reference)
                if resolution.state in FINAL_STATES or resolution.state_reason in (
                    StateReason.PARTIAL_APPROVAL,
                ):
                    log.info("late_answer", reference=reference, state=resolution.state)

                    on_late(label_after_abort(abort, resolution))
            except Exception:
                log.exception("late_answer_failed", reference=reference)

        call.add_done_callback(deliver)

    def _confirm_rejection(
        self, terminal_id: str, reference: str, rejected: Resolution
    ) -> Resolution:
        """A 400/404 says nothing started — unless a copy of our request (duplicated by
        the network) did. One look at last-transaction catches that. If the look itself
        fails, the refusal stands: it is the evidence, the look is an extra guard."""

        for attempt in range(1, REJECTION_LOOK_ATTEMPTS + 1):
            lookup = self._marketpay.get_last_transaction(
                terminal_id, timeout=LOOKUP_TIMEOUT_SECONDS
            )

            if isinstance(lookup, Found):
                observation = observe_last_transaction(lookup.result, reference)

                if observation.sighting is Sighting.OURS_FINISHED:
                    log.warning("rejected_but_ours_finished", resolution=observation.resolution)

                    return observation.resolution

                return rejected.model_copy(
                    update={"state_detail": f"{rejected.state_detail}; no record of ours"}
                )

            log.warning("rejection_look_failed", attempt=attempt, reason=lookup.reason)

            if attempt < REJECTION_LOOK_ATTEMPTS:
                self._clock.sleep(POLL_INTERVAL_SECONDS)

        return rejected

    def settle_transaction(
        self,
        terminal_id: str,
        reference: str,
        *,
        poll_until: float,
        answer_by: float,
        before_abort: Callable[[], None],
    ) -> Resolution:
        """For a transaction that may be running (sent, answer unknown): watch
        last-transaction; if still open at `poll_until`, abort, then confirm what the
        terminal recorded. Never re-sends it. Also used by crash recovery."""
        seen = self.poll(
            terminal_id,
            until=poll_until,
            observe=lambda last: observe_last_transaction(last, reference),
            done=lambda seen: seen[-1].sighting is Sighting.OURS_FINISHED,
        )

        if seen and seen[-1].sighting is Sighting.OURS_FINISHED:
            return seen[-1].resolution

        # Still no answer: stop the terminal, then confirm what it recorded, so we
        # answer with a known state.
        before_abort()
        abort = self.abort(terminal_id, until=answer_by)

        return self.confirm_after_abort(terminal_id, reference, abort, until=answer_by)

    def confirm_after_abort(
        self, terminal_id: str, reference: str, abort: AbortOutcome, until: float
    ) -> Resolution:
        seen = self.poll(
            terminal_id,
            until=until,
            observe=lambda last: observe_last_transaction(last, reference),
            done=lambda seen: confirmation_complete(abort, seen),
        )

        resolution = resolve_after_abort(abort, seen[-1] if seen else None)
        log.info("abort_resolved", abort=abort.kind, state=resolution.state)

        return resolution

    def abort(self, terminal_id: str, until: float) -> AbortOutcome:
        """Send abort-transaction, retrying while it gets no usable answer."""

        outcome: AbortOutcome = AbortUnconfirmed(reason="no time left to send an abort")
        for attempt in range(1, MAX_ABORT_ATTEMPTS + 1):
            remaining = until - self._clock.monotonic()

            if remaining <= 0:
                break
            outcome = self._marketpay.abort_transaction(
                terminal_id, timeout=min(ABORT_TIMEOUT_SECONDS, remaining)
            )

            log.info("abort_sent", attempt=attempt, outcome=outcome.kind)

            if abort_answered(outcome):
                break

        return outcome

    # --- Reversal (cancel-transaction) ------------------------------------------------

    def reverse(
        self,
        terminal_id: str,
        request: CancelTransactionRequest,
        *,
        until: float,
        baseline_transaction_id: str | None,
    ) -> UndoResult:
        """cancel-transaction -> (202/lost: poll last-transaction for our cancellation).

        A reversal that may have reached MarketPay is never sent again: a second one can
        answer NOK meaning "already reversed", and we would wrongly report the charge as
        standing. Only a reversal that certainly never left us is re-sent.

        It waits for the customer's tap, so it can take tens of seconds; like a
        payment it is held open past the deadline and aborted while open at the abort
        point. Its record is our ecrTransactionId with a NEW terminalTransactionId, hence the
        baseline: the terminal's last record just before sending, which
        can't be this reversal's.
        """

        abort_at = until - CLEANUP_RESERVE_SECONDS
        result: UndoResult | None = None

        for attempt in range(1, MAX_REVERSAL_SENDS + 1):
            remaining = until - self._clock.monotonic()
            if remaining <= 1:
                break

            budget = process_budget(int(remaining) + 1)
            call = self._start(
                lambda budget=budget: self._marketpay.cancel_transaction(
                    terminal_id,
                    request,
                    wait_time=budget.wait_time,
                    read_timeout=budget.read_timeout,
                )
            )

            if not self._clock.wait_for(call, abort_at - self._clock.monotonic()):
                # Still waiting for the customer's tap: stop it while it's open (the only time
                # an abort works), so the terminal is left clean and the open call says how it
                # ended.
                abort = self.abort(terminal_id, until=until)
                if not self._clock.wait_for(call, until - self._clock.monotonic()):
                    log.warning("reversal_open_at_deadline", abort=abort.kind)

                    return UndoResult(
                        outcome=UndoOutcome.UNCLEAR,
                        reason=StateReason.AWAITING_UNDO,
                        via=ResolvedVia.ABORT_RESPONSE,
                        detail=f"reversal still open at the deadline; abort {abort.kind}",
                    )

            outcome: CancelOutcome = call.result()
            log.info("reversal_sent", attempt=attempt, outcome=outcome.kind)
            result = resolve_cancel_outcome(outcome)

            if not isinstance(outcome, NotSent):
                break
            self._clock.sleep(POLL_INTERVAL_SECONDS)

        if result is None or result.outcome is not UndoOutcome.UNCLEAR:
            return result or resolve_cancel_outcome(NotSent(reason="no time left to send"))

        finished = self.watch_reversal(
            terminal_id,
            request.ecr_transaction_id,
            request.terminal_transaction_id,
            baseline_transaction_id,
            until,
        )

        return finished or result  # still UNCLEAR: the caller keeps the terminal locked

    def watch_reversal(
        self,
        terminal_id: str,
        reference: str,
        purchase_transaction_id: str | None,
        baseline_transaction_id: str | None,
        until: float,
    ) -> UndoResult | None:
        """Watch last-transaction for this reversal's own record (None: not seen in time)."""

        seen = self.poll(
            terminal_id,
            until=until,
            observe=lambda last: observe_reversal(
                last, reference, purchase_transaction_id, baseline_transaction_id
            ),
            done=lambda seen: seen[-1].sighting is ReversalSighting.REVERSAL_FINISHED,
        )

        if seen and seen[-1].sighting is ReversalSighting.REVERSAL_FINISHED:
            return seen[-1].result

        return None

    # --- One-off lookups (rechecks) ---------------------------------------------------

    def last_record_id(
        self, terminal_id: str, attempts: int = 3, until: float | None = None
    ) -> str | None:
        """terminalTransactionId of the terminal's latest record (None if unavailable).

        Read just before a reversal, while we hold the terminal (so nothing else runs):
        whatever is last now can't be this reversal's record. Stops at `until`, if given.
        """

        for attempt in range(1, attempts + 1):
            timeout = LOOKUP_TIMEOUT_SECONDS
            if until is not None:
                timeout = min(timeout, until - self._clock.monotonic())
                if timeout <= 0:
                    return None
            lookup = self._marketpay.get_last_transaction(terminal_id, timeout=timeout)

            if isinstance(lookup, Found):
                tx = lookup.result.transaction_result

                return tx.terminal_transaction_id if tx else None

            log.warning("baseline_lookup_failed", attempt=attempt, reason=lookup.reason)

            if attempt < attempts:
                self._clock.sleep(POLL_INTERVAL_SECONDS)

        return None

    def observe_once(
        self, terminal_id: str, observe: Callable[[LastTransactionResult], T]
    ) -> T | None:
        lookup = self._marketpay.get_last_transaction(terminal_id, timeout=LOOKUP_TIMEOUT_SECONDS)

        if not isinstance(lookup, Found):
            log.warning("lookup_failed", reason=lookup.reason)

            return None

        return observe(lookup.result)

    # --- Polling ----------------------------------------------------------------------

    def poll(
        self,
        terminal_id: str,
        until: float,
        observe: Callable[[LastTransactionResult], T],
        done: Callable[[list[T]], bool],
    ) -> list[T]:
        """Ask last-transaction about once a second until `done(seen)` or time runs out.

        Returns every observation made (failed lookups aren't observations). An empty or
        inconclusive list means we still don't know — never that the operation failed.
        """
        seen: list[T] = []
        polls = 0

        while (remaining := until - self._clock.monotonic()) > 0:
            if (notified := self._notified(terminal_id, observe, done)) is not None:
                return [*seen, notified]

            lookup = self._marketpay.get_last_transaction(
                terminal_id, timeout=min(LOOKUP_TIMEOUT_SECONDS, remaining)
            )
            polls += 1

            if isinstance(lookup, Found):
                seen.append(observe(lookup.result))
                log.info("last_transaction_polled", poll=polls, sighting=seen[-1].sighting)

                if done(seen):
                    return seen
                # Never re-send because of this: "not ours" is also what a transaction
                # still running looks like — a second send could charge twice.
            else:
                log.warning("last_transaction_lookup_failed", poll=polls, reason=lookup.reason)
            self._pause(terminal_id, min(POLL_INTERVAL_SECONDS, until - self._clock.monotonic()))

        if (notified := self._notified(terminal_id, observe, done)) is not None:
            return [*seen, notified]

        log.info("polling_stopped", polls=polls)

        return seen

    def _notified(
        self,
        terminal_id: str,
        observe: Callable[[LastTransactionResult], T],
        done: Callable[[list[T]], bool],
    ) -> T | None:
        """A notified record that settles this poll on its own, read by the poll's own
        observer (so it means exactly what the same record from last-transaction would).
        Anything less is ignored: a notification may confirm an outcome, but never count
        as "no record of ours" (e.g. towards the two looks that conclude an abort)."""

        if self._inbox is None:
            return None

        for record in self._inbox.records(terminal_id):
            observation = observe(record)
            if done([observation]):
                log.info("settled_by_notification", terminal_id=terminal_id)

                return observation

        return None

    def _pause(self, terminal_id: str, seconds: float) -> None:
        """Wait between polls; a notification for this terminal ends the wait early."""

        seconds = max(0.0, seconds)

        if self._inbox is None:
            self._clock.sleep(seconds)
        else:
            self._clock.pause(seconds, self._inbox.arrival(terminal_id))


def _open_at_deadline(abort: AbortOutcome) -> Resolution:
    return Resolution(
        state=PaymentState.UNKNOWN,
        state_reason=StateReason.AWAITING_RESULT,
        resolved_via=ResolvedVia.ABORT_RESPONSE,
        state_detail=f"request still open at the deadline; abort {abort.kind}",
        abort_sent=True,
    )
