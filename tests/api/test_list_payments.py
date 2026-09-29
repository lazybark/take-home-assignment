"""GET /payments: filters, newest-first order, cursor paging."""

from datetime import UTC, datetime, timedelta

import pytest
from support.marketpay_fakes import TERMINAL, ScriptedMarketPay

from payments.domain.listing import PaymentQuery, decode_cursor, encode_cursor, scan_page
from payments.domain.models import Payment, PaymentState, payment_id_for

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def seed(repo, reference, minute, state=PaymentState.APPROVED, terminal=TERMINAL):
    p = Payment(
        id=payment_id_for(reference),
        terminal_id=terminal,
        amount=100,
        currency="SEK",
        reference=reference,
        state=state,
        created_at=T0 + timedelta(minutes=minute),
        updated_at=T0 + timedelta(minutes=minute),
    )
    repo.payments[p.id] = p

    return p


@pytest.fixture
def client(make_client, repo):
    seed(repo, "a", 0, PaymentState.APPROVED)
    seed(repo, "b", 1, PaymentState.DECLINED)
    seed(repo, "c", 2, PaymentState.FAILED, terminal="PAX:OTHER_TERMINAL")
    seed(repo, "d", 3, PaymentState.UNKNOWN)
    seed(repo, "e", 3, PaymentState.APPROVED)  # same instant as d: the id breaks the tie
    marketpay = ScriptedMarketPay()
    c = make_client(marketpay)
    c.marketpay = marketpay

    return c


def refs(response):
    return [p["reference"] for p in response.get_json()["items"]]


def test_newest_first_with_a_stable_tie_break(client):
    response = client.get("/payments")
    assert response.status_code == 200

    got = refs(response)
    assert got[2:] == ["c", "b", "a"]
    assert sorted(got[:2]) == ["d", "e"]  # same createdAt: ordered by id, deterministically
    assert response.get_json()["nextCursor"] is None


def test_it_returns_stored_state_without_asking_marketpay(client):
    client.get("/payments?state=unknown")
    assert client.marketpay.calls == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("state=approved", {"a", "e"}),
        ("state=approved&state=declined", {"a", "b", "e"}),
        ("state=approved,unknown", {"a", "d", "e"}),
        (f"terminalId={TERMINAL}", {"a", "b", "d", "e"}),
        ("reference=c", {"c"}),
        ("reference=c&state=approved", set()),
        ("createdAfter=2026-09-27T12:01:00Z", {"c", "d", "e"}),  # exclusive
        ("createdBefore=2026-09-27T12:01:00Z", {"a"}),  # exclusive
        ("createdAfter=2026-09-27T12:00:00&createdBefore=2026-09-27T12:03:00", {"b", "c"}),
    ],
)
def test_filters(client, query, expected):
    assert set(refs(client.get(f"/payments?{query}"))) == expected


def test_paging_visits_every_payment_once(client):
    seen, cursor, pages = [], None, 0

    while True:
        url = "/payments?limit=2" + (f"&cursor={cursor}" if cursor else "")
        body = client.get(url).get_json()
        seen += [p["reference"] for p in body["items"]]
        pages += 1
        cursor = body["nextCursor"]
        if cursor is None:
            break

    assert sorted(seen) == ["a", "b", "c", "d", "e"]
    assert len(seen) == 5 and pages == 3  # 2 + 2 + 1, and no empty trailing page


def test_a_full_last_page_has_no_next_cursor(client):
    body = client.get("/payments?limit=5").get_json()
    assert (len(body["items"]), body["nextCursor"]) == (5, None)


def test_paging_with_a_filter(client):
    first = client.get("/payments?state=approved&limit=1").get_json()
    second = client.get(f"/payments?state=approved&limit=1&cursor={first['nextCursor']}").get_json()
    assert [p["reference"] for p in first["items"] + second["items"]] == ["e", "a"]
    assert second["nextCursor"] is None


@pytest.mark.parametrize(
    "query",
    ["limit=0", "limit=501", "limit=abc", "state=bogus", "createdAfter=yesterday", "cursor=%%%"],
)
def test_bad_queries_are_400(client, query):
    response = client.get(f"/payments?{query}")
    assert (response.status_code, response.get_json()["code"]) == (400, "validation_error")


def test_a_tampered_cursor_is_400(client):
    assert client.get("/payments?cursor=eyJmb28iOjF9").status_code == 400  # {"foo":1}


def test_a_short_scan_still_pages_on(repo):
    # A sparse filter over a long history: each call stops after scan_limit documents,
    # returns what it found (maybe nothing) and a cursor that resumes — nothing is lost.
    for i in range(10):
        seed(repo, f"p{i}", i, PaymentState.UNKNOWN if i in (1, 8) else PaymentState.APPROVED)

    query = PaymentQuery(states=frozenset({PaymentState.UNKNOWN}), limit=5)
    pages, cursor = [], None

    while True:
        page = repo.list_payments(query.model_copy(update={"after": cursor}), scan_limit=4)
        pages.append([p.reference for p in page.items])
        if (cursor := page.next_cursor) is None:
            break

    # p9..p6 -> [p8]; p5..p2 -> [] (short page, but more to come); p1..p0 -> [p1].
    assert pages == [["p8"], [], ["p1"]]


def test_scan_page_returns_partial_page_with_resume_cursor():
    payments = [
        Payment(
            id=payment_id_for(f"s{i}"),
            terminal_id=TERMINAL,
            amount=1,
            currency="SEK",
            reference=f"s{i}",
            state=PaymentState.UNKNOWN if i == 7 else PaymentState.APPROVED,
            created_at=T0 + timedelta(minutes=i),
            updated_at=T0,
        )
        for i in range(9, -1, -1)  # already newest first
    ]
    query = PaymentQuery(states=frozenset({PaymentState.UNKNOWN}), limit=5)
    page = scan_page(iter(payments), query, scan_limit=2)  # looks at s9, s8 only

    assert page.items == []
    assert page.next_cursor.id == payment_id_for("s8")  # resume after the last one scanned


def test_cursor_round_trip():
    from payments.domain.listing import PageCursor

    cursor = PageCursor(created_at=T0, id=payment_id_for("x"))
    assert decode_cursor(encode_cursor(cursor)) == cursor
