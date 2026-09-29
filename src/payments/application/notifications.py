"""Use a MarketPay notification (the bonus webhook)."""

from enum import StrEnum
from uuid import UUID

import structlog

from payments.application.context import Context, diagnostics
from payments.application.recovery import Recovery
from payments.application.store_policy import StorePolicy
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.marketpay.notification import Notification
from payments.domain.models import Operation, Payment, PaymentState, StateReason
from payments.domain.notifications import apply_notified, notified_outcome
from payments.domain.outcomes import observe_last_transaction, result_inconsistency

log = structlog.get_logger(__name__)


class NotificationUse(StrEnum):
    """What a notification was used for (logged; also what tests check)."""

    PROGRESS = "progress"  # no final outcome in it
    UNKNOWN_PAYMENT = "unknown_payment"
    ALREADY_SETTLED = "already_settled"
    NOT_CONCLUSIVE = "not_conclusive"  # not positively this operation's own record
    HANDED_TO_DRIVER = "handed_to_driver"  # the request driving it takes it from the inbox
    SETTLED = "settled"  # nobody was driving it: recorded here


class NotificationIntake:
    """Use case: use a MarketPay notification (the bonus webhook)."""

    def __init__(self, ctx: Context, store: StorePolicy, recovery: Recovery) -> None:
        self._repo = ctx.repo
        self._me = ctx.me
        self._inbox = ctx.inbox
        self._store = store
        self._recovery = recovery

    def accept(
        self, payment_id: UUID, operation: Operation, notification: Notification
    ) -> NotificationUse:
        """A notification whose signed URL checked out (webhook worker thread).

        Its final record settles the operation exactly as the same record from
        last-transaction would (domain.notifications): handed to the request driving it,
        or, if nobody is, recorded here. Anything else changes nothing; polling stays the
        fallback, since MarketPay never re-sends a notification.

        SECURITY: unauthenticated apart from the secret URL — see
        payments.infrastructure.marketpay.notification_urls before relying on this in production.
        """

        record = notification.as_record()
        if record is None:
            return NotificationUse.PROGRESS  # progress, or no usable outcome: logged only

        payment = self._store.run(lambda: self._repo.get(payment_id), what="get")
        if payment is None:
            log.warning("notification_for_unknown_payment", notified_payment_id=str(payment_id))

            return NotificationUse.UNKNOWN_PAYMENT

        if record.transaction_result is not None and (
            problem := result_inconsistency(record.transaction_result)
        ):
            log.error("marketpay_result_inconsistent", problem=problem, source="notification")

        if payment.operation is not operation:
            # Already settled (or moved on to its undo). If the notification disagrees with
            # what we recorded, a person should look; the re-check of inferred
            # outcomes may correct it.
            self._log_late_notification(payment, operation, record)

            return NotificationUse.ALREADY_SETTLED

        if notified_outcome(payment, operation, record) is None:
            log.warning("notification_not_conclusive", operation=operation)

            return NotificationUse.NOT_CONCLUSIVE

        self._inbox.push(payment.terminal_id, record)  # a poll in this process takes it

        if not self._recovery.orphaned(payment):
            log.info("notification_handed_to_driver", operation=operation)

            return NotificationUse.HANDED_TO_DRIVER
        updated = self._store.change(
            payment.id,
            lambda cur, now: apply_notified(cur, operation, record, self._me, now),
        )

        if updated.operation is operation:
            # Taken over in the meantime: that request gets the record from the inbox.
            return NotificationUse.HANDED_TO_DRIVER

        log.info("payment_settled_by_notification", **diagnostics(updated))

        return NotificationUse.SETTLED

    def _log_late_notification(
        self, payment: Payment, operation: Operation, record: LastTransactionResult
    ) -> None:
        seen = (
            observe_last_transaction(record, payment.reference)
            if operation is Operation.PURCHASE
            else None
        )

        notified = seen.resolution if seen and seen.resolution else None
        # Charged at some point: approved (or a PARTIAL), possibly reversed since. A final
        # notification also follows a 201, so agreement is the normal case.
        notified_charge = notified is not None and (
            notified.state is PaymentState.APPROVED
            or notified.state_reason is StateReason.PARTIAL_APPROVAL
        )

        recorded_charge = payment.state is PaymentState.APPROVED or payment.reversed
        if (
            notified is not None
            and payment.operation is None
            and notified_charge != recorded_charge
        ):
            log.error(
                "notification_contradicts_record",
                operation=operation,
                recorded_state=payment.state,
                notified_state=notified.state,
            )
        else:
            log.info("notification_after_settlement", operation=operation, state=payment.state)
