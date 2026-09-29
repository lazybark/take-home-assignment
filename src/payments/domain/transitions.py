"""Pure payment transitions: current version in, next version out.

They run inside the store's transaction (which may re-run them), so they must be pure and
must decide from the *current* stored version — never from a copy a request held earlier.
Returning the payment unchanged means "nothing to do".
"""

from datetime import datetime

from payments.domain.cancel import UndoOutcome, UndoResult
from payments.domain.models import (
    FINAL_STATES,
    Operation,
    Owner,
    Payment,
    PaymentState,
    Resolution,
    ResolvedVia,
    StateReason,
    UndoReason,
    refund_reference_for,
)

# Outcomes where nothing reached the bank: after a POS cancel, these are "cancelled".
_STOPPED_ON_TERMINAL = {StateReason.TERMINAL_STOPPED, StateReason.ABORTED}

# Recording an outcome ends the driving request's ownership: if the operation is still
# unresolved, nobody drives it any more, which makes it orphaned (see domain.recovery).
_NO_OWNER = {"owner": None, "lease_until": None}


def apply_purchase(current: Payment, resolution: Resolution, now: datetime) -> Payment:
    """Record what MarketPay says about the purchase itself."""

    if current.operation is not Operation.PURCHASE:
        return current  # already settled by someone else (a recheck, a cancel): keep theirs

    update = {**resolution.model_dump(exclude={"abort_sent"}), **_NO_OWNER, "updated_at": now}
    if resolution.abort_sent and current.abort_requested_at is None:
        update["abort_requested_at"] = now  # the write-ahead note was lost: keep the fact
    if resolution.state in FINAL_STATES:
        update["operation"] = None  # confirmed: the terminal is free again

    if resolution.state_reason is StateReason.PARTIAL_APPROVAL:
        # The request that learned it goes on to reverse it: it stays the owner, so no
        # look from elsewhere releases the reversal meanwhile. Learned by anyone else (a
        # late answer, a recheck), there was no owner, and it's released on the next look.
        keep_owner = {"owner": current.owner, "lease_until": current.lease_until}
        return current.model_copy(update=update | keep_owner | _partial_reversal_due(resolution))

    if current.cancel_requested_at is not None:
        if resolution.state_reason in _STOPPED_ON_TERMINAL:
            # The POS asked to cancel and the terminal stopped before any charge.
            update |= {
                "state": PaymentState.CANCELLED,
                "state_reason": StateReason.CANCELLED_BEFORE_CHARGE,
            }
        elif resolution.state is PaymentState.APPROVED:
            # Completed despite the abort: MarketPay requires a REFUND to undo it. Keep the
            # terminal: the refund must be the very next transaction on it.
            update["operation"] = Operation.REFUND  # due; `claim_undo` sends it

    return current.model_copy(update=update)


def _partial_reversal_due(resolution: Resolution) -> dict:
    """A PARTIAL is reversed, then reported DECLINED. It is always a reversal
    (cancel-transaction, by terminalTransactionId), never a REFUND: a refund names an
    amount, and MarketPay never tells us how much of a PARTIAL was approved."""

    if resolution.provider_transaction_id is None:
        # Nothing to reverse it by. The terminal's record is final: free the terminal,
        # and leave the charge (of unknown size) to a person.
        return {
            "state": PaymentState.UNKNOWN,
            "operation": None,
            "state_reason": StateReason.PARTIAL_NOT_REVERSED,
            "state_detail": "PARTIAL without a terminalTransactionId: cannot be reversed",
        }

    # Keep the terminal: the reversal must be the very next transaction on it.
    return {
        "state": PaymentState.UNKNOWN,  # a charge of unknown size stands until reversed
        "operation": Operation.REVERSAL,
        "undo_reason": UndoReason.PARTIAL_APPROVAL,
        "undo_started_at": None,  # due; `claim_undo` sends it
    }


def abandon_unsent(current: Payment, now: datetime) -> Payment:
    """A purchase we decided not to send after all (nothing reached MarketPay): it fails
    as not sent, and frees the terminal. Unchanged once anything may have been sent."""

    if current.operation is not Operation.PURCHASE or current.abort_requested_at is not None:
        return current

    return apply_purchase(
        current,
        Resolution(
            state=PaymentState.FAILED,
            state_reason=StateReason.NOT_SENT,
            resolved_via=ResolvedVia.PROCESS_RESPONSE,
            state_detail="not sent: an earlier payment's correction could not be stored first",
        ),
        now,
    )


def settle_without_operation(current: Payment, resolution: Resolution, now: datetime) -> Payment:
    """An open payment that holds no operation (e.g. a record from before operations
    existed) learns its final outcome. Never takes the terminal lock."""

    if current.operation is not None or current.state not in (
        PaymentState.PENDING,
        PaymentState.UNKNOWN,
    ):
        return current

    if resolution.state not in FINAL_STATES:
        return current

    return current.model_copy(
        update={**resolution.model_dump(exclude={"abort_sent"}), "updated_at": now}
    )


def mark_abort_requested(current: Payment, now: datetime) -> Payment:
    """Write-ahead record, before we send abort-transaction for the purchase."""

    if current.operation is not Operation.PURCHASE or current.abort_requested_at is not None:
        return current

    return current.model_copy(update={"abort_requested_at": now, "updated_at": now})


def request_cancel(current: Payment, now: datetime) -> Payment:
    """The POS asked to cancel a purchase still in flight (we are about to abort it)."""

    if current.operation is not Operation.PURCHASE or current.cancel_requested_at is not None:
        return current

    return current.model_copy(
        update={
            "cancel_requested_at": now,
            "abort_requested_at": current.abort_requested_at or now,
            "updated_at": now,
        }
    )


def undo_operation(payment: Payment) -> Operation:
    """How to undo an approved payment (a POS cancel).

    MarketPay: a payment that completed despite an abort request must be undone with a
    REFUND; so must one whose terminalTransactionId we never learned (cancel needs it).
    Either way the customer must tap the card again (the REFUND path is untested
    live). A REFUND names an amount — the full original one is right here, because
    the payment was approved in full. A PARTIAL never comes through here: its approved
    amount is unknown, so it is always reversed by id (`_partial_reversal_due`).
    """

    if payment.abort_requested_at is not None or payment.provider_transaction_id is None:
        return Operation.REFUND

    return Operation.REVERSAL


def claim_undo(
    current: Payment,
    now: datetime,
    owner: Owner,
    lease_until: datetime,
    claim_id: str | None = None,
) -> Payment:
    """Take the terminal to undo a payment. Exactly one request wins the claim: the one
    whose `claim_id` the stored version then carries.

    Either starts a new undo (a POS cancel of an approved payment, nothing in flight), or
    claims one that is due but not yet sent (set by `apply_purchase`: a REFUND after an
    abort, or the reversal of a PARTIAL). Anything else: unchanged.
    """

    if current.undo_started_at is not None:
        return current

    if current.operation is None and current.state is PaymentState.APPROVED:
        operation, reason = undo_operation(current), UndoReason.POS_CANCEL
    elif current.operation in (Operation.REFUND, Operation.REVERSAL):
        operation, reason = current.operation, current.undo_reason or UndoReason.POS_CANCEL
    else:
        return current

    attempt = current.undo_attempts + 1
    update = {
        "undo_baseline_transaction_id": None,  # taken afresh for each attempt
        "undo_reason": reason,
        "operation": operation,
        "owner": owner,
        "lease_until": lease_until,
        "cancel_requested_at": (
            current.cancel_requested_at or now
            if reason is UndoReason.POS_CANCEL
            else current.cancel_requested_at
        ),
        "undo_started_at": now,
        "undo_attempts": attempt,
        "claim_id": claim_id,
        "updated_at": now,
    }

    # The reference of the latest refund attempt, or None: a later reversal attempt clears
    # it, so the stored payment always says which kind its latest undo was.
    update["refund_reference"] = (
        refund_reference_for(current.id, attempt) if operation is Operation.REFUND else None
    )

    return current.model_copy(update=update)


def record_undo_baseline(current: Payment, transaction_id: str | None, now: datetime) -> Payment:
    """Write-ahead, before sending a reversal: the terminal's last record right now."""

    if (
        current.operation is not Operation.REVERSAL
        or transaction_id is None
        or current.undo_baseline_transaction_id == transaction_id
    ):
        return current

    return current.model_copy(
        update={"undo_baseline_transaction_id": transaction_id, "updated_at": now}
    )


def apply_undo(current: Payment, result: UndoResult, now: datetime) -> Payment:
    """Record what MarketPay says about the reversal/refund."""

    if current.operation not in (Operation.REVERSAL, Operation.REFUND):
        return current

    base = {
        **_NO_OWNER,
        "state_reason": result.reason,
        "state_detail": result.detail,
        "resolved_via": result.via,
        "updated_at": now,
    }

    if current.undo_reason is UndoReason.PARTIAL_APPROVAL:
        return _apply_partial_reversal(current, result, base)

    match result.outcome:
        case UndoOutcome.DONE:
            return current.model_copy(
                update=base | {"state": PaymentState.CANCELLED, "reversed": True, "operation": None}
            )

        case UndoOutcome.NOT_DONE:
            # The charge still stands. Free the terminal; allow another cancel attempt.
            return current.model_copy(
                update=base
                | {"state": PaymentState.APPROVED, "operation": None, "undo_started_at": None}
            )

        case UndoOutcome.PARTIAL:
            # The terminal's record is final, but only part may have been undone.
            return current.model_copy(
                update=base | {"state": PaymentState.UNKNOWN, "operation": None}
            )

        case UndoOutcome.UNCLEAR:
            # Keep the terminal: the evidence must survive until this is resolved.
            return current.model_copy(update=base | {"state": PaymentState.UNKNOWN})


def _apply_partial_reversal(current: Payment, result: UndoResult, base: dict) -> Payment:
    """The outcome of reversing a PARTIAL."""

    match result.outcome:
        case UndoOutcome.DONE:
            # Nothing stands any more: report it as declined, so the waiter asks for another
            # card. declineReason keeps MarketPay's code from the PARTIAL result, verbatim.
            return current.model_copy(
                update=base
                | {
                    "state": PaymentState.DECLINED,
                    "reversed": True,
                    "operation": None,
                    "state_reason": StateReason.PARTIAL_APPROVAL_REVERSED,
                }
            )

        case UndoOutcome.UNCLEAR:
            # Keep the terminal: the evidence must survive until this is resolved.
            return current.model_copy(update=base | {"state": PaymentState.UNKNOWN})

        case _:
            # Refused, never sent, or only partly undone: a charge of unknown size may
            # stand. The terminal's record is final, so free it; a person settles the rest.
            return current.model_copy(
                update=base
                | {
                    "state": PaymentState.UNKNOWN,
                    "operation": None,
                    "state_reason": StateReason.PARTIAL_NOT_REVERSED,
                    "state_detail": f"{result.reason.value}: {result.detail or 'no detail'}",
                }
            )


def release_partial_due(current: Payment, now: datetime) -> Payment:
    """Recovery found a PARTIAL whose reversal was never sent. It does not start a card
    transaction on its own (a reversal needs the customer's tap): free the terminal
    and leave the charge to a person."""

    if (
        current.operation is not Operation.REVERSAL
        or current.undo_reason is not UndoReason.PARTIAL_APPROVAL
        or current.undo_started_at is not None
    ):
        return current

    return current.model_copy(
        update={
            **_NO_OWNER,
            "operation": None,
            "state": PaymentState.UNKNOWN,
            "state_reason": StateReason.PARTIAL_NOT_REVERSED,
            "state_detail": "reversal due but never sent; not started by recovery",
            "updated_at": now,
        }
    )
