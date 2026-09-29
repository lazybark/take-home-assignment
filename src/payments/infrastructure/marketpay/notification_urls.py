"""MarketPay notifications: the `ecrParams.notificationUrl` webhook (the bonus; integration
guide §3). Every operation we send carries a signed URL; a final notification is turned into
a last-transaction record and handled by the same rules as polling (domain.notifications).

What the guide and the spec promise, and what staging does:
- progress notifications (WAITING_FOR_CARD, PIN_REQUIRED, BANK_AUTHORIZATION) for every
  transaction: never sent on staging;
- the final result (COMPLETED + `result`) for a transaction that answered 202; staging sends
  it after a 201 too, so it also covers a 201 lost on its way back;
- delivered once, never retried, and nobody checks our URL beforehand: polling
  last-transaction stays the source we rely on;
- the call is NOT authenticated.

SECURITY: the only proof that a notification came from MarketPay is that its URL is secret.
Each URL carries an HMAC of (payment id, operation) under NOTIFICATION_SECRET, so it can't
be guessed or reused for another payment or operation, but anyone who learns a URL (a log,
a proxy) can post a fake result for that one operation. Before trusting a notification in
production, add what MarketPay can offer: an IP allowlist of its senders, a signature header,
mTLS towards us (an open question for MarketPay). Until then the signature must never be logged.
"""

import hashlib
import hmac
import re
from uuid import UUID

import structlog

log = structlog.get_logger(__name__)

WEBHOOK_PREFIX = "/webhooks/marketpay"

# Our own logs must not leak a working URL: the signature is masked wherever paths are logged.
_SIGNED_PATH = re.compile(rf"({re.escape(WEBHOOK_PREFIX)}/[^/\s]+/[^/\s]+/)[^/\s?\"]+")


def mask_signature(text: str) -> str:
    return _SIGNED_PATH.sub(r"\1<signature>", text)


class NotificationUrls:
    """Builds and checks the per-operation notification URLs."""

    def __init__(self, base_url: str, secret: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._secret = secret.encode()

    def for_operation(self, payment_id: UUID, operation: str) -> str:
        """The URL MarketPay should notify about this operation (purchase, reversal, refund)."""

        return (
            f"{self._base_url}{WEBHOOK_PREFIX}/{payment_id}/{operation}/"
            f"{self._sign(payment_id, operation)}"
        )

    def verify(self, payment_id: UUID, operation: str, signature: str) -> bool:
        return hmac.compare_digest(self._sign(payment_id, operation), signature)

    def _sign(self, payment_id: UUID, operation: str) -> str:
        message = f"{payment_id}:{operation}".encode()

        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()


# Receipts and card data carry the card's BIN and last four: never logged.
_CARD_DATA_KEYS = {"customerReceipt", "cashierReceipt", "cardData"}


def redact_card_data(value):
    if isinstance(value, dict):
        return {
            key: "<redacted>" if key in _CARD_DATA_KEYS else redact_card_data(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [redact_card_data(item) for item in value]

    return value
