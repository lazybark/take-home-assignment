"""Cancel: planning, claiming an undo, and reading a cancellation or reversal (pure rules)."""

from datetime import UTC, datetime

import pytest
from support.marketpay_fakes import (
    IN_PROGRESS,
    REF,
    TERMINAL,
    cancellation,
    cancelled_last,
    finished,
    reversal_record,
    tx,
)

from payments.domain.cancel import (
    CancelPlan,
    ReversalSighting,
    UndoOutcome,
    UndoResult,
    observe_reversal,
    plan_cancel,
    resolve_cancel_outcome,
)
from payments.domain.marketpay.models import CancellationResult, LastTransactionResult
from payments.domain.marketpay.outcomes import CancelCompleted, Completed, NotSent, Rejected
from payments.domain.models import (
    Operation,
    Owner,
    Payment,
    PaymentState,
    ResolvedVia,
    StateReason,
    payment_id_for,
    refund_reference_for,
)
from payments.domain.outcomes import resolve_process_outcome
from payments.domain.transitions import apply_purchase, apply_undo, claim_undo, undo_operation

PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def payment(**update) -> Payment:
    base = Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=PaymentState.APPROVED,
        provider_transaction_id="14",
        created_at=T0,
        updated_at=T0,
    )
    return base.model_copy(update=update)


@pytest.mark.parametrize(
    ("update", "plan"),
    [
        ({"state": PaymentState.CANCELLED}, CancelPlan.ALREADY_CANCELLED),
        ({}, CancelPlan.UNDO),
        ({"state": PaymentState.DECLINED}, CancelPlan.NOT_CANCELLABLE),
        ({"state": PaymentState.FAILED}, CancelPlan.NOT_CANCELLABLE),
        (
            {"state": PaymentState.PENDING, "operation": Operation.PURCHASE},
            CancelPlan.STOP_PURCHASE,
        ),
        (
            {"state": PaymentState.UNKNOWN, "operation": Operation.PURCHASE},
            CancelPlan.STOP_PURCHASE,
        ),
        ({"operation": Operation.REVERSAL, "undo_started_at": T0}, CancelPlan.AWAIT_UNDO),
        ({"operation": Operation.REFUND}, CancelPlan.UNDO),  # due, not yet sent: claimable
        ({"state": PaymentState.UNKNOWN}, CancelPlan.NEEDS_ATTENTION),  # e.g. partial
    ],
)
def test_plan_cancel(update, plan):
    assert plan_cancel(payment(**update)) is plan


def test_undo_is_a_reversal_unless_an_abort_was_ever_sent():
    assert undo_operation(payment()) is Operation.REVERSAL
    assert undo_operation(payment(abort_requested_at=T0)) is Operation.REFUND
    assert undo_operation(payment(provider_transaction_id=None)) is Operation.REFUND


ME = Owner(instance_id="local-1", boot_id="boot-1")


def claim(p: Payment) -> Payment:
    return claim_undo(p, T0, ME, T0)


def test_claim_undo_is_won_exactly_once():
    first = claim(payment())
    assert (first.operation, first.undo_attempts, first.owner) == (Operation.REVERSAL, 1, ME)
    assert claim(first) == first  # already claimed: unchanged


def test_each_refund_attempt_gets_its_own_reference():
    one = claim(payment(abort_requested_at=T0))
    retry = apply_undo(
        one,
        UndoResult(
            outcome=UndoOutcome.NOT_DONE,
            reason=StateReason.UNDO_REFUSED,
            via=ResolvedVia.PROCESS_RESPONSE,
        ),
        T0,
    )
    two = claim(retry)
    assert one.refund_reference == refund_reference_for(PAYMENT_ID, 1)
    assert two.refund_reference == refund_reference_for(PAYMENT_ID, 2)
    assert len(two.refund_reference) <= 36


def test_purchase_stopped_after_a_pos_cancel_is_cancelled():
    in_flight = payment(
        state=PaymentState.PENDING, operation=Operation.PURCHASE, cancel_requested_at=T0
    )
    stopped = resolve_process_outcome(Completed(result=_result("NOK")), REF)
    result = apply_purchase(in_flight, stopped, T0)
    assert (result.state, result.state_reason, result.operation) == (
        PaymentState.CANCELLED,
        StateReason.CANCELLED_BEFORE_CHARGE,
        None,
    )


def test_purchase_approved_despite_a_pos_cancel_keeps_the_terminal_for_a_refund():
    in_flight = payment(
        state=PaymentState.PENDING, operation=Operation.PURCHASE, cancel_requested_at=T0
    )
    approved = resolve_process_outcome(Completed(result=_result("OK")), REF)
    result = apply_purchase(in_flight, approved, T0)
    assert (result.state, result.operation) == (PaymentState.APPROVED, Operation.REFUND)


def test_bank_decline_during_a_pos_cancel_stays_declined():
    in_flight = payment(
        state=PaymentState.PENDING, operation=Operation.PURCHASE, cancel_requested_at=T0
    )
    declined = resolve_process_outcome(Completed(result=_result("NOK", "116")), REF)
    assert apply_purchase(in_flight, declined, T0).state is PaymentState.DECLINED


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (
            CancelCompleted(result=CancellationResult.model_validate(cancellation("OK"))),
            UndoOutcome.DONE,
        ),
        (
            CancelCompleted(result=CancellationResult.model_validate(cancellation("NOK"))),
            UndoOutcome.NOT_DONE,
        ),
        (
            CancelCompleted(result=CancellationResult.model_validate(cancellation("PARTIAL"))),
            UndoOutcome.PARTIAL,
        ),
        (Rejected(status_code=400, body=""), UndoOutcome.NOT_DONE),
        (NotSent(reason="dns"), UndoOutcome.NOT_DONE),
    ],
)
def test_resolve_cancel_outcome(outcome, expected):
    assert resolve_cancel_outcome(outcome).outcome is expected


@pytest.mark.parametrize(
    ("body", "baseline", "sighting", "outcome"),
    [
        # Staging's shape: our ecrTransactionId, a NEW terminalTransactionId.
        (reversal_record("OK", terminal_tx="15"), None, ReversalSighting.REVERSAL_FINISHED, "done"),
        (
            reversal_record("NOK", terminal_tx="15"),
            None,
            ReversalSighting.REVERSAL_FINISHED,
            "not_done",
        ),
        # The purchase itself (same id "14") is still the last record: nothing new yet.
        (finished("OK"), None, ReversalSighting.NO_NEW_RECORD, None),
        # An earlier attempt's record (the baseline) is not this attempt's result.
        (reversal_record("NOK", terminal_tx="15"), "15", ReversalSighting.NO_NEW_RECORD, None),
        (reversal_record("OK", terminal_tx="16"), "15", ReversalSighting.REVERSAL_FINISHED, "done"),
        # The spec's shape: a cancellationResult for our payment.
        (cancelled_last("OK"), None, ReversalSighting.REVERSAL_FINISHED, "done"),
        (cancelled_last("OK", ecr_id="someone-else"), None, ReversalSighting.OTHER, None),
        (cancelled_last("OK", terminal_tx="99"), None, ReversalSighting.OTHER, None),
        # Another payment's record is still last: with the terminal held, ours hasn't landed.
        (finished("OK", ecr_id="order-previous"), None, ReversalSighting.NO_NEW_RECORD, None),
        (IN_PROGRESS, None, ReversalSighting.IN_PROGRESS, None),
    ],
)
def test_observe_reversal(body, baseline, sighting, outcome):
    last = LastTransactionResult.model_validate(body)
    observation = observe_reversal(last, REF, "14", baseline)
    assert observation.sighting is sighting
    assert (observation.result.outcome if observation.result else None) == outcome


def test_observe_reversal_cannot_tell_without_the_purchase_id():
    last = LastTransactionResult.model_validate(reversal_record("OK", terminal_tx="15"))
    assert observe_reversal(last, REF, None).sighting is ReversalSighting.OTHER


def _result(status, response_code="default"):
    from payments.domain.marketpay.models import TransactionResult

    return TransactionResult.model_validate(tx(status, response_code=response_code))
