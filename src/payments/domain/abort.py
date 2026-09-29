"""The deadline: what an abort's answer lets us conclude (pure rules).

Only a 204 means the terminal stopped; a 409 says nothing either way.
"""

from payments.domain.marketpay.outcomes import (
    Aborted,
    AbortOutcome,
    AbortRefused,
    AbortUnconfirmed,
    TooLate,
)
from payments.domain.models import PaymentState, Resolution, ResolvedVia, StateReason
from payments.domain.outcomes import Observation, Sighting

# Abort calls without a usable answer are retried, up to this many attempts in total.
MAX_ABORT_ATTEMPTS = 3
# After a 204, this many consecutive "not ours" lookups mean no record of ours will appear.
NOT_OURS_TO_CONCLUDE = 2


def abort_answered(outcome: AbortOutcome) -> bool:
    """True once MarketPay gave the abort a definite answer (no point retrying)."""

    return not isinstance(outcome, AbortUnconfirmed)


def abort_says_nothing_runs(abort: AbortOutcome) -> bool:
    """Only a 204 says the terminal stopped what was running.

    Observed on staging: an idle terminal answers 409, and so does a transaction whose
    originating request is gone (after our crash). 409 carries no information either way.
    """

    return isinstance(abort, Aborted)


def confirmation_complete(abort: AbortOutcome, seen: list[Observation]) -> bool:
    """Can we stop polling after the abort?"""

    if seen and seen[-1].sighting is Sighting.OURS_FINISHED:
        return True

    recent = seen[-NOT_OURS_TO_CONCLUDE:]

    return (
        abort_says_nothing_runs(abort)
        and len(recent) == NOT_OURS_TO_CONCLUDE
        and all(o.sighting is Sighting.NOT_OURS for o in recent)
    )


def label_after_abort(abort: AbortOutcome, resolution: Resolution) -> Resolution:
    """Stopped before the bank right after the terminal confirmed our abort: that was us.
    (After a 409 the terminal ended it on its own, e.g. its card-wait timeout.)"""

    update: dict = {"abort_sent": True}
    if resolution.state_reason is StateReason.TERMINAL_STOPPED and isinstance(abort, Aborted):
        update["state_reason"] = StateReason.ABORTED

    return resolution.model_copy(update=update)


def resolve_after_abort(abort: AbortOutcome, last: Observation | None) -> Resolution:
    """Combine the abort's answer with the last thing last-transaction showed us.

    What the terminal recorded always wins: if our payment completed despite the abort
    (409 "too late"), it is APPROVED — we report the truth, we don't reverse it.
    """

    return _resolve_after_abort(abort, last).model_copy(update={"abort_sent": True})


def _resolve_after_abort(abort: AbortOutcome, last: Observation | None) -> Resolution:

    if last is not None and last.sighting is Sighting.OURS_FINISHED:
        return label_after_abort(abort, last.resolution)

    if abort_says_nothing_runs(abort) and last is not None and last.sighting is Sighting.NOT_OURS:
        # Nothing is running on the terminal, and it holds no record of ours (it never
        # started, or the abort left no trace): nothing was charged.
        # A request delayed in the network could still arrive later: this outcome is an
        # inference, so it is re-checked before the terminal's next payment (verification).
        return Resolution(
            state=PaymentState.FAILED,
            state_reason=StateReason.ABORTED,
            resolved_via=ResolvedVia.ABORT_RESPONSE,
            state_detail=f"abort {abort.kind}; no record of ours in last-transaction",
        )

    match abort:
        case TooLate():
            detail = "abort 409 (too late); outcome not visible yet"
        case AbortRefused(status_code=status_code):
            detail = f"abort refused (HTTP {status_code}); outcome not visible yet"
        case AbortUnconfirmed(reason=reason):
            detail = f"abort unconfirmed ({reason}); outcome not visible yet"
        case _:
            detail = "abort 204; outcome not visible yet"

    return Resolution(
        state=PaymentState.UNKNOWN,
        state_reason=StateReason.AWAITING_RESULT,
        resolved_via=ResolvedVia.ABORT_RESPONSE,
        state_detail=detail,
    )
