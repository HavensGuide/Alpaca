"""Runtime settings, read from environment variables with sensible defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Where the SQLite database and the encryption key live.
    data_dir: Path = field(
        default_factory=lambda: Path(
            os.environ.get("ALPACA_MANAGER_DATA_DIR", Path.home() / ".alpaca_manager")
        )
    )
    # How often (seconds) price rules are evaluated.
    poll_seconds: int = field(
        default_factory=lambda: int(os.environ.get("ALPACA_MANAGER_POLL_SECONDS", "15"))
    )
    # Timezone used for scheduled orders. US markets run on New York time.
    timezone: str = field(
        default_factory=lambda: os.environ.get("ALPACA_MANAGER_TIMEZONE", "America/New_York")
    )
    # Start the background scheduler (disabled in tests).
    run_scheduler: bool = field(
        default_factory=lambda: _bool(os.environ.get("ALPACA_MANAGER_SCHEDULER"), True)
    )
    # Create a simulated account on first start so the UI can be tried without keys.
    seed_demo: bool = field(
        default_factory=lambda: _bool(os.environ.get("ALPACA_MANAGER_DEMO"), False)
    )

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'alpaca_manager.db'}"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
