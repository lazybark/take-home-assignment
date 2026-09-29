"""Crash recovery: kill the process mid-flight, restart it, reconcile, check convergence."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from support.marketpay_fakes import (
    ABORTED,
    IN_PROGRESS,
    REF,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    cancellation,
    cancelled_last,
    created,
    finished,
    ok,
    reversal_record,
    tx,
)

from payments.app import create_app
from payments.config import Settings
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    StateReason,
    payment_id_for,
    refund_reference_for,
)
from payments.domain.terminal_lock import lock_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


class SimulatedCrash(BaseException):
    """Not an Exception: nothing catches it, like the process dying on the spot."""


def crash(request):
    raise SimulatedCrash


PREVIOUS = finished("OK", ecr_id="order-previous")


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


# --- Crash, restart, reconcile ----------------------------------------------------------


def crashed_while(make_client, **scripts):
    """Run a payment until the scripted crash; return MarketPay as the dead process left it."""

    marketpay = ScriptedMarketPay(**scripts)

    with pytest.raises(SimulatedCrash):
        make_client(marketpay).post("/payments", json=BODY)

    return marketpay


def reconcile(client, body=None):
    response = client.post("/reconcile", json=body) if body else client.post("/reconcile")
    assert response.status_code == 200
    return response.get_json()


def test_crash_after_marketpay_approved_but_before_we_recorded_it(make_client, repo):
    crashed_while(make_client, process=[crash])
    assert repo.payments[PAYMENT_ID].state is PaymentState.PENDING
    assert TERMINAL in repo.locks  # the dead process still holds the terminal

    restarted = make_client(ScriptedMarketPay(lookups=[ok(finished("OK"))]), boot_id="boot-2")
    summary = reconcile(restarted)

    assert summary == {
        "scanned": 1,
        "resolved": 1,
        "stillOpen": 0,
        "resolvedIds": [str(PAYMENT_ID)],
    }
    assert restarted.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "approved"
    assert TERMINAL not in repo.locks


def test_crash_before_the_request_ever_reached_marketpay(make_client, repo, clock):
    crashed_while(make_client, process=[crash])

    # The terminal never saw it: it keeps showing the previous transaction, and (as on
    # staging) abort answers 409 whether or not anything runs.
    marketpay = ScriptedMarketPay(lookups=[ok(PREVIOUS)], aborts=[TOO_LATE])
    restarted = make_client(marketpay, boot_id="boot-2")

    # Within the first minutes "no record" proves nothing: it might still be running.
    first = reconcile(restarted)
    assert (first["resolved"], first["stillOpen"]) == (0, 1)
    assert TERMINAL in repo.locks

    # Long past the longest a terminal transaction can take: it never ran.
    clock.elapsed = 200
    assert reconcile(restarted)["resolved"] == 1
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (PaymentState.FAILED, StateReason.NEVER_RECORDED)
    assert "process" not in marketpay.calls  # recovery never re-sends a payment
    assert TERMINAL not in repo.locks


def test_replay_of_try_20_on_staging(make_client, repo, clock):
    """Seen live: killed while the terminal waited for a card. After the restart,
    last-transaction showed only the *previous* transaction (never IN_PROGRESS), the abort
    answered 409, and the terminal recorded NOK on its own timeout, ~65s after sending."""

    crashed_while(make_client, process=[crash])
    clock.elapsed = 22  # reconcile ran ~22s after the payment started

    def last_transaction(request):
        record = finished("NOK") if clock.elapsed >= 65 else PREVIOUS

        return httpx.Response(200, json=record)

    marketpay = ScriptedMarketPay(lookups=[last_transaction], aborts=[TOO_LATE])
    summary = reconcile(make_client(marketpay, boot_id="boot-2"))

    assert summary["resolved"] == 1  # converged in one call
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (
        PaymentState.FAILED,
        StateReason.TERMINAL_STOPPED,
    )
    assert TERMINAL not in repo.locks


def test_get_returns_the_stored_state_and_never_asks_marketpay(make_client, repo):
    """A read shows what we know for sure; it never changes it."""

    crashed_while(make_client, process=[crash])
    marketpay = ScriptedMarketPay(lookups=[ok(finished("OK"))])
    restarted = make_client(marketpay, boot_id="boot-2")

    assert restarted.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "pending"
    assert marketpay.calls == []  # the terminal shows it approved, but GET doesn't look
    assert restarted.post("/reconcile").get_json()["resolved"] == 1  # the explicit route
    assert restarted.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "approved"


def test_crash_while_the_customer_is_still_at_the_terminal(make_client, repo, clock):
    crashed_while(make_client, process=[accepted], lookups=[crash])

    # After restart the terminal still waits for a card; we abort, it records NOK.
    marketpay = ScriptedMarketPay(
        lookups=[ok(IN_PROGRESS)] * 50 + [ok(finished("NOK"))], aborts=[ABORTED]
    )
    restarted = make_client(marketpay, boot_id="boot-2")

    assert reconcile(restarted)["resolved"] == 1
    assert repo.payments[PAYMENT_ID].state is PaymentState.FAILED

    # It waited until the original request would have aborted (50s), not earlier.
    assert marketpay.calls.index("abort") >= 50


def test_crash_while_aborting(make_client, repo):
    crashed_while(make_client, process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[crash])
    assert repo.payments[PAYMENT_ID].abort_requested_at is not None  # written first

    restarted = make_client(
        ScriptedMarketPay(lookups=[ok(finished("NOK"))], aborts=[ABORTED]), boot_id="boot-2"
    )
    assert reconcile(restarted)["resolved"] == 1
    assert repo.payments[PAYMENT_ID].state is PaymentState.FAILED


def crashed_mid_reversal(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[created(tx())], lookups=[ok(finished())], cancels=[crash]
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    with pytest.raises(SimulatedCrash):
        client.post(f"/payments/{PAYMENT_ID}/cancel")

    assert repo.payments[PAYMENT_ID].operation is Operation.REVERSAL
    assert repo.payments[PAYMENT_ID].undo_baseline_transaction_id == "14"


@pytest.mark.parametrize("record", [reversal_record("OK"), cancelled_last("OK")])
def test_crash_mid_reversal_that_went_through(make_client, repo, record):
    crashed_mid_reversal(make_client, repo)
    restarted = make_client(ScriptedMarketPay(lookups=[ok(record)]), boot_id="b2")
    assert reconcile(restarted)["resolved"] == 1
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.reversed) == (PaymentState.CANCELLED, True)
    assert TERMINAL not in repo.locks


def test_crash_mid_reversal_that_never_landed(make_client, repo, clock):
    crashed_mid_reversal(make_client, repo)
    sent_at = clock.elapsed

    # The purchase stays the last record. Within the terminal's longest wait that proves
    # nothing (the reversal may be waiting for a tap): reconcile leaves it open...
    recovery = ScriptedMarketPay(lookups=[ok(finished())])
    restarted = make_client(recovery, boot_id="b2")
    first = reconcile(restarted)
    assert (first["resolved"], first["stillOpen"]) == (0, 1)
    assert TERMINAL in repo.locks

    # ...and settles it once that time has clearly passed.
    clock.elapsed = sent_at + 200
    assert reconcile(restarted)["resolved"] == 1
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (
        PaymentState.APPROVED,
        StateReason.UNDO_NOT_RECORDED,
    )
    assert TERMINAL not in repo.locks
    assert "cancel" not in recovery.calls


def test_crash_mid_reversal_that_lands_while_recovery_watches(make_client, repo):
    crashed_mid_reversal(make_client, repo)
    recovery = ScriptedMarketPay(lookups=[ok(finished())] * 3 + [ok(reversal_record("OK"))])
    assert reconcile(make_client(recovery, boot_id="b2"))["resolved"] == 1
    assert repo.payments[PAYMENT_ID].state is PaymentState.CANCELLED


def test_crash_mid_refund(make_client, repo):
    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(
        process=[accepted, crash],  # the purchase, then the refund
        lookups=[ok(IN_PROGRESS)] * 50 + [ok(finished("OK"))],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)  # approved despite the deadline abort

    with pytest.raises(SimulatedCrash):
        client.post(f"/payments/{PAYMENT_ID}/cancel")

    assert repo.payments[PAYMENT_ID].operation is Operation.REFUND

    restarted = make_client(
        ScriptedMarketPay(lookups=[ok(finished("OK", ecr_id=refund_ref))]), boot_id="b2"
    )
    assert reconcile(restarted)["resolved"] == 1
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (PaymentState.CANCELLED, StateReason.REFUNDED)


def test_refund_due_but_never_sent_is_released_for_a_new_cancel(make_client, repo):
    repo.payments[PAYMENT_ID] = payment(
        state=PaymentState.APPROVED,
        operation=Operation.REFUND,
        owner=None,
        provider_transaction_id="14",
        abort_requested_at=T0,
        cancel_requested_at=T0,
    )
    repo.locks[TERMINAL] = lock_for(repo.payments[PAYMENT_ID])
    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(process=[created(tx(ecr_id=refund_ref))])
    client = make_client(marketpay, boot_id="b2")

    summary = reconcile(client)
    assert summary["resolved"] == 1  # settled: approved, the charge stands
    assert "process" not in marketpay.calls  # reconcile never starts a card transaction
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.REFUND_DUE
    assert TERMINAL not in repo.locks

    assert client.post(f"/payments/{PAYMENT_ID}/cancel").get_json()["state"] == "cancelled"


def test_live_request_is_left_alone(make_client, repo):
    repo.payments[PAYMENT_ID] = payment(owner=Owner(instance_id="local-1", boot_id="boot-1"))
    marketpay = ScriptedMarketPay()
    summary = reconcile(make_client(marketpay))  # same boot: that request is alive

    assert (summary["scanned"], summary["resolved"], summary["stillOpen"]) == (1, 0, 1)
    assert marketpay.calls == []


def test_other_instance_is_left_alone_until_its_lease_expires(make_client, repo, clock):
    repo.payments[PAYMENT_ID] = payment(owner=Owner(instance_id="other", boot_id="x"))
    marketpay = ScriptedMarketPay(lookups=[ok(finished("OK"))])
    client = make_client(marketpay)
    assert reconcile(client)["resolved"] == 0

    clock.elapsed += 120  # past lease_until
    assert reconcile(client)["resolved"] == 1


def test_reconcile_is_idempotent(make_client):
    crashed_while(make_client, process=[crash])
    restarted = make_client(ScriptedMarketPay(lookups=[ok(finished("OK"))]), boot_id="b2")
    assert reconcile(restarted)["resolved"] == 1
    assert reconcile(restarted) == {"scanned": 0, "resolved": 0, "stillOpen": 0, "resolvedIds": []}


def test_reconcile_filters(make_client, repo):
    crashed_while(make_client, process=[crash])
    restarted = make_client(ScriptedMarketPay(lookups=[ok(finished("OK"))]), boot_id="b2")

    assert reconcile(restarted, {"terminalId": "PAX:other"})["scanned"] == 0
    assert reconcile(restarted, {"olderThan": "2026-09-27T11:00:00Z"})["scanned"] == 0
    assert reconcile(restarted, {"terminalId": TERMINAL})["resolved"] == 1


def test_reconcile_rejects_a_bad_body(make_client):
    client = make_client(ScriptedMarketPay())
    assert client.post("/reconcile", json={"olderThan": "yesterday"}).status_code == 400


# --- Other flows that meet an orphan ----------------------------------------------------


def test_pos_retry_of_the_same_order_after_a_crash_gets_the_real_outcome(make_client, repo):
    crashed_while(make_client, process=[crash])
    marketpay = ScriptedMarketPay(lookups=[ok(finished("OK"))])
    restarted = make_client(marketpay, boot_id="boot-2")

    response = restarted.post("/payments", json=BODY)  # the waiter's POS retries
    assert (response.status_code, response.get_json()["state"]) == (200, "approved")
    assert "process" not in marketpay.calls  # recovered, never charged again
    assert TERMINAL not in repo.locks


def test_new_order_on_a_terminal_held_by_a_crashed_payment(make_client, repo):
    crashed_while(make_client, process=[crash])
    other = {**BODY, "reference": "order-next"}

    # The terminal shows the crashed payment finished: one look settles it, the new one runs.
    marketpay = ScriptedMarketPay(
        process=[created(tx(ecr_id="order-next"))], lookups=[ok(finished("NOK"))]
    )
    restarted = make_client(marketpay, boot_id="boot-2")
    assert restarted.post("/payments", json=other).status_code == 201
    assert repo.payments[PAYMENT_ID].state is PaymentState.FAILED


def test_new_order_stays_blocked_while_the_crashed_payment_is_unclear(make_client):
    crashed_while(make_client, process=[crash])
    restarted = make_client(ScriptedMarketPay(lookups=[ok(PREVIOUS)]), boot_id="boot-2")
    response = restarted.post("/payments", json={**BODY, "reference": "order-next"})
    assert (response.status_code, response.get_json()["code"]) == (409, "terminal_busy")


def test_the_time_rule_settles_a_crashed_payment_on_a_later_reconcile(make_client, repo, clock):
    crashed_while(make_client, process=[crash])
    restarted = make_client(
        ScriptedMarketPay(lookups=[ok(PREVIOUS)], aborts=[TOO_LATE]), boot_id="boot-2"
    )
    assert restarted.post("/reconcile").get_json()["stillOpen"] == 1  # proves nothing yet
    clock.elapsed = 200  # long past the longest a terminal transaction can take
    assert restarted.post("/reconcile").get_json()["resolved"] == 1
    assert restarted.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "failed"


def test_cancel_of_a_crashed_payment_takes_it_over(make_client, repo):
    crashed_while(make_client, process=[crash])
    marketpay = ScriptedMarketPay(lookups=[ok(IN_PROGRESS), ok(finished("NOK"))], aborts=[ABORTED])
    restarted = make_client(marketpay, boot_id="boot-2")

    response = restarted.post(f"/payments/{PAYMENT_ID}/cancel")
    assert (response.status_code, response.get_json()["state"]) == (200, "cancelled")
    assert TERMINAL not in repo.locks


def test_reversal_request_shape_is_unchanged_by_recovery(make_client):
    # Guard: recovery reads, it never sends a cancel-transaction on its own.
    marketpay = ScriptedMarketPay(process=[created(tx())], cancels=[crash])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    with pytest.raises(SimulatedCrash):
        client.post(f"/payments/{PAYMENT_ID}/cancel")

    recovery = ScriptedMarketPay(lookups=[ok(cancelled_last("OK"))], cancels=[ok(cancellation())])
    reconcile(make_client(recovery, boot_id="b2"))
    assert "cancel" not in recovery.calls


# --- Edge cases (found in the final review) ------------------------------------------------


def test_older_than_without_a_time_zone_is_utc(make_client):
    """It used to compare a zone-less time with stored UTC times, and answer 500."""
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    summary = reconcile(client, {"olderThan": "2030-01-01T00:00:00"})  # asserts 200
    assert summary["scanned"] == 1


def test_a_restart_converges_by_itself(
    settings, make_client, make_marketpay, db, repo, clock, owner
):
    """With reconcile_on_start (the prod default), a new boot settles what the previous
    one left open, without anyone calling POST /reconcile."""
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    make_client(marketpay).post("/payments", json=BODY)  # unknown: the terminal is held
    marketpay.scripts["last"] = [ok(finished("OK"))]  # it finished meanwhile

    restarted = create_app(
        Settings(**(settings.model_dump() | {"reconcile_on_start": True})),
        marketpay=make_marketpay(marketpay),
        db=db,
        repo=repo,
        clock=clock,
        owner=owner.model_copy(update={"boot_id": "boot-2"}),
    )
    restarted.extensions["startup_reconcile"].join(10)

    assert repo.payments[PAYMENT_ID].state is PaymentState.APPROVED
    assert TERMINAL not in repo.locks
