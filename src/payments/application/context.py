"""What every use case works with: the store, MarketPay, the clock, this process."""

from uuid import UUID

from payments.application.clock import Clock
from payments.application.notification_inbox import NotificationInbox
from payments.application.terminal_ops import TerminalOperations
from payments.domain.marketpay.gateway import NotificationAddresses
from payments.domain.marketpay.models import EcrParams
from payments.domain.models import Operation, Owner, Payment
from payments.domain.repository import PaymentRepository


class Context:
    """The collaborators shared by all use cases (one per process)."""

    def __init__(
        self,
        repo: PaymentRepository,
        clock: Clock,
        ops: TerminalOperations,
        me: Owner,
        inbox: NotificationInbox | None,
        notification_urls: NotificationAddresses | None,
    ) -> None:
        self.repo, self.clock, self.ops, self.me = repo, clock, ops, me
        self.inbox, self._notification_urls = inbox, notification_urls

    def ecr_params(self, payment_id: UUID, operation: Operation) -> EcrParams | None:
        """Where MarketPay should send this operation's notifications (if configured)."""

        if self._notification_urls is None:
            return None

        return EcrParams(
            notification_url=self._notification_urls.for_operation(payment_id, operation.value)
        )


def diagnostics(payment: Payment) -> dict:
    return {
        "state": payment.state,
        "operation": payment.operation,
        "owner": payment.owner.boot_id if payment.owner else None,
        "state_reason": payment.state_reason,
        "state_detail": payment.state_detail,
        "resolved_via": payment.resolved_via,
    }
