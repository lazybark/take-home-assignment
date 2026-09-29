"""The deadline budget, and what an abort's answer lets us conclude (pure rules)."""

import pytest
from support.marketpay_fakes import IN_PROGRESS, REF, finished

from payments.domain.abort import confirmation_complete, resolve_after_abort
from payments.domain.budget import process_budget
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.marketpay.outcomes import Aborted, AbortRefused, AbortUnconfirmed, TooLate
from payments.domain.models import PaymentState, StateReason
from payments.domain.outcomes import Observation, Sighting, observe_last_transaction

PREVIOUS = finished("OK", ecr_id="order-previous")


def seen(body: dict) -> Observation:
    return observe_last_transaction(LastTransactionResult.model_validate(body), REF)


def test_budget_timeline_at_default_deadline():
    budget = process_budget(60)
    # MarketPay holds the request past our deadline: abort at 50s while it's open.
    assert (budget.wait_time, budget.read_timeout) == (150, 155)
    assert (budget.resolve_within, budget.answer_within) == (50, 59)


@pytest.mark.parametrize(
    ("abort", "last", "state", "reason"),
    [
        # The abort stopped our payment: the terminal records it as NOK (no bank code).
        (Aborted(), seen(finished("NOK")), PaymentState.FAILED, StateReason.ABORTED),
        # It completed just before the abort landed: the truth wins.
        (Aborted(), seen(finished("OK")), PaymentState.APPROVED, StateReason.BANK_APPROVED),
        (TooLate(), seen(finished("OK")), PaymentState.APPROVED, StateReason.BANK_APPROVED),
        (
            TooLate(),
            seen(finished("NOK", response_code="116")),
            PaymentState.DECLINED,
            StateReason.BANK_DECLINED,
        ),
        # 204 and no record of ours at all: nothing ran, nothing charged.
        (Aborted(), seen(PREVIOUS), PaymentState.FAILED, StateReason.ABORTED),
        # Not visible yet: we must not guess.
        (Aborted(), seen(IN_PROGRESS), PaymentState.UNKNOWN, StateReason.AWAITING_RESULT),
        (Aborted(), None, PaymentState.UNKNOWN, StateReason.AWAITING_RESULT),
        (TooLate(), seen(IN_PROGRESS), PaymentState.UNKNOWN, StateReason.AWAITING_RESULT),
        (TooLate(), seen(PREVIOUS), PaymentState.UNKNOWN, StateReason.AWAITING_RESULT),
        # Only a 204 says it was stopped: 404/409 prove nothing (staging: idle -> 409).
        (
            AbortRefused(status_code=404),
            seen(PREVIOUS),
            PaymentState.UNKNOWN,
            StateReason.AWAITING_RESULT,
        ),
        (
            AbortRefused(status_code=404),
            seen(IN_PROGRESS),
            PaymentState.UNKNOWN,
            StateReason.AWAITING_RESULT,
        ),
        (
            AbortRefused(status_code=400),
            seen(PREVIOUS),
            PaymentState.UNKNOWN,
            StateReason.AWAITING_RESULT,
        ),
        (
            AbortUnconfirmed(reason="reset"),
            seen(PREVIOUS),
            PaymentState.UNKNOWN,
            StateReason.AWAITING_RESULT,
        ),
    ],
)
def test_resolve_after_abort(abort, last, state, reason):
    resolution = resolve_after_abort(abort, last)
    assert (resolution.state, resolution.state_reason) == (state, reason)


def test_only_an_abort_saying_nothing_runs_turns_no_record_into_failed():
    for abort in (TooLate(), AbortRefused(status_code=400), AbortUnconfirmed(reason="x")):
        assert resolve_after_abort(abort, seen(PREVIOUS)).state is not PaymentState.FAILED


def test_confirmation_needs_two_consecutive_not_ours_after_a_204():
    assert not confirmation_complete(Aborted(), [seen(PREVIOUS)])
    assert confirmation_complete(Aborted(), [seen(PREVIOUS), seen(PREVIOUS)])
    assert not confirmation_complete(Aborted(), [seen(PREVIOUS), seen(IN_PROGRESS)])
    assert not confirmation_complete(TooLate(), [seen(PREVIOUS), seen(PREVIOUS)])
    assert confirmation_complete(TooLate(), [seen(finished("OK"))])
    assert seen(finished("OK")).sighting is Sighting.OURS_FINISHED
