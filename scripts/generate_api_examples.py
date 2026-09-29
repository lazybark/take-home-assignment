"""Regenerate docs/examples/service-api.md from the real service code.

Every example runs the actual Flask app (handlers, service, store rules) against a scripted
MarketPay and an in-memory store, with a fixed clock, and records the exact request and
response. Nothing in the output is hand-written, so it can't drift from the code: run this
again after changing the API.

    docker compose run --rm --no-deps api uv run python scripts/generate_api_examples.py
"""

import contextlib
import json
import sys
import threading
from concurrent.futures import wait
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from support.marketpay_fakes import (  # noqa: E402
    IN_PROGRESS,
    TOO_LATE,
    ScriptedMarketPay,
    accepted,
    cancellation,
    created,
    finished,
    ok,
    tx,
)

from payments.app import create_app  # noqa: E402
from payments.config import Settings  # noqa: E402
from payments.domain.models import Owner, payment_id_for  # noqa: E402
from payments.infrastructure.marketpay.client import MarketPayClient  # noqa: E402
from payments.infrastructure.store.memory import InMemoryPaymentRepository  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "examples" / "service-api.md"
TERMINAL = "PAX:TEST_TERMINAL"
SETTINGS = Settings(
    marketpay_base_url="https://marketpay.example.test",
    marketpay_store_code="STORE",
    marketpay_ecr_id="TEST_ECR_ID",
    marketpay_client_cert="unused.crt",
    marketpay_client_key="unused.key",
    google_cloud_project="example",
    log_level="CRITICAL",
    log_format="console",
)


class Clock:
    """Fixed start (2026-09-27 12:00 UTC); time moves only when the code sleeps."""

    def __init__(self) -> None:
        self.elapsed = 0.0

    def now(self) -> datetime:
        return datetime(2026, 9, 27, 12, 0, tzinfo=UTC) + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.elapsed += seconds

    def wait_for(self, future, timeout: float) -> bool:
        wait([future], timeout=1.0)
        if not future.done():
            self.elapsed += max(0.0, timeout)
        return future.done()


def service(handler, repo=None, clock=None, boot="boot-1", db=None):
    repo = repo if repo is not None else InMemoryPaymentRepository()
    clock = clock or Clock()
    app = create_app(
        SETTINGS,
        marketpay=MarketPayClient.from_settings(SETTINGS, transport=httpx.MockTransport(handler)),
        db=db or MagicMock(),
        repo=repo,
        clock=clock,
        owner=Owner(instance_id="local-1", boot_id=boot),
    )
    return app.test_client(), repo, clock


def body(reference, **extra):
    return {
        "terminalId": TERMINAL,
        "amount": 1299,
        "currency": "SEK",
        "reference": reference,
        **extra,
    }


def tx_for(ref, status="OK", **kw):
    return tx(status, ecr_id=ref, **kw)


class WaitingTerminal:
    """Staging behaviour: a payment waits for a card until an abort arrives while its
    request is open; the open request then returns the terminal's result."""

    def __init__(self, after_abort, abort_status=204):
        self.after_abort, self.abort_status = after_abort, abort_status
        self.aborted = threading.Event()

    def process(self, request):
        self.aborted.wait(5)
        return self.after_abort()

    def abort(self, request):
        self.aborted.set()
        return httpx.Response(self.abort_status)


# --- recording ---------------------------------------------------------------------------

SECTIONS: list[tuple[str, str, list[dict]]] = []


def section(title, intro=""):
    SECTIONS.append((title, intro, []))


def record(client, title, method, path, *, json_body=None, note="", marketpay=""):
    kwargs = {"json": json_body} if json_body is not None else {}
    response = client.open(path, method=method, **kwargs)
    SECTIONS[-1][2].append(
        {
            "title": title,
            "note": note,
            "marketpay": marketpay,
            "method": method,
            "path": path,
            "request": json_body,
            "status": response.status_code,
            "response": response.get_json(silent=True),
        }
    )
    return response


# --- scenarios -----------------------------------------------------------------------------


def take_a_payment():
    section(
        "POST /payments — take a payment",
        "Synchronous: the response is the payment in its resolved state. See flows.md §1.",
    )

    c, _, _ = service(ScriptedMarketPay(process=[created(tx_for("order-1001"))]))
    record(
        c,
        "Approved (the happy path)",
        "POST",
        "/payments",
        json_body=body("order-1001"),
        marketpay="process-transaction → 201, status OK, responseCode 000",
    )

    c, _, _ = service(
        ScriptedMarketPay(process=[created(tx_for("order-1002", "NOK", response_code="116"))])
    )
    record(
        c,
        "Declined by the bank",
        "POST",
        "/payments",
        json_body=body("order-1002"),
        marketpay="201, status NOK, responseCode 116 (kept as declineReason)",
    )

    c, _, _ = service(ScriptedMarketPay(process=[created(tx_for("order-1003", "NOK"))]))
    record(
        c,
        "Failed: stopped on the terminal (Cancel pressed)",
        "POST",
        "/payments",
        json_body=body("order-1003"),
        note=(
            "NOK without an acquirer code means it never reached the bank: `failed`, not"
            " `declined`."
        ),
        marketpay="201, status NOK, no responseCode",
    )

    terminal = WaitingTerminal(lambda: httpx.Response(201, json=tx_for("order-1004", "NOK")))
    c, _, _ = service(ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort]))
    record(
        c,
        "Failed: nobody tapped, aborted at the deadline",
        "POST",
        "/payments",
        json_body=body("order-1004", deadlineSeconds=20),
        note=(
            "The abort is sent at 10 s while MarketPay still holds the request open; "
            "the open request returns the NOK."
        ),
        marketpay=(
            "process-transaction held open → abort-transaction 204 → the open request returns"
            " 201 NOK"
        ),
    )

    bank = threading.Event()
    terminal = WaitingTerminal(
        lambda: (bank.wait(5), httpx.Response(201, json=tx_for("order-1005")))[1], abort_status=409
    )
    c, _, _ = service(ScriptedMarketPay(process=[terminal.process], aborts=[terminal.abort]))
    record(
        c,
        "Unknown: the bank hadn't answered by the deadline (rare)",
        "POST",
        "/payments",
        json_body=body("order-1005", deadlineSeconds=20),
        note=(
            "Honest `unknown` instead of a guess. The terminal stays locked; the late answer "
            "is recorded when it arrives."
        ),
        marketpay="held open → abort 409 (too late: the customer just tapped) → no answer by 19 s",
    )
    bank.set()

    c, _, _ = service(
        ScriptedMarketPay(
            process=[created(tx_for("order-1006", "PARTIAL", response_code="010"))],
            lookups=[ok(finished("PARTIAL", ecr_id="order-1006"))],
            cancels=[ok(cancellation("OK", ecr_id="order-1006"))],
        )
    )
    record(
        c,
        "Declined: a PARTIAL approval, reversed at once",
        "POST",
        "/payments",
        json_body=body("order-1006"),
        note=(
            "A partial approval is never kept, never refunded (the approved amount is unknown); "
            "reversed by terminalTransactionId."
        ),
        marketpay="201 PARTIAL → last-transaction (baseline) → cancel-transaction 200 OK",
    )

    c, _, _ = service(ScriptedMarketPay(process=[lambda r: httpx.Response(404)], lookups=[]))
    record(
        c,
        "Failed: refused by MarketPay (terminal offline, wrong currency)",
        "POST",
        "/payments",
        json_body=body("order-1007"),
        marketpay="process-transaction → 404 (empty body) → one confirming last-transaction look",
    )

    c, _, _ = service(ScriptedMarketPay(process=[created(tx_for("order-1008"))]))
    c.post("/payments", json=body("order-1008"))
    record(
        c,
        "The same order again: returns the existing payment",
        "POST",
        "/payments",
        json_body=body("order-1008"),
        note="`200` instead of `201`. MarketPay is not called again.",
    )
    record(
        c,
        "The same reference, a different order",
        "POST",
        "/payments",
        json_body=body("order-1008", amount=500),
    )

    c, _, _ = service(
        ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    )
    c.post("/payments", json=body("order-1009"))
    record(
        c,
        "The terminal is busy",
        "POST",
        "/payments",
        json_body=body("order-1010"),
        note=(
            "order-1009 is still unresolved and holds the terminal; order-1010 is refused "
            "before anything is sent."
        ),
    )

    c, _, _ = service(ScriptedMarketPay())
    record(
        c,
        "Validation error",
        "POST",
        "/payments",
        json_body={
            "terminalId": "pax:test_terminal",
            "amount": 0,
            "currency": "USD",
            "reference": "x" * 40,
        },
    )

    repo = InMemoryPaymentRepository()
    repo.fail(1000)
    c, _, _ = service(ScriptedMarketPay(), repo=repo)
    record(
        c,
        "Firestore unavailable before anything was sent",
        "POST",
        "/payments",
        json_body=body("order-1011"),
        note="Retried for 5 s first. Nothing reached MarketPay, so repeating is safe.",
    )


def read_payments():
    section(
        "GET /payments/{id} and GET /payments — reads",
        "Stored state only; never calls MarketPay.",
    )
    marketpay = ScriptedMarketPay(
        process=[
            created(tx_for("order-2001")),
            created(tx_for("order-2002", "NOK", response_code="05")),
            created(tx_for("order-2003")),
        ]
    )
    c, _, clock = service(marketpay)
    for ref in ("order-2001", "order-2002", "order-2003"):
        c.post("/payments", json=body(ref))
        clock.elapsed += 60
    record(c, "One payment", "GET", f"/payments/{payment_id_for('order-2001')}")
    record(
        c,
        "One payment's history",
        "GET",
        f"/payments/{payment_id_for('order-2001')}/history",
        note="Beyond the contract: one line per change of state, reason or terminal hold.",
    )
    record(c, "Unknown id", "GET", "/payments/00000000-0000-0000-0000-000000000000")
    first = record(c, "List, first page", "GET", "/payments?limit=2", note="Newest first.")
    cursor = first.get_json()["nextCursor"]
    record(c, "List, next page (pass nextCursor back)", "GET", f"/payments?limit=2&cursor={cursor}")
    record(
        c,
        "List with filters",
        "GET",
        "/payments?state=approved,declined&terminalId=PAX:TEST_TERMINAL"
        "&createdAfter=2026-09-27T12:00:30Z",
    )
    record(c, "A cursor the service didn't issue", "GET", "/payments?cursor=garbage")


def cancel_payments():
    section("POST /payments/{id}/cancel — cancel / reverse", "See flows.md §2.")

    marketpay = ScriptedMarketPay(
        process=[created(tx_for("order-3001"))],
        lookups=[ok(finished("OK", ecr_id="order-3001"))],
        cancels=[ok(cancellation("OK", ecr_id="order-3001"))],
    )
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3001"))
    pid = payment_id_for("order-3001")
    record(
        c,
        "Reverse an approved payment",
        "POST",
        f"/payments/{pid}/cancel",
        note="The terminal asks the customer to tap again (a reversal is card-present on staging).",
        marketpay="last-transaction (baseline) → cancel-transaction 200, status OK",
    )
    record(c, "Cancel again: already cancelled", "POST", f"/payments/{pid}/cancel")

    # Still waiting on the terminal when the cancel arrives: the service's abort stops it.
    marketpay = ScriptedMarketPay(
        process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE, httpx.Response(204)]
    )
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3002"))  # left unknown: nobody drives it now
    marketpay.scripts["last"] = [
        ok(IN_PROGRESS),  # the cancel's first look: nothing finished yet
        ok(finished("NOK", ecr_id="order-3002")),  # after the abort: stopped
    ]
    record(
        c,
        "Cancel a payment still waiting on the terminal",
        "POST",
        f"/payments/{payment_id_for('order-3002')}/cancel",
        marketpay="abort-transaction 204 → last-transaction shows it stopped (NOK)",
    )

    # The terminal had already given up on its own before the cancel arrived.
    marketpay = ScriptedMarketPay(process=[accepted], lookups=[ok(IN_PROGRESS)], aborts=[TOO_LATE])
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3006"))
    marketpay.scripts["last"] = [ok(finished("NOK", ecr_id="order-3006"))]
    record(
        c,
        "Cancel arrives after the terminal already gave up",
        "POST",
        f"/payments/{payment_id_for('order-3006')}/cancel",
        note=(
            "The cancel first takes one look; it shows the payment ended NOK (no card in "
            "time), so it is settled as `failed` — nothing was charged, nothing to cancel."
        ),
        marketpay="last-transaction shows it NOK",
    )

    marketpay = ScriptedMarketPay(
        process=[created(tx_for("order-3003"))],
        lookups=[ok(finished("OK", ecr_id="order-3003"))],
        cancels=[ok(cancellation("NOK", ecr_id="order-3003"))],
    )
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3003"))
    record(
        c,
        "The reversal didn't take effect (e.g. nobody tapped): still approved",
        "POST",
        f"/payments/{payment_id_for('order-3003')}/cancel",
        marketpay="cancel-transaction 200, status NOK",
    )

    marketpay = ScriptedMarketPay(
        process=[created(tx_for("order-3004", "NOK", response_code="116"))]
    )
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3004"))
    record(
        c,
        "Nothing to cancel (declined)",
        "POST",
        f"/payments/{payment_id_for('order-3004')}/cancel",
    )

    marketpay = ScriptedMarketPay(
        process=[created(tx_for("order-3005"))],
        lookups=[ok(finished("OK", ecr_id="order-3005"))],
        cancels=[ok(cancellation("PARTIAL", ecr_id="order-3005"))],
    )
    c, _, _ = service(marketpay)
    c.post("/payments", json=body("order-3005"))
    record(
        c,
        "A partial reversal: needs a person",
        "POST",
        f"/payments/{payment_id_for('order-3005')}/cancel",
        marketpay="cancel-transaction 200, status PARTIAL",
    )
    record(c, "Unknown id", "POST", "/payments/00000000-0000-0000-0000-000000000000/cancel")


def reconcile():
    section("POST /reconcile — recover open payments", "See flows.md §3.")

    class Crash(BaseException):
        pass

    def crash(request):
        raise Crash

    repo = InMemoryPaymentRepository()
    c, _, clock = service(ScriptedMarketPay(process=[crash]), repo=repo)
    with contextlib.suppress(Crash):
        c.post("/payments", json=body("order-4001"))
    restarted, _, _ = service(
        ScriptedMarketPay(lookups=[ok(finished("OK", ecr_id="order-4001"))]),
        repo=repo,
        clock=clock,
        boot="boot-2",
    )
    record(
        restarted,
        "After a crash: the payment is settled from last-transaction",
        "POST",
        "/reconcile",
        note=(
            "The process died after MarketPay approved but before it was recorded; the "
            "restarted process (new boot id) takes it over."
        ),
        marketpay="last-transaction shows order-4001 approved",
    )
    record(restarted, "Again: nothing left to do (idempotent)", "POST", "/reconcile")
    record(
        restarted,
        "Limited to one terminal",
        "POST",
        "/reconcile",
        json_body={"terminalId": TERMINAL},
    )
    record(restarted, "Invalid body", "POST", "/reconcile", json_body={"olderThan": "yesterday"})


def diagnostics():
    section("Diagnostics (beyond the contract)", "See api-extensions.md.")
    terminals = [
        {"terminalId": TERMINAL, "connected": True, "wsCreatedTime": "2026-09-28T01:29:30.047915Z"}
    ]

    def handler(request):
        if request.url.path == "/terminals":
            return ok(terminals)
        if request.url.path.startswith("/last-transaction/"):
            return ok(finished("OK", ecr_id="order-1001"))
        return httpx.Response(500)

    c, _, _ = service(handler)
    record(c, "Liveness", "GET", "/healthz")
    record(c, "Readiness (Firestore reachable)", "GET", "/readyz")
    record(c, "Terminals, with the service's lock state", "GET", "/terminals")
    record(
        c,
        "The terminal's last transaction, as parsed",
        "GET",
        f"/terminals/{TERMINAL}/last-transaction",
    )

    down = MagicMock()
    down.collection.return_value.document.return_value.get.side_effect = RuntimeError(
        "no route to Firestore"
    )
    c, _, _ = service(lambda r: httpx.Response(500), db=down)
    record(c, "Readiness (Firestore unreachable)", "GET", "/readyz")

    def unreachable(request):
        raise httpx.ConnectError("connection reset by peer", request=request)

    c, _, _ = service(unreachable)
    record(c, "MarketPay unreachable", "GET", "/terminals")


# --- output --------------------------------------------------------------------------------


def render() -> str:
    lines = [
        "# The service's API: request and response examples",
        "",
        "Every example below was produced by **running the real service code** "
        "(`scripts/generate_api_examples.py`): the Flask handlers, the service and the store "
        "rules, "
        "against a scripted MarketPay and an in-memory store, with the clock fixed at "
        "2026-09-27 12:00 UTC. Nothing is hand-written, but potentially sensitive data is "
        "replaced by placeholders. Ids are deterministic examples (UUIDv5 of the reference); "
        "timestamps are the fixed clock. Regenerate after changing the API:",
        "",
        "```bash",
        "docker compose run --rm --no-deps api uv run python scripts/generate_api_examples.py",
        "```",
        "",
        "Contract: [`payment-api.yaml`](../payment-api.yaml). "
        "Beyond it: [api-extensions.md](../api-extensions.md). What MarketPay really sends: "
        "[marketpay-api.md](marketpay-api.md).",
        "",
    ]
    for title, _, examples in SECTIONS:
        anchor = title.split(" — ")[0]
        lines.append(f"- [{anchor}](#{_slug(title)}) ({len(examples)} examples)")
    lines.append("")
    for title, intro, examples in SECTIONS:
        lines += ["---", "", f"## {title}", ""]
        if intro:
            lines += [intro, ""]
        for ex in examples:
            lines += [f"### {ex['title']}", ""]
            if ex["note"]:
                lines += [ex["note"], ""]
            if ex["marketpay"]:
                lines += [f"*MarketPay (scripted):* {ex['marketpay']}", ""]
            lines += ["**Request**", "", "```http", f"{ex['method']} {ex['path']}"]
            if ex["request"] is not None:
                lines += ["Content-Type: application/json", "", json.dumps(ex["request"], indent=2)]
            lines += ["```", "", f"**Response** `{ex['status']}`", ""]
            if ex["response"] is not None:
                lines += ["```json", json.dumps(ex["response"], indent=2), "```", ""]
            else:
                lines += ["*(no body)*", ""]
    rendered = "\n".join(lines).rstrip() + "\n"
    terminal_tx_field = '"terminalTransactionId": ' + json.dumps("14")
    return rendered.replace(
        terminal_tx_field, '"terminalTransactionId": "<TERMINAL_TRANSACTION_ID>"'
    )


def _slug(title: str) -> str:
    keep = "".join(ch for ch in title.lower() if ch.isalnum() or ch in " -")
    return keep.replace(" ", "-")


if __name__ == "__main__":
    take_a_payment()
    read_payments()
    cancel_payments()
    reconcile()
    diagnostics()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render())
    count = sum(len(ex) for _, _, ex in SECTIONS)
    print(f"wrote {count} examples to {OUT}")
