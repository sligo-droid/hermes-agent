"""Discord channel-to-project mapping helpers."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from fnmatch import fnmatch
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from hermes_cli.config import cfg_get, load_config
from hermes_cli.project_inspection import (
    ProjectInspectionCandidate,
    project_inspection_candidates_to_dicts,
    resolve_project_inspection,
)
from hermes_constants import get_hermes_home
from hermes_state import SessionDB

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordProjectContext:
    guild_id: str
    channel_id: str
    parent_channel_id: Optional[str]
    channel_name: Optional[str]
    guild_name: Optional[str]
    project_key: Optional[str]
    project_name: Optional[str]
    project_path: Optional[str]
    github_url: Optional[str]
    mapping_source: str
    resolved: bool = True
    inspection_candidates: tuple[ProjectInspectionCandidate, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        result = {
            "guild_id": self.guild_id,
            "project_channel_id": self.channel_id,
            "parent_channel_id": self.parent_channel_id,
            "channel_name": self.channel_name,
            "guild_name": self.guild_name,
            "project_key": self.project_key,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "project_github_url": self.github_url,
            "project_mapping_source": self.mapping_source,
            "project_mapping_resolved": self.resolved,
        }
        if self.inspection_candidates:
            result["project_inspection_candidates"] = (
                project_inspection_candidates_to_dicts(self.inspection_candidates)
            )
        return result


def resolve_discord_project_context(
    channel: Any,
    *,
    session_db: Optional[SessionDB] = None,
    workspace_root: Optional[Path] = None,
    config: Optional[dict[str, Any]] = None,
) -> Optional[DiscordProjectContext]:
    """Resolve the project context for a Discord channel or thread.

    Existing DB mappings are authoritative.  When there is no row, bootstrap
    only from a unique deterministic directory match under Hermes workspace.
    """
    cfg = _resolve_config(config)
    target = _project_channel(channel)
    if target is None or _is_dm_like(target):
        return None
    if _is_ignored_channel(target, cfg) or _is_ignored_category(target, cfg):
        return None

    guild = getattr(target, "guild", None) or getattr(channel, "guild", None)
    guild_id = _string_id(getattr(guild, "id", None))
    channel_id = _string_id(getattr(target, "id", None))
    if not guild_id or not channel_id:
        return None

    db = session_db
    owns_db = False
    if db is None:
        try:
            db = SessionDB()
            owns_db = True
        except Exception as exc:
            logger.warning("Discord project mapping disabled: state.db unavailable: %s", exc)
            db = None

    try:
        if db is not None:
            row = db.get_discord_project_mapping(guild_id=guild_id, channel_id=channel_id)
            if row:
                return _with_inspection_candidates(_context_from_row(row), cfg)

        configured = _context_from_configured_channel_cwd(
            target,
            guild_id=guild_id,
            channel_id=channel_id,
            config=cfg,
        )
        if configured is not None:
            return _with_inspection_candidates(configured, cfg)

        bootstrapped = _bootstrap_from_workspace(
            target,
            guild_id=guild_id,
            channel_id=channel_id,
            workspace_root=workspace_root,
        )
        if bootstrapped is None:
            return _with_inspection_candidates(DiscordProjectContext(
                guild_id=guild_id,
                channel_id=channel_id,
                parent_channel_id=_string_id(getattr(target, "parent_id", None)),
                channel_name=_channel_name(target),
                guild_name=str(getattr(guild, "name", "") or "") or None,
                project_key=None,
                project_name=None,
                project_path=None,
                github_url=None,
                mapping_source="unresolved",
                resolved=False,
            ), cfg)

        if db is not None:
            try:
                row = db.upsert_discord_project_mapping(**bootstrapped)
                return _with_inspection_candidates(_context_from_row(row), cfg)
            except Exception:
                logger.debug("Failed to persist Discord project mapping", exc_info=True)
        return _with_inspection_candidates(DiscordProjectContext(
            guild_id=bootstrapped["guild_id"],
            channel_id=bootstrapped["channel_id"],
            parent_channel_id=bootstrapped.get("parent_channel_id"),
            channel_name=bootstrapped.get("channel_name"),
            guild_name=bootstrapped.get("guild_name"),
            project_key=bootstrapped["project_key"],
            project_name=bootstrapped.get("project_name"),
            project_path=bootstrapped["project_path"],
            github_url=bootstrapped.get("github_url"),
            mapping_source=bootstrapped["source"],
            resolved=True,
        ), cfg)
    finally:
        if owns_db and db is not None:
            try:
                db.close()
            except Exception:
                pass


def _context_from_row(row: dict[str, Any]) -> DiscordProjectContext:
    return DiscordProjectContext(
        guild_id=str(row.get("guild_id") or ""),
        channel_id=str(row.get("channel_id") or ""),
        parent_channel_id=row.get("parent_channel_id"),
        channel_name=row.get("channel_name"),
        guild_name=row.get("guild_name"),
        project_key=row.get("project_key"),
        project_name=row.get("project_name"),
        project_path=row.get("project_path"),
        github_url=row.get("github_url"),
        mapping_source=row.get("source") or "manual",
        resolved=True,
    )


def _with_inspection_candidates(
    context: DiscordProjectContext,
    config: dict[str, Any],
) -> DiscordProjectContext:
    resolution = resolve_project_inspection(
        config.get("projects"),
        github_repo=context.github_url,
        project_key=context.project_key,
    )
    if resolution.project_key is None:
        return context
    return replace(
        context,
        project_key=resolution.project_key,
        inspection_candidates=resolution.candidates,
    )


def _resolve_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(config, dict):
        return config
    try:
        loaded = load_config() or {}
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _context_from_configured_channel_cwd(
    channel: Any,
    *,
    guild_id: str,
    channel_id: str,
    config: Optional[dict[str, Any]],
) -> Optional[DiscordProjectContext]:
    raw = cfg_get(config or {}, "discord", "channel_cwds", default={})
    if not isinstance(raw, dict):
        return None
    configured = raw.get(channel_id) or raw.get(str(channel_id))
    if not configured:
        return None
    project_path = Path(os.path.expandvars(str(configured))).expanduser()
    if not project_path.is_absolute() or not project_path.is_dir():
        return None
    project_path = project_path.resolve()
    guild = getattr(channel, "guild", None)
    return DiscordProjectContext(
        guild_id=guild_id,
        channel_id=channel_id,
        parent_channel_id=_string_id(getattr(channel, "parent_id", None)),
        channel_name=_channel_name(channel),
        guild_name=str(getattr(guild, "name", "") or "") or None,
        project_key=project_path.name,
        project_name=_humanize_project_name(project_path.name),
        project_path=str(project_path),
        github_url=_git_remote_url(project_path),
        mapping_source="configured_channel_cwd",
        resolved=True,
    )


def _bootstrap_from_workspace(
    channel: Any,
    *,
    guild_id: str,
    channel_id: str,
    workspace_root: Optional[Path],
) -> Optional[dict[str, Any]]:
    root = Path(workspace_root) if workspace_root is not None else get_hermes_home() / "workspace"
    try:
        candidates = [path for path in root.iterdir() if path.is_dir()]
    except OSError:
        return None

    channel_name = _channel_name(channel)
    channel_key = _match_key(channel_name or "")
    matches = [path for path in candidates if _match_key(path.name) == channel_key]
    if len(matches) != 1:
        if len(matches) > 1:
            logger.warning(
                "Discord project mapping for #%s is ambiguous under %s: %s",
                channel_name,
                root,
                ", ".join(path.name for path in matches),
            )
        return None

    project_path = matches[0].resolve()
    guild = getattr(channel, "guild", None)
    return {
        "guild_id": guild_id,
        "channel_id": channel_id,
        "parent_channel_id": _string_id(getattr(channel, "parent_id", None)),
        "channel_name": channel_name,
        "guild_name": str(getattr(guild, "name", "") or "") or None,
        "project_key": project_path.name,
        "project_name": _humanize_project_name(project_path.name),
        "project_path": str(project_path),
        "github_url": _git_remote_url(project_path),
        "source": "deterministic_directory_bootstrap",
    }


def _project_channel(channel: Any) -> Any:
    parent = getattr(channel, "parent", None)
    if parent is not None and not _is_category_like(parent):
        return parent
    return channel


def _is_dm_like(channel: Any) -> bool:
    return channel is None or getattr(channel, "guild", None) is None


def _is_category_like(channel: Any) -> bool:
    channel_type = getattr(channel, "type", None)
    type_value = getattr(channel_type, "value", channel_type)
    if type_value == 4:
        return True
    return channel.__class__.__name__.lower() in {"categorychannel", "category"}


def _coerce_pattern_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return [str(item).strip().lower().lstrip("#") for item in items if str(item).strip()]


def _matches_any_pattern(name: str, patterns: list[str]) -> bool:
    normalized = name.strip().lower().lstrip("#")
    if not normalized:
        return False
    return any(fnmatch(normalized, pattern) for pattern in patterns)


def _is_ignored_channel(channel: Any, config: dict[str, Any]) -> bool:
    patterns = _coerce_pattern_list(
        cfg_get(config, "discord", "project_mapping_ignored_channel_names", default="")
    )
    if not patterns:
        return False
    for candidate in (channel, getattr(channel, "parent", None)):
        if candidate is None:
            continue
        name = _channel_name(candidate) or ""
        if _matches_any_pattern(name, patterns):
            return True
    return False


def _is_ignored_category(channel: Any, config: dict[str, Any]) -> bool:
    patterns = _coerce_pattern_list(
        cfg_get(config, "discord", "project_mapping_ignored_category_names", default="")
    )
    if not patterns:
        return False
    category = getattr(channel, "category", None)
    if category is None:
        return False
    return _matches_any_pattern(_channel_name(category) or "", patterns)


def _channel_name(channel: Any) -> Optional[str]:
    name = str(getattr(channel, "name", "") or "").strip()
    return name or None


def _string_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _match_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _humanize_project_name(value: str) -> str:
    text = re.sub(r"[-_]+", " ", str(value or "").strip())
    return text.title() if text else ""


def _git_remote_url(path: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return _normalize_github_remote_url(result.stdout)


def _normalize_github_remote_url(raw: Optional[str]) -> Optional[str]:
    url = str(raw or "").strip()
    if not url:
        return None
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url[len("git@github.com:"):]
    elif url.startswith("ssh://git@github.com/"):
        url = "https://github.com/" + url[len("ssh://git@github.com/"):]
    if url.endswith(".git"):
        url = url[:-4]
    return url.rstrip("/") or None
