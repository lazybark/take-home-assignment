"""What a MarketPay call that *changes* something can tell us.

Only `Completed` carries an outcome. `Accepted` (202) and `Ambiguous` (read timeout, reset
after sending, 5xx, unparseable 201) mean "the payment may or may not have happened" — the
caller must find out via last-transaction, never guess. `NotSent` means the connection
failed before the request left us (DNS, TCP/TLS connect): MarketPay never saw it.
`Rejected` is a definitive 4xx answer from MarketPay.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from payments.domain.marketpay.models import (
    CancellationResult,
    LastTransactionResult,
    TransactionResult,
)


class _Outcome(BaseModel):
    model_config = ConfigDict(frozen=True)


class Completed(_Outcome):
    kind: Literal["completed"] = "completed"
    result: TransactionResult


class Accepted(_Outcome):
    kind: Literal["accepted"] = "accepted"


class Rejected(_Outcome):
    kind: Literal["rejected"] = "rejected"
    status_code: int
    body: str


class Ambiguous(_Outcome):
    kind: Literal["ambiguous"] = "ambiguous"
    reason: str


class NotSent(_Outcome):
    kind: Literal["not_sent"] = "not_sent"
    reason: str


ProcessOutcome = Completed | Accepted | Rejected | Ambiguous | NotSent


class CancelCompleted(_Outcome):
    """cancel-transaction answered 200 with the cancellation's result."""

    kind: Literal["cancel_completed"] = "cancel_completed"
    result: CancellationResult


CancelOutcome = CancelCompleted | Accepted | Rejected | Ambiguous | NotSent


# --- Read-only lookups (safe to repeat) ---


class Found(_Outcome):
    kind: Literal["found"] = "found"
    result: LastTransactionResult


class LookupFailed(_Outcome):
    """No usable answer this time (network, 5xx, 404...). Says nothing about the payment."""

    kind: Literal["lookup_failed"] = "lookup_failed"
    reason: str


LookupOutcome = Found | LookupFailed


# --- abort-transaction ---


class Aborted(_Outcome):
    """204: the terminal stopped the transaction in progress."""

    kind: Literal["aborted"] = "aborted"


class TooLate(_Outcome):
    """409: too late to abort — the payment may already have completed."""

    kind: Literal["too_late"] = "too_late"


class AbortRefused(_Outcome):
    """Another 4xx (e.g. 404: nothing to abort, or terminal not connected)."""

    kind: Literal["abort_refused"] = "abort_refused"
    status_code: int


class AbortUnconfirmed(_Outcome):
    """No usable answer (network, 5xx): we don't know whether the abort landed."""

    kind: Literal["abort_unconfirmed"] = "abort_unconfirmed"
    reason: str


AbortOutcome = Aborted | TooLate | AbortRefused | AbortUnconfirmed
