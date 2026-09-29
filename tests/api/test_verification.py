"""An outcome we only inferred is re-checked once, before the terminal's next
transaction can overwrite the evidence."""

from datetime import UTC, datetime

from support.marketpay_fakes import (
    ABORTED,
    IN_PROGRESS,
    REF,
    TERMINAL,
    ScriptedMarketPay,
    created,
    finished,
    lost,
    ok,
    reversal_record,
    tx,
)

from payments.domain.models import Payment, PaymentState, ResolvedVia, StateReason, payment_id_for
from payments.domain.repository import StoreUnavailable

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
NEXT = {**BODY, "reference": "order-next"}
PAYMENT_ID = payment_id_for(REF)
PREVIOUS = finished("OK", ecr_id="order-previous")


# --- Through the service ------------------------------------------------------------------


def inferred_failure(make_client, repo):
    """A payment whose reply was lost, then aborted (204) with no record of ours ever
    seen: `failed` by inference. Its terminal is flagged."""

    marketpay = ScriptedMarketPay(process=[lost], lookups=[ok(PREVIOUS)], aborts=[ABORTED])
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "failed"
    assert repo.verify[TERMINAL] == PAYMENT_ID

    return client, marketpay


def test_a_late_charge_is_found_before_the_next_payment_overwrites_it(make_client, repo):
    client, marketpay = inferred_failure(make_client, repo)

    # The lost request had reached the terminal after all, and was approved.
    marketpay.scripts["last"] = [ok(finished("OK"))]
    marketpay.scripts["process"] = [created(tx(ecr_id="order-next"))]
    marketpay.calls.clear()
    response = client.post("/payments", json=NEXT)

    assert response.status_code == 201  # the new payment isn't held up
    assert marketpay.calls == ["last", "process"]  # checked BEFORE sending ours
    old = repo.payments[PAYMENT_ID]
    assert (old.state, old.state_reason) == (PaymentState.APPROVED, StateReason.LATE_CHARGE_FOUND)
    assert TERMINAL not in repo.verify  # checked once
    assert "cancel" not in marketpay.calls  # never reversed in front of the next customer


def test_nothing_found_just_clears_the_flag(make_client, repo):
    client, marketpay = inferred_failure(make_client, repo)
    marketpay.scripts["process"] = [created(tx(ecr_id="order-next"))]
    client.post("/payments", json=NEXT)

    assert repo.payments[PAYMENT_ID].state is PaymentState.FAILED
    assert TERMINAL not in repo.verify


def test_the_happy_path_makes_no_extra_lookup(make_client, repo):
    marketpay = ScriptedMarketPay(process=[created(tx()), created(tx(ecr_id="order-next"))])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)
    client.post("/payments", json=NEXT)
    assert marketpay.calls == ["process", "process"]
    assert repo.verify == {}


def test_reconcile_checks_a_flagged_terminal_without_waiting_for_a_payment(make_client, repo):
    client, marketpay = inferred_failure(make_client, repo)
    marketpay.scripts["last"] = [ok(finished("OK"))]

    client.post("/reconcile")
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.LATE_CHARGE_FOUND
    assert TERMINAL not in repo.verify


def test_a_failed_lookup_does_not_block_the_next_payment(make_client, repo):
    client, marketpay = inferred_failure(make_client, repo)
    marketpay.scripts["last"] = []  # MarketPay answers 503
    marketpay.scripts["process"] = [created(tx(ecr_id="order-next"))]
    assert client.post("/payments", json=NEXT).status_code == 201
    assert TERMINAL not in repo.verify  # the new transaction replaces the evidence anyway


def test_a_late_reversal_is_found_too(make_client, repo, clock):
    # An approved payment whose reversal reply was lost and, much later, judged never landed.
    marketpay = ScriptedMarketPay(process=[created(tx())], lookups=[ok(finished())], cancels=[lost])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)
    marketpay.scripts["last"] = [ok(IN_PROGRESS)]
    client.post(f"/payments/{PAYMENT_ID}/cancel")  # unknown
    marketpay.scripts["last"] = [ok(finished())]
    clock.elapsed += 200
    client.post("/reconcile")
    assert repo.payments[PAYMENT_ID].state_reason is StateReason.UNDO_NOT_RECORDED
    assert repo.verify[TERMINAL] == PAYMENT_ID

    # The next look finds the reversal did land after all.
    marketpay.scripts["last"] = [ok(reversal_record("OK", terminal_tx="15"))]
    client.post("/reconcile")
    old = repo.payments[PAYMENT_ID]
    assert (old.state, old.reversed, old.state_reason) == (
        PaymentState.CANCELLED,
        True,
        StateReason.LATE_REVERSAL_FOUND,
    )


def test_reconcile_keeps_the_flag_when_it_finds_nothing(make_client, repo):
    # A late request may still land before the next payment: only that payment (which
    # overwrites the evidence) gives up on the check.
    client, marketpay = inferred_failure(make_client, repo)
    client.post("/reconcile")
    assert repo.verify[TERMINAL] == PAYMENT_ID


# --- Edge cases (found in the final review) ------------------------------------------------


def test_a_late_charge_that_cannot_be_recorded_stops_the_next_payment(make_client, repo):
    """The next payment's last-chance look finds the earlier payment's late charge, but the
    store won't take the correction. Sending now would overwrite the only evidence, so the
    next payment isn't sent (503, nothing charged) and the flag stays for a later look."""
    old_id, new_ref = payment_id_for("order-old"), "order-new"
    t = datetime(2026, 9, 27, 11, 0, tzinfo=UTC)
    repo.payments[old_id] = Payment(
        id=old_id,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference="order-old",
        state=PaymentState.FAILED,
        state_reason=StateReason.NEVER_RECORDED,
        resolved_via=ResolvedVia.LAST_TRANSACTION,
        created_at=t,
        updated_at=t,
    )
    repo.verify[TERMINAL] = old_id
    write = repo.update

    def old_payment_cannot_be_written(payment_id, change):
        if payment_id == old_id:
            raise StoreUnavailable("injected: this write keeps failing", transient=True)
        return write(payment_id, change)

    repo.update = old_payment_cannot_be_written
    marketpay = ScriptedMarketPay(
        process=[created(tx(ecr_id=new_ref, terminal_tx="21"))],
        lookups=[
            ok(
                {
                    "lastTransactionState": "FINISHED",
                    "transactionResult": tx("OK", ecr_id="order-old", terminal_tx="20"),
                }
            )
        ],
    )
    response = make_client(marketpay).post("/payments", json={**BODY, "reference": new_ref})

    assert (response.status_code, response.get_json()["code"]) == (503, "store_unavailable")
    assert "process" not in marketpay.calls
    assert repo.verify[TERMINAL] == old_id  # still flagged: a later look can correct it
    new = repo.payments[payment_id_for(new_ref)]
    assert (new.state, new.state_reason, new.operation) == (
        PaymentState.FAILED,
        StateReason.NOT_SENT,
        None,
    )
    assert TERMINAL not in repo.locks
