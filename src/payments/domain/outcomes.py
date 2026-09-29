"""Reading MarketPay's answers (pure rules): what a call's outcome or a last-transaction
record says about our payment.

Anything we cannot prove from MarketPay's own answer becomes UNKNOWN, never FAILED.
"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from payments.domain.marketpay.models import (
    LastTransactionResult,
    LastTransactionState,
    TransactionResult,
    TransactionStatus,
)
from payments.domain.marketpay.outcomes import (
    Accepted,
    Ambiguous,
    Completed,
    NotSent,
    ProcessOutcome,
    Rejected,
)
from payments.domain.models import PaymentState, Resolution, ResolvedVia, StateReason

# 4xx answers to process-transaction that prove nothing started. The spec documents
# 404 (terminal not connected, lowercase manufacturer, currency mismatch) and 400
# (malformed request, missing User-Agent): an identical copy of our request — e.g. one
# duplicated by the network — would meet the same refusal. Any OTHER 4xx is undocumented
# here and might mean "busy" (with a copy of ours running?), so it is not trusted.
DEFINITIVE_REJECTIONS = frozenset({400, 404})


def is_definitive_rejection(outcome: ProcessOutcome) -> bool:
    return isinstance(outcome, Rejected) and outcome.status_code in DEFINITIVE_REJECTIONS


def needs_lookup(outcome: ProcessOutcome) -> bool:
    """The payment may be running or done — go and look: a 202, a lost/unreadable
    response, or a 4xx that doesn't prove it never started."""

    return isinstance(outcome, Accepted | Ambiguous) or (
        isinstance(outcome, Rejected) and not is_definitive_rejection(outcome)
    )


def resolve_process_outcome(outcome: ProcessOutcome, reference: str) -> Resolution:
    via = ResolvedVia.PROCESS_RESPONSE
    match outcome:
        case Completed(result=result):
            return resolve_transaction_result(result, reference, via)

        case NotSent(reason=reason):
            # Nothing left our side, so MarketPay cannot have started it. (By the time
            # this is the answer, re-sending was already tried: TerminalOperations.)
            return Resolution(
                state=PaymentState.FAILED,
                state_reason=StateReason.NOT_SENT,
                resolved_via=via,
                state_detail=reason,
            )

        case Rejected(status_code=status_code) if status_code in DEFINITIVE_REJECTIONS:
            # Refused before anything started. (The caller still takes one look at
            # last-transaction, in case a duplicate copy of our request did run.)
            return Resolution(
                state=PaymentState.FAILED,
                state_reason=StateReason.PROVIDER_REJECTED,
                resolved_via=via,
                state_detail=f"HTTP {status_code}",
            )

        case Rejected(status_code=status_code):
            # Undocumented 4xx: not proof of anything — handled like a lost answer.
            return Resolution(
                state=PaymentState.UNKNOWN,
                state_reason=StateReason.AWAITING_RESULT,
                resolved_via=via,
                state_detail=f"HTTP {status_code} (undocumented; not trusted)",
            )

        case Accepted():
            return Resolution(
                state=PaymentState.UNKNOWN,
                state_reason=StateReason.AWAITING_RESULT,
                resolved_via=via,
                state_detail="202 Accepted",
            )

        case Ambiguous(reason=reason):
            return Resolution(
                state=PaymentState.UNKNOWN,
                state_reason=StateReason.AWAITING_RESULT,
                resolved_via=via,
                state_detail=reason,
            )


APPROVED_RESPONSE_CODE = "000"


def result_inconsistency(result: TransactionResult) -> str | None:
    """A result whose `responseCode` contradicts its `status`. `status` wins either
    way; the contradiction is reported (an error log with the details, and a
    note in `state_detail`), because it may mean MarketPay and the bank disagree."""

    code = result.response_code
    if result.status is TransactionStatus.NOK and code == APPROVED_RESPONSE_CODE:
        return "responseCode '000' (approved) with status NOK; status wins"

    if result.status is TransactionStatus.OK and code != APPROVED_RESPONSE_CODE:
        return f"status OK with responseCode {code!r} (not '000'); status wins"

    return None


def resolve_transaction_result(
    result: TransactionResult, reference: str, via: ResolvedVia
) -> Resolution:
    echoed = result.final_transaction_params
    if echoed is not None and echoed.ecr_transaction_id != reference:
        # Someone else's transaction: it tells us nothing about ours.
        return Resolution(
            state=PaymentState.UNKNOWN,
            state_reason=StateReason.NOT_OUR_TRANSACTION,
            resolved_via=via,
            state_detail=f"echoed ecrTransactionId {echoed.ecr_transaction_id!r}",
        )

    common = {"resolved_via": via, "provider_transaction_id": result.terminal_transaction_id}
    match result.status:
        case TransactionStatus.OK:
            return Resolution(
                state=PaymentState.APPROVED,
                state_reason=StateReason.BANK_APPROVED,
                state_detail=result_inconsistency(result),  # OK without code "000"
                **common,
            )

        case TransactionStatus.NOK if result.response_code:
            # responseCode is "obtained from acquirer during the authorization": the bank
            # saw the card and said no. Keep its code verbatim. If that code is "000" the
            # result contradicts itself; `status` still decides.
            return Resolution(
                state=PaymentState.DECLINED,
                state_reason=StateReason.BANK_DECLINED,
                decline_reason=result.response_code,
                state_detail=result_inconsistency(result),
                **common,
            )

        case TransactionStatus.NOK:
            # No acquirer code: it stopped on the terminal before any authorization
            # (cancel pressed, no card in time, an abort). Nothing was charged; MarketPay
            # doesn't say who stopped it, so neither do we.
            return Resolution(
                state=PaymentState.FAILED, state_reason=StateReason.TERMINAL_STOPPED, **common
            )

        case TransactionStatus.PARTIAL:
            # A charge of unknown size may stand: it is reversed next (transitions).
            # Keep MarketPay's code verbatim; it ends as the declineReason.
            return Resolution(
                state=PaymentState.UNKNOWN,
                state_reason=StateReason.PARTIAL_APPROVAL,
                decline_reason=result.response_code,
                **common,
            )

        case _:
            return Resolution(
                state=PaymentState.UNKNOWN,
                state_reason=StateReason.UNRECOGNISED_RESULT,
                **common,
            )


class Sighting(StrEnum):
    OURS_FINISHED = "ours_finished"  # our transaction, with its final result
    IN_PROGRESS = "in_progress"  # the terminal is busy (customer not done yet)
    NOT_OURS = "not_ours"  # nothing, or another transaction: tells us nothing about ours


class Observation(BaseModel):
    model_config = ConfigDict(frozen=True)

    sighting: Sighting
    resolution: Resolution | None = None  # set only for OURS_FINISHED


def observe_last_transaction(last: LastTransactionResult, reference: str) -> Observation:
    """Interpret last-transaction for the payment whose ecrTransactionId is `reference`."""

    result = last.transaction_result
    echoed = result.final_transaction_params if result else None
    echoed_id = echoed.ecr_transaction_id if echoed else None

    match last.last_transaction_state:
        case LastTransactionState.IN_PROGRESS if echoed_id in (None, reference):
            # Never seen on staging (a running transaction shows as the previous one),
            # kept for the spec. Identity may be unknown while in progress; the terminal
            # lock means we run one payment per terminal, so a busy terminal is ours.
            return Observation(sighting=Sighting.IN_PROGRESS)

        case LastTransactionState.FINISHED if (
            result is not None and echoed_id == reference and last.cancellation_result is None
        ):
            return Observation(
                sighting=Sighting.OURS_FINISHED,
                resolution=resolve_transaction_result(
                    result, reference, ResolvedVia.LAST_TRANSACTION
                ),
            )

        case _:
            # NOT_FOUND, someone else's transaction, the one-before-last, or a cancellation.
            return Observation(sighting=Sighting.NOT_OURS)
