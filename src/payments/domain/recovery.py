"""Pure rules for crash recovery: who may take over an operation, and when to conclude.

Recovery never re-sends anything that may already have reached MarketPay (a purchase, a
refund or a reversal): it only reads last-transaction, and aborts what nobody waits for.
"""

from datetime import datetime, timedelta

from payments.domain.budget import CLEANUP_RESERVE_SECONDS, RESPONSE_MARGIN_SECONDS
from payments.domain.cancel import (
    CANCEL_DEADLINE_SECONDS,
    ReversalObservation,
    ReversalSighting,
    UndoOutcome,
    UndoResult,
    resolve_refund,
)
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    Resolution,
    ResolvedVia,
    StateReason,
)
from payments.domain.outcomes import Observation, Sighting

# Added to a request's own deadline to form its lease: past that, it is surely dead.
LEASE_GRACE_SECONDS = 30
# Minimum time recovery watches last-transaction before it aborts an orphaned transaction.
RECOVERY_MIN_POLL_SECONDS = 5
# Longest a transaction can stay unrecorded on the terminal (card wait + PIN + issuer).
# Staging: last-transaction never shows a running transaction — only the previous one —
# and after our crash an abort answers 409 and cannot stop it: the terminal ends it on its
# own timeout. Past this, "no record of ours" means it never ran (or, per the spec, failed
# very late, when MarketPay reports the previous transaction). Conservative on purpose.
TERMINAL_MAX_TRANSACTION_SECONDS = 180
# Upper bound one reconcile spends on a single payment (it answers an HTTP request).
RECOVERY_MAX_WAIT_SECONDS = 90
# A reversal/refund counts as "never landed" only this long after it was sent. Both wait
# for the customer to tap the card, so the same limit as any terminal transaction.
UNDO_LANDING_SECONDS = TERMINAL_MAX_TRANSACTION_SECONDS


def lease_for(now: datetime, seconds: float) -> datetime:
    return now + timedelta(seconds=seconds + LEASE_GRACE_SECONDS)


def is_orphaned(payment: Payment, me: Owner, now: datetime) -> bool:
    """An operation is in flight but nobody alive is driving it."""

    if payment.operation is None:
        return False

    owner = payment.owner

    if owner is None:
        return True  # its request ended without settling it (UNKNOWN)

    if owner.instance_id == me.instance_id and owner.boot_id != me.boot_id:
        return True  # this process restarted: its previous life is certainly gone

    return payment.lease_until is not None and payment.lease_until <= now


def take_over(
    current: Payment,
    me: Owner,
    now: datetime,
    lease_seconds: float,
    claim_id: str | None = None,
) -> Payment:
    """Become the owner of an orphaned operation. Unchanged if it isn't orphaned."""
    if not is_orphaned(current, me, now):
        return current

    return current.model_copy(
        update={
            "owner": me,
            "lease_until": lease_for(now, lease_seconds),
            "claim_id": claim_id,
            "updated_at": now,
        }
    )


def release_refund_due(current: Payment, now: datetime) -> Payment:
    """Recovery found a refund due but never sent: it does not start a new card
    transaction on its own (the customer may be long gone). The charge stands — the
    payment stays APPROVED — and the terminal is freed; a new POS cancel runs the refund."""

    if current.operation is not Operation.REFUND or current.undo_started_at is not None:
        return current

    return current.model_copy(
        update={
            "operation": None,
            "owner": None,
            "lease_until": None,
            "state": PaymentState.APPROVED,
            "state_reason": StateReason.REFUND_DUE,
            "state_detail": "approved despite a cancel request; cancel again to refund",
            "updated_at": now,
        }
    )


def recovery_poll_seconds(payment: Payment, now: datetime) -> float:
    """How long recovery watches last-transaction before acting (abort / conclude).

    Never less than a few seconds, and never cut short of what the original request
    itself would have allowed:
    - purchase: until its own abort point (deadline minus the cleanup reserve);
    - refund: likewise, counted from when the refund was sent (the customer may be tapping);
    - reversal: until it has had time to land.
    Capped: reconcile answers an HTTP request (a later look finishes the job).
    """

    match payment.operation:
        case Operation.REVERSAL if payment.undo_started_at is not None:
            until = payment.undo_started_at + timedelta(seconds=UNDO_LANDING_SECONDS)

        case Operation.REFUND if payment.undo_started_at is not None:
            until = payment.undo_started_at + timedelta(
                seconds=CANCEL_DEADLINE_SECONDS - CLEANUP_RESERVE_SECONDS
            )

        case _ if payment.deadline_at is not None:
            until = payment.deadline_at - timedelta(seconds=CLEANUP_RESERVE_SECONDS)

        case _:
            return RECOVERY_MIN_POLL_SECONDS

    seconds = max(RECOVERY_MIN_POLL_SECONDS, (until - now).total_seconds())

    return min(seconds, RECOVERY_MAX_WAIT_SECONDS)


def transaction_sent_at(payment: Payment) -> datetime | None:
    """When the terminal operation now in flight was sent, roughly."""

    match payment.operation:
        case Operation.PURCHASE:
            return payment.created_at  # sent right after it was recorded
        case Operation.REFUND | Operation.REVERSAL:
            return payment.undo_started_at

    return None


def recovery_window(payment: Payment, now: datetime) -> tuple[float, float]:
    """(seconds to watch before acting, seconds until recovery must answer).

    Recovery keeps confirming until the terminal has had time to end the transaction on
    its own — capped, since reconcile answers an HTTP request.
    """

    poll = recovery_poll_seconds(payment, now)
    answer = poll + CLEANUP_RESERVE_SECONDS - RESPONSE_MARGIN_SECONDS
    sent = transaction_sent_at(payment)

    if sent is not None:
        unrecorded_after = (
            sent + timedelta(seconds=TERMINAL_MAX_TRANSACTION_SECONDS) - now
        ).total_seconds()
        answer = max(answer, unrecorded_after + 2 * RESPONSE_MARGIN_SECONDS)
    answer = max(poll, min(answer, RECOVERY_MAX_WAIT_SECONDS))

    return poll, answer


def conclude_never_recorded(
    payment: Payment, observation: Observation | None, now: datetime
) -> Resolution | None:
    """No record of our transaction long after it was sent: nothing was charged."""

    sent = transaction_sent_at(payment)
    if (
        sent is None
        or observation is None
        or observation.sighting is not Sighting.NOT_OURS
        or now < sent + timedelta(seconds=TERMINAL_MAX_TRANSACTION_SECONDS)
    ):
        return None

    return Resolution(
        state=PaymentState.FAILED,
        state_reason=StateReason.NEVER_RECORDED,
        resolved_via=ResolvedVia.LAST_TRANSACTION,
        state_detail=(f"no record of it {TERMINAL_MAX_TRANSACTION_SECONDS}s after it was sent"),
    )


def undo_had_time_to_land(payment: Payment, now: datetime) -> bool:
    started = payment.undo_started_at
    return started is not None and (now - started).total_seconds() >= UNDO_LANDING_SECONDS


def conclude_reversal(
    observation: ReversalObservation, payment: Payment, now: datetime
) -> UndoResult | None:
    """What a single last-transaction look says about a reversal in flight (None: unclear)."""
    if observation.sighting is ReversalSighting.REVERSAL_FINISHED:
        return observation.result
    if observation.sighting is ReversalSighting.NO_NEW_RECORD and undo_had_time_to_land(
        payment, now
    ):
        # Long after it was sent, still no record of this reversal (a running one isn't
        # shown — hence the long wait): it was never applied. The charge stands.
        return _not_recorded("no reversal recorded long after it was sent")
    return None


def conclude_refund(
    observation: Observation, purchase_still_last: bool, payment: Payment, now: datetime
) -> UndoResult | None:
    """What a single last-transaction look says about a refund in flight (None: unclear)."""
    if observation.sighting is Sighting.OURS_FINISHED:
        return resolve_refund(observation.resolution)
    if purchase_still_last and undo_had_time_to_land(payment, now):
        return _not_recorded("no refund recorded; the purchase is still the last")
    never = conclude_never_recorded(payment, observation, now)
    return resolve_refund(never) if never is not None else None


def _not_recorded(detail: str) -> UndoResult:
    return UndoResult(
        outcome=UndoOutcome.NOT_DONE,
        reason=StateReason.UNDO_NOT_RECORDED,
        via=ResolvedVia.LAST_TRANSACTION,
        detail=detail,
    )
