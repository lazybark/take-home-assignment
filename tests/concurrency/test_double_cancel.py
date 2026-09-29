"""A double-tapped cancel: two cancels of one payment, in one process, at the same instant."""

import threading

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

from payments.domain.models import PaymentState, payment_id_for
from payments.infrastructure.store.memory import InMemoryPaymentRepository

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)
CANCEL = f"/payments/{PAYMENT_ID}/cancel"


class PausingRepository(InMemoryPaymentRepository):
    """Holds one named thread just before its next write, to line two requests up."""

    def __init__(self) -> None:
        super().__init__()
        self.pause_thread: str | None = None
        self.paused, self.resume = threading.Event(), threading.Event()

    def update(self, payment_id, change):
        if threading.current_thread().name == self.pause_thread:
            self.pause_thread = None
            self.paused.set()
            assert self.resume.wait(5)
        return super().update(payment_id, change)


@pytest.fixture
def repo() -> PausingRepository:
    return PausingRepository()


def test_a_double_tapped_cancel_sends_one_reversal(make_client, repo):
    """Both cancels read `approved` before either claims the undo. They run in one
    process, so the process (the owner) can't tell them apart: the claim itself must, or
    both would send a reversal and the second's "already reversed" NOK could win."""
    reversal_on_terminal, release = threading.Event(), threading.Event()

    def reversal(request):
        reversal_on_terminal.set()
        release.wait(3)
        return httpx.Response(200, json=cancellation("OK"))

    marketpay = ScriptedMarketPay(
        process=[created(tx())], cancels=[reversal], lookups=[ok(finished())]
    )
    first_pos, second_pos = make_client(marketpay), make_client(marketpay)
    assert first_pos.post("/payments", json=BODY).get_json()["state"] == "approved"

    answers: dict = {}
    repo.pause_thread = "second-tap"
    second = threading.Thread(
        name="second-tap", target=lambda: answers.update(second=second_pos.post(CANCEL))
    )
    second.start()
    assert repo.paused.wait(5)  # it has read "approved" and is about to claim
    first = threading.Thread(target=lambda: answers.update(first=first_pos.post(CANCEL)))
    first.start()
    assert reversal_on_terminal.wait(5)  # the first cancel's reversal is on the terminal
    repo.resume.set()  # now the second one claims
    second.join(10)
    release.set()
    first.join(10)

    assert marketpay.calls.count("cancel") == 1
    assert answers["first"].get_json()["state"] == "cancelled"
    assert repo.payments[PAYMENT_ID].state is PaymentState.CANCELLED
