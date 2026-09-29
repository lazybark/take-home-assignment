"""The terminal lock (pure rules): one unresolved operation per terminal.

MarketPay only remembers a terminal's *last* transaction, so a payment keeps the terminal until
its outcome is confirmed; the lock is released only in the same write that records it.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from payments.domain.models import Payment, TerminalLock


class BeginKind(StrEnum):
    CREATED = "created"  # new payment recorded, terminal locked for it
    DUPLICATE = "duplicate"  # this reference already has a payment
    TERMINAL_BUSY = "terminal_busy"  # another payment holds the terminal


class BeginDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: BeginKind
    payment: Payment | None = None  # CREATED: the new one; DUPLICATE: the existing one
    blocking_lock: TerminalLock | None = None  # TERMINAL_BUSY
    # CREATED: an earlier payment on this terminal whose inferred outcome must be checked
    # before this one is sent (domain.verification).
    verify_payment_id: UUID | None = None


def decide_begin(
    candidate: Payment, existing: Payment | None, lock: TerminalLock | None
) -> BeginDecision:
    """Runs inside the store's transaction, so it may be re-run: it must stay pure."""

    if existing is not None and existing.created_by != candidate.created_by:
        return BeginDecision(kind=BeginKind.DUPLICATE, payment=existing)

    # (Same `created_by`: this very request's earlier attempt of the write already landed,
    # e.g. a store retry after a lost commit response — not a second request.)
    if lock is not None and lock.payment_id != candidate.id:
        return BeginDecision(kind=BeginKind.TERMINAL_BUSY, blocking_lock=lock)

    return BeginDecision(kind=BeginKind.CREATED, payment=candidate)


def idempotency_mismatch(existing: Payment, candidate: Payment) -> list[str]:
    """Fields in which a repeated reference differs from the payment it already names.

    Only what defines the order counts (terminal, amount, currency). `deadlineSeconds` is
    how long *that request* waits, so a retry with a different deadline is the same order.
    """

    return [
        name
        for name in ("terminal_id", "amount", "currency")
        if getattr(existing, name) != getattr(candidate, name)
    ]


def lock_for(payment: Payment) -> TerminalLock:
    return TerminalLock(
        terminal_id=payment.terminal_id,
        payment_id=payment.id,
        reference=payment.reference,
        locked_at=payment.updated_at,  # when this operation took the terminal
    )


def releases_lock(payment: Payment, lock: TerminalLock | None) -> bool:
    """Only a confirmed outcome frees the terminal — an unresolved operation keeps it."""

    return lock is not None and lock.payment_id == payment.id and payment.operation is None


class LockAction(StrEnum):
    ACQUIRE = "acquire"  # the payment starts an operation on a free terminal
    KEEP = "keep"  # it already holds the lock (or holds none and needs none)
    RELEASE = "release"  # its operation is settled
    BUSY = "busy"  # it needs the terminal, but another payment holds it


def lock_action(payment: Payment, lock: TerminalLock | None) -> LockAction:
    """What storing this version of `payment` must do to the terminal lock."""

    holds = lock is not None and lock.payment_id == payment.id

    if payment.operation is None:
        return LockAction.RELEASE if holds else LockAction.KEEP

    if holds:
        return LockAction.KEEP

    return LockAction.BUSY if lock is not None else LockAction.ACQUIRE


class UpdateKind(StrEnum):
    UPDATED = "updated"
    UNCHANGED = "unchanged"  # the change function returned the payment as it was
    TERMINAL_BUSY = "terminal_busy"  # nothing written: another payment holds the terminal


class UpdateResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: UpdateKind
    payment: Payment  # the stored version after the update (or as it was)
    blocking_lock: TerminalLock | None = None
