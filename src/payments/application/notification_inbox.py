"""Notified records waiting for the request that polls their terminal (the bonus webhook).

The webhook pushes a verified final record here; a poll in this process picks it up at once
instead of waiting for last-transaction (which may lag or answer 500). In memory
only: lost on a restart, like the notification itself would be, so polling stays the fallback.
Only records that already passed the webhook's checks (signed URL, the payment's running
operation) are pushed, and a poll reads each one through its own observer function.
"""

import threading
from concurrent.futures import Future

from payments.application.clock import Clock
from payments.domain.marketpay.models import LastTransactionResult

# A record older than this is of no use to any poll still running (the longest waits ~5 min).
RECORD_TTL_SECONDS = 600
RECORDS_PER_TERMINAL = 8


class NotificationInbox:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._records: dict[str, list[tuple[float, LastTransactionResult]]] = {}
        self._arrivals: dict[str, Future] = {}

    def push(self, terminal_id: str, record: LastTransactionResult) -> None:
        with self._lock:
            kept = self._fresh(terminal_id)
            kept.append((self._clock.monotonic(), record))
            self._records[terminal_id] = kept[-RECORDS_PER_TERMINAL:]
            arrival = self._arrivals.pop(terminal_id, None)

        if arrival is not None:
            arrival.set_result(None)  # wakes a poll waiting on this terminal

    def records(self, terminal_id: str) -> list[LastTransactionResult]:
        """Recent notified records for this terminal, newest first."""

        with self._lock:
            kept = self._fresh(terminal_id)
            self._records[terminal_id] = kept

            return [record for _, record in reversed(kept)]

    def arrival(self, terminal_id: str) -> Future:
        """Completes when the next record for this terminal is pushed."""

        with self._lock:
            return self._arrivals.setdefault(terminal_id, Future())

    def _fresh(self, terminal_id: str) -> list[tuple[float, LastTransactionResult]]:
        oldest = self._clock.monotonic() - RECORD_TTL_SECONDS

        return [(at, r) for at, r in self._records.get(terminal_id, []) if at >= oldest]
