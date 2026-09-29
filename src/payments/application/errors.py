"""Errors the use cases raise; the API maps them to HTTP answers."""

from payments.domain.models import Payment, TerminalLock


class IdempotencyMismatch(Exception):
    """The reference already names a payment with a different terminal/amount/currency."""

    def __init__(self, existing: Payment, fields: list[str], candidate: Payment) -> None:
        self.existing = existing
        self.fields = fields
        differences = ", ".join(
            f"{name} {getattr(existing, name)!r} (now {getattr(candidate, name)!r})"
            for name in fields
        )

        super().__init__(
            f"Reference {existing.reference!r} is already used by payment {existing.id} "
            f"with a different {differences}. A new payment needs a new reference."
        )


class TerminalBusy(Exception):
    """Another payment holds the terminal until its outcome is confirmed."""

    def __init__(self, lock: TerminalLock) -> None:
        self.lock = lock

        super().__init__(
            f"Terminal {lock.terminal_id} is locked by payment {lock.payment_id} "
            f"(reference {lock.reference!r}) until its outcome is confirmed."
        )
