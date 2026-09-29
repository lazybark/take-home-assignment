"""POST /payments and GET /payments/{id} end to end: the request MarketPay gets, the
answer the POS gets, and why a 202 or a lost response is `unknown`, never `failed`."""

import json

import httpx

TERMINAL = "PAX:TEST_TERMINAL"
BODY = {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": "order-7f3a9c"}


def result_body(status: str = "OK", response_code: str = "000", ecr_id: str = "order-7f3a9c"):
    return {
        "status": status,
        "responseCode": response_code,
        "terminalTransactionId": "14",
        "authorizationCode": "213462" if status == "OK" else None,
        "finalTransactionParams": {
            "ecrTransactionId": ecr_id,
            "amount": "1299",
            "currency": "752",
            "transactionType": "PURCHASE",
        },
    }


class FakeMarketPay:
    """Records requests and answers /process-transaction with a canned response."""

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)

        return self.respond(request)


def test_happy_path_approves_and_sends_the_right_request(make_client):
    marketpay = FakeMarketPay(lambda r: httpx.Response(201, json=result_body()))
    response = make_client(marketpay).post("/payments", json={**BODY, "deadlineSeconds": 60})

    assert response.status_code == 201
    payment = response.get_json()
    assert payment["state"] == "approved"
    assert payment["providerTransactionId"] == "14"
    assert payment["reversed"] is False

    [request] = marketpay.requests
    assert request.url.path == f"/process-transaction/{TERMINAL}"
    assert request.url.params["waitTime"] == "150"  # held open past the 60s deadline
    assert json.loads(request.content) == {
        "ecrTransactionId": "order-7f3a9c",
        "amount": "1299",
        "currency": "752",
        "transactionType": "PURCHASE",
        "ecrParams": {"ecrId": "TEST_ECR_ID"},
    }


def test_repeated_reference_returns_existing_payment_without_charging_again(make_client):
    marketpay = FakeMarketPay(lambda r: httpx.Response(201, json=result_body()))
    client = make_client(marketpay)

    first = client.post("/payments", json=BODY)
    second = client.post("/payments", json=BODY)

    assert (first.status_code, second.status_code) == (201, 200)
    assert second.get_json()["id"] == first.get_json()["id"]
    assert len(marketpay.requests) == 1


def test_get_payment(make_client):
    client = make_client(FakeMarketPay(lambda r: httpx.Response(201, json=result_body())))
    created = client.post("/payments", json=BODY).get_json()

    fetched = client.get(f"/payments/{created['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json() == created


def test_get_unknown_payment_is_404(make_client):
    response = make_client(FakeMarketPay(lambda r: httpx.Response(500))).get(
        "/payments/00000000-0000-0000-0000-000000000000"
    )
    assert response.status_code == 404
    assert response.get_json()["code"] == "not_found"


def test_declined(make_client):
    marketpay = FakeMarketPay(lambda r: httpx.Response(201, json=result_body("NOK", "116")))
    payment = make_client(marketpay).post("/payments", json=BODY).get_json()
    assert payment["state"] == "declined"
    assert payment["declineReason"] == "116"


def test_202_is_unknown_not_failed(make_client):
    payment = make_client(FakeMarketPay(lambda r: httpx.Response(202))).post("/payments", json=BODY)
    assert payment.get_json()["state"] == "unknown"


def test_lost_response_is_unknown_not_failed(make_client):
    def lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    payment = make_client(FakeMarketPay(lost)).post("/payments", json=BODY)
    assert payment.get_json()["state"] == "unknown"


def test_validation_errors_are_400(make_client):
    client = make_client(FakeMarketPay(lambda r: httpx.Response(500)))

    for bad in (
        {**BODY, "currency": "USD"},
        {**BODY, "amount": 0},
        {**BODY, "reference": "x" * 37},
        {k: v for k, v in BODY.items() if k != "terminalId"},
    ):
        response = client.post("/payments", json=bad)
        assert response.status_code == 400, bad
        assert response.get_json()["code"] == "validation_error"

    assert client.post("/payments", data="not json").status_code == 400
