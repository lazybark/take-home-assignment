"""Reconcile: POST /reconcile."""

from datetime import datetime
from uuid import UUID

import structlog
from pydantic import BaseModel, ConfigDict

from payments.application.context import Context
from payments.application.recovery import Recovery, partial_reversal_due
from payments.application.store_policy import StorePolicy
from payments.domain.models import FINAL_STATES, Operation, Payment
from payments.domain.recovery import recovery_window, release_refund_due
from payments.domain.repository import StoreUnavailable
from payments.domain.transitions import release_partial_due

log = structlog.get_logger(__name__)


class ReconcileSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    scanned: int
    resolved: int
    still_open: int
    resolved_ids: list[UUID]


class Reconcile:
    """Use case: settle every open payment nobody is driving (POST /reconcile)."""

    def __init__(self, ctx: Context, store: StorePolicy, recovery: Recovery) -> None:
        self._repo = ctx.repo
        self._clock = ctx.clock
        self._store = store
        self._recovery = recovery

    def run(
        self, terminal_id: str | None = None, older_than: datetime | None = None
    ) -> ReconcileSummary:
        """Settle every open payment nobody is driving, against what MarketPay holds.

        Idempotent and safe to run concurrently: each payment is taken over atomically,
        and nothing that may have reached MarketPay is ever sent again.
        """

        candidates = [
            p
            for p in self._store.run(
                lambda: self._repo.open_payments(terminal_id), what="open_payments"
            )
            if older_than is None or p.created_at < older_than
        ]

        resolved: list[UUID] = []
        for payment in sorted(candidates, key=lambda p: p.created_at):
            with structlog.contextvars.bound_contextvars(
                payment_id=str(payment.id), reference=payment.reference
            ):
                try:
                    settled = self._reconcile_one(payment)
                except StoreUnavailable:
                    # Counted as still open; the next reconcile picks it up again.
                    log.error("reconcile_payment_skipped", reason="store unavailable")
                    settled = payment

            if settled.operation is None and settled.state in FINAL_STATES:
                resolved.append(settled.id)

        self._recovery.verify_flagged_terminals(terminal_id)
        summary = ReconcileSummary(
            scanned=len(candidates),
            resolved=len(resolved),
            still_open=len(candidates) - len(resolved),
            resolved_ids=resolved,
        )

        log.info("reconcile_finished", **summary.model_dump(exclude={"resolved_ids"}))

        return summary

    def _reconcile_one(self, payment: Payment) -> Payment:
        if payment.operation is None:
            return self._recovery.look_once_without_operation(payment)

        if not self._recovery.orphaned(payment):
            log.info("reconcile_skipped_live_owner", owner=payment.owner)

            return payment  # a live request is driving it

        poll_seconds, answer_seconds = recovery_window(payment, self._clock.now())
        start = self._clock.monotonic()
        recovered = self._recovery.take_over_and_recover(
            payment, poll_until=start + poll_seconds, answer_by=start + answer_seconds
        )

        if recovered.operation is Operation.REFUND and recovered.undo_started_at is None:
            recovered = self._store.change(recovered.id, release_refund_due)

        if partial_reversal_due(recovered):
            recovered = self._store.change(recovered.id, release_partial_due)

        return recovered
