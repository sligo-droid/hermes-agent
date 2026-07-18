"""Generic proxy adapter for user-configured inference providers."""

from __future__ import annotations

from typing import Any, FrozenSet
from urllib.parse import urlparse

from agent.credential_pool import get_custom_provider_pool_key, load_pool
from hermes_cli.auth import has_usable_secret
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.runtime_provider import (
    _get_named_custom_provider,
    _getenv,
    resolve_runtime_provider,
)

_ALLOWED_PATHS: FrozenSet[str] = frozenset(
    {
        "/chat/completions",
        "/responses",
        "/messages",
        "/models",
    }
)
_CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "api-key"})


class ConfiguredProviderAdapter(UpstreamAdapter):
    """Byte-preserving proxy upstream backed by a saved provider entry."""

    def __init__(
        self,
        provider_name: str,
        *,
        display_name: str | None = None,
        key_env: str | None = None,
    ) -> None:
        self._name = provider_name.strip().lower()
        self._display_name = (display_name or provider_name).strip()
        self._key_env = (key_env or "").strip()

    @property
    def name(self) -> str:
        return self._name

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def allowed_paths(self) -> FrozenSet[str]:
        return _ALLOWED_PATHS

    @property
    def auth_hint(self) -> str:
        if self._key_env:
            return f"set {self._key_env} in the active Hermes profile"
        return f"configure credentials for provider {self.name!r}"

    def _configured_entry(self) -> dict[str, Any] | None:
        return _get_named_custom_provider(f"custom:{self.name}")

    @staticmethod
    def _valid_base_url(base_url: str) -> bool:
        parsed = urlparse(base_url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    def _load_pool(self, entry: dict[str, Any]):
        base_url = str(entry.get("base_url") or "").strip().rstrip("/")
        pool_key = get_custom_provider_pool_key(
            base_url,
            provider_name=str(entry.get("name") or self.name),
        )
        if not pool_key:
            return None
        try:
            return load_pool(pool_key)
        except Exception:
            return None

    def is_authenticated(self) -> bool:
        """Check readiness without selecting or rotating a pooled credential."""
        try:
            entry = self._configured_entry()
            if not entry:
                return False
            base_url = str(entry.get("base_url") or "").strip().rstrip("/")
            if not self._valid_base_url(base_url):
                return False

            inline_key = str(entry.get("api_key") or "").strip()
            key_env = str(entry.get("key_env") or "").strip()
            declared_key = _getenv(key_env, "").strip() if key_env else ""
            if has_usable_secret(inline_key) or has_usable_secret(declared_key):
                return True

            pool = self._load_pool(entry)
            if pool is not None:
                pooled = pool.peek()
                return bool(
                    pooled is not None
                    and has_usable_secret(pooled.runtime_api_key)
                )

            if key_env:
                return False
            # Configured local endpoints may intentionally require no credential.
            return True
        except Exception:
            return False

    def _credential_from_runtime(
        self,
        runtime: dict[str, Any],
    ) -> UpstreamCredential:
        api_key = str(runtime.get("api_key") or "").strip()
        if api_key == "no-key-required":
            api_key = ""
        base_url = str(runtime.get("base_url") or "").strip().rstrip("/")
        if not self._valid_base_url(base_url):
            raise RuntimeError(
                f"Configured provider {self.display_name!r} is missing a usable base URL."
            )

        headers = {
            str(key): str(value)
            for key, value in dict(runtime.get("extra_headers") or {}).items()
            if value is not None
        }
        configured_auth = any(
            key.lower() in _CREDENTIAL_HEADERS for key in headers
        )
        api_mode = str(runtime.get("api_mode") or "").strip()
        hostname = (urlparse(base_url).hostname or "").lower()

        bearer = api_key
        if configured_auth:
            bearer = ""
        elif api_key and self.name != "cli-proxy-api" and api_mode == "anthropic_messages":
            headers["x-api-key"] = api_key
            bearer = ""
        elif api_key and hostname.endswith(".openai.azure.com"):
            headers["api-key"] = api_key
            bearer = ""

        return UpstreamCredential(
            bearer=bearer,
            base_url=base_url,
            headers=headers,
            expires_at=runtime.get("expires_at"),
        )

    def get_credential(self) -> UpstreamCredential:
        # Prefixing with custom: forces the named configured-provider path even
        # for a literal providers.custom entry or a name that is also a built-in
        # provider alias. The registry itself gives concrete built-in adapters
        # precedence before constructing this generic adapter.
        requested = f"custom:{self.name}"
        try:
            runtime = resolve_runtime_provider(requested=requested)
        except Exception as exc:
            raise RuntimeError(
                f"Configured provider {self.display_name!r} credentials could not be resolved."
            ) from exc
        return self._credential_from_runtime(runtime)

    def get_retry_credential(
        self,
        *,
        failed_credential: UpstreamCredential,
        status_code: int,
    ) -> UpstreamCredential | None:
        if status_code not in {401, 429} or not failed_credential.bearer:
            return None
        entry = self._configured_entry()
        if not entry:
            return None
        pool = self._load_pool(entry)
        if pool is None:
            return None
        next_entry = pool.mark_exhausted_and_rotate(
            status_code=status_code,
            api_key_hint=failed_credential.bearer,
        )
        if next_entry is None:
            return None
        next_key = str(next_entry.runtime_api_key or "").strip()
        if not next_key or next_key == failed_credential.bearer:
            return None
        runtime = {
            "api_key": next_key,
            "base_url": (
                str(getattr(next_entry, "base_url", "") or "").strip()
                or str(entry.get("base_url") or "").strip()
            ),
            "api_mode": entry.get("api_mode"),
            "extra_headers": entry.get("extra_headers") or {},
            "expires_at": getattr(next_entry, "expires_at", None),
        }
        return self._credential_from_runtime(runtime)


__all__ = ["ConfiguredProviderAdapter"]
