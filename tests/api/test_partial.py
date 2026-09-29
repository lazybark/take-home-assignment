"""A PARTIAL approval is reversed and reported declined — never kept, never
left locking the terminal."""

import json
from datetime import UTC, datetime

import httpx
import pytest
from support.marketpay_fakes import (
    IN_PROGRESS,
    REF,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    cancellation,
    created,
    finished,
    lost,
    ok,
    reversal_record,
    tx,
)

from payments.domain.marketpay.models import TransactionResult
from payments.domain.marketpay.outcomes import Completed
from payments.domain.models import (
    Operation,
    Payment,
    PaymentState,
    StateReason,
    UndoReason,
    payment_id_for,
)
from payments.domain.outcomes import resolve_process_outcome
from payments.domain.transitions import apply_purchase

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def partial(terminal_tx="14", response_code="010"):
    return tx("PARTIAL", terminal_tx=terminal_tx, response_code=response_code)


def in_flight() -> Payment:
    return Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.PENDING,
        operation=Operation.PURCHASE,
        created_at=T0,
        updated_at=T0,
    )


def resolved(body) -> Payment:
    resolution = resolve_process_outcome(
        Completed(result=TransactionResult.model_validate(body)), REF
    )

    return apply_purchase(in_flight(), resolution, T0)


# --- Pure rules -----------------------------------------------------------------------


def test_partial_keeps_the_terminal_for_its_reversal():
    payment = resolved(partial())
    assert (payment.state, payment.operation, payment.undo_reason) == (
        PaymentState.UNKNOWN,
        Operation.REVERSAL,
        UndoReason.PARTIAL_APPROVAL,
    )
    assert payment.undo_started_at is None  # due: the request reverses it next


def test_partial_without_a_terminal_transaction_id_needs_a_person():
    payment = resolved(partial(terminal_tx=None))
    assert (payment.state, payment.operation, payment.state_reason) == (
        PaymentState.UNKNOWN,
        None,  # nothing to wait for: the terminal is freed
        StateReason.PARTIAL_NOT_REVERSED,
    )


# --- Through the API --------------------------------------------------------------------


def test_partial_is_reversed_and_reported_declined(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[created(partial())],
        lookups=[ok(finished("PARTIAL"))],  # the baseline read before the reversal
        cancels=[ok(cancellation("OK"))],
    )
    response = make_client(marketpay).post("/payments", json=BODY)

    body = response.get_json()
    assert response.status_code == 201
    assert (body["state"], body["reversed"], body["declineReason"]) == ("declined", True, "010")
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.PARTIAL_APPROVAL_REVERSED
    assert TERMINAL not in repo.locks
    assert marketpay.calls == ["process", "last", "cancel"]

    # A reversal by transaction id — never a REFUND, whose amount we couldn't know.
    reversal = json.loads(marketpay.requests["cancel"][0].content)
    assert (reversal["terminalTransactionId"], reversal["ecrTransactionId"]) == ("14", REF)


def test_partial_seen_through_last_transaction_is_reversed_too(make_client):
    marketpay = ScriptedMarketPay(
        process=[accepted],
        lookups=[
            ok(IN_PROGRESS),
            ok({"lastTransactionState": "FINISHED", "transactionResult": partial()}),
        ],
        cancels=[ok(cancellation("OK"))],
    )

    body = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert (body["state"], body["reversed"]) == ("declined", True)


def test_refused_reversal_frees_the_terminal_and_needs_a_person(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[created(partial()), created(tx(ecr_id="order-next"))],
        lookups=[ok(finished("PARTIAL"))],
        cancels=[ok(cancellation("NOK"))],
    )
    client = make_client(marketpay)
    body = client.post("/payments", json=BODY).get_json()

    assert body["state"] == "unknown"
    stored = repo.payments[PAYMENT_ID]
    assert (stored.operation, stored.state_reason) == (None, StateReason.PARTIAL_NOT_REVERSED)
    assert TERMINAL not in repo.locks  # the terminal is never locked for good

    # The next order can run...
    assert client.post("/payments", json={**BODY, "reference": "order-next"}).status_code == 201

    # ...and a POS cancel can't fix a partial charge automatically.
    cancel = client.post(f"/payments/{PAYMENT_ID}/cancel")
    assert (cancel.status_code, cancel.get_json()["code"]) == (409, "needs_attention")


def test_reversal_that_stays_unclear_keeps_the_terminal_then_settles(make_client, repo, clock):
    marketpay = ScriptedMarketPay(
        process=[created(partial())],
        lookups=[ok(finished("PARTIAL"))],  # baseline, then nothing new while it waits
        cancels=[lost],
    )
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"
    assert TERMINAL in repo.locks  # evidence must survive until it's resolved

    marketpay.scripts["last"] = [ok(reversal_record("OK", terminal_tx="15"))]
    client.post("/reconcile")
    fetched = client.get(f"/payments/{PAYMENT_ID}").get_json()
    assert (fetched["state"], fetched["reversed"]) == ("declined", True)
    assert TERMINAL not in repo.locks


def crashed_owner():
    from payments.domain.models import Owner

    return Owner(instance_id="local-1", boot_id="boot-1")


def test_crash_before_the_reversal_was_claimed_is_released_not_reversed(make_client, repo):
    # The PARTIAL was recorded, then the process died before claiming the reversal.
    from payments.domain.terminal_lock import lock_for

    due = resolved(partial()).model_copy(update={"owner": crashed_owner()})
    repo.payments[PAYMENT_ID] = due
    repo.locks[TERMINAL] = lock_for(due)

    recovery = ScriptedMarketPay(lookups=[ok(finished("PARTIAL"))], cancels=[ok(cancellation())])
    summary = make_client(recovery, boot_id="boot-2").post("/reconcile").get_json()

    assert summary["stillOpen"] == 1  # settled as far as a machine can: needs a person
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.operation, stored.state_reason) == (
        PaymentState.UNKNOWN,
        None,
        StateReason.PARTIAL_NOT_REVERSED,
    )
    assert "cancel" not in recovery.calls  # recovery never starts a card transaction
    assert TERMINAL not in repo.locks


def test_crash_after_the_reversal_was_claimed_is_watched_not_released(make_client, repo, clock):
    class SimulatedCrash(BaseException):
        pass

    def crash(request):
        raise SimulatedCrash

    # Claimed (so it may have been sent), then the process died.
    marketpay = ScriptedMarketPay(process=[created(partial())], lookups=[crash])
    with pytest.raises(SimulatedCrash):
        make_client(marketpay).post("/payments", json=BODY)
    assert repo.payments[PAYMENT_ID].undo_started_at is not None

    # Soon after: nothing new on the terminal proves nothing yet — keep the terminal.
    recovery = ScriptedMarketPay(
        lookups=[ok({"lastTransactionState": "FINISHED", "transactionResult": partial()})]
    )
    restarted = make_client(recovery, boot_id="boot-2")
    assert restarted.post("/reconcile").get_json()["stillOpen"] == 1
    assert TERMINAL in repo.locks

    # Long past any terminal transaction: it was never reversed; hand it to a person.
    clock.elapsed = 200
    restarted.post("/reconcile")
    stored = repo.payments[PAYMENT_ID]
    assert (stored.operation, stored.state_reason) == (None, StateReason.PARTIAL_NOT_REVERSED)
    assert TERMINAL not in repo.locks
    assert "cancel" not in recovery.calls


def test_partial_with_no_time_left_is_left_to_a_person(make_client, repo, clock):
    # The PARTIAL arrives right at the deadline: no time to reverse within it.
    def late_partial(request):
        clock.elapsed = 59
        return httpx.Response(201, json=partial())

    marketpay = ScriptedMarketPay(process=[late_partial], lookups=[ok(finished("PARTIAL"))])
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert body["state"] == "unknown"
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.PARTIAL_NOT_REVERSED
    assert TERMINAL not in repo.locks
    assert "cancel" not in marketpay.calls


def test_pos_cancel_during_a_partial_ends_declined_and_reversed(make_client, repo):
    # The waiter cancels while the purchase is in flight; the bank answers PARTIAL anyway.
    marketpay = ScriptedMarketPay(
        process=[accepted],
        lookups=[ok(IN_PROGRESS)],
        aborts=[TOO_LATE],
        cancels=[ok(cancellation("OK"))],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)  # unknown: nobody drives it now

    marketpay.scripts["last"] = [
        ok({"lastTransactionState": "FINISHED", "transactionResult": partial()})
    ]
    response = client.post(f"/payments/{PAYMENT_ID}/cancel")
    body = response.get_json()
    assert (response.status_code, body["state"], body["reversed"]) == (200, "declined", True)


# --- Edge cases (found in the final review) ------------------------------------------------


def test_a_partial_reversal_never_runs_past_the_deadline(make_client, clock):
    """The PARTIAL arrives at 55 s of 60 while last-transaction is timing out (5 s a look).
    The reversal's baseline looks stop at the deadline (they used to add up to ~17 s)."""

    def slow_partial(request):
        clock.elapsed += 55
        return httpx.Response(201, json=partial())

    def timing_out(request):
        clock.elapsed += 5
        return httpx.Response(500)

    marketpay = ScriptedMarketPay(
        process=[slow_partial], lookups=[timing_out], cancels=[ok(cancellation("OK"))]
    )
    body = make_client(marketpay).post("/payments", json=BODY).get_json()

    assert clock.elapsed <= 61  # answered by the deadline, give or take the last look
    assert body["state"] == "unknown"  # no time to reverse it: a person settles it


def test_a_partial_learned_after_its_request_does_not_block_the_next_order(make_client, repo):
    """The payment answered `unknown`; when the next order arrives, the terminal's record
    shows a PARTIAL. Nobody is there to reverse it (a POS cancel would, with the customer
    present), so the next order gets the terminal and a person settles the PARTIAL."""
    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx(ecr_id="order-next", terminal_tx="15"))],
        lookups=[ok(IN_PROGRESS)],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"
    marketpay.scripts["last"] = [
        ok({"lastTransactionState": "FINISHED", "transactionResult": partial()})
    ]

    nxt = client.post("/payments", json={**BODY, "reference": "order-next"})

    assert nxt.get_json()["state"] == "approved"
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (
        PaymentState.UNKNOWN,
        StateReason.PARTIAL_NOT_REVERSED,
    )
