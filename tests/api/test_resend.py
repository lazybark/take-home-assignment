"""A request that never left us is re-sent (same ecrTransactionId), briefly."""

import json

import httpx
from support.marketpay_fakes import (
    REF,
    TERMINAL,
    ScriptedMarketPay,
    created,
    dns_failure,
    lost,
    ok,
    tx,
)

from payments.domain.models import StateReason, payment_id_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


def test_dns_blip_is_absorbed(make_client, repo):
    # Like try-2 after the laptop woke up: the name didn't resolve for a moment.
    marketpay = ScriptedMarketPay(process=[dns_failure, dns_failure, created(tx())])
    response = make_client(marketpay).post("/payments", json=BODY)

    assert (response.status_code, response.get_json()["state"]) == (201, "approved")
    sent = marketpay.requests["process"]
    assert len(sent) == 3

    # Always the same order: MarketPay never saw the first two, so no double charge.
    assert {json.loads(r.content)["ecrTransactionId"] for r in sent} == {REF}


def test_a_connect_timeout_is_resent_too(make_client):
    def connect_timeout(request):
        raise httpx.ConnectTimeout("connect timed out", request=request)

    marketpay = ScriptedMarketPay(process=[connect_timeout, created(tx())])
    assert make_client(marketpay).post("/payments", json=BODY).get_json()["state"] == "approved"


def test_a_permanent_connect_failure_gives_up_quickly(make_client, repo, clock):
    # E.g. a rejected client certificate: nothing ever leaves us.
    marketpay = ScriptedMarketPay(process=[dns_failure])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert body["state"] == "failed"
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.NOT_SENT
    assert clock.elapsed <= 15  # the resend window, far from the 50s abort point
    assert 3 <= marketpay.calls.count("process") <= 6  # backoff 0.5, 1, 2, 4, 4...
    assert TERMINAL not in repo.locks


def test_a_lost_reply_is_never_resent(make_client):
    # The request DID leave us; it may be running, unseen. Re-sending could charge twice.
    marketpay = ScriptedMarketPay(
        process=[lost, created(tx())], lookups=[ok({"lastTransactionState": "NOT_FOUND"})]
    )
    make_client(marketpay).post("/payments", json=BODY)
    assert marketpay.calls.count("process") == 1
