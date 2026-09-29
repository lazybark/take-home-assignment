"""POST /payments/{id}/cancel: reverse an approved payment, abort one in flight."""

import json
import threading
from datetime import timedelta

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
    cancellation,
    cancelled_last,
    created,
    dns_failure,
    finished,
    lost,
    ok,
    reversal_record,
    tx,
)

from payments.domain.models import (
    Operation,
    Payment,
    PaymentState,
    Resolution,
    ResolvedVia,
    StateReason,
    UndoReason,
    payment_id_for,
    refund_reference_for,
)
from payments.domain.repository import StoreUnavailable
from payments.domain.terminal_lock import lock_for
from payments.domain.transitions import apply_purchase

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


def approved_client(make_client, **scripts):
    """A client whose first payment was approved (terminalTransactionId "14")."""

    process = [created(tx()), *scripts.pop("process", [])]
    marketpay = ScriptedMarketPay(process=process, **scripts)
    client = make_client(marketpay)

    assert client.post("/payments", json=BODY).get_json()["state"] == "approved"

    return client, marketpay


def cancel(client):
    return client.post(f"/payments/{PAYMENT_ID}/cancel")


# --- Approved: reversal via cancel-transaction -------------------------------------------


def test_reversal_of_an_approved_payment(make_client, repo):
    client, marketpay = approved_client(make_client, cancels=[ok(cancellation("OK"))])
    response = cancel(client)

    assert response.status_code == 200

    body = response.get_json()
    assert (body["state"], body["reversed"]) == ("cancelled", True)
    [request] = marketpay.requests["cancel"]
    assert request.url.path == f"/cancel-transaction/{TERMINAL}"
    assert json.loads(request.content) == {
        "terminalTransactionId": "14",
        "ecrTransactionId": REF,
        "amount": "1299",
        "currency": "752",
        "ecrParams": {"ecrId": "TEST_ECR_ID"},
    }
    assert TERMINAL not in repo.locks
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.REVERSED


def test_cancel_is_idempotent(make_client):
    client, marketpay = approved_client(make_client, cancels=[ok(cancellation("OK"))])

    cancel(client)

    again = cancel(client)

    assert (again.status_code, again.get_json()["state"]) == (200, "cancelled")
    assert marketpay.calls.count("cancel") == 1


def test_refused_reversal_is_409_and_the_charge_stands(make_client, repo):
    client, marketpay = approved_client(
        make_client,
        cancels=[ok(cancellation("NOK")), ok(cancellation("OK"))],
        # Each attempt's baseline: the purchase (14), then attempt 1's own NOK record (15).
        # A repeated reversal is only sent once its baseline is known and stored.
        lookups=[ok(finished()), ok(reversal_record("NOK", terminal_tx="15"))],
    )
    response = cancel(client)

    assert response.status_code == 409
    assert response.get_json()["code"] == "cancel_failed"
    assert client.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "approved"
    assert TERMINAL not in repo.locks

    # A later cancel may try again.
    assert cancel(client).get_json()["state"] == "cancelled"


def test_lost_reversal_response_is_recovered_and_never_resent(make_client):
    # As on staging: the reversal shows up as our reference with a new id (15), not as a
    # cancellationResult. Lookups: baseline (the purchase, 14), then two, then the record.
    client, marketpay = approved_client(
        make_client,
        cancels=[lost],
        lookups=[ok(finished()), ok(finished()), ok(finished()), ok(reversal_record("OK"))],
    )

    body = cancel(client).get_json()
    assert (body["state"], body["reversed"]) == ("cancelled", True)
    assert marketpay.calls.count("cancel") == 1  # a second one could say "NOK: already done"


def test_lost_reversal_response_in_the_spec_shape_is_recovered_too(make_client):
    client, _ = approved_client(
        make_client, cancels=[lost], lookups=[ok(finished()), ok(cancelled_last("OK"))]
    )

    assert cancel(client).get_json()["state"] == "cancelled"


def test_reversal_takes_a_baseline_before_it_is_sent(make_client, repo):
    client, marketpay = approved_client(
        make_client, cancels=[ok(cancellation("OK"))], lookups=[ok(finished())]
    )

    cancel(client)

    assert marketpay.calls[-2:] == ["last", "cancel"]  # baseline first, then the reversal
    assert repo.payments[PAYMENT_ID].undo_baseline_transaction_id == "14"


def test_second_attempt_is_not_fooled_by_the_first_attempts_record(make_client, repo, clock):
    """Attempt 1 is refused (record 15, NOK). Attempt 2's reply is lost; while it waits
    for the card the terminal still shows record 15. That must not count as attempt 2's
    answer — only a record newer than the baseline does."""

    client, marketpay = approved_client(
        make_client,
        cancels=[ok(cancellation("NOK"))],
        lookups=[ok(finished())],
    )

    assert cancel(client).status_code == 409  # attempt 1 refused; still approved

    marketpay.scripts["cancel"] = [lost]
    marketpay.scripts["last"] = [ok(reversal_record("NOK", terminal_tx="15"))]
    second = cancel(client)
    assert second.get_json()["state"] == "unknown"  # not "approved"!
    assert repo.payments[PAYMENT_ID].undo_baseline_transaction_id == "15"

    marketpay.scripts["last"] = [ok(reversal_record("OK", terminal_tx="16"))]
    client.post("/reconcile")
    assert client.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "cancelled"


def test_reversal_202_is_resolved_from_last_transaction(make_client):
    client, _ = approved_client(
        make_client,
        cancels=[accepted],
        lookups=[ok(finished()), ok(finished()), ok(reversal_record("OK"))],
    )

    assert cancel(client).get_json()["state"] == "cancelled"


def test_reversal_never_visible_is_unknown_and_keeps_the_terminal(make_client, repo):
    client, _ = approved_client(make_client, cancels=[lost], lookups=[ok(IN_PROGRESS)])
    response = cancel(client)

    assert (response.status_code, response.get_json()["state"]) == (200, "unknown")
    assert TERMINAL in repo.locks
    assert repo.payments[PAYMENT_ID].operation is Operation.REVERSAL


def test_unknown_reversal_settles_on_a_later_look(make_client, repo):
    client, marketpay = approved_client(make_client, cancels=[lost], lookups=[ok(IN_PROGRESS)])
    cancel(client)

    marketpay.scripts["last"] = [ok(reversal_record("OK"))]
    client.post("/reconcile")
    fetched = client.get(f"/payments/{PAYMENT_ID}").get_json()
    assert (fetched["state"], fetched["reversed"]) == ("cancelled", True)
    assert TERMINAL not in repo.locks


def test_reversal_that_never_landed_returns_to_approved(make_client, repo, clock):
    client, marketpay = approved_client(make_client, cancels=[lost], lookups=[ok(IN_PROGRESS)])
    cancel(client)

    # The purchase is still the last record. A minute later that proves nothing: the
    # reversal may still be waiting for the customer's tap.
    marketpay.scripts["last"] = [ok(finished())]
    client.post("/reconcile")
    assert client.get(f"/payments/{PAYMENT_ID}").get_json()["state"] == "unknown"

    clock.elapsed += 180  # long past any terminal transaction: it never happened
    client.post("/reconcile")

    fetched = client.get(f"/payments/{PAYMENT_ID}").get_json()
    assert (fetched["state"], fetched["reversed"]) == ("approved", False)
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.UNDO_NOT_RECORDED
    assert TERMINAL not in repo.locks


def test_reversal_that_never_left_us_is_resent(make_client):
    client, marketpay = approved_client(make_client, cancels=[dns_failure, ok(cancellation("OK"))])
    assert cancel(client).get_json()["state"] == "cancelled"
    assert marketpay.calls.count("cancel") == 2


def test_reversal_that_can_never_be_sent_is_409(make_client, repo):
    client, marketpay = approved_client(make_client, cancels=[dns_failure])
    response = cancel(client)

    assert (response.status_code, response.get_json()["code"]) == (409, "cancel_failed")
    assert marketpay.calls.count("cancel") == 3
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.UNDO_NOT_SENT


def test_partial_reversal_needs_attention(make_client, repo):
    client, _ = approved_client(make_client, cancels=[ok(cancellation("PARTIAL"))])
    response = cancel(client)

    assert (response.status_code, response.get_json()["code"]) == (409, "needs_attention")
    assert repo.payments[PAYMENT_ID].state is PaymentState.UNKNOWN
    assert TERMINAL not in repo.locks


def test_reversal_needs_the_terminal(make_client):
    # Payment A approved; payment B is unresolved and holds the terminal.
    other = {**BODY, "reference": "order-other"}
    marketpay = ScriptedMarketPay(
        process=[created(tx()), accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE]
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    assert client.post("/payments", json=other).get_json()["state"] == "unknown"

    response = cancel(client)
    assert (response.status_code, response.get_json()["code"]) == (409, "terminal_busy")
    assert "cancel" not in marketpay.calls


# --- Approved despite an abort: REFUND ------------------------------------------------------


def test_approved_despite_our_deadline_abort_is_undone_with_a_refund(make_client, repo):
    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx(ecr_id=refund_ref, terminal_tx="15"))],
        lookups=[ok(IN_PROGRESS)] * 50 + [ok(finished("OK"))],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)

    assert client.post("/payments", json=BODY).get_json()["state"] == "approved"

    response = cancel(client)

    assert (response.status_code, response.get_json()["state"]) == (200, "cancelled")
    assert "cancel" not in marketpay.calls  # MarketPay: after an abort, undo with a REFUND

    refund_request = json.loads(marketpay.requests["process"][1].content)
    assert refund_request["transactionType"] == "REFUND"
    assert refund_request["ecrTransactionId"] == refund_ref
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.REFUNDED


def test_declined_refund_is_409_and_the_charge_stands(make_client):
    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx("NOK", ecr_id=refund_ref, response_code="05"))],
        lookups=[ok(IN_PROGRESS)] * 50 + [ok(finished("OK"))],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    response = cancel(client)

    assert (response.status_code, response.get_json()["code"]) == (409, "cancel_failed")


# --- In flight, nobody driving it (unknown): abort + confirm ----------------------------


def test_cancel_of_an_unknown_purchase_aborts_and_confirms(make_client, repo):
    marketpay = ScriptedMarketPay(
        process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE, ABORTED]
    )
    client = make_client(marketpay)

    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"

    marketpay.scripts["last"] = [ok(IN_PROGRESS), ok(finished("NOK"))]
    response = cancel(client)

    assert (response.status_code, response.get_json()["state"]) == (200, "cancelled")

    stored = repo.payments[PAYMENT_ID]
    assert (stored.state_reason, stored.reversed) == (StateReason.CANCELLED_BEFORE_CHARGE, False)
    assert TERMINAL not in repo.locks


def test_cancel_of_an_unknown_purchase_that_was_approved_refunds_it(make_client):
    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx(ecr_id=refund_ref, terminal_tx="15"))],
        lookups=[ok(IN_PROGRESS)],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)  # unknown

    marketpay.scripts["last"] = [ok(finished("OK"))]  # it had been approved after all
    response = cancel(client)
    body = response.get_json()

    assert (response.status_code, body["state"], body["reversed"]) == (200, "cancelled", True)


# --- Nothing to cancel ------------------------------------------------------------------------


@pytest.mark.parametrize("process", [created(tx("NOK", response_code="116")), created(tx("NOK"))])
def test_declined_or_failed_is_409_not_cancellable(make_client, process):
    marketpay = ScriptedMarketPay(process=[process])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    response = cancel(client)
    assert (response.status_code, response.get_json()["code"]) == (409, "not_cancellable")
    assert marketpay.calls == ["process"]


def test_cancel_of_unknown_id_is_404(make_client):
    response = make_client(ScriptedMarketPay()).post(
        "/payments/00000000-0000-0000-0000-000000000000/cancel"
    )

    assert response.status_code == 404


# --- Edge cases (found in the final review) ------------------------------------------------


def test_no_abort_for_a_purchase_that_settled_just_before_the_cancel(
    make_client, repo, owner, clock
):
    """The cancel read the purchase as running, but it settled before the cancel was
    recorded. An abort now names no transaction and could stop the NEXT payment on the
    terminal: none is sent, and the approved payment is reversed instead."""
    now = clock.now()
    running = Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.PENDING,
        operation=Operation.PURCHASE,
        owner=owner,
        lease_until=now + timedelta(minutes=5),
        created_at=now,
        updated_at=now,
    )
    repo.payments[PAYMENT_ID], repo.locks[TERMINAL] = running, lock_for(running)
    approved = Resolution(
        state=PaymentState.APPROVED,
        state_reason=StateReason.BANK_APPROVED,
        resolved_via=ResolvedVia.PROCESS_RESPONSE,
        provider_transaction_id="14",
    )
    write = repo.update

    def purchase_records_first(payment_id, change):
        repo.update = write  # once: the purchase's own request wins the race
        write(payment_id, lambda cur: apply_purchase(cur, approved, clock.now()))
        return write(payment_id, change)

    repo.update = purchase_records_first
    marketpay = ScriptedMarketPay(lookups=[ok(finished())], cancels=[ok(cancellation("OK"))])

    assert cancel(make_client(marketpay)).get_json()["state"] == "cancelled"
    assert "abort" not in marketpay.calls


def test_a_repeated_reversal_is_not_sent_without_its_baseline(make_client, repo):
    """Attempt 1 is refused. For attempt 2 last-transaction fails, so its baseline is
    unknown, and its record couldn't be told from attempt 1's (both carry our
    ecrTransactionId and a new id). It is not sent; the charge stands; cancel may retry."""
    client, marketpay = approved_client(
        make_client,
        cancels=[ok(cancellation("NOK"))],
        lookups=[ok(finished()), httpx.Response(500)],  # attempt 1's baseline, then 500s
    )
    cancel(client)
    response = cancel(client)

    assert (response.status_code, response.get_json()["code"]) == (409, "cancel_failed")
    assert marketpay.calls.count("cancel") == 1
    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (PaymentState.APPROVED, StateReason.UNDO_NOT_SENT)
    assert TERMINAL not in repo.locks


def test_a_lost_reversal_of_an_earlier_purchase_frees_the_terminal(make_client, repo, clock):
    """P1 is reversed while P2 is the terminal's last record, and the reversal request is
    lost. P2's record (the baseline) still being last means ours never landed: after the
    time rule, reconcile concludes so and the terminal is free again (it used to stay
    locked for good)."""
    second = {**BODY, "reference": "order-second"}
    last_is_second = ok(
        {
            "lastTransactionState": "FINISHED",
            "transactionResult": tx(ecr_id="order-second", terminal_tx="15"),
        }
    )
    marketpay = ScriptedMarketPay(
        process=[
            created(tx(terminal_tx="14")),
            created(tx(ecr_id="order-second", terminal_tx="15")),
            created(tx(ecr_id="order-third", terminal_tx="16")),
        ],
        cancels=[httpx.Response(500)],
        lookups=[last_is_second],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)
    client.post("/payments", json=second)
    cancel(client)  # the reversal's fate is unknown for now

    clock.elapsed += 200
    client.post("/reconcile")

    stored = repo.payments[PAYMENT_ID]
    assert (stored.state, stored.state_reason) == (
        PaymentState.APPROVED,
        StateReason.UNDO_NOT_RECORDED,
    )
    third = client.post("/payments", json={**BODY, "reference": "order-third"})
    assert third.get_json()["state"] == "approved"


def test_cancelling_again_first_checks_whether_the_reversal_landed_late(make_client, repo, clock):
    """We concluded "the reversal never landed" only from the missing record, and then it
    landed. Cancelling again looks first instead of sending a second reversal, whose
    "already reversed" NOK would leave the record saying charged."""
    now = clock.now()
    repo.payments[PAYMENT_ID] = Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.APPROVED,
        state_reason=StateReason.UNDO_NOT_RECORDED,
        resolved_via=ResolvedVia.LAST_TRANSACTION,
        provider_transaction_id="14",
        undo_attempts=1,
        undo_reason=UndoReason.POS_CANCEL,
        undo_baseline_transaction_id="14",
        cancel_requested_at=now,
        created_at=now,
        updated_at=now,
    )
    repo.verify[TERMINAL] = PAYMENT_ID
    marketpay = ScriptedMarketPay(
        lookups=[ok(reversal_record("OK", terminal_tx="16"))],  # it did land, late
        cancels=[ok(cancellation("NOK"))],
    )

    assert cancel(make_client(marketpay)).get_json()["state"] == "cancelled"
    assert "cancel" not in marketpay.calls
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.LATE_REVERSAL_FOUND


def test_approved_despite_our_abort_is_refunded_even_if_the_abort_note_was_lost(make_client, repo):
    """The deadline abort is too late (409) and the payment completes, while Firestore
    fails exactly as we note "abort requested". The recorded outcome still says an abort
    was sent, so a later cancel refunds it (MarketPay's rule) instead of reversing it."""
    aborted = threading.Event()

    def purchase(request):
        aborted.wait(5)
        return httpx.Response(201, json=tx(terminal_tx="14"))

    def too_late(request):
        aborted.set()
        return httpx.Response(409)

    refund_ref = refund_reference_for(PAYMENT_ID, 1)
    marketpay = ScriptedMarketPay(
        process=[purchase, created(tx(ecr_id=refund_ref, terminal_tx="15"))], aborts=[too_late]
    )
    write = repo.update

    def lose_the_abort_note(payment_id, change):
        current = repo.payments[payment_id]
        proposed = change(current)
        if (
            current.abort_requested_at is None
            and proposed.abort_requested_at is not None
            and proposed.operation is Operation.PURCHASE
        ):
            raise StoreUnavailable("injected: the note is lost", transient=True)
        return write(payment_id, change)

    repo.update = lose_the_abort_note
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "approved"
    assert repo.payments[PAYMENT_ID].abort_requested_at is not None  # kept by the outcome

    assert cancel(client).get_json()["state"] == "cancelled"
    assert "cancel" not in marketpay.calls  # no reversal
    assert json.loads(marketpay.requests["process"][1].content)["transactionType"] == "REFUND"
