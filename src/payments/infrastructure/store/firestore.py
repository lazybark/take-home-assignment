"""Firestore client construction, connectivity probe, and the Firestore PaymentRepository.

Collections:
  payments/{paymentId}    one document per payment (id derived from the reference)
  payments/{paymentId}/history/{entry}  its timeline, written with each change
                          (domain.history)
  terminals/{terminalId}  {"terminal_id", "lock": {...} | None,
                           "verify_payment_id": str | None, "updated_at"}
"""

import functools
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from google.api_core import exceptions as google_errors
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from payments.config import Settings
from payments.domain.history import HistoryEntry, with_history
from payments.domain.listing import PaymentPage, PaymentQuery, matches, scan_page
from payments.domain.models import Payment, TerminalLock, payment_id_for
from payments.domain.repository import OPEN_STATES, OPERATIONS, Change, StoreUnavailable
from payments.domain.terminal_lock import (
    BeginDecision,
    BeginKind,
    LockAction,
    UpdateKind,
    UpdateResult,
    decide_begin,
    lock_action,
    lock_for,
)
from payments.domain.verification import flag_after_settling

PAYMENTS = "payments"
TERMINALS = "terminals"
HISTORY = "history"  # subcollection of each payment: payments/{id}/history/{entry id}
_TIMEOUT_SECONDS = 10.0


def make_client(settings: Settings) -> firestore.Client:
    # Credentials come from GOOGLE_APPLICATION_CREDENTIALS (service account key), or the
    # emulator when FIRESTORE_EMULATOR_HOST is set — both are read by the library itself.
    return firestore.Client(
        project=settings.google_cloud_project, database=settings.firestore_database
    )


def ping(client: firestore.Client, timeout: float = 5.0) -> None:
    """Round-trip to Firestore: proves credentials, project and database. Raises on failure."""

    client.collection("_health").document("ping").get(timeout=timeout)


# Worth retrying: contention, overload, a temporary outage, a timed-out call.
_TRANSIENT = (
    google_errors.Aborted,
    google_errors.ServiceUnavailable,
    google_errors.DeadlineExceeded,
    google_errors.InternalServerError,
    google_errors.ResourceExhausted,
    google_errors.Unknown,
    google_errors.Cancelled,
    google_errors.RetryError,
)


def _translating[R](method: Callable[..., R]) -> Callable[..., R]:
    """Turn Google client errors into StoreUnavailable, so the service needn't know them."""

    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)

        except _TRANSIENT as exc:
            raise StoreUnavailable(
                f"Firestore: {type(exc).__name__}: {exc}", transient=True
            ) from exc

        except google_errors.GoogleAPICallError as exc:  # permission, bad request, ...
            raise StoreUnavailable(
                f"Firestore: {type(exc).__name__}: {exc}", transient=False
            ) from exc

        except ValueError as exc:
            # The client library retries a transaction aborted by contention 5 times, then
            # raises ValueError("Failed to commit transaction in 5 attempts.") from Aborted.
            if isinstance(exc.__cause__, google_errors.Aborted) or "commit transaction" in str(exc):
                raise StoreUnavailable(f"Firestore: {exc}", transient=True) from exc
            raise

    return wrapper


class FirestorePaymentRepository:
    def __init__(self, client: firestore.Client) -> None:
        self._client = client
        self._payments = client.collection(PAYMENTS)
        self._terminals = client.collection(TERMINALS)

    @_translating
    def begin(self, candidate: Payment) -> BeginDecision:
        payment_ref = self._payments.document(str(candidate.id))
        terminal_ref = self._terminals.document(candidate.terminal_id)

        # The body may run several times if Firestore aborts it under contention:
        # it only reads, decides (pure) and stages writes — no side effects.
        @firestore.transactional
        def run(transaction: firestore.Transaction) -> BeginDecision:
            payment_snap = payment_ref.get(transaction=transaction, timeout=_TIMEOUT_SECONDS)
            terminal_snap = terminal_ref.get(transaction=transaction, timeout=_TIMEOUT_SECONDS)
            terminal = terminal_snap.to_dict() if terminal_snap.exists else None

            decision = decide_begin(
                candidate,
                _payment_from(payment_snap.to_dict()) if payment_snap.exists else None,
                _lock_from(terminal),
            )
            if decision.kind is BeginKind.CREATED:
                flag = _verify_from(terminal)
                stored, entry = with_history(None, candidate)
                transaction.set(payment_ref, _payment_to(stored))
                _set_history(transaction, payment_ref, entry)
                transaction.set(
                    terminal_ref,
                    _terminal_to(candidate.terminal_id, lock_for(candidate), flag),
                )
                decision = decision.model_copy(update={"verify_payment_id": flag})

            return decision

        return run(self._client.transaction())

    @_translating
    def update(self, payment_id: UUID, change: Change) -> UpdateResult:
        payment_ref = self._payments.document(str(payment_id))

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> UpdateResult:
            payment_snap = payment_ref.get(transaction=transaction, timeout=_TIMEOUT_SECONDS)

            if not payment_snap.exists:
                raise KeyError(payment_id)

            current = _payment_from(payment_snap.to_dict())
            new = change(current)

            if new == current:
                return UpdateResult(kind=UpdateKind.UNCHANGED, payment=current)

            terminal_ref = self._terminals.document(new.terminal_id)
            terminal_snap = terminal_ref.get(transaction=transaction, timeout=_TIMEOUT_SECONDS)
            terminal = terminal_snap.to_dict() if terminal_snap.exists else None
            lock, flag = _lock_from(terminal), _verify_from(terminal)

            match lock_action(new, lock):
                case LockAction.BUSY:
                    return UpdateResult(
                        kind=UpdateKind.TERMINAL_BUSY, payment=current, blocking_lock=lock
                    )
                case LockAction.ACQUIRE:
                    transaction.set(
                        terminal_ref, _terminal_to(new.terminal_id, lock_for(new), flag)
                    )
                case LockAction.RELEASE:
                    transaction.set(
                        terminal_ref,
                        _terminal_to(new.terminal_id, None, flag_after_settling(new, flag)),
                    )

            new, entry = with_history(current, new)
            transaction.set(payment_ref, _payment_to(new))
            _set_history(transaction, payment_ref, entry)

            return UpdateResult(kind=UpdateKind.UPDATED, payment=new)

        return run(self._client.transaction())

    @_translating
    def get(self, payment_id: UUID) -> Payment | None:
        snapshot = self._payments.document(str(payment_id)).get(timeout=_TIMEOUT_SECONDS)

        return _payment_from(snapshot.to_dict()) if snapshot.exists else None

    @_translating
    def history(self, payment_id: UUID) -> list[HistoryEntry]:
        entries = (
            self._payments.document(str(payment_id))
            .collection(HISTORY)
            .order_by("number")
            .stream(timeout=_TIMEOUT_SECONDS)
        )

        return [HistoryEntry.model_validate(snapshot.to_dict()) for snapshot in entries]

    @_translating
    def open_payments(self, terminal_id: str | None = None) -> list[Payment]:
        # Two equality ("in") queries — no composite index needed — merged by id.
        found: dict[str, Payment] = {}

        for field, values in (
            ("state", [s.value for s in OPEN_STATES]),
            ("operation", [o.value for o in OPERATIONS]),
        ):
            query = self._payments.where(filter=FieldFilter(field, "in", values))

            if terminal_id is not None:
                query = query.where(filter=FieldFilter("terminal_id", "==", terminal_id))

            for snapshot in query.stream(timeout=_TIMEOUT_SECONDS):
                found[snapshot.id] = _payment_from(snapshot.to_dict())

        return list(found.values())

    @_translating
    def list_payments(self, query: PaymentQuery, scan_limit: int) -> PaymentPage:
        # A reference names at most one payment (its id is derived from the reference).
        if query.reference is not None:
            snapshot = self._payments.document(str(payment_id_for(query.reference))).get(
                timeout=_TIMEOUT_SECONDS
            )
            payment = _payment_from(snapshot.to_dict()) if snapshot.exists else None
            items = [payment] if payment is not None and matches(payment, query) else []

            return PaymentPage(items=items, next_cursor=None)

        # Only created_at is filtered and ordered in Firestore: that needs no composite
        # index (a deploy step anyone running this against their own project would need).
        # State / terminal are filtered while scanning. Firestore breaks ties on the
        # document id, in the same direction — the order `domain.listing` defines.
        q = self._payments.order_by("created_at", direction=firestore.Query.DESCENDING)

        if query.created_after is not None:
            q = q.where(filter=FieldFilter("created_at", ">", query.created_after))

        if query.created_before is not None:
            q = q.where(filter=FieldFilter("created_at", "<", query.created_before))

        if query.after is not None:
            # Resume after that exact document: a snapshot cursor also carries its id, so
            # payments created in the same instant are neither skipped nor repeated.
            anchor = self._payments.document(str(query.after.id)).get(timeout=_TIMEOUT_SECONDS)
            q = q.start_after(anchor if anchor.exists else {"created_at": query.after.created_at})

        stream = q.limit(scan_limit).stream(timeout=_TIMEOUT_SECONDS)

        return scan_page((_payment_from(s.to_dict()) for s in stream), query, scan_limit)

    @_translating
    def clear_verification(self, terminal_id: str, payment_id: UUID) -> None:
        terminal_ref = self._terminals.document(terminal_id)

        @firestore.transactional
        def run(transaction: firestore.Transaction) -> None:
            snap = terminal_ref.get(transaction=transaction, timeout=_TIMEOUT_SECONDS)
            terminal = snap.to_dict() if snap.exists else None
            if _verify_from(terminal) == payment_id:
                transaction.set(terminal_ref, _terminal_to(terminal_id, _lock_from(terminal), None))

        run(self._client.transaction())

    @_translating
    def terminals_to_verify(self) -> dict[str, UUID]:
        # Any non-empty id sorts after "": a single-field filter, no composite index.
        query = self._terminals.where(filter=FieldFilter("verify_payment_id", ">", ""))
        found = {}

        for snap in query.stream(timeout=_TIMEOUT_SECONDS):
            if (flag := _verify_from(snap.to_dict())) is not None:
                found[snap.id] = flag

        return found

    @_translating
    def terminal_locks(self, terminal_ids: Iterable[str]) -> dict[str, TerminalLock]:
        refs = [self._terminals.document(t) for t in terminal_ids]
        locks: dict[str, TerminalLock] = {}

        for snapshot in self._client.get_all(refs, timeout=_TIMEOUT_SECONDS):
            if snapshot.exists and (lock := _lock_from(snapshot.to_dict())) is not None:
                locks[lock.terminal_id] = lock

        return locks


# --- Document mapping ---------------------------------------------------------------
# Datetimes stay native (Firestore timestamps, queryable); UUIDs and enums become strings.


def _payment_to(payment: Payment) -> dict[str, Any]:
    native_datetimes = {name: value for name, value in payment if isinstance(value, datetime)}

    return payment.model_dump(mode="json") | native_datetimes


def _set_history(transaction, payment_ref, entry: HistoryEntry | None) -> None:
    """Staged in the change's own transaction. The id is fixed per change, so a re-run
    transaction (or a retry after a lost commit) rewrites the entry instead of adding one."""

    if entry is not None:
        document = payment_ref.collection(HISTORY).document(entry.entry_id)
        transaction.set(document, entry.model_dump(mode="json") | {"at": entry.at})


def _payment_from(data: dict[str, Any] | None) -> Payment:
    return Payment.model_validate(data)


def _terminal_to(
    terminal_id: str, lock: TerminalLock | None, verify: UUID | None
) -> dict[str, Any]:
    lock_data = None

    if lock is not None:
        lock_data = lock.model_dump(mode="json") | {"locked_at": lock.locked_at}

    return {
        "terminal_id": terminal_id,
        "lock": lock_data,
        "verify_payment_id": str(verify) if verify is not None else None,
        "updated_at": datetime.now(UTC),
    }


def _verify_from(data: dict[str, Any] | None) -> UUID | None:
    flag = (data or {}).get("verify_payment_id")

    return UUID(flag) if flag else None


def _lock_from(data: dict[str, Any] | None) -> TerminalLock | None:
    lock = (data or {}).get("lock")

    return TerminalLock.model_validate(lock) if lock else None
