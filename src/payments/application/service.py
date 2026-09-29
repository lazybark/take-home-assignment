"""The application's entry point: one PaymentService per process, one use case per operation.

Pattern for everything that touches the terminal:
  1. record the intent atomically (and take the terminal lock),
  2. run the MarketPay operation (TerminalOperations),
  3. record the outcome atomically — applied to the *current* stored version.
"""

from datetime import datetime
from uuid import UUID, uuid4

from payments.application.cancel_payment import CancelPayment, CancelResult
from payments.application.clock import Clock, SystemClock
from payments.application.context import Context
from payments.application.notification_inbox import NotificationInbox
from payments.application.notifications import NotificationIntake, NotificationUse
from payments.application.queries import Queries
from payments.application.reconcile import Reconcile, ReconcileSummary
from payments.application.recovery import Recovery
from payments.application.store_policy import StorePolicy
from payments.application.take_payment import CreatePaymentResult, NewPayment, TakePayment
from payments.application.terminal_ops import TerminalOperations
from payments.application.undo import Undo
from payments.domain.history import HistoryEntry
from payments.domain.listing import PaymentPage, PaymentQuery
from payments.domain.marketpay.gateway import MarketPayGateway, NotificationAddresses
from payments.domain.marketpay.notification import Notification
from payments.domain.models import Operation, Owner, Payment
from payments.domain.repository import PaymentRepository


class PaymentService:
    """Wires the use cases together; the API calls only this."""

    def __init__(
        self,
        marketpay: MarketPayGateway,
        repo: PaymentRepository,
        clock: Clock | None = None,
        owner: Owner | None = None,
        notification_urls: NotificationAddresses | None = None,
    ) -> None:
        clock = clock or SystemClock()
        # Notified records for polls in this process (only with notifications configured).
        inbox = NotificationInbox(clock) if notification_urls else None
        ctx = Context(
            repo=repo,
            clock=clock,
            ops=TerminalOperations(marketpay, clock, inbox),
            # This process, as the owner of the operations it drives. A new boot id per start.
            me=owner or Owner(instance_id="default", boot_id=uuid4().hex),
            inbox=inbox,
            notification_urls=notification_urls,
        )
        store = StorePolicy(ctx)
        recovery = Recovery(ctx, store)
        undo = Undo(ctx, store, recovery)
        self._take = TakePayment(ctx, store, recovery, undo)
        self._cancel = CancelPayment(ctx, store, recovery, undo)
        self._reconcile = Reconcile(ctx, store, recovery)
        self._notifications = NotificationIntake(ctx, store, recovery)
        self._queries = Queries(ctx, store)

    def create_payment(self, new: NewPayment) -> CreatePaymentResult:
        return self._take.take(new)

    def get_payment(self, payment_id: UUID) -> Payment | None:
        return self._queries.get(payment_id)

    def list_payments(self, query: PaymentQuery) -> PaymentPage:
        return self._queries.page(query)

    def payment_history(self, payment_id: UUID) -> list[HistoryEntry] | None:
        return self._queries.history(payment_id)

    def cancel_payment(self, payment_id: UUID) -> CancelResult | None:
        return self._cancel.cancel(payment_id)

    def reconcile(
        self, terminal_id: str | None = None, older_than: datetime | None = None
    ) -> ReconcileSummary:
        return self._reconcile.run(terminal_id=terminal_id, older_than=older_than)

    def accept_notification(
        self, payment_id: UUID, operation: Operation, notification: Notification
    ) -> NotificationUse:
        return self._notifications.accept(payment_id, operation, notification)
