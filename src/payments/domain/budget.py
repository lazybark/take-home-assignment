"""The time budget of one terminal operation, and the timeouts inside it (pure).

At the default 60 s deadline: MarketPay holds the request up to 150 s (waitTime), we abort at
50 s while it is still open, and answer the POS by 59 s.
"""

from pydantic import BaseModel, ConfigDict

# Seconds kept back from the POS deadline to abort, and hear the result, before answering.
# (The abort only works while our request is still open — see `process_budget`.)
CLEANUP_RESERVE_SECONDS = 10
# Seconds kept back at the very end to write the result and send the HTTP response.
RESPONSE_MARGIN_SECONDS = 1
# Our read timeout exceeds MarketPay's waitTime by this much, so a 202 always reaches us.
READ_MARGIN_SECONDS = 5
# MarketPay holds process/cancel requests this much longer than our deadline, so that an
# abort at the deadline still works (it only works while the request is open).
WAIT_BEYOND_DEADLINE_SECONDS = 90
MAX_WAIT_TIME_SECONDS = 300  # the API's maximum waitTime
# How often we ask last-transaction while resolving a 202 / lost response.
POLL_INTERVAL_SECONDS = 1.0
# Upper bound for a single last-transaction / abort call.
LOOKUP_TIMEOUT_SECONDS = 5.0
ABORT_TIMEOUT_SECONDS = 5.0


class CallBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    wait_time: int  # MarketPay waitTime query param (1–300)
    read_timeout: float  # our HTTP read timeout for that call
    resolve_within: float  # seconds from start: stop waiting, start the abort ("soft")
    answer_within: float  # seconds from start: stop confirming, answer the POS ("hard")


def process_budget(deadline_seconds: int) -> CallBudget:
    """At the default 60s:

        0s ─ process (waitTime 150, held open) ─ 50s: abort while it's open ─ 59s: answer

    `waitTime` runs PAST our deadline on purpose. An abort only stops the terminal
    while MarketPay still holds our request open: with a waitTime that ended before
    the abort point, MarketPay would already have answered 202 and the abort would get a
    useless 409. Held open, the abort gets 204 and the open call itself returns the
    terminal's NOK within about a second (seen live: try-11). Waiting ~150 s also covers
    the terminal's own ~120 s card timeout, so a late answer still reaches us.
    """

    # Answer 1 s before the deadline; a deadline of 3 s or less answers at two thirds.
    margin = min(RESPONSE_MARGIN_SECONDS, deadline_seconds / 3)
    answer_within = deadline_seconds - margin
    # The abort point: 10 s before the deadline, or halfway for short deadlines (so they
    # don't abort the instant they're sent) — but always leaving the abort time to go out
    # before we answer: a tiny deadline aborts at a third.
    resolve_within = min(
        max(deadline_seconds - CLEANUP_RESERVE_SECONDS, deadline_seconds / 2),
        answer_within - margin,
    )
    wait_time = min(MAX_WAIT_TIME_SECONDS, deadline_seconds + WAIT_BEYOND_DEADLINE_SECONDS)

    return CallBudget(
        wait_time=wait_time,
        read_timeout=wait_time + READ_MARGIN_SECONDS,
        resolve_within=resolve_within,
        answer_within=answer_within,
    )
