"""Maps exceptions to the API's `Error` body ({code, message})."""

import structlog
from flask import Flask
from pydantic import BaseModel, ValidationError
from werkzeug.exceptions import HTTPException

from payments.application.errors import IdempotencyMismatch, TerminalBusy
from payments.domain.marketpay.gateway import MarketPayHTTPError, MarketPayUnavailable
from payments.domain.repository import StoreUnavailable

log = structlog.get_logger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str


def error_response(status: int, code: str, message: str) -> tuple[dict, int]:
    return ErrorBody(code=code, message=message).model_dump(), status


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ValidationError)
    def _validation_error(exc: ValidationError):
        details = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'body'}: {err['msg']}"
            for err in exc.errors()
        )

        return error_response(400, "validation_error", details)

    @app.errorhandler(IdempotencyMismatch)
    def _idempotency_mismatch(exc: IdempotencyMismatch):
        return error_response(409, "idempotency_mismatch", str(exc))

    @app.errorhandler(TerminalBusy)
    def _terminal_busy(exc: TerminalBusy):
        return error_response(409, "terminal_busy", str(exc))

    @app.errorhandler(StoreUnavailable)
    def _store_unavailable(exc: StoreUnavailable):
        # Only raised before anything reached MarketPay (after that we answer with its
        # outcome instead), so repeating the same request is safe.
        return error_response(
            503,
            "store_unavailable",
            "The payment datastore is unavailable. Nothing new was sent to the terminal; "
            "retry the same request (same reference) — it is safe to repeat.",
        )

    @app.errorhandler(MarketPayUnavailable)
    def _provider_unavailable(exc: MarketPayUnavailable):

        return error_response(502, "provider_unavailable", str(exc))

    @app.errorhandler(MarketPayHTTPError)
    def _provider_error(exc: MarketPayHTTPError):

        return error_response(502, "provider_error", str(exc))

    @app.errorhandler(HTTPException)
    def _http_error(exc: HTTPException):
        code = (exc.name or "error").lower().replace(" ", "_")

        return error_response(exc.code or 500, code, exc.description or code)

    @app.errorhandler(Exception)
    def _unhandled(exc: Exception):
        log.exception("unhandled_error")

        return error_response(500, "internal_error", "Internal server error.")
