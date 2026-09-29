"""A 4xx from process-transaction is not taken on trust."""

import httpx
import pytest
from support.marketpay_fakes import (
    ABORTED,
    IN_PROGRESS,
    REF,
    TERMINAL,
    ScriptedMarketPay,
    finished,
    ok,
)

from payments.domain.marketpay.outcomes import Rejected
from payments.domain.models import PaymentState, StateReason, payment_id_for
from payments.domain.outcomes import needs_lookup, resolve_process_outcome

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)
PREVIOUS = finished("OK", ecr_id="order-previous")


def rejected(status):
    return lambda request: httpx.Response(status)


@pytest.mark.parametrize(
    ("status", "state", "looks"),
    [
        (400, PaymentState.FAILED, False),  # malformed / no User-Agent: deterministic
        (404, PaymentState.FAILED, False),  # terminal offline / case / currency
        (409, PaymentState.UNKNOWN, True),  # undocumented: maybe "busy" with a copy of ours
        (429, PaymentState.UNKNOWN, True),
        (422, PaymentState.UNKNOWN, True),
    ],
)
def test_which_4xx_prove_nothing_started(status, state, looks):
    outcome = Rejected(status_code=status, body="")
    assert resolve_process_outcome(outcome, REF).state is state
    assert needs_lookup(outcome) is looks


def test_404_is_confirmed_then_failed(make_client, repo):
    marketpay = ScriptedMarketPay(process=[rejected(404)], lookups=[ok(PREVIOUS)])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert body["state"] == "failed"
    assert marketpay.calls == ["process", "last"]  # exactly one confirming look
    stored = repo.payments[PAYMENT_ID]
    assert stored.state_reason is StateReason.PROVIDER_REJECTED
    assert "HTTP 404" in stored.state_detail and "no record of ours" in stored.state_detail


def test_404_while_a_duplicate_of_our_request_ran_reports_the_truth(make_client):
    # The network duplicated our request: one copy was refused, the other one ran.
    marketpay = ScriptedMarketPay(process=[rejected(404)], lookups=[ok(finished("OK"))])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert (body["state"], body["providerTransactionId"]) == ("approved", "14")


def test_rejection_stands_when_the_confirming_look_fails(make_client):
    # No lookups scripted: MarketPay answers 503 to them. The 400 itself is the evidence.
    marketpay = ScriptedMarketPay(process=[rejected(400)])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert body["state"] == "failed"
    assert marketpay.calls.count("last") == 2  # tried twice, then trusted the refusal


def test_undocumented_4xx_is_resolved_like_a_lost_answer(make_client):
    marketpay = ScriptedMarketPay(
        process=[rejected(409)], lookups=[ok(IN_PROGRESS), ok(finished("OK"))]
    )
    body = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert body["state"] == "approved"  # a copy of ours was running after all


def test_undocumented_4xx_with_nothing_running_goes_through_the_abort(make_client):
    marketpay = ScriptedMarketPay(
        process=[rejected(409)],
        lookups=[ok(PREVIOUS)],
        aborts=[ABORTED],
    )
    body = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert body["state"] == "failed"  # confirmed by the abort, not by the 409
    assert "abort" in marketpay.calls
