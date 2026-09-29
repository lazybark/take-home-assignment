"""A stateful MarketPay + terminal for the fault suite, and a way to crash our service.

Unlike ScriptedMarketPay (a scripted answer per call), this models what the terminal DOES,
as observed on staging, and keeps its own ledger of every transaction it recorded. The
suite then checks our records against that ledger, not against what a test expected:
the ledger is the "what MarketPay holds" of the brief's invariants.

Modelled behaviour with original staging API notes:
- one transaction at a time; a second one while busy gets 409;
- the call is held open while the customer decides; an abort answers 204 only while that
  request is still open, 409 otherwise (idle terminal, or already at the bank);
- a slow transaction answers 202 and finishes later; while it runs, last-transaction still
  shows the previous record;
- a reversal is a new record with the purchase's ecrTransactionId and a new
  terminalTransactionId; a refund is a transaction with its own ecrTransactionId;
- a duplicate of a finished ecrTransactionId is charged AGAIN (the unsafe assumption: we
  don't know that MarketPay deduplicates, so our service must never send one).

Faults are injected per call: the request never leaves us, is lost on the way, is delayed
and delivered later, or its response is lost; or our process crashes before or after
MarketPay applied it.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from unittest.mock import MagicMock

import httpx

from payments.app import create_app
from payments.config import Settings
from payments.domain.models import Owner, payment_id_for
from payments.infrastructure.marketpay.client import MarketPayClient

TERMINAL = "PAX:TEST_TERMINAL"
# Real seconds a waiting terminal gives the customer before it gives up by itself. Short:
# our service's own waits run on a fake clock; only the terminal's run in real time.
TERMINAL_TIMEOUT_REAL = 3.0


class Crash(BaseException):
    """Our process died right here. A BaseException, so no `except Exception` in the
    service can survive it: the request unwinds without writing anything more."""


class Customer(StrEnum):
    TAPS = "taps"  # approved at once
    DECLINED = "declined"  # the bank says no (NOK, code 116)
    PARTIAL = "partial"  # approved for part of the amount (PARTIAL, code 010)
    NEVER_TAPS = "never_taps"  # waits; an abort (or the terminal's timeout) ends it: NOK
    TAPS_LATE = "taps_late"  # taps as our abort arrives: 409, and it is approved anyway
    ON_SIGNAL = "on_signal"  # waits until the test releases it (OK) or it's aborted (NOK)
    SLOW = "slow"  # answers 202; approved after a few last-transaction looks


class Fault(StrEnum):
    NOT_SENT = "not_sent"  # connect failure: MarketPay never saw it (re-send is safe)
    LOST_REQUEST = "lost_request"  # sent, never arrived; we see a timeout
    DELAYED = "delayed"  # sent, arrives only when the test delivers it; we see a timeout
    LOST_RESPONSE = "lost_response"  # applied by MarketPay; the answer never reaches us
    CRASH_BEFORE = "crash_before"  # our process dies while sending: nothing applied
    CRASH_AFTER = "crash_after"  # MarketPay applied it; our process dies before the answer


@dataclass
class Record:
    """One finished transaction, as the terminal keeps it."""

    kind: str  # PURCHASE, REFUND, REVERSAL
    ecr_id: str
    amount: str
    tx_id: str
    status: str  # OK, NOK, PARTIAL
    code: str | None
    reverses: str | None = None  # a reversal: the purchase's terminalTransactionId

    def result(self) -> dict:
        charged = self.status in ("OK", "PARTIAL")

        return {
            "status": self.status,
            "responseCode": self.code,
            "terminalTransactionId": self.tx_id,
            "authorizationCode": "213462" if charged else None,
            "finalTransactionParams": {
                "ecrTransactionId": self.ecr_id,
                "amount": self.amount if charged else "0",  # a NOK echoes "0" on staging
                "currency": "752" if charged else "0",
            },
        }


@dataclass
class Running:
    kind: str
    ecr_id: str
    amount: str
    customer: Customer
    reverses: str | None = None
    open: bool = False  # our request is still being held open
    wake: threading.Event = field(default_factory=threading.Event)
    released: bool = False  # ON_SIGNAL: the customer tapped
    looks_left: int = 3  # SLOW: finishes after this many last-transaction looks


class SimulatedMarketPay:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._lookup_busy = threading.Lock()
        self.records: list[Record] = []
        self.running: Running | None = None
        self.customers: dict[str, Customer] = {}  # per ecrTransactionId; default TAPS
        self.reversal_customers: dict[str, Customer] = {}  # per purchase ecrTransactionId
        self._faults: list[list] = []  # [operation, ecr_id or None, fault, times]
        self._failing_lookups = 0
        self.delayed: list[httpx.Request] = []
        self.calls: list[str] = []
        self.on_answer = None  # optional hook, run just before a 201 is returned
        self._next_id = 14

    # --- Test controls ---------------------------------------------------------------

    def inject(self, operation: str, fault: Fault, ecr_id: str | None = None, times=1):
        """Next `times` calls of `operation` ("process", "cancel") hit `fault`."""
        self._faults.append([operation, ecr_id, fault, times])

    def fail_lookups(self, times: int) -> None:
        self._failing_lookups = times  # transient 500s, as staging gives right after a 202

    def release(self) -> None:
        """The customer taps (ON_SIGNAL)."""

        with self._lock:
            if self.running is not None:
                self.running.released = True
                self.running.wake.set()

    def wait_until_open(self, timeout=5.0) -> None:
        """Until a transaction is running and our request for it is held open."""

        self._wait(lambda: self.running is not None and self.running.open, timeout)

    def wait_until_idle(self, timeout=TERMINAL_TIMEOUT_REAL + 3) -> None:
        self._wait(lambda: self.running is None, timeout)

    def deliver_delayed(self) -> None:
        """The delayed requests finally arrive (and, on an idle terminal, run)."""

        pending, self.delayed = self.delayed, []
        for request in pending:
            self._process(request, fault=None)

    # --- The ledger ----------------------------------------------------------------

    def standing_charges(self, reference: str) -> int:
        """Charges MarketPay holds for this order: approved purchases (a PARTIAL counts)
        that were neither reversed nor refunded. Below 0 means we refunded money that
        was never charged."""

        with self._lock:
            reversed_ids = {
                r.reverses for r in self.records if r.kind == "REVERSAL" and r.status == "OK"
            }
            purchases = [
                r
                for r in self.records
                if r.kind == "PURCHASE"
                and r.ecr_id == reference
                and r.status in ("OK", "PARTIAL")
                and r.tx_id not in reversed_ids
            ]
            refund_prefix = payment_id_for(reference).hex
            refunds = [
                r
                for r in self.records
                if r.kind == "REFUND" and r.status == "OK" and r.ecr_id.endswith(refund_prefix)
            ]

            return len(purchases) - len(refunds)

    def purchases_sent(self, reference: str) -> int:
        return sum(1 for r in self.records if r.kind == "PURCHASE" and r.ecr_id == reference)

    # --- HTTP -------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/process-transaction/"):
            self.calls.append("process")
            return self._process(request, self._take_fault("process", request))
        if path.startswith("/cancel-transaction/"):
            self.calls.append("cancel")
            return self._cancel(request, self._take_fault("cancel", request))
        if path.startswith("/abort-transaction/"):
            self.calls.append("abort")
            return self._abort()
        if path.startswith("/last-transaction/"):
            self.calls.append("last")
            return self._last_transaction()

        raise AssertionError(f"unexpected call {request.url}")

    def _process(self, request: httpx.Request, fault: Fault | None) -> httpx.Response:
        body = json.loads(request.content)
        ecr_id, kind = body["ecrTransactionId"], body.get("transactionType", "PURCHASE")

        if early := self._before_applying(request, fault):
            return early

        with self._lock:
            if self.running is not None:
                return httpx.Response(409, json={"errorMessage": "terminal busy"})

            # Refunds: the customer taps (a refund is card-present too).
            customer = self.customers.get(ecr_id, Customer.TAPS)
            run = Running(kind=kind, ecr_id=ecr_id, amount=body["amount"], customer=customer)
            self.running = run

        response = self._run(run)

        return self._after_applying(request, fault, response)

    def _cancel(self, request: httpx.Request, fault: Fault | None) -> httpx.Response:
        body = json.loads(request.content)
        ecr_id, purchase_id = body["ecrTransactionId"], body["terminalTransactionId"]

        if early := self._before_applying(request, fault):
            return early
        with self._lock:
            if self.running is not None:
                return httpx.Response(409, json={"errorMessage": "terminal busy"})

            standing = any(
                r.kind == "PURCHASE" and r.tx_id == purchase_id and r.ecr_id == ecr_id
                for r in self.records
            ) and not any(
                r.kind == "REVERSAL" and r.reverses == purchase_id and r.status == "OK"
                for r in self.records
            )

            customer = self.reversal_customers.get(ecr_id, Customer.TAPS)
            if not standing:
                customer = Customer.DECLINED  # nothing (left) to reverse

            run = Running(
                kind="REVERSAL",
                ecr_id=ecr_id,
                amount=body["amount"],
                customer=customer,
                reverses=purchase_id,
            )

            self.running = run

        result = self._run(run)
        status = json.loads(result.content)["status"]
        response = httpx.Response(
            200,
            json={
                "status": status,
                "cancellationParams": {
                    "terminalTransactionId": purchase_id,
                    "ecrTransactionId": ecr_id,
                    "amount": body["amount"],
                    "currency": body["currency"],
                },
            },
        )

        return self._after_applying(request, fault, response)

    def _run(self, run: Running) -> httpx.Response:
        """The terminal works through one transaction; returns what the call answers."""

        match run.customer:
            case Customer.TAPS:
                return self._finish(run, "OK", "000")
            case Customer.DECLINED:
                return self._finish(run, "NOK", "116")
            case Customer.PARTIAL:
                return self._finish(run, "PARTIAL", "010")
            case Customer.SLOW:
                return httpx.Response(202)  # finishes during later last-transaction looks

        run.open = True
        run.wake.wait(TERMINAL_TIMEOUT_REAL)  # an abort, a tap, or the terminal gives up

        with self._lock:
            run.open = False
            if run.customer is Customer.TAPS_LATE or run.released:
                return self._finish(run, "OK", "000")

            return self._finish(run, "NOK", None)  # stopped before the bank

    def _finish(self, run: Running, status: str, code: str | None) -> httpx.Response:
        with self._lock:
            record = Record(
                kind=run.kind,
                ecr_id=run.ecr_id,
                amount=run.amount,
                tx_id=str(self._next_id),
                status=status,
                code=code,
                reverses=run.reverses,
            )
            self._next_id += 1
            self.records.append(record)
            if self.running is run:
                self.running = None

        if self.on_answer is not None:
            self.on_answer()

        return httpx.Response(201, json=record.result())

    def _abort(self) -> httpx.Response:
        with self._lock:
            run = self.running
            if run is None or not run.open:
                return httpx.Response(409)  # nothing we can abort (idle, or request gone)

            run.wake.set()
            if run.customer is Customer.TAPS_LATE:
                return httpx.Response(409)  # too late: the bank is already deciding

            return httpx.Response(204)

    def _last_transaction(self) -> httpx.Response:
        if not self._lookup_busy.acquire(blocking=False):
            return httpx.Response(500)  # overlapping lookups fail, as on staging

        try:
            with self._lock:
                if self._failing_lookups > 0:
                    self._failing_lookups -= 1
                    return httpx.Response(500)

                run = self.running
                if run is not None and run.customer is Customer.SLOW:
                    run.looks_left -= 1
                    if run.looks_left <= 0:
                        self._finish(run, "OK", "000")

                if not self.records:
                    return httpx.Response(200, json={"lastTransactionState": "NOT_FOUND"})

                # A running transaction is never shown: always the last FINISHED one. A
                # reversal, too, is shown as a transactionResult (no cancellationResult).
                last = self.records[-1]
                return httpx.Response(
                    200,
                    json={"lastTransactionState": "FINISHED", "transactionResult": last.result()},
                )

        finally:
            self._lookup_busy.release()

    # --- Faults ---------------------------------------------------------------------

    def _take_fault(self, operation: str, request: httpx.Request) -> Fault | None:
        ecr_id = json.loads(request.content).get("ecrTransactionId")

        with self._lock:
            for entry in self._faults:
                op, only, fault, times = entry
                if op == operation and only in (None, ecr_id) and times > 0:
                    entry[3] -= 1

                    return fault

        return None

    def _before_applying(self, request: httpx.Request, fault: Fault | None):
        match fault:
            case Fault.NOT_SENT:
                raise httpx.ConnectError("injected: name resolution failed", request=request)
            case Fault.CRASH_BEFORE:
                raise Crash()
            case Fault.LOST_REQUEST:
                raise httpx.ReadTimeout("injected: request lost", request=request)
            case Fault.DELAYED:
                self.delayed.append(request)

                raise httpx.ReadTimeout("injected: request delayed", request=request)

        return None

    def _after_applying(self, request, fault, response: httpx.Response) -> httpx.Response:
        match fault:
            case Fault.LOST_RESPONSE:
                raise httpx.ReadTimeout("injected: response lost", request=request)
            case Fault.CRASH_AFTER:
                raise Crash()

        return response

    def _wait(self, condition, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "the simulated terminal never got there"
            time.sleep(0.01)


# --- Our service, as a process that can crash ---------------------------------------------


class DyingRepository:
    """The store as one process sees it: once that process is dead, it can't write."""

    def __init__(self, repo, process: "ServiceProcess") -> None:
        self._repo, self._process = repo, process

    def __getattr__(self, name):
        attr = getattr(self._repo, name)
        if not callable(attr):
            return attr

        def call(*args, **kwargs):
            self._process.check_alive()
            if name == "update" and self._process.crash_when is not None:
                payment_id, change = args
                args = (payment_id, self._crashing(change))

            return attr(*args, **kwargs)

        return call

    def _crashing(self, change):
        def wrapped(current):
            proposed = change(current)
            if self._process.crash_when(current, proposed):
                self._process.die()

                raise Crash()

            return proposed

        return wrapped


class ServiceProcess:
    """One run of our service (one boot). `die()` is a crash: every later MarketPay call or
    store access from it raises Crash. A new ServiceProcess on the same store and the same
    simulated MarketPay is the restart."""

    def __init__(self, sim, repo, clock, settings: Settings, boot_id: str) -> None:
        self.sim = sim
        self.alive = True
        self.crash_when = None  # (current, proposed) -> bool: die instead of that write
        marketpay = MarketPayClient.from_settings(
            settings, transport=httpx.MockTransport(self._to_marketpay)
        )
        self.app = create_app(
            settings,
            marketpay=marketpay,
            db=MagicMock(name="firestore.Client"),
            repo=DyingRepository(repo, self),
            clock=clock,
            owner=Owner(instance_id="local-1", boot_id=boot_id),
        )

    def _to_marketpay(self, request: httpx.Request) -> httpx.Response:
        self.check_alive()
        try:
            return self.sim.handle(request)

        except Crash:
            self.die()
            raise

    def check_alive(self) -> None:
        if not self.alive:
            raise Crash()

    def die(self) -> None:
        self.alive = False

    def post(self, path: str, json: dict | None = None):
        """The POS's call; None if our process died while handling it (connection dropped)."""

        try:
            return self.app.test_client().post(path, json=json)
        except Crash:
            return None

    def get(self, path: str):
        try:
            return self.app.test_client().get(path)
        except Crash:
            return None
