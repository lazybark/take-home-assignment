"""The store failing must never turn into a false failure for a real charge."""

import httpx
import pytest
from google.api_core import exceptions as google_errors
from support.marketpay_fakes import (
    ABORTED,
    IN_PROGRESS,
    REF,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    cancellation,
    created,
    finished,
    ok,
    tx,
)

from payments.domain.models import PaymentState, payment_id_for
from payments.domain.repository import StoreUnavailable
from payments.infrastructure.store.firestore import _translating

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


# --- Before anything was sent: retry, then 503 ------------------------------------------


def test_contention_while_creating_is_absorbed(make_client, repo):
    repo.fail(2)  # e.g. Firestore ABORTED twice
    marketpay = ScriptedMarketPay(process=[created(tx())])
    response = make_client(marketpay).post("/payments", json=BODY)
    assert (response.status_code, response.get_json()["state"]) == (201, "approved")


def test_store_down_before_sending_is_503_and_nothing_is_charged(make_client, repo):
    repo.fail(1000)
    marketpay = ScriptedMarketPay(process=[created(tx())])
    response = make_client(marketpay).post("/payments", json=BODY)

    assert (response.status_code, response.get_json()["code"]) == (503, "store_unavailable")
    assert marketpay.calls == []  # the terminal was never touched


def test_a_permanent_store_error_is_not_retried(make_client, repo):
    repo.fail(1, transient=False)  # e.g. permission denied
    response = make_client(ScriptedMarketPay()).post("/payments", json=BODY)
    assert response.status_code == 503
    assert repo.calls == 1


def test_lost_commit_response_on_create_is_not_a_duplicate(make_client, repo):
    repo.fail_after_commit(1)  # the payment was stored, but the store call "failed"
    marketpay = ScriptedMarketPay(process=[created(tx())])
    response = make_client(marketpay).post("/payments", json=BODY)

    # The retry recognises its own write (created_by) and goes on to take the payment.
    assert (response.status_code, response.get_json()["state"]) == (201, "approved")
    assert marketpay.calls == ["process"]


# --- After MarketPay answered: answer with the truth, converge later ----------------------


def test_store_down_after_approval_still_answers_approved(make_client, repo, clock):
    marketpay = ScriptedMarketPay(process=[created(tx())], lookups=[ok(finished("OK"))])
    client = make_client(marketpay)

    def approve_then_fail(request):
        repo.fail(1000)  # the store goes down while the customer taps
        return httpx.Response(201, json=tx())

    marketpay.scripts["process"] = [approve_then_fail]
    response = client.post("/payments", json=BODY)

    # Not a 500: the waiter hears the truth, so they won't run the card again.
    assert (response.status_code, response.get_json()["state"]) == (201, "approved")

    # The stored record lags behind but still holds the terminal (evidence preserved)...
    assert repo.payments[PAYMENT_ID].state is PaymentState.PENDING
    assert TERMINAL in repo.locks

    # ...and converges once the store is back and the request's lease has run out.
    repo.fail(0)
    clock.elapsed += 120
    client.post("/reconcile")
    assert client.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "approved"
    assert TERMINAL not in repo.locks


def test_store_contention_after_approval_is_absorbed(make_client, repo):
    marketpay = ScriptedMarketPay()

    def approve_then_contend(request):
        repo.fail(2)
        return httpx.Response(201, json=tx())

    marketpay.scripts["process"] = [approve_then_contend]
    response = make_client(marketpay).post("/payments", json=BODY)
    assert response.get_json()["state"] == "approved"
    assert repo.payments[PAYMENT_ID].state is PaymentState.APPROVED  # recorded after retrying
    assert TERMINAL not in repo.locks


def test_failed_write_ahead_note_does_not_stop_the_abort(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[accepted], lookups=[ok(IN_PROGRESS)] * 50 + [ok(finished("NOK"))]
    )

    def abort(request):
        return ABORTED

    # The store fails exactly around the "abort requested" note.
    real_update = repo.update

    def flaky_update(payment_id, change):
        current = repo.payments[payment_id]
        if current.abort_requested_at is None and change(current).abort_requested_at:
            raise StoreUnavailable("down", transient=False)
        return real_update(payment_id, change)

    repo.update = flaky_update
    marketpay.scripts["abort"] = [abort]
    response = make_client(marketpay).post("/payments", json=BODY)
    assert response.get_json()["state"] == "failed"
    assert "abort" in marketpay.calls


# --- Claims recognise their own lost commit -------------------------------------------------


def test_lost_commit_on_a_cancel_claim_is_recognised_as_ours(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[created(tx())], lookups=[ok(finished())], cancels=[ok(cancellation("OK"))]
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    repo.fail_after_commit(1)  # the claim lands, its response is lost
    response = client.post(f"/payments/{PAYMENT_ID}/cancel")
    body = response.get_json()
    assert (response.status_code, body["state"], body["reversed"]) == (200, "cancelled", True)
    assert marketpay.calls.count("cancel") == 1


def test_cancel_with_the_store_down_is_503_and_nothing_is_sent(make_client, repo):
    marketpay = ScriptedMarketPay(process=[created(tx())], cancels=[ok(cancellation("OK"))])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    repo.fail(1000)
    response = client.post(f"/payments/{PAYMENT_ID}/cancel")
    assert response.status_code == 503
    assert "cancel" not in marketpay.calls


def test_reconcile_skips_a_payment_the_store_cant_write_and_keeps_going(make_client, repo):
    # Two payments left unknown on two terminals; both found finished after a restart.
    for ref in ("order-a", "order-b"):
        client = make_client(
            ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
        )
        client.post("/payments", json={**BODY, "reference": ref, "terminalId": f"PAX:{ref}"})

    def last_transaction(request):
        ref = request.url.path.rsplit(":", 1)[-1]

        return httpx.Response(200, json=finished("OK", ecr_id=ref))

    # The store refuses every write for order-a only.
    stuck = payment_id_for("order-a")
    real_update = repo.update

    def update(payment_id, change):
        if payment_id == stuck:
            raise StoreUnavailable("down for this document", transient=False)

        return real_update(payment_id, change)

    repo.update = update
    restarted = make_client(ScriptedMarketPay(lookups=[last_transaction]), boot_id="boot-2")
    response = restarted.post("/reconcile")

    assert response.status_code == 200  # one bad document doesn't fail the whole reconcile
    summary = response.get_json()
    assert (summary["scanned"], summary["resolved"], summary["stillOpen"]) == (2, 1, 1)
    assert summary["resolvedIds"] == [str(payment_id_for("order-b"))]


# --- Firestore error translation -------------------------------------------------------------


def _raising(exc):
    @_translating
    def call():
        raise exc

    return call


@pytest.mark.parametrize(
    ("exc", "transient"),
    [
        (google_errors.Aborted("contention"), True),
        (google_errors.ServiceUnavailable("down"), True),
        (google_errors.DeadlineExceeded("slow"), True),
        (google_errors.PermissionDenied("no"), False),
        # What the client library raises after 5 aborted attempts of one transaction.
        (ValueError("Failed to commit transaction in 5 attempts."), True),
    ],
)
def test_firestore_errors_are_translated(exc, transient):
    with pytest.raises(StoreUnavailable) as info:
        _raising(exc)()
    assert info.value.transient is transient


def test_unrelated_value_errors_are_not_swallowed():
    with pytest.raises(ValueError):
        _raising(ValueError("a bug"))()
