"""Cancel racing the request that is still driving the payment, and cancel racing cancel."""

import threading
import time

import httpx
import pytest
from conftest import FakeClock
from support.marketpay_fakes import REF, TERMINAL, ScriptedMarketPay, cancellation, created, tx

from payments.domain.models import PaymentState, StateReason, payment_id_for, refund_reference_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


class YieldingClock(FakeClock):
    """Fake time, but sleeping really yields briefly so other threads can run."""

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        time.sleep(0.002)


@pytest.fixture
def clock() -> FakeClock:  # overrides conftest's clock for this module
    return YieldingClock()


def run(fn):
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("value", fn()))
    thread.start()

    return thread, result


def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout

    while not predicate():
        assert time.monotonic() < deadline, "timed out waiting"
        time.sleep(0.001)


def test_cancel_while_the_customer_has_not_tapped(make_client, repo):
    """The waiter presses cancel while the terminal shows "present card"."""
    aborted = threading.Event()

    def purchase(request):  # the terminal waits for a card until it is aborted
        assert aborted.wait(5)

        return httpx.Response(201, json=tx("NOK"))  # stopped before the bank

    def abort(request):
        aborted.set()

        return httpx.Response(204)

    marketpay = ScriptedMarketPay(process=[purchase], aborts=[abort])
    pos, cancel_client = make_client(marketpay), make_client(marketpay)

    thread, created_result = run(lambda: pos.post("/payments", json=BODY))
    wait_for(lambda: PAYMENT_ID in repo.payments)
    cancel_response = cancel_client.post(f"/payments/{PAYMENT_ID}/cancel")
    thread.join(5)

    assert (cancel_response.status_code, cancel_response.get_json()["state"]) == (200, "cancelled")
    assert created_result["value"].get_json()["state"] == "cancelled"
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state_reason, stored.reversed) == (StateReason.CANCELLED_BEFORE_CHARGE, False)
    assert TERMINAL not in repo.locks
    assert marketpay.calls.count("abort") == 1


def test_cancel_too_late_the_bank_approved_so_it_is_refunded(make_client, repo):
    """The customer tapped just as the waiter pressed cancel: approved, then refunded."""

    abort_sent = threading.Event()
    refund_ref = refund_reference_for(PAYMENT_ID, 1)

    def purchase(request):
        assert abort_sent.wait(5)
        return httpx.Response(201, json=tx("OK"))  # the bank approved anyway

    def abort(request):
        abort_sent.set()
        return httpx.Response(409)  # too late

    marketpay = ScriptedMarketPay(
        process=[purchase, created(tx("OK", ecr_id=refund_ref, terminal_tx="15"))],
        aborts=[abort],
    )
    pos, cancel_client = make_client(marketpay), make_client(marketpay)

    thread, created_result = run(lambda: pos.post("/payments", json=BODY))
    wait_for(lambda: PAYMENT_ID in repo.payments)
    cancel_response = cancel_client.post(f"/payments/{PAYMENT_ID}/cancel")
    thread.join(5)

    # The payment request reports the truth at its moment: approved.
    assert created_result["value"].get_json()["state"] == "approved"

    # The cancel finished the job with a REFUND (MarketPay: not a cancel-transaction).
    body = cancel_response.get_json()
    assert (cancel_response.status_code, body["state"], body["reversed"]) == (
        200,
        "cancelled",
        True,
    )
    assert "cancel" not in marketpay.calls
    assert marketpay.calls.count("process") == 2
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.REFUNDED
    assert TERMINAL not in repo.locks


def test_two_cancels_at_once_reverse_exactly_once(make_client, repo):
    release = threading.Event()
    reversal_started = threading.Event()

    def reversal(request):
        reversal_started.set()
        assert release.wait(5)

        return httpx.Response(200, json=cancellation("OK"))

    marketpay = ScriptedMarketPay(process=[created(tx())], cancels=[reversal])
    first, second = make_client(marketpay), make_client(marketpay)
    assert first.post("/payments", json=BODY).get_json()["state"] == "approved"

    thread, first_result = run(lambda: first.post(f"/payments/{PAYMENT_ID}/cancel"))
    assert reversal_started.wait(5)
    thread2, second_result = run(lambda: second.post(f"/payments/{PAYMENT_ID}/cancel"))
    time.sleep(0.05)  # the second cancel is now waiting for the first
    release.set()
    thread.join(5)
    thread2.join(5)

    assert marketpay.calls.count("cancel") == 1

    for result in (first_result, second_result):
        assert result["value"].status_code == 200
        assert result["value"].get_json()["state"] == "cancelled"

    assert repo.payments[PAYMENT_ID].state is PaymentState.CANCELLED


def test_new_payment_cannot_start_while_a_reversal_is_running(make_client):
    release = threading.Event()
    reversal_started = threading.Event()

    def reversal(request):
        reversal_started.set()
        assert release.wait(5)

        return httpx.Response(200, json=cancellation("OK"))

    marketpay = ScriptedMarketPay(
        process=[created(tx()), created(tx(ecr_id="order-next"))], cancels=[reversal]
    )
    pos, other = make_client(marketpay), make_client(marketpay)
    pos.post("/payments", json=BODY)

    thread, _ = run(lambda: pos.post(f"/payments/{PAYMENT_ID}/cancel"))
    assert reversal_started.wait(5)
    busy = other.post("/payments", json={**BODY, "reference": "order-next"})
    release.set()
    thread.join(5)

    assert (busy.status_code, busy.get_json()["code"]) == (409, "terminal_busy")
    # Once the reversal is confirmed, the terminal is free again.
    after = other.post("/payments", json={**BODY, "reference": "order-next"})
    assert after.status_code == 201


def test_duplicate_submit_while_in_flight_waits_for_the_real_outcome(make_client, repo):
    """The POS re-sends the same order while the first request still waits for the card."""
    tapped = threading.Event()

    def purchase(request):
        assert tapped.wait(5)

        return httpx.Response(201, json=tx("OK"))

    marketpay = ScriptedMarketPay(process=[purchase])
    first, second = make_client(marketpay), make_client(marketpay)

    thread, first_result = run(lambda: first.post("/payments", json=BODY))
    wait_for(lambda: PAYMENT_ID in repo.payments)
    thread2, second_result = run(lambda: second.post("/payments", json=BODY))
    time.sleep(0.05)  # the duplicate is now waiting on the first
    tapped.set()
    thread.join(5)
    thread2.join(5)

    assert first_result["value"].status_code == 201
    assert second_result["value"].status_code == 200
    assert second_result["value"].get_json()["state"] == "approved"  # not "pending"
    assert marketpay.calls.count("process") == 1  # charged once
