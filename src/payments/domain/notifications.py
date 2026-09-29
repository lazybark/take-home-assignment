"""Pure rules for MarketPay notifications (the bonus webhook).

A final notification carries the terminal's record of one transaction. We turn it into the
shape last-transaction returns (`Notification.as_record`) and read it with the SAME observer
functions polling uses, so the outcome is the same whichever way it arrives. Two limits:
- a notification can only *settle* an operation, with a record that is positively its own;
  it never counts as "no record of ours" (that would conclude a failure from a message that
  may have been about something else, or reordered);
- it settles directly only an operation nobody is driving (UNKNOWN, orphaned). A live request
  is handed the record instead (the inbox in TerminalOperations), because writing behind its
  back could free the terminal while it is about to send an abort.

SECURITY: a notification is unauthenticated (see
payments.infrastructure.marketpay.notification_urls). Trusting it rests on the secret, signed
URL alone.
"""

from datetime import datetime

from payments.domain.cancel import ReversalSighting, UndoResult, observe_reversal, resolve_refund
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.models import Operation, Owner, Payment, Resolution, ResolvedVia
from payments.domain.outcomes import Sighting, observe_last_transaction
from payments.domain.recovery import is_orphaned
from payments.domain.transitions import apply_purchase, apply_undo


def notified_outcome(
    payment: Payment, operation: Operation, record: LastTransactionResult
) -> Resolution | UndoResult | None:
    """What this notified record settles for the payment's running `operation`.

    None: it says nothing definite (not this operation's own record, or the operation is
    no longer running). Polling then decides, as it would without notifications.
    """

    if payment.operation is not operation:
        return None

    match operation:
        case Operation.PURCHASE:
            seen = observe_last_transaction(record, payment.reference)

            if seen.sighting is Sighting.OURS_FINISHED:
                return seen.resolution.model_copy(update={"resolved_via": ResolvedVia.NOTIFICATION})

        case Operation.REVERSAL if payment.undo_started_at is not None:
            # Staging shows a reversal as a transaction with our ecrTransactionId and a new
            # terminalTransactionId: the same check as for last-transaction.
            reversal = observe_reversal(
                record,
                payment.reference,
                payment.provider_transaction_id,
                payment.undo_baseline_transaction_id,
            )

            if reversal.sighting is ReversalSighting.REVERSAL_FINISHED:
                return reversal.result.model_copy(update={"via": ResolvedVia.NOTIFICATION})

        case Operation.REFUND if (
            payment.undo_started_at is not None and payment.refund_reference is not None
        ):
            seen = observe_last_transaction(record, payment.refund_reference)
            if seen.sighting is Sighting.OURS_FINISHED:
                return resolve_refund(seen.resolution).model_copy(
                    update={"via": ResolvedVia.NOTIFICATION}
                )

    return None


def apply_notified(
    current: Payment,
    operation: Operation,
    record: LastTransactionResult,
    me: Owner,
    now: datetime,
) -> Payment:
    """Transition: settle an operation nobody drives from its notified record.

    Decided from the *current* stored version: if a request took the payment over in the
    meantime (reconcile, a POS retry), it is left to that request, which gets the record
    through the inbox.
    """

    if not is_orphaned(current, me, now):
        return current

    outcome = notified_outcome(current, operation, record)
    if isinstance(outcome, Resolution):
        return apply_purchase(current, outcome, now)

    if isinstance(outcome, UndoResult):
        return apply_undo(current, outcome, now)

    return current
