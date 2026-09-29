"""The notification webhook (the bonus). Signed URLs go out with every operation; what comes
back settles a payment exactly as the same record from last-transaction would — handed to
the request driving it, or recorded when nobody is. Never received by our service live:
these tests, with the real notifications captured on staging, prove that MarketPay's data is
used."""

import json
import logging
import threading
from uuid import uuid4

import httpx
import pytest
from support.marketpay_fakes import (
    GUIDE_OK,
    IN_PROGRESS,
    REF,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    cancellation,
    completed,
    created,
    finished,
    lost,
    observed_notification,
    observed_timeout_notification,
    ok,
    tx,
)

from payments.config import Settings
from payments.domain.models import Operation, PaymentState, ResolvedVia, StateReason, payment_id_for
from payments.infrastructure.marketpay.notification_urls import NotificationUrls

SECRET = "s" * 32
BASE = "https://hooks.example.test"
BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": "order-7f3a9c"}


def configured(settings: Settings, **update) -> Settings:
    """A validated copy (model_copy would skip validation)."""
    return Settings(**(settings.model_dump() | update))


@pytest.fixture
def settings(settings: Settings) -> Settings:
    return configured(settings, notification_base_url=BASE, notification_secret=SECRET)


def sent_url(request: httpx.Request) -> str | None:
    return json.loads(request.content)["ecrParams"].get("notificationUrl")


@pytest.mark.parametrize(
    "update",
    [
        {"notification_base_url": BASE},  # no secret
        {"notification_secret": SECRET},  # no URL
        {"notification_base_url": "http://plain.test", "notification_secret": SECRET},
        {"notification_base_url": BASE, "notification_secret": "short"},
    ],
)
def test_settings_refuse_half_or_weak_configuration(settings, update):
    values = settings.model_dump() | {"notification_base_url": None, "notification_secret": None}
    with pytest.raises(ValueError):
        Settings(**(values | update))


# --- Sent with every operation -----------------------------------------------------------


def test_purchase_sends_its_signed_url(make_client):
    marketpay = ScriptedMarketPay(process=[created(tx())])
    response = make_client(marketpay).post("/payments", json=BODY)

    payment_id = response.get_json()["id"]
    request = marketpay.requests["process"][0]
    assert json.loads(request.content)["ecrParams"]["ecrId"] == "TEST_ECR_ID"
    assert sent_url(request).startswith(f"{BASE}/webhooks/marketpay/{payment_id}/purchase/")


def test_reversal_sends_its_own_url(make_client):
    marketpay = ScriptedMarketPay(process=[created(tx())], cancels=[ok(cancellation())])
    client = make_client(marketpay)
    payment_id = client.post("/payments", json=BODY).get_json()["id"]

    assert client.post(f"/payments/{payment_id}/cancel").status_code == 200
    url = sent_url(marketpay.requests["cancel"][0])
    assert url.startswith(f"{BASE}/webhooks/marketpay/{payment_id}/{Operation.REVERSAL}/")


def test_off_by_default(make_client, settings, make_marketpay, db, repo, clock, owner):
    from payments.app import create_app

    plain = configured(settings, notification_base_url=None, notification_secret=None)
    marketpay = ScriptedMarketPay(process=[created(tx())])
    app = create_app(
        plain, marketpay=make_marketpay(marketpay), db=db, repo=repo, clock=clock, owner=owner
    )
    app.test_client().post("/payments", json=BODY)

    assert sent_url(marketpay.requests["process"][0]) is None


# --- Received --------------------------------------------------------------------------


@pytest.fixture
def webhook_log():
    """Our log events. (create_app replaces the root handlers, which caplog relies on, so
    this listens on the package's own logger.)"""
    records: list[dict] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if isinstance(record.msg, dict):
                records.append(record.msg)

    logger = logging.getLogger("payments")
    handler = Collect(level=logging.DEBUG)
    logger.addHandler(handler)

    yield records

    logger.removeHandler(handler)


def notification_path(payment_id, operation="purchase") -> str:
    return NotificationUrls(BASE, SECRET).for_operation(payment_id, operation).removeprefix(BASE)


def test_signed_notification_is_logged_and_answered_200(make_client, webhook_log):
    payment_id = uuid4()
    response = make_client(ScriptedMarketPay()).post(
        notification_path(payment_id),
        json={
            "status": "COMPLETED",
            "ecrTransactionId": "order-7f3a9c",
            "terminalTransactionId": "14",
            "result": {"status": "OK", "customerReceipt": "4111 **** 1234"},
        },
        headers={"Cf-Connecting-Ip": "203.0.113.7"},
    )

    assert response.status_code == 200
    [entry] = [e for e in webhook_log if e["event"] == "marketpay_notification"]
    assert entry["notification_status"] == "COMPLETED"
    assert entry["has_result"] is True
    assert entry["cf_connecting_ip"] == "203.0.113.7"
    assert entry["body"]["result"]["customerReceipt"] == "<redacted>"


@pytest.mark.parametrize("tamper", ["signature", "operation", "payment"])
def test_unsigned_notification_is_refused(make_client, tamper, webhook_log):
    payment_id = uuid4()
    path = notification_path(payment_id)

    if tamper == "signature":
        path = path[:-1] + ("0" if path[-1] != "0" else "1")
    elif tamper == "operation":
        path = path.replace("/purchase/", "/refund/")
    else:
        path = path.replace(str(payment_id), str(uuid4()))

    response = make_client(ScriptedMarketPay()).post(path, json={"status": "COMPLETED"})

    assert response.status_code == 404
    assert [e["event"] for e in webhook_log if "notification" in e["event"]] == [
        "marketpay_notification_rejected"
    ]


def test_unparseable_body_is_still_answered_200(make_client):
    response = make_client(ScriptedMarketPay()).post(
        notification_path(uuid4()), data="not json", content_type="text/plain"
    )
    assert response.status_code == 200


def test_access_log_masks_the_signature(make_client):
    make_client(ScriptedMarketPay())  # installs the filter
    path = notification_path(uuid4())
    *_, signature = path.split("/")
    record = logging.LogRecord(
        "werkzeug", logging.INFO, "", 0, '"%s" %s', (f"POST {path}", 200), None
    )

    for f in logging.getLogger("werkzeug").filters:
        f.filter(record)

    assert signature not in record.getMessage()


# --- From notification to record ----------------------------------------------------------

PAYMENT_ID = payment_id_for(REF)


def notify(client, body: dict, payment_id=PAYMENT_ID, operation: str = "purchase"):
    """POST a notification and wait until the webhook's worker has used it."""
    response = client.post(notification_path(payment_id, operation), json=body)
    worker = client.application.extensions["notification_worker"]
    worker.submit(lambda: None).result(timeout=5)  # one worker, in order: now it's done

    return response


def uses(log: list[dict]) -> list[str]:
    return [e["use"] for e in log if e["event"] == "notification_used"]


# --- End to end: through the webhook -------------------------------------------------------


def test_unknown_payment_settles_when_its_notification_arrives(make_client, repo, webhook_log):
    """Our deadline passed with nothing known (202, abort too late): `unknown`, terminal
    locked. The final notification settles it at once — no reconcile needed."""
    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx(ecr_id="order-next", terminal_tx="15"))],
        lookups=[ok(IN_PROGRESS)],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"

    assert notify(client, completed(GUIDE_OK)).status_code == 200

    payment = repo.get(PAYMENT_ID)
    assert (payment.state, payment.provider_transaction_id) == (PaymentState.APPROVED, "14")
    assert payment.resolved_via is ResolvedVia.NOTIFICATION
    assert uses(webhook_log) == ["settled"]

    # The terminal is free: the next order goes through.
    nxt = client.post("/payments", json={**BODY, "reference": "order-next"})
    assert nxt.get_json()["state"] == "approved"


def test_staging_notification_settles_an_unknown_payment(make_client, repo, webhook_log):
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"

    notify(client, observed_notification())

    payment = repo.get(PAYMENT_ID)
    assert (payment.state, payment.provider_transaction_id) == (PaymentState.APPROVED, "40")
    [logged] = [e for e in webhook_log if e["event"] == "marketpay_notification"]
    assert logged["body"]["result"]["cardData"] == "<redacted>"
    assert logged["body"]["result"]["customerReceipt"] == "<redacted>"


def test_staging_timeout_notification_fails_an_unknown_payment_and_frees_the_terminal(
    make_client, repo
):
    """The staging case: 202, the customer never taps, our deadline answers `unknown`; the
    terminal's timeout then arrives as a notification: a known `failed`, terminal free."""

    marketpay = ScriptedMarketPay(
        process=[accepted, created(tx(ecr_id="order-next", terminal_tx="42"))],
        lookups=[ok(IN_PROGRESS)],
        aborts=[TOO_LATE],
    )
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "unknown"

    notify(client, observed_timeout_notification())

    payment = repo.get(PAYMENT_ID)
    assert (payment.state, payment.state_reason) == (
        PaymentState.FAILED,
        StateReason.TERMINAL_STOPPED,
    )
    assert payment.operation is None

    nxt = client.post("/payments", json={**BODY, "reference": "order-next"})
    assert nxt.get_json()["state"] == "approved"


@pytest.mark.parametrize("answer", [accepted, lost], ids=["202", "reply_lost"])
def test_polling_request_is_settled_by_the_notification(make_client, webhook_log, answer):
    """A 202, or the 201 lost on its way back (the money moved, the reply didn't — staging sends
    a notification after a 201 too), while last-transaction keeps failing (500s): the
    notification ends the wait — no abort is sent, and the waiter gets `approved`."""

    polling, notified = threading.Event(), threading.Event()

    def failing_lookup(request):
        polling.set()
        notified.wait(5)

        return httpx.Response(500)

    marketpay = ScriptedMarketPay(process=[answer], lookups=[failing_lookup])
    client = make_client(marketpay)
    result: dict = {}
    waiter = threading.Thread(target=lambda: result.update(r=client.post("/payments", json=BODY)))
    waiter.start()
    assert polling.wait(5)

    notify(client.application.test_client(), completed(GUIDE_OK))
    notified.set()
    waiter.join(10)

    assert uses(webhook_log) == ["handed_to_driver"]
    body = result["r"].get_json()
    assert (body["state"], body["providerTransactionId"]) == ("approved", "14")
    assert "abort" not in marketpay.calls


def test_reversal_waiting_for_its_record_is_settled_by_the_notification(make_client, webhook_log):
    polling, notified = threading.Event(), threading.Event()

    def failing_lookup(request):
        polling.set()
        notified.wait(5)

        return httpx.Response(500)

    marketpay = ScriptedMarketPay(
        process=[created(tx())],
        lookups=[ok(finished("OK")), failing_lookup],  # baseline "14", then failing
        cancels=[accepted],
    )
    client = make_client(marketpay)
    client.post("/payments", json=BODY)
    answer: dict = {}
    waiter = threading.Thread(
        target=lambda: answer.update(r=client.post(f"/payments/{PAYMENT_ID}/cancel"))
    )
    waiter.start()
    assert polling.wait(5)

    reversal = completed(tx("OK", terminal_tx="15"), terminal_tx="15")
    notify(client.application.test_client(), reversal, operation="reversal")
    notified.set()
    waiter.join(10)

    assert uses(webhook_log) == ["handed_to_driver"]
    body = answer["r"].get_json()
    assert (body["state"], body["reversed"]) == ("cancelled", True)


def test_progress_notifications_change_nothing(make_client, repo, webhook_log):
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    for status in ("WAITING_FOR_CARD", "PIN_REQUIRED", "BANK_AUTHORIZATION"):
        notify(client, {"status": status, "ecrTransactionId": REF})

    assert repo.get(PAYMENT_ID).state is PaymentState.UNKNOWN
    assert uses(webhook_log) == ["progress"] * 3


def test_notification_for_a_settled_payment_changes_nothing(make_client, repo, webhook_log):
    """A declined payment and a notification saying "approved": the record isn't flipped
    (the waiter may already have re-run the card); an error asks a person to look."""
    client = make_client(ScriptedMarketPay(process=[created(tx("NOK", response_code="116"))]))
    assert client.post("/payments", json=BODY).get_json()["state"] == "declined"

    notify(client, completed(GUIDE_OK))

    assert repo.get(PAYMENT_ID).state is PaymentState.DECLINED
    assert uses(webhook_log) == ["already_settled"]
    [error] = [e for e in webhook_log if e["event"] == "notification_contradicts_record"]
    assert (error["recorded_state"], error["notified_state"]) == ("declined", "approved")


def test_notification_that_is_not_ours_changes_nothing(make_client, repo, webhook_log):
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    # Signed for our payment, but the record is another transaction's.
    notify(client, completed(tx(ecr_id="someone-else"), ecr_id="someone-else"))

    assert repo.get(PAYMENT_ID).state is PaymentState.UNKNOWN
    assert uses(webhook_log) == ["not_conclusive"]


def test_notification_for_an_unknown_payment_is_ignored(make_client, webhook_log):
    notify(make_client(ScriptedMarketPay()), completed(GUIDE_OK), payment_id=uuid4())
    assert uses(webhook_log) == ["unknown_payment"]


def test_contradictory_result_in_a_notification_is_logged(make_client, webhook_log):
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    client = make_client(marketpay)
    client.post("/payments", json=BODY)

    notify(client, completed(tx("OK", response_code="116")))

    [error] = [e for e in webhook_log if e["event"] == "marketpay_result_inconsistent"]
    assert error["source"] == "notification"


# --- A notification also follows a 201 -----------------------------------------------------


def contradictions(log: list[dict]) -> list[dict]:
    return [e for e in log if e["event"] == "notification_contradicts_record"]


def test_notification_after_a_201_is_a_quiet_cross_check(make_client, repo, webhook_log):
    client = make_client(ScriptedMarketPay(process=[created(tx(terminal_tx="40"))]))
    assert client.post("/payments", json=BODY).get_json()["state"] == "approved"

    notify(client, observed_notification())

    assert repo.get(PAYMENT_ID).resolved_via is ResolvedVia.PROCESS_RESPONSE  # untouched
    assert uses(webhook_log) == ["already_settled"]
    assert contradictions(webhook_log) == []


def test_notification_before_the_201_is_recorded_is_harmless(make_client, repo, webhook_log):
    """The notification overtakes the HTTP answer: it's parked for the request, which
    records the 201 as usual."""
    sent, notified = threading.Event(), threading.Event()

    def slow_201(request):
        sent.set()
        notified.wait(5)

        return httpx.Response(201, json=tx(terminal_tx="40"))

    client = make_client(ScriptedMarketPay(process=[slow_201]))
    result: dict = {}
    waiter = threading.Thread(target=lambda: result.update(r=client.post("/payments", json=BODY)))
    waiter.start()
    assert sent.wait(5)

    notify(client.application.test_client(), observed_notification())
    notified.set()
    waiter.join(10)

    assert uses(webhook_log) == ["handed_to_driver"]
    assert result["r"].get_json()["state"] == "approved"
    assert repo.get(PAYMENT_ID).resolved_via is ResolvedVia.PROCESS_RESPONSE


def test_partial_notification_after_its_reversal_is_no_alarm(make_client, repo, webhook_log):
    """A PARTIAL is reversed within the request and reported declined. Its own
    notification arriving afterwards agrees ("charged, then reversed"): no error."""
    marketpay = ScriptedMarketPay(
        process=[created(tx("PARTIAL", response_code="010"))],
        lookups=[ok(finished("PARTIAL"))],
        cancels=[ok(cancellation("OK"))],
    )
    client = make_client(marketpay)
    assert client.post("/payments", json=BODY).get_json()["state"] == "declined"

    notify(client, completed(tx("PARTIAL", response_code="010")))

    assert repo.get(PAYMENT_ID).state is PaymentState.DECLINED
    assert contradictions(webhook_log) == []
