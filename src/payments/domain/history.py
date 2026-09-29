"""A payment's history: one entry per change of its state, its reason, or what holds the
terminal for it (pure rules).

The repository writes an entry in the SAME transaction as the change, so the history is
exact: no entry for a change that didn't commit, none missing for one that did. Coordination
alone (a new owner after a take-over, a lease, a write-ahead note) gets no entry.

A deliberately simple trail, as an example of how it can be done; see README › Payment
history for what a production-grade audit trail would add.
"""

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from payments.domain.models import Operation, Payment, PaymentState, ResolvedVia, StateReason

_BASED_ON = {
    ResolvedVia.PROCESS_RESPONSE: "from the payment's response",
    ResolvedVia.LAST_TRANSACTION: "found in last-transaction",
    ResolvedVia.ABORT_RESPONSE: "after the service's abort",
    ResolvedVia.CANCEL_RESPONSE: "from the reversal's response",
    ResolvedVia.NOTIFICATION: "from a MarketPay notification",
}


class HistoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int  # 1, 2, 3… in the order the changes committed
    at: datetime
    state: PaymentState
    reason: StateReason | None
    based_on: ResolvedVia | None  # which MarketPay answer the state rests on
    operation: Operation | None  # what holds the terminal now; None: released
    detail: str | None
    summary: str  # one line for humans

    @property
    def entry_id(self) -> str:
        """Sortable and readable, so the Firestore console lists the timeline in order. The
        same change always gets the same id: a re-run transaction rewrites, never adds."""

        return f"{self.number:04d}_{self.at.astimezone(UTC):%Y-%m-%dT%H:%M:%S.%fZ}"


def history_entry(previous: Payment | None, current: Payment) -> HistoryEntry | None:
    """The entry `current` adds after `previous` (None: it was just created), or None if
    nothing worth a line changed."""

    if previous is not None and _line(previous) == _line(current):
        return None
    return HistoryEntry(
        number=(previous.history_length if previous else 0) + 1,
        at=current.updated_at,
        state=current.state,
        reason=current.state_reason,
        based_on=current.resolved_via,
        operation=current.operation,
        detail=current.state_detail,
        summary=summarise(previous, current),
    )


def with_history(previous: Payment | None, new: Payment) -> tuple[Payment, HistoryEntry | None]:
    """What the repository stores for a change: the new version (counting its entry) and
    the entry to write in the same transaction, if the change deserves one."""

    entry = history_entry(previous, new)
    if entry is None:
        return new, None

    return new.model_copy(update={"history_length": entry.number}), entry


def summarise(previous: Payment | None, current: Payment) -> str:
    """E.g. "approved, found in last-transaction; terminal released"."""

    if previous is None:
        return f"{current.state.value}: payment started; terminal held for the purchase"

    text = current.state.value
    if current.state_reason is not None:
        text += f" ({current.state_reason.value.replace('_', ' ')})"

    if current.resolved_via in _BASED_ON and current.resolved_via != previous.resolved_via:
        text += f", {_BASED_ON[current.resolved_via]}"

    if current.operation != previous.operation:
        text += (
            "; terminal released"
            if current.operation is None
            else f"; terminal held for the {current.operation.value}"
        )

    return text


def _line(payment: Payment) -> tuple:
    return (payment.state, payment.state_reason, payment.operation)
