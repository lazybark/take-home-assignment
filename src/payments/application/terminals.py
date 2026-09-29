"""Terminal views: MarketPay's connection status combined with our lock."""

from pydantic import BaseModel, ConfigDict

from payments.domain.marketpay.gateway import MarketPayGateway
from payments.domain.marketpay.models import TerminalSession
from payments.domain.models import TerminalLock
from payments.domain.repository import PaymentRepository


class TerminalView(BaseModel):
    model_config = ConfigDict(frozen=True)

    session: TerminalSession
    lock: TerminalLock | None  # None: free for a new payment


def list_terminals(
    marketpay: MarketPayGateway, repo: PaymentRepository, connected: bool | None = None
) -> list[TerminalView]:
    sessions = marketpay.list_terminals(connected=connected)
    locks = repo.terminal_locks(s.terminal_id for s in sessions)

    return [TerminalView(session=s, lock=locks.get(s.terminal_id)) for s in sessions]
