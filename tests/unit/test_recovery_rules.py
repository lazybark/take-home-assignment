"""Crash recovery: who may take over an operation, and when recovery may act (pure rules)."""

from datetime import UTC, datetime, timedelta

import pytest
from support.marketpay_fakes import REF, TERMINAL

from payments.domain.cancel import ReversalObservation, ReversalSighting
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    StateReason,
    payment_id_for,
)
from payments.domain.recovery import (
    conclude_reversal,
    is_orphaned,
    recovery_poll_seconds,
    release_refund_due,
    take_over,
)

PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ME = Owner(instance_id="local-1", boot_id="boot-2")


def payment(**update) -> Payment:
    base = Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.PENDING,
        operation=Operation.PURCHASE,
        owner=Owner(instance_id="local-1", boot_id="boot-1"),
        lease_until=T0 + timedelta(seconds=90),
        deadline_at=T0 + timedelta(seconds=60),
        created_at=T0,
        updated_at=T0,
    )
    return base.model_copy(update=update)


@pytest.mark.parametrize(
    ("update", "orphaned"),
    [
        ({}, True),  # same instance, older boot: the process restarted
        ({"owner": ME}, False),  # we are driving it
        ({"owner": None}, True),  # its request ended without settling it
        ({"operation": None}, False),  # nothing in flight
        ({"owner": Owner(instance_id="other", boot_id="x")}, False),  # another live instance
        (
            {"owner": Owner(instance_id="other", boot_id="x"), "lease_until": T0},
            True,  # ...whose lease has run out
        ),
    ],
)
def test_is_orphaned(update, orphaned):
    assert is_orphaned(payment(**update), ME, T0 + timedelta(seconds=1)) is orphaned


def test_take_over_only_an_orphan():
    taken = take_over(payment(), ME, T0, 30)
    assert taken.owner == ME
    assert take_over(taken, Owner(instance_id="local-1", boot_id="boot-3"), T0, 30) != taken
    assert take_over(payment(owner=ME), ME, T0, 30) == payment(owner=ME)


def test_recovery_never_acts_before_the_original_request_would_have():
    assert recovery_poll_seconds(payment(), T0) == 50  # purchase: deadline 60 - reserve 10
    assert recovery_poll_seconds(payment(), T0 + timedelta(minutes=5)) == 5  # minimum
    reversal = payment(operation=Operation.REVERSAL, undo_started_at=T0)
    assert recovery_poll_seconds(reversal, T0 + timedelta(seconds=10)) == 90  # capped
    refund = payment(operation=Operation.REFUND, undo_started_at=T0)
    assert recovery_poll_seconds(refund, T0) == 50  # the customer may still be tapping


def test_refund_due_is_released_not_run():
    due = payment(state=PaymentState.APPROVED, operation=Operation.REFUND, owner=None)
    released = release_refund_due(due, T0)
    assert (released.state, released.operation, released.state_reason) == (
        PaymentState.APPROVED,
        None,
        StateReason.REFUND_DUE,
    )


def test_reversal_counts_as_not_landed_only_after_a_while():
    nothing_new = ReversalObservation(sighting=ReversalSighting.NO_NEW_RECORD)
    sent = payment(operation=Operation.REVERSAL, undo_started_at=T0)

    # A reversal waits for the customer's tap, and a running one isn't shown:
    # "nothing new" proves it never happened only after the terminal's longest wait.
    assert conclude_reversal(nothing_new, sent, T0 + timedelta(seconds=60)) is None
    assert conclude_reversal(nothing_new, sent, T0 + timedelta(seconds=179)) is None
    assert conclude_reversal(nothing_new, sent, T0 + timedelta(seconds=181)).outcome == "not_done"
