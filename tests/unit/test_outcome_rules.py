"""How MarketPay's answers become payment states, and the time budget of one payment
(pure rules)."""

import pytest

from payments.domain.budget import process_budget
from payments.domain.marketpay.models import TransactionResult
from payments.domain.marketpay.outcomes import Accepted, Ambiguous, Completed, NotSent, Rejected
from payments.domain.models import PaymentState, StateReason
from payments.domain.outcomes import resolve_process_outcome

REF = "order-1"


def completed(
    status: str | None, response_code: str | None = "000", ecr_id: str = REF
) -> Completed:
    body = {
        "responseCode": response_code,
        "terminalTransactionId": "14",
        "finalTransactionParams": {"ecrTransactionId": ecr_id, "amount": "100"},
    }

    if status is not None:
        body["status"] = status

    return Completed(result=TransactionResult.model_validate(body))


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (completed("OK"), PaymentState.APPROVED),
        (completed("NOK", response_code="116"), PaymentState.DECLINED),
        (completed("NOK", response_code=None), PaymentState.FAILED),
        (completed("PARTIAL"), PaymentState.UNKNOWN),
        (completed(None), PaymentState.UNKNOWN),
        (completed("OK", ecr_id="someone-else"), PaymentState.UNKNOWN),
        (Accepted(), PaymentState.UNKNOWN),
        (Ambiguous(reason="read timeout"), PaymentState.UNKNOWN),
        (Rejected(status_code=404, body=""), PaymentState.FAILED),
        (NotSent(reason="DNS failure"), PaymentState.FAILED),
    ],
)
def test_resolve_process_outcome(outcome, state):
    assert resolve_process_outcome(outcome, REF).state is state


def test_approved_keeps_terminal_transaction_id():
    assert resolve_process_outcome(completed("OK"), REF).provider_transaction_id == "14"


def test_declined_records_response_code():
    assert resolve_process_outcome(completed("NOK", "116"), REF).decline_reason == "116"


def test_a_timeout_is_never_a_failure():
    for outcome in (Accepted(), Ambiguous(reason="connection reset")):
        assert resolve_process_outcome(outcome, REF).state is not PaymentState.FAILED


@pytest.mark.parametrize(
    ("deadline", "wait", "read", "resolve"),
    [
        (60, 150, 155, 50),
        (120, 210, 215, 110),
        (20, 110, 115, 10),
        (8, 98, 103, 4),
        (5, 95, 100, 2.5),
    ],
)
def test_process_budget(deadline, wait, read, resolve):
    budget = process_budget(deadline)
    assert (budget.wait_time, budget.read_timeout, budget.resolve_within) == (wait, read, resolve)


def test_real_staging_nok_without_response_code_is_failed_terminal_stopped():
    # Verbatim shape of a staging result: Cancel pressed / no card in time. No acquirer
    # responseCode, so the bank never saw it — "failed", not "declined".
    result = TransactionResult.model_validate(
        {
            "cardData": {"loyaltyErrorFlag": False},
            "finalTransactionParams": {
                "ecrTransactionId": REF,
                "amount": "0",
                "currency": "0",
                "amountCashback": "0",
                "amountTip": "0",
            },
            "forcedOffline": False,
            "signatureRequired": False,
            "status": "NOK",
            "terminalTransactionId": "11",
        }
    )
    resolution = resolve_process_outcome(Completed(result=result), REF)
    assert resolution.state is PaymentState.FAILED
    assert resolution.state_reason is StateReason.TERMINAL_STOPPED
    assert resolution.decline_reason is None
    assert resolution.provider_transaction_id == "11"


def test_status_wins_when_response_code_contradicts_it():
    # "000" means approved, but status says NOK: we follow status, and say so.
    resolution = resolve_process_outcome(completed("NOK", response_code="000"), REF)
    assert resolution.state is PaymentState.DECLINED
    assert resolution.decline_reason == "000"
    assert "status wins" in resolution.state_detail


def test_consistent_results_carry_no_inconsistency_note():
    for outcome in (completed("OK"), completed("NOK", response_code="116")):
        assert resolve_process_outcome(outcome, REF).state_detail is None


def test_a_short_deadline_leaves_time_before_the_abort_and_after_it():
    budget = process_budget(8)
    assert 0 < budget.resolve_within < budget.answer_within < 8  # abort at 4 s, answer by 7 s


@pytest.mark.parametrize(
    ("deadline", "abort_at", "answer_by"),
    [(1, 1 / 3, 2 / 3), (2, 2 / 3, 4 / 3), (3, 1, 2), (8, 4, 7), (20, 10, 19), (60, 50, 59)],
)
def test_the_abort_always_has_time_before_the_answer(deadline, abort_at, answer_by):
    budget = process_budget(deadline)
    assert budget.resolve_within == pytest.approx(abort_at)
    assert budget.answer_within == pytest.approx(answer_by)
