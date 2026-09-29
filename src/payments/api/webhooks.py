"""MarketPay's notification webhook (the bonus): check the signed URL, log what arrived and
who sent it, answer 200 at once, then use it on a worker thread (the guide: respond quickly,
process afterwards). What a notification can settle: application/notifications.py.

SECURITY: MarketPay doesn't authenticate the call; the secret, signed URL is the only proof
it came from MarketPay. Before production add an IP allowlist of MarketPay's senders,
or check a signature if MarketPay offers one. The sender details logged here are for that.
"""

import contextvars
from concurrent.futures import Executor
from uuid import UUID

import structlog
from flask import Blueprint, current_app, request
from pydantic import ValidationError

from payments.api.errors import error_response
from payments.application.service import PaymentService
from payments.domain.marketpay.notification import Notification
from payments.domain.models import Operation
from payments.infrastructure.marketpay.notification_urls import (
    WEBHOOK_PREFIX,
    NotificationUrls,
    redact_card_data,
)

log = structlog.get_logger(__name__)

bp = Blueprint("webhooks", __name__)

_OPERATIONS = {operation.value for operation in Operation}


@bp.post(f"{WEBHOOK_PREFIX}/<uuid:payment_id>/<operation>/<signature>")
def marketpay_notification(payment_id: UUID, operation: str, signature: str):
    urls: NotificationUrls | None = current_app.extensions.get("notification_urls")
    if (
        urls is None
        or operation not in _OPERATIONS
        or not urls.verify(payment_id, operation, signature)
    ):
        # Not a URL we handed out: someone guessing, or a stale secret. Look like no route.
        log.warning(
            "marketpay_notification_rejected",
            payment_id=str(payment_id),
            operation=operation,
            **_sender(),
        )

        return error_response(404, "not_found", "Not found.")

    body = request.get_json(silent=True)
    notification: Notification | None = None

    try:
        notification = Notification.model_validate(body)
        parsed = {
            "notification_status": notification.status,
            "ecr_transaction_id": notification.ecr_transaction_id,
            "terminal_transaction_id": notification.terminal_transaction_id,
            "has_result": notification.result is not None,
        }
    except ValidationError as exc:
        parsed = {"parse_error": str(exc)}

    # The raw body (card data removed) and the sender: evidence for authenticating it
    # (fixed addresses? a signature header?) and for questions to MarketPay about what it sent.
    log.info(
        "marketpay_notification",
        payment_id=str(payment_id),
        operation=operation,
        **parsed,
        body=redact_card_data(body) if body is not None else request.get_data(as_text=True),
        **_sender(),
    )

    if notification is not None:
        worker: Executor = current_app.extensions["notification_worker"]
        payments: PaymentService = current_app.extensions["payments"]
        context = contextvars.copy_context()  # keeps request_id etc. in the worker's logs
        worker.submit(context.run, _use, payments, payment_id, Operation(operation), notification)

    # The guide: answer 200 quickly, whatever it was — MarketPay never retries.
    return {}, 200


def _use(
    payments: PaymentService, payment_id: UUID, operation: Operation, notification: Notification
) -> None:
    try:
        use = payments.accept_notification(payment_id, operation, notification)
        log.info("notification_used", use=use, notification_status=notification.status)
    except Exception:
        # Lost, like an undelivered notification: polling / reconcile settle it instead.
        log.exception("notification_processing_failed")


def _sender() -> dict:
    headers = request.headers
    return {
        "remote_addr": request.remote_addr,
        # Set by the tunnel (cloudflared) / proxies: the real sender's address.
        "cf_connecting_ip": headers.get("Cf-Connecting-Ip"),
        "x_forwarded_for": headers.get("X-Forwarded-For"),
        "user_agent": headers.get("User-Agent"),
        # Names only: shows whether MarketPay sends anything like a signature header.
        "header_names": sorted(headers.keys()),
    }
