"""Pure rules for re-checking outcomes we only inferred.

A few outcomes rest on *no record* rather than on MarketPay's own record of the transaction:
- `failed` / `aborted` after a 204 with no record of ours (resolved from the abort),
- `failed` / `never_recorded` (no record long after sending),
- `approved` / `undo_not_recorded` (we concluded a reversal never landed).
A request stuck in the network could still reach the terminal later and change the truth.
So the terminal is flagged when such a payment settles, and the next look at it (the next
payment on that terminal, before it sends anything, or a reconcile) checks once whether
the old payment's transaction turned up after all.

We never reverse a late charge automatically: a reversal needs the customer's tap,
and the terminal would show "Refund" to whoever is paying next. We correct the record to
what MarketPay holds (no record stays "failed" while a charge stands) and log an error so
a person can refund it.
"""

from datetime import datetime
from uuid import UUID

from payments.domain.cancel import ReversalSighting, UndoOutcome, observe_reversal
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.models import Payment, PaymentState, ResolvedVia, StateReason
from payments.domain.outcomes import Sighting, observe_last_transaction


def needs_verification(payment: Payment) -> bool:
    """Did this payment settle on an inference that a late request could overturn?"""

    if payment.operation is not None:
        return False

    if payment.state is PaymentState.FAILED:
        return payment.state_reason is StateReason.NEVER_RECORDED or (
            payment.state_reason is StateReason.ABORTED
            and payment.resolved_via is ResolvedVia.ABORT_RESPONSE
        )

    return (
        payment.state is PaymentState.APPROVED
        and payment.state_reason is StateReason.UNDO_NOT_RECORDED
    )


def flag_after_settling(new: Payment, current_flag: UUID | None) -> UUID | None:
    """The terminal's verification flag once `new` has released it."""

    return new.id if needs_verification(new) else current_flag


def verify_against(payment: Payment, last: LastTransactionResult, now: datetime) -> Payment:
    """Correct an inferred outcome if the terminal's record says otherwise. Pure; returns
    the payment unchanged when there's nothing to correct (or it no longer qualifies)."""

    if not needs_verification(payment):
        return payment

    if payment.state is PaymentState.FAILED:
        seen = observe_last_transaction(last, payment.reference)
        if (
            seen.sighting is Sighting.OURS_FINISHED
            and seen.resolution.state is PaymentState.APPROVED
        ):
            return payment.model_copy(
                update={
                    "state": PaymentState.APPROVED,
                    "state_reason": StateReason.LATE_CHARGE_FOUND,
                    "provider_transaction_id": seen.resolution.provider_transaction_id,
                    "resolved_via": ResolvedVia.LAST_TRANSACTION,
                    "state_detail": (
                        f"reported {payment.state_reason.value}, but the terminal later "
                        "recorded an approval: refund manually"
                    ),
                    "updated_at": now,
                }
            )

        return payment

    if payment.refund_reference is not None:
        # The latest undo was a REFUND: its own transaction, under its own reference.
        refund = observe_last_transaction(last, payment.refund_reference)
        if (
            refund.sighting is Sighting.OURS_FINISHED
            and refund.resolution.state is PaymentState.APPROVED
        ):
            return payment.model_copy(
                update={
                    "state": PaymentState.CANCELLED,
                    "reversed": True,
                    "state_reason": StateReason.LATE_REVERSAL_FOUND,
                    "resolved_via": ResolvedVia.LAST_TRANSACTION,
                    "state_detail": "reported still charged, but the refund landed later",
                    "updated_at": now,
                }
            )

        return payment

    seen = observe_reversal(
        last,
        payment.reference,
        payment.provider_transaction_id,
        payment.undo_baseline_transaction_id,
    )

    if (
        seen.sighting is ReversalSighting.REVERSAL_FINISHED
        and seen.result.outcome is UndoOutcome.DONE
    ):
        return payment.model_copy(
            update={
                "state": PaymentState.CANCELLED,
                "reversed": True,
                "state_reason": StateReason.LATE_REVERSAL_FOUND,
                "resolved_via": ResolvedVia.LAST_TRANSACTION,
                "state_detail": "reported still charged, but the reversal landed later",
                "updated_at": now,
            }
        )

    return payment
