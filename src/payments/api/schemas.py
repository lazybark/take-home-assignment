"""HTTP request/response bodies for payment-api.yaml (camelCase on the wire)."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

from payments.domain.history import HistoryEntry
from payments.domain.listing import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from payments.domain.marketpay.currency import ALPHA_TO_NUMERIC
from payments.domain.models import Operation, Payment, PaymentState, ResolvedVia, StateReason


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class CreatePaymentRequest(ApiModel):
    # MANUFACTURER:serial, manufacturer uppercase (MarketPay 404s on "pax:..."). No "/":
    # the id is also our Firestore document id for the terminal lock.
    terminal_id: str = Field(pattern=r"^[A-Z0-9]+:[^/\s]+$", max_length=128)
    amount: int = Field(ge=1)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    reference: str = Field(min_length=1, max_length=36)
    deadline_seconds: int = Field(default=60, ge=1, le=120)

    @field_validator("currency")
    @classmethod
    def _supported(cls, value: str) -> str:
        if value not in ALPHA_TO_NUMERIC:
            raise ValueError(f"unsupported currency; expected one of {sorted(ALPHA_TO_NUMERIC)}")

        return value


class PaymentResponse(ApiModel):
    id: UUID
    terminal_id: str
    amount: int
    currency: str
    reference: str
    state: PaymentState
    provider_transaction_id: str | None
    reversed: bool
    decline_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, payment: Payment) -> "PaymentResponse":
        return cls.model_validate(payment.model_dump())

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


class HistoryItem(ApiModel):
    """One change of a payment, for humans first (`summary`); the rest is for filtering."""

    number: int
    at: datetime
    state: PaymentState
    reason: StateReason | None
    based_on: ResolvedVia | None
    operation: Operation | None
    detail: str | None
    summary: str


class HistoryResponse(ApiModel):
    payment_id: UUID
    items: list[HistoryItem]

    @classmethod
    def from_domain(cls, payment_id: UUID, entries: list[HistoryEntry]) -> "HistoryResponse":
        return cls(
            payment_id=payment_id,
            items=[HistoryItem.model_validate(e.model_dump()) for e in entries],
        )

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)


class ReconcileRequest(ApiModel):
    terminal_id: str | None = None
    older_than: datetime | None = None

    @field_validator("older_than")
    @classmethod
    def _utc_if_naive(cls, value: datetime | None) -> datetime | None:
        """A time without a zone is UTC (as for the list's filters), so it compares with
        the stored UTC timestamps instead of failing."""
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class ReconcileSummaryResponse(ApiModel):
    scanned: int
    resolved: int
    still_open: int
    resolved_ids: list[UUID]


class ListPaymentsQuery(ApiModel):
    """Query string of GET /payments. `state` may repeat (?state=a&state=b) or be a
    comma-separated list (?state=a,b); both are accepted."""

    state: list[PaymentState] | None = None
    terminal_id: str | None = None
    reference: str | None = None
    created_after: datetime | None = None
    created_before: datetime | None = None
    limit: int = Field(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE)
    cursor: str | None = None

    @field_validator("created_after", "created_before")
    @classmethod
    def _utc_if_naive(cls, value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


class PaymentListResponse(ApiModel):
    items: list[PaymentResponse]
    next_cursor: str | None

    def to_json(self) -> dict:
        return self.model_dump(mode="json", by_alias=True)
