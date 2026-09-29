"""In-memory PaymentRepository for tests. Same atomicity as Firestore, via one mutex.

Faults can be injected to test how the service survives the store failing:
- `fail(n)`: the next n calls raise StoreUnavailable before doing anything;
- `fail_after_commit(n)`: the next n writes land, then raise — a commit whose response was
  lost, the case where a retry must recognise its own earlier write.
"""

import threading
from collections.abc import Iterable
from uuid import UUID

from payments.domain.history import HistoryEntry, with_history
from payments.domain.listing import PaymentPage, PaymentQuery, is_after, order_key, scan_page
from payments.domain.models import Payment, TerminalLock
from payments.domain.repository import Change, StoreUnavailable, is_open
from payments.domain.terminal_lock import (
    BeginDecision,
    BeginKind,
    LockAction,
    UpdateKind,
    UpdateResult,
    decide_begin,
    lock_action,
    lock_for,
)
from payments.domain.verification import flag_after_settling


class InMemoryPaymentRepository:
    def __init__(self) -> None:
        self.payments: dict[UUID, Payment] = {}
        self.locks: dict[str, TerminalLock] = {}
        self.verify: dict[str, UUID] = {}  # terminal -> payment whose outcome to re-check
        # payment -> its history, keyed by entry id like the Firestore subcollection (so a
        # rewrite of the same change replaces its entry instead of adding one)
        self.history_entries: dict[UUID, dict[str, HistoryEntry]] = {}
        self._mutex = threading.Lock()
        self._fail_before = 0
        self._fail_after_commit = 0
        self.calls = 0

    def fail(self, times: int, transient: bool = True) -> None:
        self._fail_before, self._fail_transient = times, transient

    def fail_after_commit(self, times: int) -> None:
        self._fail_after_commit = times

    def _maybe_fail(self) -> None:
        self.calls += 1
        if self._fail_before > 0:
            self._fail_before -= 1

            raise StoreUnavailable("injected failure", transient=self._fail_transient)

    def _maybe_lose_commit(self) -> None:
        if self._fail_after_commit > 0:
            self._fail_after_commit -= 1

            raise StoreUnavailable("injected: commit landed, response lost", transient=True)

    def begin(self, candidate: Payment) -> BeginDecision:
        self._maybe_fail()

        with self._mutex:
            decision = decide_begin(
                candidate,
                self.payments.get(candidate.id),
                self.locks.get(candidate.terminal_id),
            )

            if decision.kind is BeginKind.CREATED:
                stored, entry = with_history(None, candidate)
                self.payments[candidate.id] = stored
                self._write_history(candidate.id, entry)
                self.locks[candidate.terminal_id] = lock_for(candidate)
                decision = decision.model_copy(
                    update={"verify_payment_id": self.verify.get(candidate.terminal_id)}
                )
                self._maybe_lose_commit()

            return decision

    def update(self, payment_id: UUID, change: Change) -> UpdateResult:
        self._maybe_fail()

        with self._mutex:
            current = self.payments[payment_id]
            new = change(current)

            if new == current:
                return UpdateResult(kind=UpdateKind.UNCHANGED, payment=current)
            lock = self.locks.get(new.terminal_id)

            match lock_action(new, lock):
                case LockAction.BUSY:
                    return UpdateResult(
                        kind=UpdateKind.TERMINAL_BUSY, payment=current, blocking_lock=lock
                    )

                case LockAction.ACQUIRE:
                    self.locks[new.terminal_id] = lock_for(new)

                case LockAction.RELEASE:
                    del self.locks[new.terminal_id]
                    flag = flag_after_settling(new, self.verify.get(new.terminal_id))

                    if flag is not None:
                        self.verify[new.terminal_id] = flag

            new, entry = with_history(current, new)

            self.payments[payment_id] = new
            self._write_history(payment_id, entry)
            self._maybe_lose_commit()

            return UpdateResult(kind=UpdateKind.UPDATED, payment=new)

    def get(self, payment_id: UUID) -> Payment | None:
        self._maybe_fail()

        return self.payments.get(payment_id)

    def history(self, payment_id: UUID) -> list[HistoryEntry]:
        self._maybe_fail()

        return sorted(self.history_entries.get(payment_id, {}).values(), key=lambda e: e.number)

    def _write_history(self, payment_id: UUID, entry: HistoryEntry | None) -> None:
        if entry is not None:
            self.history_entries.setdefault(payment_id, {})[entry.entry_id] = entry

    def open_payments(self, terminal_id: str | None = None) -> list[Payment]:
        self._maybe_fail()

        with self._mutex:
            return [
                p
                for p in self.payments.values()
                if is_open(p) and (terminal_id is None or p.terminal_id == terminal_id)
            ]

    def list_payments(self, query: PaymentQuery, scan_limit: int) -> PaymentPage:
        self._maybe_fail()

        with self._mutex:
            ordered = sorted(self.payments.values(), key=order_key, reverse=True)

        return scan_page((p for p in ordered if is_after(p, query.after)), query, scan_limit)

    def clear_verification(self, terminal_id: str, payment_id: UUID) -> None:
        self._maybe_fail()

        with self._mutex:
            if self.verify.get(terminal_id) == payment_id:
                del self.verify[terminal_id]

    def terminals_to_verify(self) -> dict[str, UUID]:
        self._maybe_fail()

        with self._mutex:
            return dict(self.verify)

    def terminal_locks(self, terminal_ids: Iterable[str]) -> dict[str, TerminalLock]:
        self._maybe_fail()

        return {t: self.locks[t] for t in terminal_ids if t in self.locks}
