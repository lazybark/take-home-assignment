"""GET /payments/{id}/history: the timeline of a payment, exact even when writes are retried."""

import httpx
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
    ok,
    tx,
)

from payments.domain.models import payment_id_for

BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": REF}
PAYMENT_ID = payment_id_for(REF)


def history(client) -> list[dict]:
    response = client.get(f"/payments/{PAYMENT_ID}/history")

    assert response.status_code == 200
    return response.get_json()["items"]


def summaries(client) -> list[str]:
    return [item["summary"] for item in history(client)]


def test_a_normal_payment_has_two_lines(make_client):
    client = make_client(ScriptedMarketPay(process=[created(tx())]))
    client.post("/payments", json=BODY)

    items = history(client)

    assert [(i["number"], i["state"], i["operation"]) for i in items] == [
        (1, "pending", "purchase"),
        (2, "approved", None),
    ]

    assert (
        items[1]["summary"]
        == "approved (bank approved), from the payment's response; terminal released"
    )

    assert items[1]["basedOn"] == "process_response"


def test_the_timeline_of_an_unknown_payment_settled_by_reconcile(make_client, clock):
    client = make_client(
        ScriptedMarketPay(
            process=[accepted],
            lookups=[ok(IN_PROGRESS)],
            aborts=[TOO_LATE],
        )
    )
    client.post("/payments", json=BODY)
    settled = make_client(ScriptedMarketPay(lookups=[ok(finished("OK"))]), boot_id="boot-2")
    clock.elapsed += 200
    settled.post("/reconcile")

    assert summaries(client) == [
        "pending: payment started; terminal held for the purchase",
        "unknown (awaiting result), after the service's abort",
        "approved (bank approved), found in last-transaction; terminal released",
    ]


def test_a_cancel_shows_the_reversal(make_client):
    client = make_client(ScriptedMarketPay(process=[created(tx())], cancels=[ok(cancellation())]))
    client.post("/payments", json=BODY)
    client.post(f"/payments/{PAYMENT_ID}/cancel")

    assert [(i["state"], i["operation"]) for i in history(client)] == [
        ("pending", "purchase"),
        ("approved", None),
        ("approved", "reversal"),  # the reversal claimed the terminal
        ("cancelled", None),
    ]


def test_a_lost_commit_on_create_adds_no_second_line(make_client, repo):
    repo.fail_after_commit(1)  # stored, but the store call "failed": the retry rewrites it
    client = make_client(ScriptedMarketPay(process=[created(tx())]))
    client.post("/payments", json=BODY)

    assert [i["number"] for i in history(client)] == [1, 2]


def test_a_lost_commit_on_the_outcome_adds_no_second_line(make_client, repo):
    def approve_then_lose_the_commit(request):
        repo.fail_after_commit(1)

        return httpx.Response(201, json=tx())

    client = make_client(ScriptedMarketPay(process=[approve_then_lose_the_commit]))
    assert client.post("/payments", json=BODY).get_json()["state"] == "approved"

    assert [(i["number"], i["state"]) for i in history(client)] == [(1, "pending"), (2, "approved")]


def test_unknown_payment_is_404(make_client):
    response = make_client(ScriptedMarketPay()).get(f"/payments/{PAYMENT_ID}/history")
    assert response.status_code == 404
