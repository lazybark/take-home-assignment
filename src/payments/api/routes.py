"""HTTP handlers. Thin by design: parse with pydantic, call a collaborator, serialize."""

from datetime import datetime
from uuid import UUID

import structlog
from flask import Blueprint, current_app, request
from pydantic import BaseModel

from payments.api.errors import error_response
from payments.api.schemas import (
    ApiModel,
    CreatePaymentRequest,
    HistoryResponse,
    ListPaymentsQuery,
    PaymentListResponse,
    PaymentResponse,
    ReconcileRequest,
    ReconcileSummaryResponse,
)
from payments.application import terminals as terminal_views
from payments.application.cancel_payment import CancelKind
from payments.application.service import PaymentService
from payments.application.take_payment import NewPayment
from payments.domain.listing import InvalidCursor, PaymentQuery, decode_cursor, encode_cursor
from payments.domain.marketpay.gateway import MarketPayGateway
from payments.domain.marketpay.outcomes import Found
from payments.infrastructure.store import firestore

log = structlog.get_logger(__name__)

bp = Blueprint("api", __name__)


class TerminalsQuery(BaseModel):
    connected: bool | None = None


class LockedBy(ApiModel):
    payment_id: UUID
    reference: str
    locked_at: datetime


class TerminalItem(ApiModel):
    terminal_id: str
    connected: bool | None
    ws_created_time: datetime | None
    locked: bool  # True: a payment on it is unresolved; new payments get 409
    locked_by: LockedBy | None


class TerminalList(ApiModel):
    items: list[TerminalItem]


def _marketpay() -> MarketPayGateway:
    return current_app.extensions["marketpay"]


def _payments() -> PaymentService:
    return current_app.extensions["payments"]


@bp.post("/payments")
def create_payment():
    body = request.get_json(silent=True)

    if not isinstance(body, dict):
        return error_response(400, "validation_error", "Request body must be a JSON object.")

    req = CreatePaymentRequest.model_validate(body)
    result = _payments().create_payment(NewPayment(**req.model_dump()))

    return PaymentResponse.from_domain(result.payment).to_json(), 201 if result.created else 200


# 409 bodies for cancels that cannot (or did not) take effect.
_CANCEL_CONFLICTS = {
    CancelKind.NOT_CANCELLABLE: (
        "not_cancellable",
        "Payment {id} is {state}: nothing was charged, so there is nothing to cancel.",
    ),
    CancelKind.NEEDS_ATTENTION: (
        "needs_attention",
        "Payment {id} cannot be cancelled automatically ({reason}); it needs manual review.",
    ),
    CancelKind.UNDO_FAILED: (
        "cancel_failed",
        "Payment {id} is still approved: the reversal did not take effect ({reason}).",
    ),
}


@bp.post("/payments/<uuid:payment_id>/cancel")
def cancel_payment(payment_id: UUID):
    result = _payments().cancel_payment(payment_id)
    if result is None:
        return error_response(404, "not_found", f"No payment with id {payment_id}.")

    if result.kind in _CANCEL_CONFLICTS:
        code, message = _CANCEL_CONFLICTS[result.kind]
        payment = result.payment
        reason = payment.state_reason.value if payment.state_reason else "no reason recorded"

        return error_response(
            409, code, message.format(id=payment.id, state=payment.state.value, reason=reason)
        )

    return PaymentResponse.from_domain(result.payment).to_json()


@bp.post("/reconcile")
def reconcile():
    """Settle open payments nobody is driving (e.g. after a crash). Safe to repeat."""

    body = request.get_json(silent=True) if request.data else {}

    if not isinstance(body, dict):
        return error_response(400, "validation_error", "Request body must be a JSON object.")

    req = ReconcileRequest.model_validate(body)
    summary = _payments().reconcile(terminal_id=req.terminal_id, older_than=req.older_than)

    return ReconcileSummaryResponse.model_validate(summary.model_dump()).model_dump(
        mode="json", by_alias=True
    )


@bp.get("/payments")
def list_payments():
    states = [s for raw in request.args.getlist("state") for s in raw.split(",") if s]
    args = {k: v for k, v in request.args.items() if k != "state"}
    params = ListPaymentsQuery.model_validate({**args, **({"state": states} if states else {})})

    try:
        after = decode_cursor(params.cursor) if params.cursor else None
    except InvalidCursor as exc:
        return error_response(400, "validation_error", f"cursor: {exc}")

    page = _payments().list_payments(
        PaymentQuery(
            states=frozenset(params.state) if params.state else None,
            terminal_id=params.terminal_id,
            reference=params.reference,
            created_after=params.created_after,
            created_before=params.created_before,
            limit=params.limit,
            after=after,
        )
    )

    return PaymentListResponse(
        items=[PaymentResponse.from_domain(p) for p in page.items],
        next_cursor=encode_cursor(page.next_cursor) if page.next_cursor else None,
    ).to_json()


@bp.get("/payments/<uuid:payment_id>")
def get_payment(payment_id: UUID):
    payment = _payments().get_payment(payment_id)
    if payment is None:
        return error_response(404, "not_found", f"No payment with id {payment_id}.")

    return PaymentResponse.from_domain(payment).to_json()


@bp.get("/payments/<uuid:payment_id>/history")
def payment_history(payment_id: UUID):
    """How the payment got to its state (beyond the contract; see docs/api-extensions.md)."""

    entries = _payments().payment_history(payment_id)
    if entries is None:
        return error_response(404, "not_found", f"No payment with id {payment_id}.")

    return HistoryResponse.from_domain(payment_id, entries).to_json()


@bp.get("/healthz")
def healthz():
    """Liveness: the process is up."""
    return {"status": "ok"}


@bp.get("/readyz")
def readyz():
    """Readiness: we can reach the datastore."""
    try:
        firestore.ping(current_app.extensions["firestore"])
    except Exception as exc:
        log.warning("firestore_unreachable", error=type(exc).__name__, detail=str(exc))
        return {"status": "unavailable", "firestore": f"{type(exc).__name__}: {exc}"}, 503
    return {"status": "ok", "firestore": "ok"}


@bp.get("/terminals")
def list_terminals():
    """Diagnostic: MarketPay's terminal list, marked with our locks (not in payment-api.yaml)."""

    query = TerminalsQuery.model_validate(request.args.to_dict())
    views = terminal_views.list_terminals(
        _marketpay(), current_app.extensions["repo"], connected=query.connected
    )
    items = [
        TerminalItem(
            terminal_id=v.session.terminal_id,
            connected=v.session.connected,
            ws_created_time=v.session.ws_created_time,
            locked=v.lock is not None,
            locked_by=LockedBy.model_validate(v.lock.model_dump()) if v.lock else None,
        )
        for v in views
    ]

    return TerminalList(items=items).model_dump(mode="json", by_alias=True)


@bp.get("/terminals/<terminal_id>/last-transaction")
def last_transaction(terminal_id: str):
    """Diagnostic: what MarketPay reports as this terminal's latest transaction (parsed)."""

    lookup = _marketpay().get_last_transaction(terminal_id, timeout=10.0)
    if not isinstance(lookup, Found):
        return error_response(502, "provider_unavailable", lookup.reason)

    return lookup.result.model_dump(mode="json", by_alias=True, exclude_none=True)
