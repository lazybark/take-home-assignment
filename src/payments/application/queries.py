"""Read payments: GET /payments, GET /payments/{id} and its history."""

from uuid import UUID

import structlog

from payments.application.context import Context
from payments.application.store_policy import StorePolicy
from payments.domain.history import HistoryEntry
from payments.domain.listing import PaymentPage, PaymentQuery
from payments.domain.models import Payment

log = structlog.get_logger(__name__)


# Documents one GET /payments may examine; sparse filters then page on via nextCursor.
LIST_SCAN_LIMIT = 2000


class Queries:
    """Use case: read payments. Stored state only: reads never ask MarketPay."""

    def __init__(self, ctx: Context, store: StorePolicy) -> None:
        self._repo = ctx.repo
        self._store = store

    def page(self, query: PaymentQuery) -> PaymentPage:
        """Stored state only — no MarketPay lookups: fast, and a stable snapshot for
        back-office views and test suites."""

        return self._store.run(
            lambda: self._repo.list_payments(query, LIST_SCAN_LIMIT), what="list_payments"
        )

    def get(self, payment_id: UUID) -> Payment | None:
        """The stored record, as is: reads never ask MarketPay. Our store is the one
        source of truth callers see; an unresolved payment converges through an explicit
        action (POST /reconcile, a POS retry, a cancel, the next payment on its terminal),
        so a GET can't change what another reader saw a moment ago."""

        return self._store.run(lambda: self._repo.get(payment_id), what="get")

    def history(self, payment_id: UUID) -> list[HistoryEntry] | None:
        """How the payment got to its state, oldest first; None if there is no such payment."""

        if self.get(payment_id) is None:
            return None

        return self._store.run(lambda: self._repo.history(payment_id), what="history")
