"""A repeated reference must describe the same order."""

import pytest
from support.marketpay_fakes import REF, TERMINAL, ScriptedMarketPay, created, tx

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}


@pytest.fixture
def paid(make_client):
    marketpay = ScriptedMarketPay(process=[created(tx())])
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).status_code == 201

    return client, marketpay


def test_same_order_again_returns_the_existing_payment(paid):
    client, marketpay = paid
    response = client.post("/payments", json=BODY)
    assert (response.status_code, response.get_json()["state"]) == (200, "approved")
    assert marketpay.calls == ["process"]


def test_a_different_deadline_is_still_the_same_order(paid):
    client, _ = paid
    assert client.post("/payments", json={**BODY, "deadlineSeconds": 90}).status_code == 200


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"amount": 100}, "amount"),
        ({"terminalId": "PAX:OTHER_TERMINAL"}, "terminal_id"),
        ({"currency": "EUR"}, "currency"),
    ],
)
def test_same_reference_different_order_is_409(paid, change, field):
    client, marketpay = paid
    response = client.post("/payments", json={**BODY, **change})

    assert response.status_code == 409

    error = response.get_json()
    assert error["code"] == "idempotency_mismatch"
    assert field in error["message"] and REF in error["message"]
    assert marketpay.calls == ["process"]  # never sent to the terminal


def test_mismatch_is_checked_even_while_the_first_is_still_unresolved(make_client):
    from support.marketpay_fakes import IN_PROGRESS, TOO_LATE, accepted, ok

    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"

    response = client.post("/payments", json={**BODY, "amount": 1})
    assert (response.status_code, response.get_json()["code"]) == (409, "idempotency_mismatch")
