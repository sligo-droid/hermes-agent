"""Upstream adapter registry for the local proxy server.

Concrete built-in adapters take precedence over user-configured providers with
matching names. Configured providers are discovered from config.yaml on every
registry lookup so ``proxy providers`` and ``proxy status`` reflect current
configuration without restarting the CLI process.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Callable

from hermes_cli.config import get_compatible_custom_providers, load_config
from hermes_cli.proxy.adapters.base import UpstreamAdapter
from hermes_cli.proxy.adapters.configured import ConfiguredProviderAdapter
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.openai_codex import OpenAICodexAdapter
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter

logger = logging.getLogger(__name__)

AdapterFactory = Callable[[], UpstreamAdapter]

_BUILTIN_ADAPTERS: dict[str, AdapterFactory] = {
    "nous": NousPortalAdapter,
    "openai-codex": OpenAICodexAdapter,
    "xai": XAIGrokAdapter,
}


@dataclass(frozen=True)
class _ConfiguredAdapterSpec:
    name: str
    display_name: str
    key_env: str = ""

    def build(self) -> UpstreamAdapter:
        return ConfiguredProviderAdapter(
            self.name,
            display_name=self.display_name,
            key_env=self.key_env,
        )


def _normalize_adapter_name(value: str) -> str:
    normalized = (value or "").strip().lower().replace(" ", "-")
    if normalized.startswith("custom:"):
        normalized = normalized.split(":", 1)[1]
    return normalized


def _configured_adapter_specs() -> dict[str, _ConfiguredAdapterSpec]:
    try:
        entries = get_compatible_custom_providers(load_config())
    except Exception as exc:
        logger.warning(
            "proxy: configured provider discovery failed (%s)",
            type(exc).__name__,
        )
        return {}

    # Runtime resolution checks the new keyed providers mapping before the
    # legacy list. Mirror that precedence when duplicate normalized names exist.
    entries.sort(key=lambda entry: 0 if entry.get("provider_key") else 1)

    specs: dict[str, _ConfiguredAdapterSpec] = {}
    for entry in entries:
        provider_key = str(entry.get("provider_key") or "").strip()
        display_name = str(entry.get("name") or provider_key).strip()
        name = _normalize_adapter_name(provider_key or display_name)
        if not name or name in _BUILTIN_ADAPTERS:
            continue
        specs.setdefault(
            name,
            _ConfiguredAdapterSpec(
                name=name,
                display_name=display_name or name,
                key_env=str(entry.get("key_env") or "").strip(),
            ),
        )
    return specs


def list_adapter_names() -> list[str]:
    """Return built-in and currently configured provider names."""
    return sorted({*_BUILTIN_ADAPTERS, *_configured_adapter_specs()})


class _DynamicAdapterRegistry(Mapping[str, AdapterFactory]):
    """Read-only mapping compatibility facade with dynamic configured entries."""

    def _snapshot(self) -> dict[str, AdapterFactory]:
        adapters = dict(_BUILTIN_ADAPTERS)
        for name, spec in _configured_adapter_specs().items():
            adapters.setdefault(name, spec.build)
        return adapters

    def __getitem__(self, key: str) -> AdapterFactory:
        return self._snapshot()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._snapshot())

    def __len__(self) -> int:
        return len(self._snapshot())


ADAPTERS: Mapping[str, AdapterFactory] = _DynamicAdapterRegistry()


def get_adapter(name: str) -> UpstreamAdapter:
    """Instantiate a built-in or currently configured adapter by name."""
    key = _normalize_adapter_name(name)
    builtin = _BUILTIN_ADAPTERS.get(key)
    if builtin is not None:
        return builtin()

    configured = _configured_adapter_specs().get(key)
    if configured is not None:
        return configured.build()

    available = ", ".join(list_adapter_names()) or "(none)"
    raise ValueError(
        f"Unknown proxy upstream provider: {name!r}. Available: {available}"
    )


__all__ = [
    "UpstreamAdapter",
    "ADAPTERS",
    "get_adapter",
    "list_adapter_names",
]
