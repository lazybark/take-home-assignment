"""Notifications: signed URLs, redaction, and reading a notification as the record
last-transaction would show (pure rules, including the real payloads captured on staging)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from support.marketpay_fakes import (
    GUIDE_OK,
    REF,
    TERMINAL,
    cancellation,
    completed,
    observed_notification,
    observed_timeout_notification,
    record,
    tx,
)

from payments.domain.cancel import UndoOutcome
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    ResolvedVia,
    StateReason,
    payment_id_for,
)
from payments.domain.notifications import apply_notified, notified_outcome
from payments.domain.outcomes import observe_last_transaction
from payments.infrastructure.marketpay.notification_urls import (
    NotificationUrls,
    mask_signature,
    redact_card_data,
)

SECRET = "s" * 32
BASE = "https://hooks.example.test"


# --- Signed URLs ----------------------------------------------------------------------


def test_url_is_signed_per_payment_and_operation():
    urls, payment_id = NotificationUrls(BASE, SECRET), uuid4()
    url = urls.for_operation(payment_id, "purchase")
    *_, signature = url.split("/")

    assert url.startswith(f"{BASE}/webhooks/marketpay/{payment_id}/purchase/")
    assert urls.verify(payment_id, "purchase", signature)
    assert not urls.verify(payment_id, "refund", signature)  # not reusable for another operation
    assert not urls.verify(uuid4(), "purchase", signature)  # nor for another payment
    assert not NotificationUrls(BASE, "x" * 32).verify(payment_id, "purchase", signature)


def test_signature_is_masked_in_logged_paths():
    url = NotificationUrls(BASE, SECRET).for_operation(uuid4(), "purchase")
    *_, signature = url.split("/")

    masked = mask_signature(f'"POST {url.removeprefix(BASE)} HTTP/1.1" 200')
    assert signature not in masked
    assert "/purchase/<signature> HTTP/1.1" in masked


def test_card_data_is_redacted():
    body = {"result": {"status": "OK", "customerReceipt": "4111 **** 1234", "cardData": {}}}
    assert redact_card_data(body) == {
        "result": {"status": "OK", "customerReceipt": "<redacted>", "cardData": "<redacted>"}
    }


# --- From notification to record ----------------------------------------------------------

PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
ME = Owner(instance_id="local-1", boot_id="boot-1")


def pending(**update) -> Payment:
    base = Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.UNKNOWN,
        operation=Operation.PURCHASE,
        deadline_at=T0,
        created_by="r1",
        created_at=T0,
        updated_at=T0,
    )
    return base.model_copy(update=update)


def test_final_notification_becomes_a_last_transaction_record():
    last = record(completed(GUIDE_OK))

    assert last.last_transaction_state == "FINISHED"
    assert last.transaction_result.final_transaction_params.ecr_transaction_id == REF
    assert last.transaction_result.terminal_transaction_id == "14"


def test_notification_as_staging_sends_it_becomes_a_record():
    """Numbers where last-transaction has strings, and a null envelope id."""

    last = record(observed_notification())

    result = last.transaction_result
    assert (result.status, result.response_code, result.terminal_transaction_id) == (
        "OK",
        "000",
        "40",
    )
    echoed = result.final_transaction_params
    assert (echoed.ecr_transaction_id, echoed.amount, echoed.currency) == (REF, "100", "752")


def test_notification_as_staging_sends_it_settles_like_polling():
    by_notification = notified_outcome(
        pending(), Operation.PURCHASE, record(observed_notification())
    )
    by_polling = observe_last_transaction(
        LastTransactionResult.model_validate(
            {"lastTransactionState": "FINISHED", "transactionResult": tx("OK", terminal_tx="40")}
        ),
        REF,
    ).resolution

    assert by_notification.state is PaymentState.APPROVED
    assert by_notification.provider_transaction_id == "40"
    ignore = {"resolved_via"}
    assert by_notification.model_dump(exclude=ignore) == by_polling.model_dump(exclude=ignore)


def test_timeout_notification_as_staging_sends_it_settles_like_polling():
    """Nobody tapped: NOK without a code is `failed` / terminal_stopped, by either path."""

    by_notification = notified_outcome(
        pending(), Operation.PURCHASE, record(observed_timeout_notification())
    )
    by_polling = observe_last_transaction(
        LastTransactionResult.model_validate(
            {"lastTransactionState": "FINISHED", "transactionResult": tx("NOK", terminal_tx="41")}
        ),
        REF,
    ).resolution

    assert (by_notification.state, by_notification.state_reason) == (
        PaymentState.FAILED,
        StateReason.TERMINAL_STOPPED,
    )
    assert by_notification.decline_reason is None  # no code: we don't invent one
    ignore = {"resolved_via"}
    assert by_notification.model_dump(exclude=ignore) == by_polling.model_dump(exclude=ignore)


def test_terminal_transaction_id_comes_from_the_notification_if_missing_in_result():
    result = {k: v for k, v in GUIDE_OK.items() if k != "terminalTransactionId"}
    assert record(
        completed(result, terminal_tx="21")
    ).transaction_result.terminal_transaction_id == ("21")


@pytest.mark.parametrize(
    "body",
    [
        {"status": "WAITING_FOR_CARD", "ecrTransactionId": REF},  # progress
        {"status": "BANK_AUTHORIZATION", "ecrTransactionId": REF, "result": GUIDE_OK},
        {"status": "COMPLETED", "ecrTransactionId": REF},  # no result
        completed(GUIDE_OK, ecr_id=None),  # nobody to attribute it to
        completed(tx(ecr_id="someone-else"), ecr_id=REF),  # result names another payment
        completed({"status": "MAYBE"}),  # unreadable
    ],
)
def test_no_record_without_a_clear_final_result(body):
    assert record(body) is None


def test_spec_cancellation_shape_is_read_as_a_cancellation():
    last = record(completed(cancellation("OK")))
    assert last.cancellation_result.status == "OK"
    assert last.transaction_result is None


@pytest.mark.parametrize(
    "result",
    [
        tx("OK"),
        tx("NOK"),  # staging: no responseCode
        tx("NOK", response_code="116"),
        tx("OK", response_code="116"),  # contradictory: status wins
        tx("PARTIAL"),
    ],
)
def test_same_record_same_outcome_whichever_way_it_arrives(result):
    by_polling = observe_last_transaction(
        LastTransactionResult.model_validate(
            {"lastTransactionState": "FINISHED", "transactionResult": result}
        ),
        REF,
    ).resolution
    by_notification = notified_outcome(pending(), Operation.PURCHASE, record(completed(result)))

    assert by_notification.resolved_via is ResolvedVia.NOTIFICATION
    ignore = {"resolved_via"}
    assert by_notification.model_dump(exclude=ignore) == by_polling.model_dump(exclude=ignore)


def test_notification_for_another_operation_says_nothing():
    last = record(completed(GUIDE_OK))
    assert notified_outcome(pending(), Operation.REFUND, last) is None
    assert notified_outcome(pending(operation=None), Operation.PURCHASE, last) is None


def test_reversal_is_recognised_by_its_new_transaction_id():
    reversing = pending(
        state=PaymentState.APPROVED,
        operation=Operation.REVERSAL,
        provider_transaction_id="14",
        undo_baseline_transaction_id="14",
        undo_started_at=T0,
    )
    own = notified_outcome(
        reversing,
        Operation.REVERSAL,
        record(completed(tx("OK", terminal_tx="15"), terminal_tx="15")),
    )
    purchase_again = notified_outcome(reversing, Operation.REVERSAL, record(completed(tx("OK"))))

    assert own.outcome is UndoOutcome.DONE and own.via is ResolvedVia.NOTIFICATION
    assert purchase_again is None  # the purchase's own record: not proof of a reversal


def test_refund_is_matched_on_its_own_reference():
    refunding = pending(
        state=PaymentState.APPROVED,
        operation=Operation.REFUND,
        refund_reference="rf01-order-7f3a9c",
        undo_started_at=T0,
    )
    refund = record(completed(tx("OK", ecr_id="rf01-order-7f3a9c"), ecr_id="rf01-order-7f3a9c"))

    assert notified_outcome(refunding, Operation.REFUND, refund).outcome is UndoOutcome.DONE
    assert notified_outcome(refunding, Operation.REFUND, record(completed(tx("OK")))) is None


def test_live_driver_is_never_written_behind():
    driven = pending(state=PaymentState.PENDING, owner=ME, lease_until=T0 + timedelta(minutes=1))
    assert apply_notified(driven, Operation.PURCHASE, record(completed(GUIDE_OK)), ME, T0) == driven


def test_orphan_is_settled_from_its_record():
    settled = apply_notified(pending(), Operation.PURCHASE, record(completed(GUIDE_OK)), ME, T0)

    assert settled.state is PaymentState.APPROVED
    assert settled.operation is None  # the terminal is free again
    assert settled.provider_transaction_id == "14"
    assert settled.resolved_via is ResolvedVia.NOTIFICATION
