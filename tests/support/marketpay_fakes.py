"""A scripted stand-in for MarketPay, and helpers to build its responses."""

import httpx

from payments.domain.marketpay.models import LastTransactionResult
from payments.domain.marketpay.notification import Notification

TERMINAL = "PAX:TEST_TERMINAL"
REF = "order-7f3a9c"


def tx(status="OK", ecr_id=REF, terminal_tx="14", response_code="default") -> dict:
    if response_code == "default":
        response_code = "000" if status == "OK" else None  # staging NOKs carry no code
    return {
        "status": status,
        "responseCode": response_code,
        "terminalTransactionId": terminal_tx,
        "finalTransactionParams": {"ecrTransactionId": ecr_id, "amount": "1299"},
    }


def finished(status="OK", ecr_id=REF, response_code="default") -> dict:
    return {
        "lastTransactionState": "FINISHED",
        "transactionResult": tx(status, ecr_id, response_code=response_code),
    }


IN_PROGRESS = {"lastTransactionState": "IN_PROGRESS"}


def cancellation(status="OK", ecr_id=REF, terminal_tx="14") -> dict:
    return {
        "status": status,
        "cancellationParams": {
            "terminalTransactionId": terminal_tx,
            "ecrTransactionId": ecr_id,
            "amount": "1299",
            "currency": "752",
        },
    }


def reversal_record(status="OK", ecr_id=REF, terminal_tx="15") -> dict:
    """last-transaction after a reversal, as staging really shows it: a normal
    transactionResult echoing the purchase's ecrTransactionId, with a new
    terminalTransactionId — no cancellationResult."""

    return {
        "lastTransactionState": "FINISHED",
        "transactionResult": tx(status, ecr_id, terminal_tx=terminal_tx),
    }


def cancelled_last(status="OK", ecr_id=REF, terminal_tx="14") -> dict:
    """last-transaction after a cancel-transaction, in the spec's shape (cancellationResult)."""

    return {
        "lastTransactionState": "FINISHED",
        "cancellationResult": cancellation(status, ecr_id, terminal_tx),
    }


def accepted(request):
    return httpx.Response(202)


def lost(request):
    raise httpx.ReadTimeout("response lost", request=request)


def dns_failure(request):
    raise httpx.ConnectError("[Errno -2] Name or service not known", request=request)


ABORTED = httpx.Response(204)
TOO_LATE = httpx.Response(409)


def ok(body):
    return httpx.Response(200, json=body)


def created(body):
    return lambda request: httpx.Response(201, json=body)


class ScriptedMarketPay:
    """Each call to an endpoint takes its next scripted answer; the last one repeats.

    An answer is an httpx.Response, or a callable(request) that returns or raises.
    An endpoint with no script answers 503; `calls` records everything that was called.
    """

    def __init__(self, process=(), lookups=(), aborts=(), cancels=()):
        self.scripts = {
            "process": list(process),
            "last": list(lookups),
            "abort": list(aborts),
            "cancel": list(cancels),
        }
        self.requests: dict[str, list[httpx.Request]] = {name: [] for name in self.scripts}
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/process-transaction/"):
            name = "process"
        elif path.startswith("/last-transaction/"):
            name = "last"
        elif path.startswith("/abort-transaction/"):
            name = "abort"
        elif path.startswith("/cancel-transaction/"):
            name = "cancel"
        else:
            raise AssertionError(f"unexpected call {request.url}")

        self.calls.append(name)
        self.requests[name].append(request)

        script = self.scripts[name]
        if not script:
            return httpx.Response(503)  # unscripted: "MarketPay unavailable"
        answer = script.pop(0) if len(script) > 1 else script[0]

        return answer(request) if callable(answer) else answer


def observed_notification(ecr_id=REF, terminal_tx="40", status="OK", response_code="000") -> dict:
    """A final notification exactly as staging sent it (2026-09-28), card data redacted.

    Unlike the guide's example: the envelope's terminalTransactionId is null (the result
    has it), the result carries finalTransactionParams, amount/currency there are NUMBERS
    (strings in last-transaction), and the type is echoed as `type`, not `transactionType`.
    """

    receipt = f"VISA CONTACTLESS\nXXXXXXXXXXXX0000 01\nREFERENCE: {ecr_id}\nAMOUNT:SEK 1,00\n"

    return {
        "ecrId": "TEST_ECR_ID",
        "ecrTransactionId": ecr_id,
        "result": {
            "authorizationCode": "213462",
            "cardData": {
                "applicationId": "A0000000031010",
                "bankId": "000000",
                "cardCapture": "CONTACTLESS",
                "extractedPan": "XXXXXXXXXXXX0000",
                "loyaltyErrorFlag": False,
                "loyaltyId": None,
                "panSequenceNumber": "01",
                "schema": "VISA",
            },
            "cashierReceipt": receipt + "MERCHANT RECEIPT\n",
            "customerReceipt": receipt + "CARDHOLDER RECEIPT\n",
            "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": False, "dccUsed": False},
            "finalTransactionParams": {
                "amount": 100,
                "amountCashback": 0,
                "amountTip": 0,
                "cashierId": "cloud",
                "currency": 752,
                "ecrTransactionId": ecr_id,
                "merchantOption": None,
                "mode": "DIRECT",
                "transactionReference": None,
                "type": "PURCHASE",
            },
            "forcedOffline": False,
            "issuerOption": None,
            "merchantId": "MERCHANT_ID",
            "parBank": None,
            "privateData": None,
            "responseCode": response_code,
            "signatureRequired": False,
            "status": status,
            "terminalId": "DEVICE_SERIAL",
            "terminalTransactionId": terminal_tx,
        },
        "status": "COMPLETED",
        "terminalTransactionId": None,
    }


def observed_timeout_notification(ecr_id=REF, terminal_tx="41") -> dict:
    """The final notification when nobody presented a card (staging, 2026-09-28): NOK,
    no responseCode, card fields null. Unlike last-transaction's NOK, the echoed
    amount/currency are the real ones, as numbers."""

    return {
        "ecrId": "TEST_ECR_ID",
        "ecrTransactionId": ecr_id,
        "result": {
            "authorizationCode": None,
            "cardData": {
                "applicationId": None,
                "bankId": None,
                "cardCapture": None,
                "extractedPan": None,
                "loyaltyErrorFlag": False,
                "loyaltyId": None,
                "panSequenceNumber": None,
                "schema": None,
            },
            "cashierReceipt": None,
            "customerReceipt": None,
            "dccDetails": {"dccAmount": 0, "dccCurrency": 0, "dccOffered": False, "dccUsed": False},
            "finalTransactionParams": {
                "amount": 100,
                "amountCashback": 0,
                "amountTip": 0,
                "cashierId": None,
                "currency": 752,
                "ecrTransactionId": ecr_id,
                "merchantOption": None,
                "mode": "DIRECT",
                "transactionReference": None,
                "type": "PURCHASE",
            },
            "forcedOffline": False,
            "issuerOption": None,
            "merchantId": None,
            "parBank": None,
            "privateData": None,
            "responseCode": None,
            "signatureRequired": False,
            "status": "NOK",
            "terminalId": None,
            "terminalTransactionId": terminal_tx,
        },
        "status": "COMPLETED",
        "terminalTransactionId": None,
    }


# --- Notifications ---------------------------------------------------------------------

# The guide's example (§3.3): the result names no ecrTransactionId; the notification does.
GUIDE_OK = {
    "status": "OK",
    "responseCode": "000",
    "authorizationCode": "213462",
    "terminalTransactionId": "14",
    "customerReceipt": "...",
}


def completed(result: dict, ecr_id: str = REF, terminal_tx: str | None = "14") -> dict:
    return {
        "status": "COMPLETED",
        "ecrId": "TEST_ECR_ID",
        "ecrTransactionId": ecr_id,
        "terminalTransactionId": terminal_tx,
        "result": result,
    }


def record(body: dict) -> LastTransactionResult | None:
    return Notification.model_validate(body).as_record()
