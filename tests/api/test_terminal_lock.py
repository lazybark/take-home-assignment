"""One unresolved payment per terminal: MarketPay only remembers the *last* transaction."""

from support.marketpay_fakes import (
    IN_PROGRESS,
    TERMINAL,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    created,
    dns_failure,
    finished,
    ok,
    tx,
)

from payments.domain.models import StateReason, payment_id_for


def body(reference: str, terminal: str = TERMINAL) -> dict:
    return {"terminalId": terminal, "amount": 100, "currency": "SEK", "reference": reference}


# --- Through the API ------------------------------------------------------------------


def test_second_payment_on_a_busy_terminal_is_409_and_never_reaches_marketpay(make_client):
    # First payment stays unknown: 202, still in progress, abort "too late".
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])
    client = make_client(marketpay)
    assert client.post("/payments", json=body("order-1")).get_json()["state"] == "unknown"

    calls_before = marketpay.calls.count("process")
    response = client.post("/payments", json=body("order-2"))

    assert response.status_code == 409
    error = response.get_json()
    assert error["code"] == "terminal_busy"
    assert "order-1" in error["message"]
    assert marketpay.calls.count("process") == calls_before  # never touched the terminal


def test_a_confirmed_outcome_frees_the_terminal(make_client):
    marketpay = ScriptedMarketPay([created(tx(ecr_id="order-1")), created(tx(ecr_id="order-2"))])
    client = make_client(marketpay)
    assert client.post("/payments", json=body("order-1")).status_code == 201
    assert client.post("/payments", json=body("order-2")).status_code == 201


def test_other_terminals_are_not_affected(make_client):
    marketpay = ScriptedMarketPay(
        [accepted, created(tx(ecr_id="order-2"))], [ok(IN_PROGRESS)], [TOO_LATE]
    )
    client = make_client(marketpay)
    client.post("/payments", json=body("order-1"))
    assert client.post("/payments", json=body("order-2", "PAX:OTHER_TERMINAL")).status_code == 201


def test_unknown_blocker_is_rechecked_and_the_new_payment_goes_ahead(make_client):
    # order-1 ends unknown; later the terminal reports it finished (Cancel pressed).
    marketpay = ScriptedMarketPay(
        [accepted, created(tx(ecr_id="order-2"))],
        [ok(IN_PROGRESS)],
        [TOO_LATE],
    )
    client = make_client(marketpay)
    first = client.post("/payments", json=body("order-1")).get_json()
    marketpay.scripts["last"] = [ok(finished("NOK", ecr_id="order-1"))]

    second = client.post("/payments", json=body("order-2"))

    assert second.status_code == 201
    assert second.get_json()["state"] == "approved"
    assert client.get(f"/payments/{first['id']}").get_json()["state"] == "failed"


def test_reconcile_settles_an_unknown_payment_and_frees_the_terminal(make_client, repo):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])
    client = make_client(marketpay)
    first = client.post("/payments", json=body("order-1")).get_json()
    assert TERMINAL in repo.locks

    marketpay.scripts["last"] = [ok(finished("OK", ecr_id="order-1"))]
    client.post("/reconcile")
    fetched = client.get(f"/payments/{first['id']}").get_json()

    assert fetched["state"] == "approved"
    assert TERMINAL not in repo.locks
    stored = repo.payments[payment_id_for("order-1")]
    assert stored.resolved_via == "last_transaction"


def test_unknown_stays_locked_when_last_transaction_is_someone_elses(make_client, repo):
    marketpay = ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])
    client = make_client(marketpay)
    first = client.post("/payments", json=body("order-1")).get_json()

    marketpay.scripts["last"] = [ok(finished("OK", ecr_id="order-previous"))]
    assert client.get(f"/payments/{first['id']}").get_json()["state"] == "unknown"
    assert client.post("/payments", json=body("order-2")).status_code == 409


def test_never_sent_is_failed_and_frees_the_terminal(make_client, repo):
    # This morning's DNS failure: the request never left, MarketPay never saw it.
    client = make_client(ScriptedMarketPay([dns_failure], [ok(IN_PROGRESS)]))
    payment_json = client.post("/payments", json=body("order-1")).get_json()

    assert payment_json["state"] == "failed"
    assert TERMINAL not in repo.locks
    stored = repo.payments[payment_id_for("order-1")]
    assert stored.state_reason is StateReason.NOT_SENT


def test_terminal_list_marks_the_lock(make_client):
    sessions = [
        {"terminalId": TERMINAL, "connected": True},
        {"terminalId": "PAX:2", "connected": True},
    ]

    def handler(request):
        if request.url.path == "/terminals":
            return ok(sessions)
        return ScriptedMarketPay([accepted], [ok(IN_PROGRESS)], [TOO_LATE])(request)

    client = make_client(handler)
    locked = client.post("/payments", json=body("order-1")).get_json()
    items = {i["terminalId"]: i for i in client.get("/terminals").get_json()["items"]}

    assert items[TERMINAL]["locked"] is True
    assert items[TERMINAL]["lockedBy"]["reference"] == "order-1"
    assert items[TERMINAL]["lockedBy"]["paymentId"] == locked["id"]
    assert items["PAX:2"] == {
        "terminalId": "PAX:2",
        "connected": True,
        "wsCreatedTime": None,
        "locked": False,
        "lockedBy": None,
    }


def test_invalid_terminal_id_is_400(make_client):
    client = make_client(ScriptedMarketPay())
    for bad in ("pax:test_terminal", "PAX/185", "TEST_TERMINAL", "PAX:12 34"):
        assert client.post("/payments", json=body("x", bad)).status_code == 400, bad
