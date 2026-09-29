from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import httpx
import pytest

from payments.app import create_app
from payments.config import Settings
from payments.domain.models import Owner
from payments.infrastructure.marketpay.client import MarketPayClient
from payments.infrastructure.store.memory import InMemoryPaymentRepository
from payments.log import configure_logging

Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True, scope="session")
def _logging_as_in_the_app() -> None:
    """Route structlog through stdlib logging, as create_app does, before any test runs:
    a test that reads logs without building an app must not depend on another having run."""
    configure_logging("INFO", "console")


@pytest.fixture
def settings() -> Settings:
    return Settings(
        marketpay_base_url="https://marketpay.test",
        marketpay_store_code="STORE-1",
        marketpay_ecr_id="TEST_ECR_ID",
        marketpay_client_cert="unused.crt",
        marketpay_client_key="unused.key",
        marketpay_user_agent="PaymentsTest/1.0",
        google_cloud_project="demo-test",
        log_format="console",
    )


@pytest.fixture
def make_marketpay(settings: Settings) -> Callable[[Handler], MarketPayClient]:
    def make(handler: Handler) -> MarketPayClient:
        return MarketPayClient.from_settings(settings, transport=httpx.MockTransport(handler))

    return make


def fake_db() -> MagicMock:
    """Stand-in for google.cloud.firestore.Client; tests never touch a real database."""
    return MagicMock(name="firestore.Client")


@pytest.fixture
def db() -> MagicMock:
    return fake_db()


class FakeClock:
    """Time only moves when the code sleeps, so 50s of polling runs instantly."""

    def __init__(self) -> None:
        self.elapsed = 0.0
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        return datetime(2026, 9, 27, 12, 0, tzinfo=UTC) + timedelta(seconds=self.elapsed)

    def monotonic(self) -> float:
        return self.elapsed

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.elapsed += seconds

    def pause(self, seconds: float, wake) -> None:
        """Like sleep: nothing real to wait for (tests push notifications explicitly)."""
        if not wake.done():
            self.sleep(seconds)

    def wait_for(self, future, timeout: float) -> bool:
        """Give the other thread a moment of *real* time to finish (a fake MarketPay
        answers at once, or blocks on an event such as an abort). If it's still busy,
        fake time jumps forward by the whole timeout."""
        from concurrent.futures import wait

        wait([future], timeout=1.0)
        if not future.done():
            self.elapsed += max(0.0, timeout)
        return future.done()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def repo() -> InMemoryPaymentRepository:
    return InMemoryPaymentRepository()


@pytest.fixture
def owner() -> Owner:
    """This test's "process". Clients built with it behave as one running service."""
    return Owner(instance_id="local-1", boot_id="boot-1")


@pytest.fixture
def make_client(settings, make_marketpay, db, repo, clock, owner):
    """Flask test client whose MarketPay traffic goes to `handler`.

    Pass `boot_id` to get a client for a *restarted* process of the same instance.
    """

    def make(handler: Handler, boot_id: str | None = None):
        process = owner if boot_id is None else owner.model_copy(update={"boot_id": boot_id})
        app = create_app(
            settings,
            marketpay=make_marketpay(handler),
            db=db,
            repo=repo,
            clock=clock,
            owner=process,
        )
        return app.test_client()

    return make
