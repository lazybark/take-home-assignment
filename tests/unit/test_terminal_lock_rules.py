"""The terminal lock: when a payment takes, keeps or releases it (pure rules)."""

from datetime import UTC, datetime

import pytest
from support.marketpay_fakes import TERMINAL

from payments.domain.models import Operation, Payment, PaymentState, payment_id_for
from payments.domain.terminal_lock import (
    BeginKind,
    LockAction,
    decide_begin,
    lock_action,
    lock_for,
    releases_lock,
)

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def payment(reference: str = "a", state=PaymentState.PENDING, terminal=TERMINAL, at=T0):
    return Payment(
        id=payment_id_for(reference),
        terminal_id=terminal,
        amount=100,
        currency="SEK",
        reference=reference,
        state=state,
        created_at=at,
        updated_at=at,
    )


def test_begin_on_a_free_terminal_creates():
    assert decide_begin(payment("a"), None, None).kind is BeginKind.CREATED


def test_begin_on_a_terminal_held_by_another_payment_is_busy():
    decision = decide_begin(payment("b"), None, lock_for(payment("a")))
    assert decision.kind is BeginKind.TERMINAL_BUSY
    assert decision.blocking_lock.reference == "a"


def test_repeated_reference_is_a_duplicate_even_if_it_holds_the_lock():
    first = payment("a").model_copy(update={"created_by": "request-1"})
    retry = first.model_copy(update={"created_by": "request-2"})  # same order, same instant
    decision = decide_begin(retry, first, lock_for(first))
    assert (decision.kind, decision.payment) == (BeginKind.DUPLICATE, first)


def test_re_running_our_own_write_is_not_a_duplicate():
    # A store transaction retried after its first attempt had already landed.
    candidate = payment("a").model_copy(update={"created_by": "request-1"})
    assert decide_begin(candidate, candidate, lock_for(candidate)).kind is BeginKind.CREATED


@pytest.mark.parametrize(
    ("operation", "released"),
    [
        (None, True),  # settled: the outcome is recorded
        (Operation.PURCHASE, False),  # unresolved purchase: evidence must survive
        (Operation.REVERSAL, False),
        (Operation.REFUND, False),
    ],
)
def test_only_a_settled_operation_releases_the_lock(operation, released):
    held = payment("a")
    settled = payment("a", state=PaymentState.UNKNOWN).model_copy(update={"operation": operation})
    assert releases_lock(settled, lock_for(held)) is released


@pytest.mark.parametrize(
    ("operation", "lock_of", "action"),
    [
        (Operation.PURCHASE, None, LockAction.ACQUIRE),
        (Operation.REVERSAL, "a", LockAction.KEEP),
        (Operation.REVERSAL, "b", LockAction.BUSY),  # never steal another payment's lock
        (None, "a", LockAction.RELEASE),
        (None, "b", LockAction.KEEP),  # settling never touches someone else's lock
        (None, None, LockAction.KEEP),
    ],
)
def test_lock_action(operation, lock_of, action):
    p = payment("a").model_copy(update={"operation": operation})
    lock = lock_for(payment(lock_of)) if lock_of else None
    assert lock_action(p, lock) is action


def test_a_payment_never_releases_someone_elses_lock():
    assert not releases_lock(payment("a", state=PaymentState.APPROVED), lock_for(payment("b")))
