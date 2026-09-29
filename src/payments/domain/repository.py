"""The storage port: the application depends on this Protocol, not on Firestore
(implementations: payments.infrastructure.store).

`begin` and `update` are each ONE atomic transaction; that is what makes the terminal lock
safe. Implementations run the pure rules from `payments.domain.terminal_lock` inside them
and never call MarketPay there (a transaction body may be re-run under contention).

Every method may raise `StoreUnavailable`. A write that raised may still have landed (a lost
commit response): retrying the same pure change is safe.
"""

from collections.abc import Callable, Iterable
from typing import Protocol
from uuid import UUID

from payments.domain.history import HistoryEntry
from payments.domain.listing import PaymentPage, PaymentQuery
from payments.domain.models import Operation, Payment, PaymentState, TerminalLock
from payments.domain.terminal_lock import BeginDecision, UpdateResult

# A pure function from the current stored version to the next one (see domain.transitions).
Change = Callable[[Payment], Payment]

OPEN_STATES = (PaymentState.PENDING, PaymentState.UNKNOWN)
OPERATIONS = tuple(Operation)


def is_open(payment: Payment) -> bool:
    return payment.operation is not None or payment.state in OPEN_STATES


class PaymentRepository(Protocol):
    def begin(self, candidate: Payment) -> BeginDecision:
        """Atomically: if the reference is new and the terminal is free, store the
        payment, lock the terminal for it, and write its first history entry. Otherwise
        change nothing."""
        ...

    def update(self, payment_id: UUID, change: Change) -> UpdateResult:
        """Atomically: read the current payment, apply `change`, store the result, and make
        the terminal lock follow its `operation` (acquire / keep / release). Releasing it
        with an inferred outcome also flags the terminal for verification.

        If the new version needs the terminal but another payment holds it, nothing is
        written (TERMINAL_BUSY). `change` may run more than once: it must be pure.
        """
        ...

    def get(self, payment_id: UUID) -> Payment | None: ...

    def history(self, payment_id: UUID) -> list[HistoryEntry]:
        """The payment's history, oldest first (empty for an unknown payment). Every
        `begin` and `update` writes its entry in the same transaction (domain.history)."""

    def open_payments(self, terminal_id: str | None = None) -> list[Payment]:
        """Payments not settled yet: an operation in flight, or state pending/unknown."""
        ...

    def list_payments(self, query: PaymentQuery, scan_limit: int) -> PaymentPage:
        """A page of payments matching `query`, newest first, starting after `query.after`.

        At most `scan_limit` documents are examined; if the scan stops early, the page may
        be short but `next_cursor` continues from where it stopped.
        """
        ...

    def clear_verification(self, terminal_id: str, payment_id: UUID) -> None:
        """Drop the terminal's verification flag if it still names `payment_id`."""
        ...

    def terminals_to_verify(self) -> dict[str, UUID]:
        """Terminals flagged for verification, with the payment each flag names."""
        ...

    def terminal_locks(self, terminal_ids: Iterable[str]) -> dict[str, TerminalLock]:
        """Current locks for these terminals (unlocked ones are absent)."""
        ...


class StoreUnavailable(Exception):
    """The datastore could not complete a call.

    `transient`: worth retrying (contention, a temporary outage, a timed-out call).
    A timed-out *write* may still have landed: callers retry with the same pure
    transition, which then sees its own result and changes nothing.
    """

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient
