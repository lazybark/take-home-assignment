"""GET /terminals: MarketPay's terminal list through our client (store code, User-Agent,
filters, outages), with our lock state added by the route."""

import httpx
import pytest

from payments.domain.marketpay.gateway import MarketPayHTTPError, MarketPayUnavailable

TERMINALS = [
    {"terminalId": "PAX:TEST_TERMINAL", "wsCreatedTime": "2026-09-26T10:00:00Z", "connected": True},
    {"terminalId": "PAX:OTHER_TERMINAL", "connected": False},
]


def test_list_terminals_sends_store_code_and_user_agent(make_marketpay):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)

        return httpx.Response(200, json=TERMINALS)

    terminals = make_marketpay(handler).list_terminals()

    [request] = seen
    assert request.method == "GET"
    assert request.url.path == "/terminals"
    assert dict(request.url.params) == {"storeCode": "STORE-1"}
    assert request.headers["User-Agent"] == "PaymentsTest/1.0"
    assert [t.terminal_id for t in terminals] == ["PAX:TEST_TERMINAL", "PAX:OTHER_TERMINAL"]
    assert terminals[0].connected is True


def test_list_terminals_passes_connected_filter(make_marketpay):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["connected"] == "false"

        return httpx.Response(200, json=[])

    assert make_marketpay(handler).list_terminals(connected=False) == []


def test_connection_reset_is_unavailable(make_marketpay):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset by peer", request=request)

    with pytest.raises(MarketPayUnavailable, match="mTLS"):
        make_marketpay(handler).list_terminals()


def test_empty_400_hints_at_user_agent(make_marketpay):
    with pytest.raises(MarketPayHTTPError, match="User-Agent") as exc_info:
        make_marketpay(lambda request: httpx.Response(400)).list_terminals()

    assert exc_info.value.status_code == 400


def test_terminals_route_returns_items(make_client):
    response = make_client(lambda request: httpx.Response(200, json=TERMINALS)).get(
        "/terminals?connected=true"
    )
    assert response.status_code == 200
    body = response.get_json()
    assert body["items"][0] == {
        "terminalId": "PAX:TEST_TERMINAL",
        "wsCreatedTime": "2026-09-26T10:00:00Z",
        "connected": True,
        "locked": False,
        "lockedBy": None,
    }
    assert response.headers["X-Request-ID"]


def test_terminals_route_rejects_bad_filter(make_client):
    response = make_client(lambda request: httpx.Response(200, json=[])).get(
        "/terminals?connected=maybe"
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "validation_error"


def test_terminals_route_maps_provider_outage_to_502(make_client):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    response = make_client(handler).get("/terminals")
    assert response.status_code == 502
    assert response.get_json()["code"] == "provider_unavailable"
