"""Hermes plugin entry point for the bundled Traces adapter."""

from __future__ import annotations

import threading
from typing import Any

from .config import Config
from .hermes_store import HermesStore
from .publisher import Publisher
from .state import State

_runtime_lock = threading.Lock()
_runtimes: dict[
    tuple[str, str, float], tuple[Config, State, Publisher, HermesStore]
] = {}


def _runtime() -> tuple[Config, State, Publisher, HermesStore]:
    config = Config.from_env()
    key = (
        str(config.hermes_home),
        config.executable,
        float(config.timeout),
    )
    with _runtime_lock:
        runtime = _runtimes.get(key)
        if runtime is None:
            state = State(config.index_path)
            runtime = (
                config,
                state,
                Publisher(config, state),
                HermesStore(config.hermes_home, config.observer_home),
            )
            _runtimes[key] = runtime
        return runtime


def register(ctx: Any) -> None:
    """Register fail-open lineage publication and one artifact provider."""

    subagent_lineage_lock = threading.Lock()
    subagent_lineage: dict[str, dict[str, str]] = {}

    def enqueue_lineage(
        session_id: str,
        *,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        publish_root: bool = True,
    ) -> str | None:
        config, state, publisher, store = _runtime()
        root_id = store.resolve_root(
            session_id,
            parent_session_id=parent_session_id,
            root_session_id=root_session_id,
        )
        if not root_id:
            return None
        root_record = state.create(root_id, "discord")
        keys: list[str] = []
        if session_id != root_id:
            child_record = state.create(session_id, "discord")
            keys.append(child_record["key"])
        if publish_root:
            keys.append(root_record["key"])
        publisher.enqueue_many(keys)
        return root_record["slug"]

    def on_session_end(
        session_id: str | None = None,
        platform: str | None = None,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        **_: Any,
    ) -> str | None:
        if not session_id or (platform or "").strip().lower() != "discord":
            return None
        try:
            return enqueue_lineage(
                session_id,
                parent_session_id=parent_session_id,
                root_session_id=root_session_id,
            )
        except Exception:
            return None

    def on_session_finalize(
        session_id: str | None = None,
        platform: str | None = None,
        **_: Any,
    ) -> None:
        if not session_id or (platform or "").strip().lower() != "discord":
            return
        try:
            _config, state, publisher, store = _runtime()
            root_id = store.resolve_root(session_id)
            if not root_id:
                return
            record = state.create(root_id, "discord")
            # on_session_end normally publishes the completed root. Finalize
            # is a fallback for missed hooks, not another refresh: repeatedly
            # refreshing a rewritten Hermes session gives Traces new source
            # row identities and duplicates already-uploaded events.
            if record.get("status") != "ready":
                publisher.enqueue(record["key"])
        except Exception:
            return

    def on_subagent_start(
        child_session_id: str | None = None,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        platform: str | None = None,
        **_: Any,
    ) -> None:
        child_id = str(child_session_id or "").strip()
        if not child_id or str(platform or "").strip().lower() != "discord":
            return
        with subagent_lineage_lock:
            subagent_lineage[child_id] = {
                "platform": "discord",
                "parent_session_id": str(parent_session_id or "").strip(),
                "root_session_id": str(root_session_id or "").strip(),
            }
            while len(subagent_lineage) > 1_024:
                subagent_lineage.pop(next(iter(subagent_lineage)))

    def on_subagent_stop(
        child_session_id: str | None = None,
        parent_session_id: str | None = None,
        root_session_id: str | None = None,
        platform: str | None = None,
        **_: Any,
    ) -> str | None:
        child_id = str(child_session_id or "").strip()
        if not child_id:
            return None
        with subagent_lineage_lock:
            remembered = subagent_lineage.pop(child_id, {})
        effective_platform = str(
            platform or remembered.get("platform") or ""
        ).strip().lower()
        if effective_platform != "discord":
            return None
        try:
            return enqueue_lineage(
                child_id,
                parent_session_id=(
                    parent_session_id
                    or remembered.get("parent_session_id")
                ),
                root_session_id=(
                    root_session_id
                    or remembered.get("root_session_id")
                ),
                publish_root=False,
            )
        except Exception:
            return None

    def persist_coding_worker(kwargs: dict[str, Any]) -> None:
        if str(kwargs.get("platform") or "").strip().lower() != "discord":
            return
        try:
            config, state, publisher, store = _runtime()
            worker_id = store.write_coding_worker(dict(kwargs))
            root_id = store.resolve_root(
                kwargs.get("parent_session_id"),
                root_session_id=kwargs.get("root_session_id"),
            )
            if not worker_id or not root_id:
                return
            worker_record = state.create(
                worker_id,
                "discord",
                trace_home=config.observer_home,
            )
            root_record = state.create(root_id, "discord")
            # Creating the root record makes the stable protected artifact URL
            # available immediately. The completed root is published exactly
            # once by on_session_end/on_session_finalize.
            _ = root_record
            publisher.enqueue(worker_record["key"])
        except Exception:
            return

    def on_coding_worker_start(**kwargs: Any) -> None:
        # Publish only completed worker transcripts. Uploading the start
        # snapshot and then replacing its observer rows on stop causes a
        # refresh to append duplicate input/start events remotely.
        return None

    def on_coding_worker_stop(**kwargs: Any) -> None:
        persist_coding_worker(dict(kwargs))

    def artifact_provider(
        session_id: str | None = None,
        surface: str | None = None,
        **_: Any,
    ) -> dict[str, str] | None:
        if not session_id or surface != "discord_feature_summary":
            return None
        config, state, _publisher, store = _runtime()
        root_id = store.resolve_root(session_id)
        record = state.get(root_id, "discord") if root_id else None
        if not record or record.get("status") not in {"pending", "ready"}:
            return None
        return {
            "kind": "external_url",
            "label": "Agent Trace",
            "url": config.sligo_url(record["slug"]),
        }

    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    ctx.register_hook("subagent_start", on_subagent_start)
    ctx.register_hook("subagent_stop", on_subagent_stop)
    ctx.register_hook("coding_worker_start", on_coding_worker_start)
    ctx.register_hook("coding_worker_stop", on_coding_worker_stop)
    ctx.register_session_artifact_provider(artifact_provider)


__all__ = ["register"]
