"""The MarketPay gateway port: the calls the application makes, and how they fail.

Implemented by payments.infrastructure.marketpay.client.MarketPayClient (mTLS, HTTP). Operations
that change something never raise for network trouble: it comes back as a typed outcome
(NotSent, Ambiguous, …), so no code path can mistake a timeout for an answer."""

from typing import Protocol
from uuid import UUID

from payments.domain.marketpay.models import (
    CancelTransactionRequest,
    ProcessTransactionRequest,
    TerminalSession,
)
from payments.domain.marketpay.outcomes import (
    AbortOutcome,
    CancelOutcome,
    LookupOutcome,
    ProcessOutcome,
)


class MarketPayError(Exception):
    """Base class for MarketPay adapter errors."""


class MarketPayUnavailable(MarketPayError):
    """No HTTP answer at all: connect failure, TLS handshake reset, timeout.

    With mTLS, a missing/expired/unprovisioned client certificate also shows up
    this way (a connection reset, not an HTTP status).
    """

    def __init__(self, message: str, *, request_sent: bool) -> None:
        super().__init__(message)
        # False only when we failed before a connection existed (DNS, TCP/TLS connect,
        # pool wait): then no byte of the request reached MarketPay.
        self.request_sent = request_sent


class MarketPayHTTPError(MarketPayError):
    """MarketPay answered, but with a status the caller did not expect."""

    def __init__(self, status_code: int, body: str, hint: str | None = None) -> None:
        self.status_code = status_code
        self.body = body
        message = f"MarketPay returned HTTP {status_code}"
        if hint:
            message += f" ({hint})"
        super().__init__(message)


class MarketPayGateway(Protocol):
    def list_terminals(self, connected: bool | None = None) -> list[TerminalSession]:
        """Raises MarketPayUnavailable / MarketPayHTTPError (a read: nothing to get wrong)."""
        ...

    def process_transaction(
        self,
        terminal_id: str,
        request: ProcessTransactionRequest,
        *,
        wait_time: int,
        read_timeout: float,
    ) -> ProcessOutcome: ...

    def cancel_transaction(
        self,
        terminal_id: str,
        request: CancelTransactionRequest,
        *,
        wait_time: int,
        read_timeout: float,
    ) -> CancelOutcome: ...

    def abort_transaction(self, terminal_id: str, *, timeout: float) -> AbortOutcome: ...

    def get_last_transaction(self, terminal_id: str, *, timeout: float) -> LookupOutcome: ...


class NotificationAddresses(Protocol):
    """Where MarketPay should send an operation's notifications (the bonus webhook)."""

    def for_operation(self, payment_id: UUID, operation: str) -> str: ...
