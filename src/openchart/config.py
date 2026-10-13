"""Runtime configuration.

Values come from environment variables, optionally loaded from a `.env` file in the
working directory. Real environment variables win over `.env`. From M4 on, the setup
wizard writes settings to the database and secrets to the OS keyring; environment
variables stay as an override for headless and Docker installs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

OANDA_HOSTS = {
    "practice": ("https://api-fxpractice.oanda.com", "https://stream-fxpractice.oanda.com"),
    "live": ("https://api-fxtrade.oanda.com", "https://stream-fxtrade.oanda.com"),
}


class ConfigError(ValueError):
    pass


def mask_secret(value: str | None) -> str:
    """Masked form of a secret: only the last 4 characters are kept."""
    if not value:
        return ""
    return "****" + value[-4:] if len(value) > 4 else "****"


@dataclass(frozen=True)
class OandaConfig:
    environment: str = "practice"
    token: str = field(default="", repr=False)
    account_id: str = ""

    @property
    def rest_url(self) -> str:
        return OANDA_HOSTS[self.environment][0]

    @property
    def stream_url(self) -> str:
        return OANDA_HOSTS[self.environment][1]

    @property
    def configured(self) -> bool:
        return bool(self.token and self.account_id)

    def __repr__(self) -> str:
        return (
            f"OandaConfig(environment={self.environment!r}, token={mask_secret(self.token)!r}, "
            f"account_id={self.account_id!r})"
        )


@dataclass(frozen=True)
class Config:
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    oanda: OandaConfig = field(default_factory=OandaConfig)
    store_ticks: bool = True
    backfill_years: float = 2.0
    # Download H1 history as far back as the provider has it (for 1h and above).
    deep_history: bool = True

    @property
    def db_path(self) -> Path:
        return self.data_dir / "openchart.db"

    @property
    def ticks_dir(self) -> Path:
        return self.data_dir / "ticks"


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(env_file: str | os.PathLike[str] | None = ".env") -> Config:
    if env_file is not None and Path(env_file).is_file():
        load_dotenv(env_file, override=False)
    env = os.environ

    environment = env.get("OPENCHART_OANDA_ENV", "practice").strip().lower()
    if environment not in OANDA_HOSTS:
        raise ConfigError(
            f"OPENCHART_OANDA_ENV must be one of {sorted(OANDA_HOSTS)}, got {environment!r}"
        )

    return Config(
        data_dir=Path(env.get("OPENCHART_DATA_DIR", "data")),
        host=env.get("OPENCHART_HOST", "127.0.0.1"),
        port=int(env.get("OPENCHART_PORT", "8000")),
        log_level=env.get("OPENCHART_LOG_LEVEL", "INFO").upper(),
        oanda=OandaConfig(
            environment=environment,
            token=env.get("OPENCHART_OANDA_TOKEN", "").strip(),
            account_id=env.get("OPENCHART_OANDA_ACCOUNT_ID", "").strip(),
        ),
        store_ticks=_bool(env.get("OPENCHART_STORE_TICKS", "true")),
        backfill_years=float(env.get("OPENCHART_BACKFILL_YEARS", "2")),
        deep_history=_bool(env.get("OPENCHART_DEEP_HISTORY", "true")),
    )
