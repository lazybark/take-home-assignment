"""HTTP adapter for the MarketPay Cloud API (mutual TLS + mandatory User-Agent)."""

import ssl
import threading
import time
from collections import deque
from urllib.parse import quote

import httpx
import structlog
from pydantic import TypeAdapter, ValidationError

from payments.config import Settings
from payments.domain.marketpay.gateway import MarketPayHTTPError, MarketPayUnavailable
from payments.domain.marketpay.models import (
    CancellationResult,
    CancelTransactionRequest,
    EcrParams,
    LastTransactionResult,
    ProcessTransactionRequest,
    TerminalSession,
    TransactionResult,
)
from payments.domain.marketpay.outcomes import (
    Aborted,
    AbortOutcome,
    AbortRefused,
    AbortUnconfirmed,
    Accepted,
    Ambiguous,
    CancelCompleted,
    CancelOutcome,
    Completed,
    Found,
    LookupFailed,
    LookupOutcome,
    NotSent,
    ProcessOutcome,
    Rejected,
    TooLate,
)
from payments.domain.outcomes import result_inconsistency

log = structlog.get_logger(__name__)

_terminal_list = TypeAdapter(list[TerminalSession])


# Failures that happen before a connection exists, so the request was certainly not sent.
_BEFORE_SENDING = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def build_http_client(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    """Build the HTTP client. A test `transport` replaces the network (and the mTLS context)."""

    verify: ssl.SSLContext | bool = True

    if transport is None:
        verify = ssl.create_default_context()
        try:
            verify.load_cert_chain(
                certfile=settings.marketpay_client_cert, keyfile=settings.marketpay_client_key
            )
        except (OSError, ssl.SSLError) as exc:
            raise RuntimeError(
                "Cannot load the MarketPay mTLS client certificate "
                f"({settings.marketpay_client_cert}, {settings.marketpay_client_key}): {exc}"
            ) from exc

    return httpx.Client(
        base_url=settings.marketpay_base_url,
        headers={"User-Agent": settings.marketpay_user_agent},
        timeout=httpx.Timeout(
            settings.marketpay_read_timeout_seconds,
            connect=settings.marketpay_connect_timeout_seconds,
        ),
        verify=verify,
        transport=transport,
    )


class MarketPayClient:
    def __init__(self, http: httpx.Client, *, store_code: str, ecr_id: str) -> None:
        self._http = http
        self._store_code = store_code
        self._ecr_id = ecr_id
        # terminalTransactionIds already reported as inconsistent: polling may see the same
        # record every second, and one error per record is enough.
        self._reported: deque[str] = deque(maxlen=256)
        self._reported_lock = threading.Lock()
        self._lookup_locks: dict[str, threading.Lock] = {}
        self._lookup_locks_guard = threading.Lock()

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.BaseTransport | None = None
    ) -> "MarketPayClient":
        return cls(
            build_http_client(settings, transport),
            store_code=settings.marketpay_store_code,
            ecr_id=settings.marketpay_ecr_id,
        )

    def list_terminals(self, connected: bool | None = None) -> list[TerminalSession]:
        params = {"storeCode": self._store_code}

        if connected is not None:
            params["connected"] = "true" if connected else "false"
        response = self._send("GET", "/terminals", params=params)

        if response.status_code != 200:
            raise _unexpected_status(response)

        return _terminal_list.validate_json(response.content)

    def process_transaction(
        self,
        terminal_id: str,
        request: ProcessTransactionRequest,
        *,
        wait_time: int,
        read_timeout: float,
    ) -> ProcessOutcome:
        """Run a payment (or refund). Never raises: network trouble is an outcome too.

        `wait_time` is how long MarketPay holds the request before answering 202 (honoured
        to the second on staging); `read_timeout` is how long we wait for any answer,
        and must exceed `wait_time`. While MarketPay holds this request open, an abort can
        still stop the terminal; once it has answered 202, it can't.
        """

        request = request.model_copy(update={"ecr_params": self._ecr_params(request.ecr_params)})
        response = self._send_operation(
            f"/process-transaction/{_terminal_path(terminal_id)}",
            request.to_wire(),
            wait_time,
            read_timeout,
        )

        if not isinstance(response, httpx.Response):
            return response

        if response.status_code == 201:
            try:
                result = TransactionResult.model_validate_json(response.content)
                self._report_inconsistency(result, source="process-transaction")
                return Completed(result=result)
            except ValidationError as exc:
                # MarketPay said "done" but we can't read how: the charge may well exist.
                return Ambiguous(reason=f"unreadable 201 body: {exc}")

        return _classify_other(response)

    def cancel_transaction(
        self,
        terminal_id: str,
        request: CancelTransactionRequest,
        *,
        wait_time: int,
        read_timeout: float,
    ) -> CancelOutcome:
        """Reverse an approved payment. Never raises: network trouble is an outcome too.

        It waits for the customer to tap the card again — a reversal is as slow and
        as customer-dependent as a payment. See CancelTransactionRequest for how it shows
        up in last-transaction afterwards.
        """

        request = request.model_copy(update={"ecr_params": self._ecr_params(request.ecr_params)})
        response = self._send_operation(
            f"/cancel-transaction/{_terminal_path(terminal_id)}",
            request.to_wire(),
            wait_time,
            read_timeout,
        )
        if not isinstance(response, httpx.Response):
            return response

        if response.status_code == 200:
            try:
                return CancelCompleted(
                    result=CancellationResult.model_validate_json(response.content)
                )
            except ValidationError as exc:
                return Ambiguous(reason=f"unreadable 200 body: {exc}")

        return _classify_other(response)

    def _ecr_params(self, given: EcrParams | None) -> EcrParams:
        """Our ECR id on every request; the caller may add a notificationUrl."""
        given = given or EcrParams()

        return given if given.ecr_id else given.model_copy(update={"ecr_id": self._ecr_id})

    def _send_operation(
        self, path: str, body: dict, wait_time: int, read_timeout: float
    ) -> httpx.Response | NotSent | Ambiguous:
        try:
            return self._send(
                "POST",
                path,
                params={"waitTime": wait_time},
                json=body,
                timeout=httpx.Timeout(read_timeout, connect=self._http.timeout.connect),
            )

        except MarketPayUnavailable as exc:
            if not exc.request_sent:
                return NotSent(reason=str(exc))

            return Ambiguous(reason=str(exc))

    def abort_transaction(self, terminal_id: str, *, timeout: float) -> AbortOutcome:
        """Ask the terminal to stop the transaction in progress. Never raises.

        Staging: only a 204 means "stopped", and it comes only while OUR request for
        that transaction is still held open by MarketPay. After it answered 202, after our
        process crashed, and on an idle terminal alike, the answer is 409 — which the spec
        calls "too late, it may have completed" but which carries no information at all.
        """

        try:
            response = self._send(
                "POST",
                f"/abort-transaction/{_terminal_path(terminal_id)}",
                json=EcrParams(ecr_id=self._ecr_id).to_wire(),
                timeout=httpx.Timeout(timeout, connect=min(timeout, self._http.timeout.connect)),
            )
        except MarketPayUnavailable as exc:
            return AbortUnconfirmed(reason=str(exc))

        if response.status_code == 204:
            return Aborted()

        if response.status_code == 409:
            return TooLate()

        if 400 <= response.status_code < 500:
            return AbortRefused(status_code=response.status_code)

        return AbortUnconfirmed(reason=f"HTTP {response.status_code}")

    def get_last_transaction(self, terminal_id: str, *, timeout: float) -> LookupOutcome:
        """Read the terminal's latest transaction. Never raises: a failure is `LookupFailed`.

        One lookup per terminal at a time: MarketPay answers HTTP 500 to *every*
        overlapping last-transaction request for the same terminal, while back-to-back
        ones succeed. So callers queue here (within `timeout`) instead of colliding.
        """

        started = time.monotonic()
        lock = self._lookup_lock(terminal_id)

        if not lock.acquire(timeout=max(0.0, timeout)):
            return LookupFailed(reason="another lookup for this terminal is still running")
        try:
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return LookupFailed(reason="no time left after waiting for another lookup")

            response = self._send(
                "GET",
                f"/last-transaction/{_terminal_path(terminal_id)}",
                timeout=httpx.Timeout(
                    remaining, connect=min(remaining, self._http.timeout.connect)
                ),
            )

        except MarketPayUnavailable as exc:
            return LookupFailed(reason=str(exc))

        finally:
            lock.release()

        if response.status_code != 200:
            return LookupFailed(reason=f"HTTP {response.status_code}")
        try:
            last = LastTransactionResult.model_validate_json(response.content)
            if last.transaction_result is not None:
                self._report_inconsistency(last.transaction_result, source="last-transaction")

            return Found(result=last)

        except ValidationError as exc:
            return LookupFailed(reason=f"unreadable body: {exc}")

    def close(self) -> None:
        self._http.close()

    def _lookup_lock(self, terminal_id: str) -> threading.Lock:
        with self._lookup_locks_guard:
            return self._lookup_locks.setdefault(terminal_id, threading.Lock())

    def _report_inconsistency(self, result: TransactionResult, source: str) -> None:
        problem = result_inconsistency(result)
        if problem is None:
            return

        key = result.terminal_transaction_id or repr(result.final_transaction_params)

        with self._reported_lock:
            if key in self._reported:
                return

            self._reported.append(key)

        echoed = result.final_transaction_params

        # Everything needed to take it up with MarketPay, without card numbers.
        log.error(
            "marketpay_result_inconsistent",
            problem=problem,
            source=source,
            status=result.status,
            response_code=result.response_code,
            authorization_code=result.authorization_code,
            terminal_transaction_id=result.terminal_transaction_id,
            ecr_transaction_id=echoed.ecr_transaction_id if echoed else None,
            echoed_amount=echoed.amount if echoed else None,
            echoed_currency=echoed.currency if echoed else None,
            echoed_type=echoed.transaction_type if echoed else None,
            card_capture=result.card_data.card_capture if result.card_data else None,
        )

    def _send(self, method: str, path: str, **kwargs) -> httpx.Response:
        started = time.perf_counter()
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            log.warning(
                "marketpay_unavailable",
                method=method,
                path=path,
                error=type(exc).__name__,
                detail=str(exc),
                duration_ms=_elapsed_ms(started),
            )

            message = f"No answer from MarketPay for {method} {path}: {type(exc).__name__}."
            if not isinstance(exc, httpx.TimeoutException):
                # A reset during the TLS handshake is how a rejected client cert shows up.
                message += " A connection reset usually means the mTLS certificate was rejected."

            raise MarketPayUnavailable(
                message, request_sent=not isinstance(exc, _BEFORE_SENDING)
            ) from exc

        log.info(
            "marketpay_call",
            method=method,
            path=path,
            status=response.status_code,
            duration_ms=_elapsed_ms(started),
            # A refusal's reason, as evidence (e.g. the undocumented 409 seen once). Error
            # bodies carry no card data; success bodies (receipts) are never logged.
            **({"error_body": response.text[:500]} if 400 <= response.status_code < 600 else {}),
        )

        return response


def _classify_other(response: httpx.Response) -> Accepted | Rejected | Ambiguous:
    """Non-success answers to an operation that changes something."""

    if response.status_code == 202:
        return Accepted()

    if 400 <= response.status_code < 500:
        return Rejected(status_code=response.status_code, body=response.text)

    return Ambiguous(reason=f"HTTP {response.status_code}")


def _terminal_path(terminal_id: str) -> str:
    # "AAA:1111111111" — the colon is legal in a path segment; everything else is escaped.
    return quote(terminal_id, safe=":")


def _unexpected_status(response: httpx.Response) -> MarketPayHTTPError:
    hint = None
    if response.status_code == 400 and not response.content:
        hint = "empty 400 body: usually a missing User-Agent header"

    return MarketPayHTTPError(response.status_code, response.text, hint)


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)
