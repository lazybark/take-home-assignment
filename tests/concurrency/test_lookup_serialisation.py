"""MarketPay answers 500 to overlapping last-transaction calls for one terminal."""

import threading
import time

import httpx
import pytest
from support.marketpay_fakes import TERMINAL, finished

from payments.domain.marketpay.gateway import MarketPayUnavailable
from payments.domain.marketpay.outcomes import Found, LookupFailed


class OneAtATimeTerminal:
    """Behaves like staging: an overlapping lookup for the same terminal gets 500."""

    def __init__(self, duration: float = 0.05):
        self.duration = duration
        self.active: dict[str, int] = {}
        self.max_overlap: dict[str, int] = {}
        self.guard = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        terminal = request.url.path.rsplit("/", 1)[-1]
        with self.guard:
            self.active[terminal] = self.active.get(terminal, 0) + 1
            overlap = self.active[terminal]
            self.max_overlap[terminal] = max(self.max_overlap.get(terminal, 0), overlap)

        try:
            time.sleep(self.duration)
            if overlap > 1:
                return httpx.Response(500)

            return httpx.Response(200, json=finished())

        finally:
            with self.guard:
                self.active[terminal] -= 1


def run_together(fns):
    results = [None] * len(fns)
    barrier = threading.Barrier(len(fns))

    def run(i):
        barrier.wait()
        results[i] = fns[i]()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results


def test_concurrent_lookups_for_one_terminal_queue_instead_of_colliding(make_marketpay):
    terminal = OneAtATimeTerminal()
    client = make_marketpay(terminal)
    results = run_together([lambda: client.get_last_transaction(TERMINAL, timeout=5)] * 4)

    assert all(isinstance(r, Found) for r in results)  # nobody got a 500
    assert terminal.max_overlap[TERMINAL] == 1


def test_lookups_for_different_terminals_still_run_in_parallel(make_marketpay):
    terminal = OneAtATimeTerminal(duration=0.2)
    client = make_marketpay(terminal)
    started = time.monotonic()
    run_together(
        [
            lambda: client.get_last_transaction("PAX:1", timeout=5),
            lambda: client.get_last_transaction("PAX:2", timeout=5),
        ]
    )
    assert time.monotonic() - started < 0.35  # not 0.4: they overlapped


def test_a_lookup_that_cannot_get_its_turn_in_time_fails_cleanly(make_marketpay):
    terminal = OneAtATimeTerminal(duration=0.5)
    client = make_marketpay(terminal)
    slow, impatient = run_together(
        [
            lambda: client.get_last_transaction(TERMINAL, timeout=5),
            lambda: (time.sleep(0.05), client.get_last_transaction(TERMINAL, timeout=0.1))[1],
        ]
    )
    assert isinstance(slow, Found)
    assert isinstance(impatient, LookupFailed)  # a failed lookup is never an outcome


def test_timeout_errors_do_not_blame_the_certificate(make_marketpay):
    def timeout(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(MarketPayUnavailable) as exc_info:
        make_marketpay(timeout).list_terminals()

    assert "mTLS" not in str(exc_info.value)
