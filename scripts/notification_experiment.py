"""Webhook experiment: force a 202 and see whether the final result arrives as a notification.

Our service never gets a 202 in normal operation: it holds process-transaction open past its
deadline, because an abort only works while the request is open. The spec promises the
final notification only after a 202, so this script sends a PURCHASE directly to MarketPay
with a short waitTime, bypassing our service and its records. The service must be running
with notifications configured: it receives and logs what MarketPay sends
(`docker compose logs -f api | grep marketpay_notification`).

    docker compose exec api uv run python scripts/notification_experiment.py \\
        --reference exp-n1 --amount 100 --wait-time 5

Then tap the card (or don't, to see a timeout's notification). The script watches
last-transaction until our record appears, so both sources can be compared.

Only on an idle terminal: this transaction replaces the terminal's "last transaction", which
our service relies on as evidence. The script refuses if the service holds the terminal.
"""

import argparse
import sys
import time

import httpx

from payments.config import Settings
from payments.domain.marketpay.currency import to_numeric
from payments.domain.marketpay.models import EcrParams, ProcessTransactionRequest
from payments.domain.marketpay.outcomes import Found
from payments.domain.models import payment_id_for
from payments.domain.outcomes import Sighting, observe_last_transaction
from payments.infrastructure.marketpay.client import MarketPayClient
from payments.infrastructure.marketpay.notification_urls import NotificationUrls


def main() -> int:
    settings = Settings()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--reference", required=True, help="ecrTransactionId, e.g. exp-n1")
    parser.add_argument("--amount", type=int, default=100, help="minor units (100 = 1 SEK)")
    parser.add_argument("--wait-time", type=int, default=5, help="MarketPay waitTime, seconds")
    parser.add_argument("--terminal", default=settings.marketpay_terminal_id)
    parser.add_argument("--watch", type=int, default=180, help="seconds to watch afterwards")
    parser.add_argument("--service", default="http://localhost:8080", help="our service")
    args = parser.parse_args()

    if settings.notification_base_url is None or settings.notification_secret is None:
        print("Set NOTIFICATION_BASE_URL and NOTIFICATION_SECRET first (see .env.example).")
        return 2
    if not args.terminal:
        print("No terminal: pass --terminal or set MARKETPAY_TERMINAL_ID.")
        return 2
    if holder := locked_by(args.service, args.terminal):
        print(f"Refusing: our service holds {args.terminal} for payment {holder}.")
        return 2

    urls = NotificationUrls(
        settings.notification_base_url, settings.notification_secret.get_secret_value()
    )
    payment_id = payment_id_for(args.reference)  # only to sign the URL; nothing is stored
    marketpay = MarketPayClient.from_settings(settings)

    print(f"{stamp()} PURCHASE {args.reference} ({args.amount}) waitTime={args.wait_time}s")
    print(f"{stamp()} notifications for payment id {payment_id} (look for it in the api log)")
    started = time.monotonic()
    outcome = marketpay.process_transaction(
        args.terminal,
        ProcessTransactionRequest(
            ecr_transaction_id=args.reference,
            amount=str(args.amount),
            currency=to_numeric("SEK"),
            ecr_params=EcrParams(notification_url=urls.for_operation(payment_id, "purchase")),
        ),
        wait_time=args.wait_time,
        read_timeout=args.wait_time + 5,
    )
    print(f"{stamp()} answer after {time.monotonic() - started:.1f}s: {outcome.kind} {outcome}")

    print(f"{stamp()} watching last-transaction for up to {args.watch}s…")
    deadline = time.monotonic() + args.watch
    while time.monotonic() < deadline:
        lookup = marketpay.get_last_transaction(args.terminal, timeout=5)
        if isinstance(lookup, Found):
            observation = observe_last_transaction(lookup.result, args.reference)
            if observation.sighting is Sighting.OURS_FINISHED:
                resolution = observation.resolution
                print(
                    f"{stamp()} last-transaction after {time.monotonic() - started:.1f}s: "
                    f"{resolution.state} ({resolution.state_reason}), "
                    f"terminalTransactionId={resolution.provider_transaction_id}"
                )
                return 0
        time.sleep(2)
    print(f"{stamp()} no record of {args.reference} after {args.watch}s")
    return 1


def locked_by(service: str, terminal: str) -> str | None:
    try:
        items = httpx.get(f"{service}/terminals", timeout=10).json()["items"]
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        sys.exit(f"Can't ask our service whether the terminal is free ({exc}); is it running?")
    for item in items:
        if item["terminalId"] == terminal and item["locked"]:
            return item["lockedBy"]["reference"]
    return None


def stamp() -> str:
    return time.strftime("%H:%M:%S", time.gmtime()) + "Z"


if __name__ == "__main__":
    sys.exit(main())
