"""A MarketPay notification (the bonus webhook), and how it reads as a last-transaction record.

Not in the OpenAPI spec: the integration guide describes it (section 3)."""

import structlog
from pydantic import ValidationError

from payments.domain.marketpay.models import (
    CancellationResult,
    FinalTransactionParams,
    LastTransactionResult,
    LastTransactionState,
    MarketPayModel,
    TransactionResult,
)

log = structlog.get_logger(__name__)

COMPLETED = "COMPLETED"


class Notification(MarketPayModel):
    """The guide's `Notification` body (§3.3); it isn't in the OpenAPI spec, so it is read
    leniently. Progress statuses carry no outcome; only COMPLETED with a `result` does."""

    status: str | None = None  # WAITING_FOR_CARD, PIN_REQUIRED, BANK_AUTHORIZATION, COMPLETED
    ecr_id: str | None = None
    ecr_transaction_id: str | None = None
    terminal_transaction_id: str | None = None
    result: dict | None = None  # final notification only

    def as_record(self) -> LastTransactionResult | None:
        """The final result as the record last-transaction would show (None: no outcome).

        The guide's example `result` has no `finalTransactionParams`, so the transaction's
        identity is taken from the notification itself. A result that names a different
        ecrTransactionId than the notification is contradictory and ignored.
        """

        if self.status != COMPLETED or not isinstance(self.result, dict):
            return None
        try:
            if "cancellationParams" in self.result:  # the spec's shape for a reversal
                return LastTransactionResult(
                    last_transaction_state=LastTransactionState.FINISHED,
                    cancellation_result=CancellationResult.model_validate(self.result),
                )

            result = TransactionResult.model_validate(self.result)
        except ValidationError as exc:
            log.warning("notification_result_unreadable", error=str(exc))
            return None

        echoed = result.final_transaction_params
        if echoed is None:
            if self.ecr_transaction_id is None:
                return None  # can't tell whose result it is

            echo = FinalTransactionParams(ecr_transaction_id=self.ecr_transaction_id)
            result = result.model_copy(update={"final_transaction_params": echo})

        elif self.ecr_transaction_id not in (None, echoed.ecr_transaction_id):
            log.warning(
                "notification_result_contradictory",
                notified=self.ecr_transaction_id,
                in_result=echoed.ecr_transaction_id,
            )

            return None

        if result.terminal_transaction_id is None and self.terminal_transaction_id:
            result = result.model_copy(
                update={"terminal_transaction_id": self.terminal_transaction_id}
            )

        return LastTransactionResult(
            last_transaction_state=LastTransactionState.FINISHED, transaction_result=result
        )
