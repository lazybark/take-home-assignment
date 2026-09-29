"""Re-checking an outcome we only inferred: what a later record changes (pure rules)."""

from datetime import UTC, datetime

import pytest
from support.marketpay_fakes import REF, TERMINAL, finished, reversal_record

from payments.domain.abort import resolve_after_abort
from payments.domain.cancel import resolve_refund
from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.marketpay.outcomes import Aborted
from payments.domain.models import (
    Operation,
    Payment,
    PaymentState,
    ResolvedVia,
    StateReason,
    payment_id_for,
    refund_reference_for,
)
from payments.domain.outcomes import Observation, Sighting
from payments.domain.transitions import apply_undo
from payments.domain.verification import needs_verification, verify_against

PAYMENT_ID = payment_id_for(REF)
T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def payment(state, reason, via=ResolvedVia.LAST_TRANSACTION, **extra) -> Payment:
    return Payment(
        id=PAYMENT_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference=REF,
        state=state,
        state_reason=reason,
        resolved_via=via,
        created_at=T0,
        updated_at=T0,
        **extra,
    )


@pytest.mark.parametrize(
    ("p", "flag"),
    [
        (payment(PaymentState.FAILED, StateReason.ABORTED, ResolvedVia.ABORT_RESPONSE), True),
        (payment(PaymentState.FAILED, StateReason.NEVER_RECORDED), True),
        (
            payment(
                PaymentState.APPROVED, StateReason.UNDO_NOT_RECORDED, provider_transaction_id="14"
            ),
            True,
        ),
        # Backed by a record of ours: nothing inferred, nothing to check.
        (payment(PaymentState.FAILED, StateReason.ABORTED, ResolvedVia.PROCESS_RESPONSE), False),
        (payment(PaymentState.FAILED, StateReason.TERMINAL_STOPPED), False),
        (payment(PaymentState.FAILED, StateReason.NOT_SENT), False),  # certainly never left us
        (payment(PaymentState.DECLINED, StateReason.BANK_DECLINED), False),
        (payment(PaymentState.APPROVED, StateReason.BANK_APPROVED), False),
    ],
)
def test_which_outcomes_are_re_checked(p, flag):
    assert needs_verification(p) is flag


def test_a_charge_that_turned_up_later_corrects_the_record():
    inferred = payment(PaymentState.FAILED, StateReason.NEVER_RECORDED)
    last = LastTransactionResult.model_validate(finished("OK"))
    corrected = verify_against(inferred, last, T0)
    assert (corrected.state, corrected.state_reason, corrected.provider_transaction_id) == (
        PaymentState.APPROVED,
        StateReason.LATE_CHARGE_FOUND,
        "14",
    )


def test_a_decline_that_turned_up_later_changes_nothing():
    inferred = payment(PaymentState.FAILED, StateReason.NEVER_RECORDED)
    last = LastTransactionResult.model_validate(finished("NOK"))
    assert verify_against(inferred, last, T0) == inferred  # still no charge


def test_a_reversal_that_landed_later_corrects_the_record():
    inferred = payment(
        PaymentState.APPROVED, StateReason.UNDO_NOT_RECORDED, provider_transaction_id="14"
    )
    last = LastTransactionResult.model_validate(reversal_record("OK", terminal_tx="15"))
    corrected = verify_against(inferred, last, T0)
    assert (corrected.state, corrected.reversed, corrected.state_reason) == (
        PaymentState.CANCELLED,
        True,
        StateReason.LATE_REVERSAL_FOUND,
    )


# --- Refunds (found in the final review) ------------------------------------------------

REFUND_T = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
REFUNDED_ID = payment_id_for("order-refunded")
REFUND_REF = refund_reference_for(REFUNDED_ID, 1)


def refunded(**update) -> Payment:
    base = Payment(
        id=REFUNDED_ID,
        terminal_id=TERMINAL,
        amount=1299,
        currency="SEK",
        reference="order-refunded",
        state=PaymentState.APPROVED,
        provider_transaction_id="14",
        abort_requested_at=REFUND_T,
        refund_reference=REFUND_REF,
        undo_attempts=1,
        created_at=REFUND_T,
        updated_at=REFUND_T,
    )
    return base.model_copy(update=update)


def test_a_refund_concluded_only_from_a_missing_record_is_re_checked():
    """204 + no record of the refund is an inference, not MarketPay's "no": it must be
    flagged like any inferred outcome, or a late refund would leave it "charged"."""
    inferred = resolve_after_abort(Aborted(), Observation(sighting=Sighting.NOT_OURS))
    in_flight = refunded(operation=Operation.REFUND, undo_started_at=REFUND_T)
    settled = apply_undo(in_flight, resolve_refund(inferred), REFUND_T)

    assert settled.state_reason is StateReason.UNDO_NOT_RECORDED
    assert needs_verification(settled)


def test_a_late_refund_is_found_under_its_own_reference():
    """A refund is its own transaction (rf01…), not a reversal of the purchase's."""
    late_refund = LastTransactionResult.model_validate(
        {
            "lastTransactionState": "FINISHED",
            "transactionResult": {
                "status": "OK",
                "responseCode": "000",
                "terminalTransactionId": "16",
                "finalTransactionParams": {"ecrTransactionId": REFUND_REF},
            },
        }
    )
    stored = refunded(
        state_reason=StateReason.UNDO_NOT_RECORDED, resolved_via=ResolvedVia.LAST_TRANSACTION
    )

    corrected = verify_against(stored, late_refund, REFUND_T)
    assert (corrected.state, corrected.reversed) == (PaymentState.CANCELLED, True)
