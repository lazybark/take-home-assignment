"""Service configuration, read from environment variables only."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore", frozen=True)

    # MarketPay Cloud API (see marketpay-creds/marketpay-staging.env)
    marketpay_base_url: str
    marketpay_store_code: str
    marketpay_ecr_id: str
    marketpay_client_cert: Path
    marketpay_client_key: Path
    marketpay_user_agent: str = Field(default="TestAssignment/1.0", min_length=1)
    marketpay_currency: str = "752"
    marketpay_terminal_id: str | None = None
    marketpay_connect_timeout_seconds: float = 5.0
    marketpay_read_timeout_seconds: float = 10.0

    # Firestore. Credentials (GOOGLE_APPLICATION_CREDENTIALS) or FIRESTORE_EMULATOR_HOST
    # are read by the client library itself.
    google_cloud_project: str
    firestore_database: str = "(default)"

    # MarketPay notifications (the bonus webhook). Off unless both are set. The base URL must
    # be public HTTPS (e.g. a cloudflared tunnel); the secret signs each notification URL.
    notification_base_url: str | None = None
    notification_secret: SecretStr | None = None

    # Identity of this service instance, used for payment leases after a restart.
    instance_id: str = "default"

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # Converge on restart: settle what an earlier boot left open, once, when the service
    # starts (not a timer). The prod entrypoint turns it on; dev (two processes under the
    # reloader) and tests leave it off.
    reconcile_on_start: bool = False

    @model_validator(mode="after")
    def _notifications_need_both(self) -> "Settings":
        if (self.notification_base_url is None) != (self.notification_secret is None):
            raise ValueError("set both NOTIFICATION_BASE_URL and NOTIFICATION_SECRET, or neither")
        if self.notification_base_url and not self.notification_base_url.startswith("https://"):
            raise ValueError("NOTIFICATION_BASE_URL must be public HTTPS (MarketPay calls it)")
        if self.notification_secret is not None and len(self.notification_secret) < 32:
            raise ValueError("NOTIFICATION_SECRET must be at least 32 characters")
        return self
