"""A payment's history: which changes get an entry, and what it says (pure rules)."""

from datetime import UTC, datetime, timedelta

from payments.domain.history import history_entry, with_history
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    ResolvedVia,
    StateReason,
    payment_id_for,
)

T0 = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


def payment(**update) -> Payment:
    base = Payment(
        id=payment_id_for("order-1"),
        terminal_id="PAX:TEST_TERMINAL",
        amount=1299,
        currency="SEK",
        reference="order-1",
        state=PaymentState.PENDING,
        operation=Operation.PURCHASE,
        created_at=T0,
        updated_at=T0,
    )
    return base.model_copy(update=update)


def test_a_new_payment_starts_its_history():
    entry = history_entry(None, payment())

    assert (entry.number, entry.state, entry.operation) == (1, "pending", Operation.PURCHASE)
    assert entry.summary == "pending: payment started; terminal held for the purchase"


def test_a_settled_outcome_is_one_readable_line():
    before = payment(history_length=1)
    after = before.model_copy(
        update={
            "state": PaymentState.APPROVED,
            "operation": None,
            "resolved_via": ResolvedVia.LAST_TRANSACTION,
            "updated_at": T0 + timedelta(seconds=40),
        }
    )
    entry = history_entry(before, after)

    assert entry.number == 2
    assert entry.at == T0 + timedelta(seconds=40)
    assert entry.summary == "approved, found in last-transaction; terminal released"


def test_the_reason_is_part_of_the_line():
    before = payment(history_length=1)
    after = before.model_copy(
        update={
            "state": PaymentState.UNKNOWN,
            "state_reason": StateReason.AWAITING_RESULT,
            "resolved_via": ResolvedVia.ABORT_RESPONSE,
        }
    )

    assert (
        history_entry(before, after).summary
        == "unknown (awaiting result), after the service's abort"
    )


def test_coordination_alone_is_not_history():
    """A take-over, a lease, a write-ahead note: nothing a reader of the timeline needs."""
    before = payment(history_length=1)
    for update in (
        {"owner": Owner(instance_id="i", boot_id="b2")},
        {"lease_until": T0 + timedelta(minutes=2)},
        {"abort_requested_at": T0},
    ):
        assert history_entry(before, before.model_copy(update=update)) is None


def test_the_stored_version_counts_its_entries():
    stored, entry = with_history(None, payment())
    assert (stored.history_length, entry.number) == (1, 1)

    unchanged, none = with_history(stored, stored.model_copy(update={"lease_until": T0}))
    assert (unchanged.history_length, none) == (1, None)


def test_entry_ids_sort_in_order_and_are_stable():
    first = history_entry(None, payment())
    later = history_entry(
        payment(history_length=9), payment(history_length=9, state=PaymentState.UNKNOWN)
    )

    assert first.entry_id == "0001_2026-09-28T12:00:00.000000Z"
    assert later.entry_id.startswith("0010_")  # 10 sorts after 9, even at the same instant
    assert history_entry(None, payment()).entry_id == first.entry_id  # a re-run rewrites it
