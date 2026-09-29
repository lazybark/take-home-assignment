"""MarketPay holds the request past our deadline; we abort while it is still open."""

import json
import threading
import time

import httpx
import pytest
from support.marketpay_fakes import (
    REF,
    TERMINAL,
    ScriptedMarketPay,
    cancellation,
    created,
    finished,
    ok,
    tx,
)

from payments.domain.models import PaymentState, StateReason, payment_id_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


class WaitingTerminal:
    """Like staging: a transaction waits for a card; an abort sent WHILE its request
    is open stops it (204), and the open request then returns the terminal's result."""

    def __init__(self, after_abort=lambda: httpx.Response(201, json=tx("NOK")), abort_status=204):
        self.after_abort = after_abort
        self.abort_status = abort_status
        self.aborted = threading.Event()
        self.open_when_aborted: bool | None = None
        self._open = threading.Event()

    def process(self, request):
        self._open.set()
        assert self.aborted.wait(5), "nobody aborted"
        return self.after_abort()

    def abort(self, request):
        self.open_when_aborted = self._open.is_set()
        self.aborted.set()
        return httpx.Response(self.abort_status)


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout

    while not predicate():
        assert time.monotonic() < deadline, "timed out"

        time.sleep(0.01)


def test_customer_never_taps_abort_while_open_gives_a_known_state_in_time(make_client, repo, clock):
    terminal = WaitingTerminal()
    marketpay = ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert body["state"] == "failed"

    stored = repo.payments[PAYMENT_ID]
    assert stored.state_reason is StateReason.ABORTED  # our 204 stopped it

    assert terminal.open_when_aborted is True  # sent while MarketPay held our request
    assert TERMINAL not in repo.locks  # a known state: the terminal is clean
    assert clock.elapsed <= 59  # answered within the 60s deadline
    assert marketpay.calls == ["process", "abort"]  # no polling was needed
    assert marketpay.requests["process"][0].url.params["waitTime"] == "150"


def test_customer_tapped_just_before_the_abort_is_approved(make_client, repo):
    # 409: too late to abort — the bank is deciding; the open call then returns its answer.
    terminal = WaitingTerminal(
        after_abort=lambda: httpx.Response(201, json=tx("OK")), abort_status=409
    )
    marketpay = ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert (body["state"], body["providerTransactionId"]) == ("approved", "14")
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.BANK_APPROVED
    assert TERMINAL not in repo.locks


def test_answer_after_the_deadline_is_recorded_when_it_arrives(make_client, repo):
    bank_answers = threading.Event()

    def slow_bank():
        assert bank_answers.wait(5)
        return httpx.Response(201, json=tx("OK"))

    terminal = WaitingTerminal(after_abort=slow_bank, abort_status=409)
    marketpay = ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    # At the deadline the bank still hadn't answered: honest "unknown", terminal kept.
    assert body["state"] == "unknown"
    assert TERMINAL in repo.locks

    bank_answers.set()  # ...then the still-open call returns the approval
    wait_until(lambda: repo.payments[PAYMENT_ID].state is PaymentState.APPROVED)

    assert TERMINAL not in repo.locks


def test_late_answer_does_not_overwrite_a_payment_settled_meanwhile(make_client, repo):
    bank_answers = threading.Event()

    def slow_bank():
        assert bank_answers.wait(5)

        return httpx.Response(201, json=tx("OK"))

    terminal = WaitingTerminal(after_abort=slow_bank, abort_status=409)
    marketpay = ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    # A reconcile settles it first from last-transaction; the late answer changes nothing.
    marketpay.scripts["last"] = [ok(finished("OK"))]
    client.post("/reconcile")
    assert client.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "approved"

    before = repo.payments[PAYMENT_ID]
    bank_answers.set()
    time.sleep(0.1)
    assert repo.payments[PAYMENT_ID] == before


def test_happy_path_is_still_one_call(make_client):
    marketpay = ScriptedMarketPay(process=[created(tx())])
    make_client(marketpay).post("/payments", json=BODY)

    assert marketpay.calls == ["process"]


def test_reversal_waiting_for_a_tap_is_aborted_while_open(make_client, repo):
    """A reversal also waits for the card. If nobody taps, it's stopped while open,
    the terminal is left clean, and the charge is reported as still standing."""

    client_marketpay = ScriptedMarketPay(process=[created(tx())], lookups=[ok(finished())])
    client = make_client(client_marketpay)
    client.post("/payments", json=BODY)

    reversal = WaitingTerminal(after_abort=lambda: httpx.Response(200, json=cancellation("NOK")))
    client_marketpay.scripts["cancel"] = [reversal.process]
    client_marketpay.scripts["abort"] = [reversal.abort]
    response = client.post(f"/payments/{PAYMENT_ID}/cancel")

    assert (response.status_code, response.get_json()["code"]) == (409, "cancel_failed")
    assert reversal.open_when_aborted is True

    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.operation) == (PaymentState.APPROVED, None)
    assert TERMINAL not in repo.locks

    body = json.loads(client_marketpay.requests["cancel"][0].content)
    assert body["terminalTransactionId"] == "14"


@pytest.mark.parametrize("deadline", [1, 2, 3])
def test_even_a_tiny_deadline_aborts_while_the_request_is_open(make_client, deadline):
    """For 1–2 s the abort point used to equal the answer time, so no abort went out and
    the terminal kept asking for a card. Now: abort at a third, answer at two thirds."""
    terminal = WaitingTerminal()
    marketpay = ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort])
    body = (
        make_client(marketpay)
        .post("/payments", json={**BODY, "deadlineSeconds": deadline})
        .get_json()
    )

    assert terminal.open_when_aborted is True
    assert body["state"] == "failed"
