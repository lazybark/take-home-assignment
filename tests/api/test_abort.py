"""At the deadline: abort the terminal, confirm what it recorded, answer in time."""

import httpx
from support.marketpay_fakes import (
    ABORTED,
    IN_PROGRESS,
    REF,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    created,
    finished,
    lost,
    ok,
    tx,
)

from payments.domain.marketpay.outcomes import Aborted
from payments.domain.models import StateReason, payment_id_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PREVIOUS = finished("OK", ecr_id="order-previous")


# --- End to end -------------------------------------------------------------------------


def test_customer_never_taps_abort_stops_the_terminal_and_frees_it(make_client, repo, clock):
    # In progress for the whole 50s of polling; after the service's abort the terminal records NOK.
    marketpay = ScriptedMarketPay(
        [accepted], [ok(IN_PROGRESS)] * 50 + [ok(finished("NOK"))], [ABORTED]
    )
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert payment["state"] == "failed"

    stored = repo.payments[payment_id_for(REF)]
    assert stored.state_reason is StateReason.ABORTED

    assert TERMINAL not in repo.locks  # a known state: the next payment can go
    assert marketpay.calls.count("abort") == 1
    assert clock.elapsed <= 59  # answered before the 60s deadline


def test_abort_is_sent_only_once_polling_time_is_used_up(make_client, clock):
    marketpay = ScriptedMarketPay(
        [accepted], [ok(IN_PROGRESS)] * 60 + [ok(finished("NOK"))], [ABORTED]
    )
    make_client(marketpay).post("/payments", json=BODY)

    first_abort = marketpay.calls.index("abort")

    lookups_before_abort = marketpay.calls[:first_abort].count("last")
    assert lookups_before_abort == 50  # polled ~1/s from 0s to 50s, then aborted


def test_completed_just_before_the_abort_is_approved(make_client, repo):
    # The customer tapped at the last moment: the abort is too late, the bank approved.
    marketpay = ScriptedMarketPay(
        [accepted], [ok(IN_PROGRESS)] * 50 + [ok(finished("OK"))], [TOO_LATE]
    )
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert payment["state"] == "approved"
    assert payment["providerTransactionId"] == "14"
    assert TERMINAL not in repo.locks


def test_too_late_and_still_running_is_unknown_and_keeps_the_lock(make_client, repo, clock):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert payment["state"] == "unknown"
    assert TERMINAL in repo.locks  # evidence must survive until it's resolved
    assert clock.elapsed <= 59


def test_lost_request_abort_confirms_nothing_ran(make_client, repo):
    # Our request was sent but its reply lost, and the terminal shows only the previous
    # transaction: after a 204 and two confirming lookups, nothing of ours ran.
    marketpay = ScriptedMarketPay([lost], [ok(PREVIOUS)], [ABORTED])
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert payment["state"] == "failed"
    assert repo.payments[payment_id_for(REF)].resolved_via == "abort_response"
    assert TERMINAL not in repo.locks


def test_abort_without_an_answer_is_retried(make_client):
    def reset(request):
        raise httpx.RemoteProtocolError("connection reset", request=request)

    marketpay = ScriptedMarketPay(
        [accepted],
        [ok(IN_PROGRESS)] * 50 + [ok(finished("NOK"))],
        [reset, httpx.Response(503), ABORTED],
    )
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert marketpay.calls.count("abort") == 3
    assert payment["state"] == "failed"


def test_abort_that_never_gets_an_answer_stays_unknown(make_client, repo):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [httpx.Response(500)])
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert marketpay.calls.count("abort") == 3  # MAX_ABORT_ATTEMPTS
    assert payment["state"] == "unknown"
    assert TERMINAL in repo.locks


def test_happy_path_never_aborts(make_client):
    marketpay = ScriptedMarketPay([created(tx())], [ok(IN_PROGRESS)], [ABORTED])

    make_client(marketpay).post("/payments", json=BODY)

    assert marketpay.calls == ["process"]


def test_resolved_by_polling_never_aborts(make_client):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS), ok(finished())], [ABORTED])

    make_client(marketpay).post("/payments", json=BODY)

    assert "abort" not in marketpay.calls


def test_abort_request_shape(make_marketpay):
    seen_requests: list[httpx.Request] = []

    def handler(request):
        seen_requests.append(request)
        return httpx.Response(204)

    assert isinstance(make_marketpay(handler).abort_transaction(TERMINAL, timeout=5), Aborted)
    [request] = seen_requests
    assert (request.method, request.url.path) == ("POST", f"/abort-transaction/{TERMINAL}")
    assert request.content == b'{"ecrId":"TEST_ECR_ID"}'


def test_there_is_no_route_to_abort_a_terminal_directly(make_client):
    # Stopping a payment goes through POST /payments/{id}/cancel, which keeps our
    # records and locks right. A raw abort route would let anyone stop a waiter's payment.
    marketpay = ScriptedMarketPay(aborts=[ABORTED])
    response = make_client(marketpay).post(f"/terminals/{TERMINAL}/abort")

    assert response.status_code in (404, 405)
    assert marketpay.calls == []
