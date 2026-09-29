"""Pure rules for listing payments: filters, ordering and the page cursor.

Order: newest first (`created_at` descending, then id descending as the tie-breaker). The
cursor is an opaque position ("continue after this payment"); a page can hold fewer items
than `limit` and still have a next page when the store stopped scanning early.
"""

import base64
import binascii
import json
from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError

from payments.domain.models import Payment, PaymentState

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


class PageCursor(BaseModel):
    """Position in the listing order: the last payment the previous page looked at."""

    model_config = ConfigDict(frozen=True)

    created_at: datetime
    id: UUID


class PaymentQuery(BaseModel):
    model_config = ConfigDict(frozen=True)

    states: frozenset[PaymentState] | None = None
    terminal_id: str | None = None
    reference: str | None = None
    created_after: datetime | None = None  # exclusive
    created_before: datetime | None = None  # exclusive
    limit: int = DEFAULT_PAGE_SIZE
    after: PageCursor | None = None


class PaymentPage(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[Payment]
    next_cursor: PageCursor | None


def order_key(payment: Payment) -> tuple[datetime, str]:
    """Sort key for newest-first order (use with reverse=True)."""

    return (payment.created_at, str(payment.id))


def position_of(payment: Payment) -> PageCursor:
    return PageCursor(created_at=payment.created_at, id=payment.id)


def is_after(payment: Payment, cursor: PageCursor | None) -> bool:
    """Does `payment` come after `cursor` in newest-first order?"""

    if cursor is None:
        return True

    return order_key(payment) < (cursor.created_at, str(cursor.id))


def matches(payment: Payment, query: PaymentQuery) -> bool:
    return (
        (query.states is None or payment.state in query.states)
        and (query.terminal_id is None or payment.terminal_id == query.terminal_id)
        and (query.reference is None or payment.reference == query.reference)
        and (query.created_after is None or payment.created_at > query.created_after)
        and (query.created_before is None or payment.created_at < query.created_before)
    )


def encode_cursor(cursor: PageCursor) -> str:
    raw = json.dumps(cursor.model_dump(mode="json"), separators=(",", ":")).encode()

    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class InvalidCursor(ValueError):
    pass


def decode_cursor(token: str) -> PageCursor:
    try:
        raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

        return PageCursor.model_validate(json.loads(raw))

    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise InvalidCursor("cursor is not one this service issued") from exc


def scan_page(ordered: Iterable[Payment], query: PaymentQuery, scan_limit: int) -> PaymentPage:
    """Shared by both stores: walk payments in listing order, keep the matching ones.

    Looks for one match beyond `limit`, so a full last page doesn't advertise an empty
    next page. Stops after `scan_limit` documents; then the cursor resumes the scan.
    """

    items: list[Payment] = []
    last_seen: Payment | None = None

    for scanned, payment in enumerate(ordered, start=1):
        if matches(payment, query):
            if len(items) == query.limit:
                return PaymentPage(items=items, next_cursor=position_of(items[-1]))

            items.append(payment)
        last_seen = payment

        if scanned >= scan_limit:
            return PaymentPage(items=items, next_cursor=position_of(last_seen))

    return PaymentPage(items=items, next_cursor=None)
