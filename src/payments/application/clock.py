"""Time as a dependency, so deadline logic can be tested without real waiting."""

import time
from concurrent.futures import Future
from concurrent.futures import wait as wait_futures
from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Wall-clock time, for timestamps we store."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that never jumps; for deadlines."""
        ...

    def sleep(self, seconds: float) -> None: ...

    def wait_for(self, future: Future, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a call running on another thread. True if done."""
        ...

    def pause(self, seconds: float, wake: Future) -> None:
        """Sleep, but return early when `wake` completes (a notification arrived)."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def wait_for(self, future: Future, timeout: float) -> bool:
        wait_futures([future], timeout=max(0.0, timeout))
        return future.done()

    def pause(self, seconds: float, wake: Future) -> None:
        wait_futures([wake], timeout=max(0.0, seconds))
