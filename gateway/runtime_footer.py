"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd,
reasoning, workers) and appends it to the final response of each
completed agent turn when enabled.
Off by default to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, reasoning, context_pct, cwd, workers]  # order shown; drop any to hide

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path — so it does not land
on tool-progress updates or streaming partials. Queued turns use their own
resolved model rather than a later follow-up's route. When streaming is on and
the final text has already been delivered piecemeal, the footer is sent as a
separate trailing adapter message.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = (
    "model",
    "reasoning",
    "context_pct",
    "cwd",
    "workers",
)
_PREVIOUS_DEFAULT_FIELDS: tuple[str, ...] = ("model", "reasoning", "context_pct", "cwd")
_LEGACY_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
_SEP = " · "


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def _footer_fields(fields: Iterable[Any]) -> list[str]:
    """Return configured footer fields, upgrading only the old default list.

    ``fields`` is persisted as a list, so changing the built-in default cannot
    update installs that already wrote a prior default. Treat those exact
    legacy lists as inherited configuration so deployed gateways render new
    default fields immediately. Any other layout remains an explicit user
    choice, including layouts that intentionally omit reasoning or workers.
    """
    normalized = [str(field) for field in fields]
    if tuple(normalized) in {_PREVIOUS_DEFAULT_FIELDS, _LEGACY_DEFAULT_FIELDS}:
        return list(_DEFAULT_FIELDS)
    return normalized


def _format_worker_runs(worker_runs: Iterable[Any] | None) -> str:
    """Render coding workers separately from general delegates."""
    grouped: dict[str, list[tuple[str, int]]] = {
        "coding": [],
        "delegates": [],
    }
    for raw_run in worker_runs or []:
        if not isinstance(raw_run, dict):
            continue
        backend = str(raw_run.get("backend") or "").strip()
        model = _model_short(str(raw_run.get("model") or "").strip())
        reasoning = str(raw_run.get("reasoning") or "").strip()
        if not backend or not model:
            continue
        label = f"{backend} {model}"
        if reasoning:
            label += f"/{reasoning}"
        group_name = "delegates" if backend.lower() == "delegate" else "coding"
        collapsed = grouped[group_name]
        existing_index = next(
            (
                index
                for index, (existing, _count) in enumerate(collapsed)
                if existing == label
            ),
            None,
        )
        if existing_index is not None:
            previous_label, count = collapsed[existing_index]
            collapsed[existing_index] = (previous_label, count + 1)
        else:
            collapsed.append((label, 1))

    rendered_groups: list[str] = []
    for group_name in ("coding", "delegates"):
        collapsed = grouped[group_name]
        if not collapsed:
            continue
        rendered = [
            f"{label} x{count}" if count > 1 else label
            for label, count in collapsed
        ]
        rendered_groups.append(f"{group_name}: {', '.join(rendered)}")
    if not rendered_groups:
        return ""
    return "workers — " + "; ".join(rendered_groups)


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = _footer_fields(global_cfg["fields"])

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = _footer_fields(plat_footer["fields"])

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    reasoning_effort: Optional[str] = None,
    cwd: Optional[str] = None,
    worker_runs: Iterable[Any] | None = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
) -> str:
    """Render the footer line, or return "" if no fields have data.

    Fields are skipped silently when their underlying data is missing — a
    partially-populated footer is better than a line with ``?%`` or empty slots.
    """
    parts: list[str] = []
    workers_line = ""
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "cwd":
            if not cwd:
                try:
                    from gateway.session_context import get_session_env

                    cwd = get_session_env("HERMES_SESSION_CWD", "")
                except Exception:
                    cwd = ""
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        elif field == "reasoning":
            effort = str(reasoning_effort or "").strip()
            if effort:
                parts.append(effort)
        elif field == "workers":
            workers = _format_worker_runs(worker_runs)
            if workers:
                workers_line = workers
        # Unknown field names are silently ignored.

    metadata_line = _SEP.join(parts)
    if metadata_line and workers_line:
        return f"{metadata_line}\n{workers_line}"
    return metadata_line or workers_line


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    reasoning_effort: Optional[str] = None,
    cwd: Optional[str] = None,
    worker_runs: Iterable[Any] | None = None,
) -> str:
    """Top-level entry point used by gateway/run.py.

    Returns the footer text (empty string when disabled or no data).  Callers
    append this to the final response themselves, preserving a single blank
    line of separation.
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        reasoning_effort=reasoning_effort,
        worker_runs=worker_runs,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
