"""Status wins over responseCode; a contradiction is logged, in detail, once per
record."""

import logging

import httpx
import pytest
from support.marketpay_fakes import REF, TERMINAL, tx

from payments.domain.marketpay.models import ProcessTransactionRequest, TransactionResult
from payments.domain.marketpay.outcomes import Completed
from payments.domain.models import PaymentState
from payments.domain.outcomes import resolve_process_outcome, result_inconsistency


def inconsistency_errors(caplog) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == "marketpay_result_inconsistent"
    ]


@pytest.mark.parametrize(
    ("status", "code", "inconsistent"),
    [
        ("NOK", "000", True),  # the "approved" code with status NOK
        ("OK", "05", True),  # status OK with a decline code
        ("OK", None, True),  # status OK with no code at all
        ("OK", "000", False),
        ("NOK", "116", False),
        ("NOK", None, False),  # a terminal stop: normal on staging
    ],
)
def test_which_results_contradict_themselves(status, code, inconsistent):
    result = TransactionResult.model_validate(tx(status, response_code=code))
    assert (result_inconsistency(result) is not None) is inconsistent


def test_ok_with_a_decline_code_is_still_approved_but_noted():
    resolution = resolve_process_outcome(
        Completed(result=TransactionResult.model_validate(tx("OK", response_code="05"))), REF
    )

    assert resolution.state is PaymentState.APPROVED  # status wins
    assert "status wins" in resolution.state_detail


def test_process_result_contradiction_is_logged_as_an_error(make_marketpay, caplog):
    body = tx("NOK", response_code="000", terminal_tx="31")
    client = make_marketpay(lambda request: httpx.Response(201, json=body))
    with caplog.at_level(logging.ERROR):
        client.process_transaction(
            TERMINAL,
            ProcessTransactionRequest(ecr_transaction_id="order-1", amount="100", currency="752"),
            wait_time=5,
            read_timeout=10,
        )
    [error] = inconsistency_errors(caplog)

    assert error["level"] == "error"

    # Detailed enough to take up with MarketPay (no card numbers).
    for field in ("authorization_code", "echoed_amount", "echoed_currency", "card_capture"):
        assert field in error

    assert (error["status"], error["response_code"], error["terminal_transaction_id"]) == (
        "NOK",
        "000",
        "31",
    )


def test_the_same_record_seen_while_polling_is_logged_once(make_marketpay, caplog):
    last = {"lastTransactionState": "FINISHED", "transactionResult": tx("NOK", response_code="000")}
    client = make_marketpay(lambda request: httpx.Response(200, json=last))

    with caplog.at_level(logging.ERROR):
        for _ in range(5):
            client.get_last_transaction(TERMINAL, timeout=5)

    assert len(inconsistency_errors(caplog)) == 1
