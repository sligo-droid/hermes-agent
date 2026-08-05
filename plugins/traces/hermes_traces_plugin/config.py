"""Configuration for the bundled Hermes Traces plugin."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_EXECUTABLE = (
    "/home/droid/.local/share/hermes-traces-cli/node_modules/.bin/traces"
)
PUBLIC_BASE_URL = "https://sligo.sligolabs.com/traces/"


def _current_hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except (ImportError, RuntimeError):
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))


@dataclass(frozen=True)
class Config:
    hermes_home: Path
    executable: str = DEFAULT_EXECUTABLE
    timeout: float = 30.0
    base_url: str = PUBLIC_BASE_URL

    def __post_init__(self) -> None:
        object.__setattr__(self, "hermes_home", Path(self.hermes_home).expanduser())
        if not str(self.executable).strip():
            raise ValueError("traces executable must not be empty")
        if isinstance(self.timeout, bool) or not isinstance(self.timeout, (int, float)):
            raise ValueError("timeout must be numeric")
        if not 0 < float(self.timeout) <= 300:
            raise ValueError("timeout must be between 0 and 300 seconds")

        parsed = urlsplit(self.base_url)
        if (
            self.base_url != PUBLIC_BASE_URL
            or parsed.scheme != "https"
            or parsed.hostname != "sligo.sligolabs.com"
            or parsed.path != "/traces/"
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url must be the exact HTTPS Sligo traces path")

    @classmethod
    def from_env(cls) -> "Config":
        try:
            timeout = float(os.environ.get("HERMES_TRACES_TIMEOUT", "30"))
        except ValueError as exc:
            raise ValueError("invalid HERMES_TRACES_TIMEOUT") from exc
        return cls(
            hermes_home=_current_hermes_home(),
            executable=os.environ.get(
                "HERMES_TRACES_EXECUTABLE", DEFAULT_EXECUTABLE
            ),
            timeout=timeout,
        )

    @property
    def index_path(self) -> Path:
        return self.hermes_home / "state" / "plugins" / "traces" / "index.json"

    @property
    def observer_home(self) -> Path:
        """Private Hermes-shaped store used only by the Traces CLI adapter."""
        return self.hermes_home / "state" / "plugins" / "traces" / "observer"

    def sligo_url(self, slug: str) -> str:
        return f"{self.base_url}{slug}"
