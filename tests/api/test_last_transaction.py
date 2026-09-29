"""Resolving a 202 / lost response by polling last-transaction."""

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
    created,
    finished,
    lost,
    ok,
    tx,
)

from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.models import PaymentState
from payments.domain.outcomes import Sighting, observe_last_transaction

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PREVIOUS = finished("OK", ecr_id="order-previous")


# --- The pure rule -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "sighting", "state"),
    [
        (finished("OK"), Sighting.OURS_FINISHED, PaymentState.APPROVED),
        (finished("NOK", response_code="116"), Sighting.OURS_FINISHED, PaymentState.DECLINED),
        (finished("NOK"), Sighting.OURS_FINISHED, PaymentState.FAILED),  # stopped on terminal
        (IN_PROGRESS, Sighting.IN_PROGRESS, None),
        (
            {"lastTransactionState": "IN_PROGRESS", "transactionResult": tx(ecr_id=REF)},
            Sighting.IN_PROGRESS,
            None,
        ),
        (
            {"lastTransactionState": "IN_PROGRESS", "transactionResult": tx(ecr_id="other")},
            Sighting.NOT_OURS,
            None,
        ),
        (PREVIOUS, Sighting.NOT_OURS, None),  # "may return the previous transaction"
        ({"lastTransactionState": "NOT_FOUND"}, Sighting.NOT_OURS, None),
        (
            {
                "lastTransactionState": "FINISHED",
                "cancellationResult": {
                    "status": "OK",
                    "cancellationParams": {"ecrTransactionId": REF, "terminalTransactionId": "14"},
                },
            },
            Sighting.NOT_OURS,
            None,
        ),
    ],
)
def test_observe_last_transaction(body, sighting, state):
    observation = observe_last_transaction(LastTransactionResult.model_validate(body), REF)
    assert observation.sighting is sighting
    assert (observation.resolution.state if observation.resolution else None) == state


# --- The service, end to end over HTTP -----------------------------------------------


def test_202_then_finished_resolves_to_approved(make_client, clock):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS), ok(IN_PROGRESS), ok(finished())])
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert payment["state"] == "approved"
    assert payment["providerTransactionId"] == "14"
    assert marketpay.calls == ["process", "last", "last", "last"]
    assert clock.sleeps == [1.0, 1.0]


def test_lost_response_is_recovered_from_last_transaction(make_client):
    # The terminal approved, but MarketPay's reply never reached us.
    marketpay = ScriptedMarketPay([lost], [ok(finished())])
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert payment["state"] == "approved"


def test_202_then_bank_declined(make_client):
    marketpay = ScriptedMarketPay(
        [accepted], [ok(IN_PROGRESS), ok(finished("NOK", response_code="116"))]
    )

    payment = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert (payment["state"], payment["declineReason"]) == ("declined", "116")


def test_202_then_stopped_on_terminal_is_failed(make_client):
    # e.g. Cancel pressed on the terminal: NOK without an acquirer responseCode.
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS), ok(finished("NOK"))])

    payment = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert (payment["state"], payment["declineReason"]) == ("failed", None)


def test_previous_transaction_is_never_mistaken_for_ours(make_client, clock):
    # last-transaction keeps showing someone else's *approved* payment.
    marketpay = ScriptedMarketPay([accepted], [ok(PREVIOUS)], [ABORTED])

    payment = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert payment["state"] != "approved"
    assert payment["state"] == "failed"  # the abort confirmed nothing of ours was running
    assert clock.elapsed <= 59


def test_lookup_errors_are_retried(make_client):
    def reset(request):
        raise httpx.ConnectError("connection reset", request=request)

    marketpay = ScriptedMarketPay(
        [accepted], [httpx.Response(500), reset, ok(IN_PROGRESS), ok(finished())]
    )

    assert make_client(marketpay).post("/payments", json=BODY).get_json()["state"] == "approved"


def test_still_in_progress_after_a_too_late_abort_stays_unknown_not_failed(make_client, clock):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])
    payment = make_client(marketpay).post("/payments", json={**BODY, "deadlineSeconds": 20})

    assert payment.get_json()["state"] == "unknown"
    assert clock.elapsed <= 19  # answered before deadline 20 (1s kept to respond)


def test_happy_path_makes_no_lookup(make_client):
    marketpay = ScriptedMarketPay([created(tx())], [ok(IN_PROGRESS)])
    make_client(marketpay).post("/payments", json=BODY)
    assert marketpay.calls == ["process"]


def test_diagnostic_last_transaction_route(make_client):
    marketpay = ScriptedMarketPay([accepted], [ok(finished())])
    response = make_client(marketpay).get(f"/terminals/{TERMINAL}/last-transaction")

    assert response.status_code == 200

    body = response.get_json()
    assert body["lastTransactionState"] == "FINISHED"
    assert body["transactionResult"]["finalTransactionParams"]["ecrTransactionId"] == REF
    assert marketpay.calls == ["last"]
