"""Payloads exchanged with the MarketPay Cloud API (v1.1.14, see api.json).

Where staging behaves differently from the spec, the comments say how.
"""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class MarketPayModel(BaseModel):
    """camelCase on the wire; unknown fields are ignored so new provider fields don't break us.

    Numbers are accepted where we keep strings: the same field comes as "100" from
    last-transaction but as 100 in a notification, and must read the same.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="ignore",
        frozen=True,
        coerce_numbers_to_str=True,
    )

    def to_wire(self) -> dict:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


class TerminalSession(MarketPayModel):
    terminal_id: str
    ws_created_time: datetime | None = None
    connected: bool | None = None


class TransactionType(StrEnum):
    PURCHASE = "PURCHASE"
    REFUND = "REFUND"


class TransactionStatus(StrEnum):
    OK = "OK"
    NOK = "NOK"
    PARTIAL = "PARTIAL"


class EcrParams(MarketPayModel):
    ecr_id: str | None = None
    notification_url: str | None = None


class ProcessTransactionRequest(MarketPayModel):
    ecr_transaction_id: str  # our idempotency key (max 36)
    amount: str  # minor units, as a STRING
    currency: str  # ISO 4217 numeric, as a string ("752")
    transaction_type: TransactionType = TransactionType.PURCHASE
    ecr_params: EcrParams | None = None


class CancelTransactionRequest(MarketPayModel):
    """Reverse an approved payment. All fields refer to the ORIGINAL payment.

    Staging, not in the spec:
    - the terminal runs it as a card-present REFUND: the customer must TAP THE CARD AGAIN,
      and it takes 10–20 s. It can time out like a payment if they've gone;
    - afterwards last-transaction shows it as a normal transactionResult with the SAME
      ecrTransactionId as the purchase and a NEW terminalTransactionId — no
      cancellationResult;
    - any transaction can be reversed, not only the terminal's last one.
    """

    terminal_transaction_id: str  # from the original payment's result
    ecr_transaction_id: str  # the original payment's ecrTransactionId
    amount: str  # the original amount, as a string
    currency: str  # ISO 4217 numeric, as a string
    ecr_params: EcrParams | None = None


class FinalTransactionParams(MarketPayModel):
    """MarketPay's echo of the request: how we know which transaction a result belongs to.

    Only ecrTransactionId is reliable: on a NOK, staging echoes amount/currency as "0".
    """

    ecr_transaction_id: str
    amount: str | None = None
    currency: str | None = None
    # The spec says required; staging never echoes it, on any kind of record. Never
    # relied on: a reversal is told from its purchase by terminalTransactionId.
    # (A notification echoes it as `type`; not read either, for the same reason.)
    transaction_type: TransactionType | None = None


class CardData(MarketPayModel):
    card_capture: str | None = None
    extracted_pan: str | None = None  # masked, e.g. "XXXXXXXXXXXX8555"


class TransactionResult(MarketPayModel):
    # The spec marks responseCode required, but real NOK results omit it (seen on staging).
    response_code: str | None = None  # "000" = approved; anything else is a decline code
    status: TransactionStatus | None = None
    terminal_transaction_id: str | None = None
    authorization_code: str | None = None
    final_transaction_params: FinalTransactionParams | None = None
    card_data: CardData | None = None


class LastTransactionState(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    IN_PROGRESS = "IN_PROGRESS"
    FINISHED = "FINISHED"


class CancellationParams(MarketPayModel):
    terminal_transaction_id: str | None = None
    ecr_transaction_id: str | None = None


class CancellationResult(MarketPayModel):
    status: TransactionStatus | None = None
    cancellation_params: CancellationParams | None = None


class LastTransactionResult(MarketPayModel):
    """GET /last-transaction: the terminal's *latest* operation only — maybe not ours.

    MarketPay may even return the one-before-last if ours failed very late, so the
    ecrTransactionId inside must be checked before trusting anything here.

    Staging, not in the spec:
    - a RUNNING transaction is never shown: it keeps returning the previous, FINISHED one
      until ours ends. "Not ours" therefore never proves "never arrived";
    - IN_PROGRESS was never observed, nor was cancellationResult;
    - overlapping calls for one terminal all fail with HTTP 500: see the client;
    - on NOK, responseCode is missing and amount/currency are echoed as "0".
    """

    last_transaction_state: LastTransactionState | None = None
    transaction_result: TransactionResult | None = None
    cancellation_result: CancellationResult | None = None
