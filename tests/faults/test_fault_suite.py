"""The fault suite: the brief's faults, run against a simulated terminal
(tests/support/terminal_sim.py), with the money invariants checked against the terminal's OWN
ledger after every scenario.

The per-feature tests elsewhere assert what we expected each script to produce; these don't
trust our expectations. After each scenario:
- no double charge: at most one standing charge per order (and never a refund of money that
  wasn't charged);
- no lost payment / phantom cancel: a failed, declined or cancelled record has no standing
  charge;
- no phantom success: an approved record has exactly one;
- what the POS was told matched what MarketPay held at that moment;
- converged: nothing left pending or unknown, and the terminal is free again;
- the payment's history is exact: 1…n, no gaps or duplicates, ending in the record's state.
"""

import threading

import pytest
from support.terminal_sim import (
    TERMINAL,
    Crash,
    Customer,
    Fault,
    ServiceProcess,
    SimulatedMarketPay,
)

from payments.domain.models import Operation, PaymentState, payment_id_for

REF = "order-7f3a9c"
OTHER = "order-next"
CHARGED_NOTHING = {"failed", "declined", "cancelled"}


def order(reference: str = REF, **extra) -> dict:
    return {"terminalId": TERMINAL, "amount": 1299, "currency": "SEK", "reference": reference}


class World:
    """The simulated terminal, the durable store, the (fake) clock, and our service."""

    def __init__(self, repo, clock, settings) -> None:
        self.sim = SimulatedMarketPay()
        self.repo, self.clock, self.settings = repo, clock, settings
        self.references: set[str] = set()
        self.boots = 0
        self.service = self.start()

    def start(self) -> ServiceProcess:
        self.boots += 1
        return ServiceProcess(self.sim, self.repo, self.clock, self.settings, f"boot-{self.boots}")

    # --- What the POS does --------------------------------------------------------------

    def pay(self, reference: str = REF) -> dict | None:
        """POST /payments; None if our process died meanwhile. Checks the answer."""

        self.references.add(reference)
        response = self.service.post("/payments", json=order(reference))
        if response is None:
            return None

        body = response.get_json()
        self.assert_told_the_truth(reference, body)

        return body

    def cancel(self, reference: str = REF) -> dict | None:
        response = self.service.post(f"/payments/{payment_id_for(reference)}/cancel")
        if response is None:
            return None

        body = response.get_json()
        self.assert_told_the_truth(reference, body)

        return body

    def in_background(self, call) -> tuple[threading.Thread, dict]:
        result: dict = {}
        thread = threading.Thread(target=lambda: result.update(body=call()))
        thread.start()

        return thread, result

    # --- Time passes, recovery runs --------------------------------------------------------

    def converge(self) -> None:
        """The terminal finishes what it was doing; later, someone runs reconcile."""

        self.sim.wait_until_idle()
        self.clock.elapsed += 200  # past every lease and the "never recorded" time rule
        self.service.post("/reconcile")
        self.service.post("/reconcile")

    def restart(self) -> None:
        """The old process is gone (crashed); a new boot starts on the same store."""

        self.service.die()
        self.service = self.start()

    # --- The invariants ------------------------------------------------------------------

    def assert_told_the_truth(self, reference: str, body: dict) -> None:
        state, charges = body.get("state"), self.sim.standing_charges(reference)

        if state == "approved":
            assert charges == 1, f"POS told approved, MarketPay holds {charges} charges"
        elif state in CHARGED_NOTHING:
            assert charges == 0, f"POS told {state}, MarketPay holds {charges} charges"

    def assert_history_matches(self, reference: str, payment) -> None:
        """The history is exact whatever happened: numbered 1…n without gaps or duplicates,
        started pending, and ends in the state (and terminal hold) the record has now."""

        entries = self.repo.history(payment.id)
        assert [e.number for e in entries] == list(range(1, len(entries) + 1)), reference
        assert len(entries) == payment.history_length, f"{reference}: history count off"
        assert entries[0].state is PaymentState.PENDING, reference
        assert (entries[-1].state, entries[-1].operation) == (payment.state, payment.operation), (
            f"{reference}: history ends {entries[-1].state}, the record is {payment.state}"
        )

    def assert_invariants(self, converged: bool = True) -> None:
        for reference in self.references:
            charges = self.sim.standing_charges(reference)
            assert charges >= 0, f"{reference}: refunded money that was never charged"
            assert charges <= 1, f"{reference}: DOUBLE CHARGE ({charges} standing)"

            payment = self.repo.payments.get(payment_id_for(reference))
            if payment is None:
                assert charges == 0, f"{reference}: a charge we have no record of"
                continue
            if converged:
                assert payment.state not in (PaymentState.PENDING, PaymentState.UNKNOWN), (
                    f"{reference}: never converged ({payment.state}, {payment.state_reason})"
                )
                assert payment.operation is None, f"{reference}: still holds the terminal"

            self.assert_history_matches(reference, payment)
            if payment.state is PaymentState.APPROVED:
                assert charges == 1, f"{reference}: approved, but MarketPay holds {charges}"
            elif payment.state.value in CHARGED_NOTHING:
                assert charges == 0, f"{reference}: {payment.state}, but a charge stands"

        if converged:
            assert not self.repo.locks, f"terminal still locked: {self.repo.locks}"


@pytest.fixture
def world(repo, clock, settings) -> World:
    return World(repo, clock, settings)


# --- The customer, no faults ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("customer", "state"),
    [
        (Customer.TAPS, "approved"),
        (Customer.DECLINED, "declined"),
        (Customer.NEVER_TAPS, "failed"),  # our abort, while the request is open, stops it
        (Customer.TAPS_LATE, "approved"),  # the abort is too late: the truth wins
        (Customer.SLOW, "approved"),  # 202, then found through last-transaction
        (Customer.PARTIAL, "declined"),  # reversed at once, reported declined
    ],
)
def test_customer_behaviour(world, customer, state):
    world.sim.customers[REF] = customer

    assert world.pay()["state"] == state
    world.assert_invariants()


def test_happy_path_is_one_call(world):
    """No retries, reversals or extra lookups on the normal path."""
    assert world.pay()["state"] == "approved"
    assert world.sim.calls == ["process"]
    world.assert_invariants()


# --- Network faults on the payment ----------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "customer"),
    [
        (Fault.NOT_SENT, Customer.TAPS),  # DNS blip: re-sent, charged once
        (Fault.LOST_REQUEST, Customer.TAPS),  # never arrived: no charge, then `failed`
        (Fault.LOST_RESPONSE, Customer.TAPS),  # charged, answer lost: found by polling
        (Fault.LOST_RESPONSE, Customer.DECLINED),
        (Fault.LOST_RESPONSE, Customer.NEVER_TAPS),
        (Fault.LOST_RESPONSE, Customer.TAPS_LATE),
    ],
)
def test_network_fault_on_the_payment(world, fault, customer):
    world.sim.customers[REF] = customer
    world.sim.inject("process", fault)

    world.pay()
    world.converge()

    world.assert_invariants()
    assert world.sim.purchases_sent(REF) <= 1


def test_lookups_failing_after_a_202(world):
    world.sim.customers[REF] = Customer.SLOW
    world.sim.fail_lookups(5)

    assert world.pay()["state"] == "approved"
    world.assert_invariants()


def test_delayed_request_that_charges_after_we_said_failed(world):
    """The request is delayed in the network; we conclude it never ran; then it arrives and
    charges. The record must not stay `failed` while MarketPay holds the charge: the next
    payment on the terminal re-checks it first."""

    world.sim.inject("process", Fault.DELAYED)
    world.pay()
    world.converge()
    assert world.repo.payments[payment_id_for(REF)].state is PaymentState.FAILED

    world.sim.deliver_delayed()  # it arrives, and the customer taps
    assert world.sim.standing_charges(REF) == 1

    assert world.pay(OTHER)["state"] == "approved"
    world.assert_invariants()


# --- Duplicate submits and concurrency ------------------------------------------------------


def test_duplicate_submit_is_one_charge(world):
    first, second = world.pay(), world.pay()

    assert first["id"] == second["id"] and second["state"] == "approved"
    assert world.sim.purchases_sent(REF) == 1
    world.assert_invariants()


def test_retrying_a_failed_order_does_not_charge(world):
    world.sim.customers[REF] = Customer.NEVER_TAPS
    assert world.pay()["state"] == "failed"
    world.sim.customers[REF] = Customer.TAPS

    assert world.pay()["state"] == "failed"  # a reference is one attempt
    assert world.sim.purchases_sent(REF) == 1
    world.assert_invariants()


def test_concurrent_submits_of_the_same_order(world):
    world.sim.customers[REF] = Customer.ON_SIGNAL
    first, first_body = world.in_background(world.pay)
    world.sim.wait_until_open()

    second = world.pay()  # while the first is still at the terminal
    world.sim.release()
    first.join(10)

    assert first_body["body"]["state"] == "approved"
    assert second["id"] == first_body["body"]["id"]
    assert world.sim.purchases_sent(REF) == 1
    world.converge()
    world.assert_invariants()


def test_another_order_on_a_busy_terminal_is_refused(world):
    world.sim.customers[REF] = Customer.ON_SIGNAL
    first, first_body = world.in_background(world.pay)
    world.sim.wait_until_open()

    response = world.service.post("/payments", json=order(OTHER))
    world.sim.release()
    first.join(10)

    assert (response.status_code, response.get_json()["code"]) == (409, "terminal_busy")
    assert first_body["body"]["state"] == "approved"
    world.references.add(OTHER)
    world.assert_invariants()


# --- Cancel -----------------------------------------------------------------------------


def test_cancel_reverses_the_charge(world):
    world.pay()
    assert world.cancel()["state"] == "cancelled"
    world.assert_invariants()


@pytest.mark.parametrize("fault", [Fault.NOT_SENT, Fault.LOST_RESPONSE, Fault.LOST_REQUEST])
def test_network_fault_on_the_reversal(world, fault):
    """A lost reversal response must not leave a cancelled charge standing, nor a reversal
    that never arrived reported as done."""

    world.pay()
    world.sim.inject("cancel", fault)

    world.cancel()
    world.converge()

    world.assert_invariants()
    assert sum(1 for c in world.sim.calls if c == "cancel") <= 2  # never re-sent blindly


def test_cancel_twice_reverses_once(world):
    world.pay()
    world.cancel()

    assert world.cancel()["state"] == "cancelled"
    assert world.sim.calls.count("cancel") == 1
    world.assert_invariants()


def test_cancel_while_the_customer_is_at_the_terminal(world):
    world.sim.customers[REF] = Customer.ON_SIGNAL
    payment, payment_body = world.in_background(world.pay)
    world.sim.wait_until_open()

    cancel = world.cancel()
    payment.join(10)

    # The cancel aborts, then waits for the payment's own request to record the outcome.
    # On the fake clock that wait can end before the other thread has recorded it; the
    # cancel then answers `pending` (it never takes over a request that is still alive).
    assert cancel["state"] in ("cancelled", "pending")
    assert payment_body["body"]["state"] == "cancelled"
    world.assert_invariants()


def test_cancel_of_a_payment_approved_despite_our_abort_refunds_it(world):
    world.sim.customers[REF] = Customer.TAPS_LATE
    assert world.pay()["state"] == "approved"

    assert world.cancel()["state"] == "cancelled"  # a REFUND, as MarketPay requires
    world.assert_invariants()


def test_customer_gone_before_the_reversal_keeps_the_charge_approved(world):
    world.pay()
    world.sim.reversal_customers[REF] = Customer.NEVER_TAPS

    world.cancel()
    world.converge()

    assert world.repo.payments[payment_id_for(REF)].state is PaymentState.APPROVED
    world.assert_invariants()


# --- Firestore failing ------------------------------------------------------------------------


def test_firestore_contention_is_absorbed(world):
    world.repo.fail(2)  # e.g. ABORTED twice
    assert world.pay()["state"] == "approved"
    world.assert_invariants()


def test_lost_commit_is_not_a_second_payment(world):
    world.repo.fail_after_commit(1)
    assert world.pay()["state"] == "approved"
    assert world.sim.purchases_sent(REF) == 1
    world.assert_invariants()


def test_contention_on_the_final_write_is_absorbed(world):
    world.sim.on_answer = lambda: world.repo.fail(2)
    assert world.pay()["state"] == "approved"
    world.assert_invariants()


def test_store_down_after_the_charge(world):
    """The POS still hears the truth; the record catches up when the store is back."""
    world.sim.on_answer = lambda: world.repo.fail(10_000)
    assert world.pay()["state"] == "approved"

    world.sim.on_answer = None
    world.repo.fail(0)
    world.converge()
    world.assert_invariants()


# --- Crashes --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fault", "customer", "state"),
    [
        (Fault.CRASH_BEFORE, Customer.TAPS, PaymentState.FAILED),  # died while sending
        (Fault.CRASH_AFTER, Customer.TAPS, PaymentState.APPROVED),  # charged, answer lost
        (Fault.CRASH_AFTER, Customer.DECLINED, PaymentState.DECLINED),
    ],
)
def test_crash_around_sending_the_payment(world, fault, customer, state):
    world.sim.customers[REF] = customer
    world.sim.inject("process", fault)

    assert world.pay() is None  # the POS lost its connection
    world.restart()
    world.converge()

    assert world.repo.payments[payment_id_for(REF)].state is state
    world.assert_invariants()


@pytest.mark.parametrize("taps", [False, True], ids=["terminal_times_out", "customer_taps"])
def test_crash_while_the_customer_is_at_the_terminal(world, taps):
    world.sim.customers[REF] = Customer.ON_SIGNAL
    payment, _ = world.in_background(world.pay)
    world.sim.wait_until_open()

    world.service.die()
    if taps:
        world.sim.release()
    payment.join(10)
    world.restart()
    world.converge()

    expected = PaymentState.APPROVED if taps else PaymentState.FAILED
    assert world.repo.payments[payment_id_for(REF)].state is expected
    world.assert_invariants()


def test_crash_while_recording_the_outcome(world):
    """MarketPay answered 201 approved; we die in the very write that records it."""

    world.service.crash_when = lambda current, proposed: (
        current.operation is Operation.PURCHASE and proposed.operation is None
    )
    assert world.pay() is None
    world.restart()
    world.converge()

    assert world.repo.payments[payment_id_for(REF)].state is PaymentState.APPROVED
    world.assert_invariants()


def test_crash_then_the_pos_retries_instead_of_reconcile(world):
    world.sim.inject("process", Fault.CRASH_AFTER)
    assert world.pay() is None
    world.restart()

    assert world.pay()["state"] == "approved"  # the same order, on the new process
    assert world.sim.purchases_sent(REF) == 1
    world.assert_invariants()


@pytest.mark.parametrize(
    ("fault", "state"),
    [
        (Fault.CRASH_BEFORE, PaymentState.APPROVED),  # the reversal never left: still charged
        (Fault.CRASH_AFTER, PaymentState.CANCELLED),  # it was applied: reversed
    ],
)
def test_crash_around_sending_the_reversal(world, fault, state):
    world.pay()
    world.sim.inject("cancel", fault)

    assert world.cancel() is None
    world.restart()
    world.converge()

    assert world.repo.payments[payment_id_for(REF)].state is state
    world.assert_invariants()


def test_a_dead_process_cannot_write(world):
    """The harness itself: after a crash, the old process can't touch the store."""

    world.service.die()
    with pytest.raises(Crash):
        world.service.app.extensions["repo"].get(payment_id_for(REF))
