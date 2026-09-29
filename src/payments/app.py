"""Flask application factory and wiring."""

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import structlog
from flask import Flask, Response, g, request
from google.cloud import firestore

from payments.api.errors import register_error_handlers
from payments.api.routes import bp
from payments.api.webhooks import bp as webhooks_bp
from payments.application.clock import Clock
from payments.application.service import PaymentService
from payments.config import Settings
from payments.domain.models import Owner
from payments.domain.repository import PaymentRepository
from payments.infrastructure.marketpay.client import MarketPayClient
from payments.infrastructure.marketpay.notification_urls import NotificationUrls, mask_signature
from payments.infrastructure.store.firestore import FirestorePaymentRepository, make_client
from payments.log import configure_logging

log = structlog.get_logger(__name__)


def create_app(
    settings: Settings | None = None,
    marketpay: MarketPayClient | None = None,
    db: firestore.Client | None = None,
    repo: PaymentRepository | None = None,
    clock: Clock | None = None,
    owner: Owner | None = None,
) -> Flask:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)

    app = Flask(__name__)
    app.extensions["settings"] = settings
    app.extensions["marketpay"] = marketpay or MarketPayClient.from_settings(settings)
    app.extensions["firestore"] = db or make_client(settings)
    app.extensions["repo"] = repo or FirestorePaymentRepository(app.extensions["firestore"])

    # A fresh boot id per process start: operations owned by an earlier boot of this
    # instance were abandoned by a crash/restart and are recovered straight away.
    owner = owner or Owner(instance_id=settings.instance_id, boot_id=uuid.uuid4().hex)

    app.extensions["notification_urls"] = _notification_urls(settings)

    # One worker: notifications are few, and handling them in arrival order keeps it simple.
    app.extensions["notification_worker"] = ThreadPoolExecutor(
        max_workers=1, thread_name_prefix="notifications"
    )

    app.extensions["payments"] = PaymentService(
        marketpay=app.extensions["marketpay"],
        repo=app.extensions["repo"],
        clock=clock,
        owner=owner,
        notification_urls=app.extensions["notification_urls"],
    )

    app.before_request(_bind_request_context)
    app.after_request(_log_request)
    register_error_handlers(app)
    app.register_blueprint(bp)
    app.register_blueprint(webhooks_bp)

    log.info(
        "app_started",
        instance_id=owner.instance_id,
        boot_id=owner.boot_id,
        marketpay=settings.marketpay_base_url,
        firestore_project=settings.google_cloud_project,
        notifications=settings.notification_base_url or "off",
    )

    if settings.reconcile_on_start:
        app.extensions["startup_reconcile"] = _reconcile_on_start(app.extensions["payments"])

    return app


def _reconcile_on_start(payments: PaymentService) -> threading.Thread:
    """Converge on restart: settle, once and in the background, the operations an earlier
    boot of this instance left open (a crash mid-flight). The same as POST /reconcile; not
    a timer, and safe alongside requests (each take-over is atomic)."""

    def run() -> None:
        try:
            summary = payments.reconcile()
            log.info(
                "startup_reconcile_finished",
                scanned=summary.scanned,
                resolved=summary.resolved,
                still_open=summary.still_open,
            )
        except Exception:
            log.exception("startup_reconcile_failed")  # POST /reconcile still works

    thread = threading.Thread(target=run, name="startup-reconcile", daemon=True)
    thread.start()
    return thread


def _notification_urls(settings: Settings) -> NotificationUrls | None:
    if settings.notification_base_url is None or settings.notification_secret is None:
        return None

    # The server's own access log (werkzeug / gunicorn) prints paths: keep the signature out.
    for name in ("werkzeug", "gunicorn.access"):
        logger = logging.getLogger(name)
        if not any(isinstance(f, _MaskSignatures) for f in logger.filters):
            logger.addFilter(_MaskSignatures())

    return NotificationUrls(
        settings.notification_base_url, settings.notification_secret.get_secret_value()
    )


class _MaskSignatures(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        mask = lambda a: mask_signature(a) if isinstance(a, str) else a  # noqa: E731

        if isinstance(record.args, dict):  # gunicorn's access log passes its atoms as a dict
            record.args = {key: mask(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(mask(a) for a in record.args)

        record.msg = mask_signature(str(record.msg))

        return True


def _bind_request_context() -> None:
    structlog.contextvars.clear_contextvars()
    g.request_id = (
        request.headers.get("X-Request-ID") or uuid.uuid4().hex
    )  # form our own request ID if not provided
    g.started = time.perf_counter()
    structlog.contextvars.bind_contextvars(request_id=g.request_id)


def _log_request(response: Response) -> Response:
    log.info(
        "http_request",
        method=request.method,
        path=mask_signature(request.path),
        status=response.status_code,
        duration_ms=round((time.perf_counter() - g.started) * 1000),
    )

    response.headers["X-Request-ID"] = g.request_id

    return response
