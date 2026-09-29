"""Concurrent writers against the store's terminal lock: exactly one wins."""

import threading
from datetime import UTC, datetime

from support.marketpay_fakes import TERMINAL

from payments.domain.models import Operation, Payment, PaymentState, payment_id_for
from payments.domain.terminal_lock import BeginKind
from payments.infrastructure.store.memory import InMemoryPaymentRepository

T0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)


def payment(reference: str = "a", state=PaymentState.PENDING, terminal=TERMINAL, at=T0):
    return Payment(
        id=payment_id_for(reference),
        terminal_id=terminal,
        amount=100,
        currency="SEK",
        reference=reference,
        state=state,
        created_at=at,
        updated_at=at,
    )


def test_concurrent_purchase_and_undo_on_one_terminal_lock_it_exactly_once():
    # An approved payment's reversal and a new purchase race for the same terminal.
    repo = InMemoryPaymentRepository()
    approved = payment("old", state=PaymentState.APPROVED)
    repo.payments[approved.id] = approved
    barrier = threading.Barrier(2)
    results = {}

    def undo():
        barrier.wait()
        results["undo"] = repo.update(
            approved.id, lambda cur: cur.model_copy(update={"operation": Operation.REVERSAL})
        ).kind

    def purchase():
        barrier.wait()
        results["purchase"] = repo.begin(
            payment("new").model_copy(update={"operation": Operation.PURCHASE})
        ).kind

    threads = [threading.Thread(target=undo), threading.Thread(target=purchase)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [k for k, v in results.items() if v in ("updated", "created")]
    assert len(winners) == 1
    assert len(repo.locks) == 1


def test_concurrent_begins_on_one_terminal_lock_it_exactly_once():
    repo = InMemoryPaymentRepository()
    barrier = threading.Barrier(8)
    kinds: list[BeginKind] = []

    def start(i: int) -> None:
        barrier.wait()
        kinds.append(
            repo.begin(payment(f"order-{i}").model_copy(update={"created_by": str(i)})).kind
        )

    threads = [threading.Thread(target=start, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert kinds.count(BeginKind.CREATED) == 1
    assert kinds.count(BeginKind.TERMINAL_BUSY) == 7
