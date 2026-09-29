"""Liveness (GET /healthz: the process is up) and readiness (GET /readyz: Firestore answers).
Neither asks MarketPay."""

import httpx


def _unused(request: httpx.Request) -> httpx.Response:
    raise AssertionError("MarketPay must not be called")


def test_healthz(make_client):
    response = make_client(_unused).get("/healthz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_readyz_ok_when_firestore_answers(make_client, db):
    response = make_client(_unused).get("/readyz")

    assert response.status_code == 200
    assert response.get_json() == {"status": "ok", "firestore": "ok"}

    db.collection.assert_called_once_with("_health")


def test_readyz_503_when_firestore_fails(make_client, db):
    db.collection.return_value.document.return_value.get.side_effect = RuntimeError("no creds")
    response = make_client(_unused).get("/readyz")

    assert response.status_code == 503
    assert response.get_json()["status"] == "unavailable"
