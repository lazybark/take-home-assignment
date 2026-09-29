"""Domain objects: what a payment *is*, independent of HTTP, Firestore or MarketPay."""

from datetime import datetime
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict

# Payment ids are derived from the POS reference, so the same order always maps to the same
# Firestore document. That makes "have we seen this order?" one atomic write (the `begin`
# transaction).
_PAYMENT_NAMESPACE = uuid5(NAMESPACE_URL, "payments/reference")


def payment_id_for(reference: str) -> UUID:
    return uuid5(_PAYMENT_NAMESPACE, reference)


class PaymentState(StrEnum):
    PENDING = "pending"  # in progress on the terminal
    APPROVED = "approved"  # committed and standing
    DECLINED = "declined"  # the card was declined
    FAILED = "failed"  # no standing charge
    CANCELLED = "cancelled"  # reversed on an explicit POS cancel
    UNKNOWN = "unknown"  # outcome not established yet; must be resolved against MarketPay


# States backed by a confirmed MarketPay answer.
FINAL_STATES = frozenset(
    {PaymentState.APPROVED, PaymentState.DECLINED, PaymentState.FAILED, PaymentState.CANCELLED}
)


class Operation(StrEnum):
    """What we are currently running on the terminal for this payment.

    While an operation is set, the payment holds the terminal lock: its outcome is (or may
    become) the terminal's "last transaction", the only evidence MarketPay will give us.
    """

    PURCHASE = "purchase"  # the payment itself
    REVERSAL = "reversal"  # cancel-transaction of an approved payment
    REFUND = "refund"  # a REFUND transaction (after a payment completed despite an abort)


class UndoReason(StrEnum):
    """Why a reversal/refund of this payment runs: it decides the state it ends in."""

    POS_CANCEL = "pos_cancel"  # the POS asked to cancel -> CANCELLED
    PARTIAL_APPROVAL = "partial_approval"  # a PARTIAL we must not keep -> DECLINED


class Owner(BaseModel):
    """Which running process drives an operation.

    `boot_id` is new on every process start: an operation owned by the same instance under
    an older boot was abandoned by a crash/restart and can be taken over at once.
    """

    model_config = ConfigDict(frozen=True)

    instance_id: str
    boot_id: str


def refund_reference_for(payment_id: UUID, attempt: int) -> str:
    """ecrTransactionId for a payment's n-th refund attempt: deterministic, ≤ 36 chars.

    Each attempt needs its own id: MarketPay treats ecrTransactionId as idempotency key.
    """
    return f"rf{attempt:02d}{payment_id.hex}"


class StateReason(StrEnum):
    """Internal: *why* a payment is in its state. Our labels, never MarketPay's codes."""

    BANK_APPROVED = "bank_approved"  # OK
    BANK_DECLINED = "bank_declined"  # NOK with an acquirer responseCode
    TERMINAL_STOPPED = "terminal_stopped"  # NOK, no responseCode: stopped before the bank
    #                                         (cancel pressed, no card in time, abort)
    ABORTED = "aborted"  # stopped by our abort-transaction at the deadline; nothing charged
    LATE_CHARGE_FOUND = "late_charge_found"  # reported failed; a charge turned up later
    LATE_REVERSAL_FOUND = "late_reversal_found"  # reported charged; the reversal landed later
    NEVER_RECORDED = "never_recorded"  # long after it was sent, still no record of it:
    #                                     it never ran, or "failed very late" — no charge
    NOT_SENT = "not_sent"  # connection failed before the request left us
    PROVIDER_REJECTED = "provider_rejected"  # 4xx from process-transaction
    AWAITING_RESULT = "awaiting_result"  # 202 / lost response, not resolved yet
    PARTIAL_APPROVAL = "partial_approval"  # PARTIAL: a charge of unknown size may stand
    NOT_OUR_TRANSACTION = "not_our_transaction"  # a result echoing another ecrTransactionId
    UNRECOGNISED_RESULT = "unrecognised_result"  # no status in the result
    # Cancellation (POST /payments/{id}/cancel)
    CANCELLED_BEFORE_CHARGE = "cancelled_before_charge"  # stopped on the terminal, POS cancel
    REVERSED = "reversed"  # cancel-transaction OK
    REFUNDED = "refunded"  # REFUND transaction OK
    UNDO_REFUSED = "undo_refused"  # reversal/refund definitively not done: charge stands
    UNDO_NOT_SENT = "undo_not_sent"  # reversal/refund never reached MarketPay: charge stands
    UNDO_NOT_RECORDED = "undo_not_recorded"  # terminal shows no reversal/refund: charge stands
    AWAITING_UNDO = "awaiting_undo"  # reversal/refund outcome not visible yet
    PARTIAL_REVERSAL = "partial_reversal"  # PARTIAL cancellation: needs manual follow-up
    REFUND_DUE = "refund_due"  # approved despite a cancel; a refund must be run (cancel again)
    # PARTIAL purchase (an approval of an unknown, smaller amount): we reverse it.
    PARTIAL_APPROVAL_REVERSED = "partial_approval_reversed"  # reversed -> DECLINED
    PARTIAL_NOT_REVERSED = "partial_not_reversed"  # could not reverse: needs a person


class ResolvedVia(StrEnum):
    """Internal: which MarketPay answer the state is based on."""

    PROCESS_RESPONSE = "process_response"
    LAST_TRANSACTION = "last_transaction"
    ABORT_RESPONSE = "abort_response"
    CANCEL_RESPONSE = "cancel_response"
    NOTIFICATION = "notification"  # MarketPay's notification webhook (the bonus)


class Payment(BaseModel):
    """Immutable; state changes produce a new copy (`model_copy(update=...)`)."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    terminal_id: str
    amount: int  # minor units (öre)
    currency: str  # ISO 4217 alpha, e.g. "SEK"
    reference: str  # POS order id; sent to MarketPay as ecrTransactionId
    state: PaymentState
    provider_transaction_id: str | None = None  # MarketPay terminalTransactionId
    reversed: bool = False
    decline_reason: str | None = None  # MarketPay's responseCode verbatim, or None
    created_at: datetime
    updated_at: datetime
    # Internal diagnostics (stored, logged, not part of the API contract).
    state_reason: StateReason | None = None
    state_detail: str | None = None  # free text, e.g. "HTTP 404"
    resolved_via: ResolvedVia | None = None
    history_length: int = 0  # entries in its history so far (domain.history)
    # Internal coordination (stored, not part of the API contract).
    operation: Operation | None = None  # set <=> this payment holds the terminal lock
    owner: Owner | None = None  # the process driving `operation`; None: nobody is
    # The request whose claim holds `operation` (a take-over, an undo claim). The owner
    # names a process; this tells two requests of one process apart (StorePolicy.won).
    claim_id: str | None = None
    lease_until: datetime | None = None  # after this, the owner is presumed dead
    deadline_at: datetime | None = None  # when the POS stops waiting for the purchase
    created_by: str | None = None  # unique id of the request that created it
    abort_requested_at: datetime | None = None  # we asked the terminal to abort it
    cancel_requested_at: datetime | None = None  # the POS asked to cancel it
    undo_started_at: datetime | None = None  # a reversal/refund was claimed and sent
    undo_attempts: int = 0
    undo_reason: UndoReason | None = None
    # terminalTransactionId of the terminal's last record just before this reversal was
    # sent. A reversal shows up as a record with *our* ecrTransactionId and a *new*
    # terminalTransactionId: new = neither the purchase's nor this baseline.
    undo_baseline_transaction_id: str | None = None
    refund_reference: str | None = None  # ecrTransactionId of the current REFUND attempt


class Resolution(BaseModel):
    """What we learned about a payment from MarketPay. Field names match `Payment`."""

    model_config = ConfigDict(frozen=True)

    state: PaymentState
    state_reason: StateReason
    resolved_via: ResolvedVia
    provider_transaction_id: str | None = None
    decline_reason: str | None = None
    state_detail: str | None = None
    # We sent an abort for this transaction. If it completed anyway, MarketPay requires a
    # REFUND to undo it, not a reversal: recorded even if the write-ahead note was lost.
    abort_sent: bool = False


class TerminalLock(BaseModel):
    """Held while a payment has an operation in flight, until its outcome is confirmed.

    MarketPay can only report the *last* transaction on a terminal, so a next payment
    must never start while the previous one is unresolved — it would erase the evidence.
    """

    model_config = ConfigDict(frozen=True)

    terminal_id: str
    payment_id: UUID
    reference: str
    locked_at: datetime
