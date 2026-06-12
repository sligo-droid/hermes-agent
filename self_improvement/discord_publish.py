"""Publish approved self-improvement proposals into Discord worker threads."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from hermes_cli.config import cfg_get, load_config_readonly

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscordApprovalRoute:
    channel_id: str
    top_level_message_id: str
    thread_id: str
    thread_url: str
    board: str
    board_public_url: str
    guild_id: str = ""
    error: str = ""

    def metadata(self) -> dict[str, Any]:
        return {
            "discord_channel_id": self.channel_id,
            "discord_top_level_message_id": self.top_level_message_id,
            "discord_thread_id": self.thread_id,
            "discord_thread_url": self.thread_url,
            "discord_board": self.board,
            "discord_board_public_url": self.board_public_url,
            "discord_guild_id": self.guild_id,
            **({"discord_publish_error": self.error} if self.error else {}),
        }


def configured_project_channel_id(project: object) -> str:
    """Return the configured Discord channel for a self-improvement project."""

    project_key = str(project or "").strip()
    if not project_key:
        return ""
    try:
        cfg = load_config_readonly()
    except Exception as exc:
        log.debug("self-improvement Discord config lookup failed: %s", exc)
        return _mapped_project_channel_id(project_key)
    section = cfg.get("self_improvement") if isinstance(cfg, dict) else None
    projects = section.get("projects") if isinstance(section, dict) else None
    project_cfg = projects.get(project_key) if isinstance(projects, dict) else None
    if not isinstance(project_cfg, dict):
        return _mapped_project_channel_id(project_key)
    for key in ("discord_channel_id", "discord_project_channel_id", "project_discord_channel_id"):
        value = str(project_cfg.get(key) or "").strip()
        if value:
            return value
    mapped = _mapped_project_channel_id(project_key)
    if mapped:
        return mapped
    for key in ("discord_channel_name", "discord_project_channel_name", "project_discord_channel_name"):
        channel_name = str(project_cfg.get(key) or "").strip()
        if not channel_name:
            continue
        resolved = _resolve_discord_channel_name(
            channel_name,
            guild_id=str(project_cfg.get("discord_guild_id") or project_cfg.get("guild_id") or "").strip(),
            guild_name=str(project_cfg.get("discord_guild_name") or project_cfg.get("guild_name") or "").strip(),
        )
        if resolved:
            return resolved
    return ""


def _resolve_discord_channel_name(channel_name: str, *, guild_id: str = "", guild_name: str = "") -> str:
    """Resolve a configured Discord channel name such as ``#dev`` to an ID."""

    wanted = _normalize_channel_name(channel_name)
    if not wanted:
        return ""
    token = ""
    try:
        from tools.discord_tool import _discord_request, _get_bot_token

        token = _get_bot_token() or ""
    except Exception as exc:
        log.debug("self-improvement Discord token lookup failed for channel name %s: %s", channel_name, exc)
        return ""
    if not token:
        return ""

    guild_ids: list[str] = []
    if guild_id:
        guild_ids = [guild_id]
    else:
        try:
            guilds = _discord_request("GET", "/users/@me/guilds", token) or []
        except Exception as exc:
            log.debug("self-improvement Discord guild lookup failed for channel name %s: %s", channel_name, exc)
            return ""
        for guild in guilds if isinstance(guilds, list) else []:
            if not isinstance(guild, dict):
                continue
            if guild_name and _normalize_channel_name(guild.get("name")) != _normalize_channel_name(guild_name):
                continue
            candidate = str(guild.get("id") or "").strip()
            if candidate:
                guild_ids.append(candidate)

    matches: dict[str, str] = {}
    for gid in guild_ids:
        try:
            channels = _discord_request("GET", f"/guilds/{gid}/channels", token) or []
        except Exception as exc:
            log.debug("self-improvement Discord channel lookup failed for guild %s: %s", gid, exc)
            continue
        for channel in channels if isinstance(channels, list) else []:
            if not isinstance(channel, dict):
                continue
            if not _is_postable_discord_channel(channel):
                continue
            if _normalize_channel_name(channel.get("name")) != wanted:
                continue
            cid = str(channel.get("id") or "").strip()
            if cid:
                matches[cid] = gid
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        log.warning("self-improvement Discord channel name %s matched multiple channels: %s", channel_name, sorted(matches))
    return ""


def _normalize_channel_name(value: object) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("#"):
        text = text[1:]
    return re.sub(r"[^a-z0-9_-]+", "", text)


def _is_postable_discord_channel(channel: dict[str, Any]) -> bool:
    try:
        channel_type = int(channel.get("type", 0))
    except (TypeError, ValueError):
        return False
    # Discord text and announcement channels accept normal message posts.
    return channel_type in {0, 5}


def _mapped_project_channel_id(project_key: str) -> str:
    mapping = _project_mapping_for_key(project_key)
    return str(mapping.get("channel_id") or "").strip() if mapping else ""


def _project_mapping_for_key(project_key: str) -> dict[str, Any]:
    wanted = _normalize_project_key(project_key)
    if not wanted:
        return {}
    try:
        from hermes_state import SessionDB

        db = SessionDB()
    except Exception as exc:
        log.debug("self-improvement Discord project mapping lookup failed: %s", exc)
        return {}
    try:
        rows = db.list_discord_project_mappings()
    except Exception as exc:
        log.debug("self-improvement Discord project mappings unreadable: %s", exc)
        return {}
    finally:
        close = getattr(db, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
    matches: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        candidates = (row.get("project_key"), row.get("project_name"))
        if any(_normalize_project_key(candidate) == wanted for candidate in candidates):
            channel_id = str(row.get("channel_id") or "").strip()
            if channel_id:
                matches[channel_id] = dict(row)
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        log.debug("self-improvement Discord project %s maps to multiple channels: %s", project_key, sorted(matches))
    return {}


def _normalize_project_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def publish_approved_proposal(
    card: dict[str, Any],
    *,
    channel_id: str,
    existing: dict[str, Any] | None = None,
) -> DiscordApprovalRoute | None:
    """Post the approval embed, start its thread, and ensure a worker board.

    Returns None when Discord is not configured or unavailable. The caller should
    then keep the existing Kanban-only approval behavior.
    """

    channel_id = str(channel_id or "").strip()
    if not channel_id:
        return None
    existing = existing or {}
    existing_thread = str(existing.get("discord_thread_id") or "").strip()
    existing_message = str(existing.get("discord_top_level_message_id") or "").strip()
    existing_board = str(existing.get("discord_board") or "").strip()
    if existing_thread and existing_message and existing_board:
        route = DiscordApprovalRoute(
            channel_id=channel_id,
            top_level_message_id=existing_message,
            thread_id=existing_thread,
            thread_url=str(existing.get("discord_thread_url") or _thread_url(str(existing.get("discord_guild_id") or ""), existing_thread)),
            board=existing_board,
            board_public_url=str(existing.get("discord_board_public_url") or ""),
            guild_id=str(existing.get("discord_guild_id") or ""),
        )
        route = ensure_approval_route_board(card, route)
        if route and route.board_public_url:
            _update_feature_embed(channel_id, existing_message, card, route.board_public_url)
        return route

    try:
        from tools.discord_tool import _discord_request, _get_bot_token

        token = _get_bot_token()
        if not token:
            return None
        message = _discord_request(
            "POST",
            f"/channels/{channel_id}/messages",
            token,
            body={"embeds": [_feature_embed(card)]},
        )
        message_id = str(message.get("id") or "").strip()
        guild_id = str(message.get("guild_id") or "").strip()
        if not message_id:
            return None
        thread = _discord_request(
            "POST",
            f"/channels/{channel_id}/messages/{message_id}/threads",
            token,
            body={"name": _thread_name(card), "auto_archive_duration": 1440},
        )
        thread_id = str(thread.get("id") or "").strip()
        guild_id = str(thread.get("guild_id") or guild_id or "").strip()
        if not thread_id:
            return None

        _add_reaction(token, channel_id, message_id, "👀")
        route = DiscordApprovalRoute(
            channel_id=channel_id,
            top_level_message_id=message_id,
            thread_id=thread_id,
            thread_url=_thread_url(guild_id, thread_id),
            board="",
            board_public_url="",
            guild_id=guild_id,
        )
        route = ensure_approval_route_board(card, route)
        if route and route.board_public_url:
            _update_feature_embed(channel_id, message_id, card, route.board_public_url)
        return route
    except Exception as exc:
        log.warning("self-improvement Discord approval publish failed: %s", exc)
        return DiscordApprovalRoute(
            channel_id=channel_id,
            top_level_message_id="",
            thread_id="",
            thread_url="",
            board="",
            board_public_url="",
            error=str(exc),
        )


def ensure_approval_route_board(
    card: dict[str, Any],
    route: DiscordApprovalRoute | None,
) -> DiscordApprovalRoute | None:
    """Ensure local durable board state exists for an approval Discord route."""

    if route is None or not route.thread_id or not route.top_level_message_id:
        return route
    try:
        from hermes_cli.discord_worker_boards import ensure_discord_thread_board

        board = ensure_discord_thread_board(
            thread_id=route.thread_id,
            chat_id=route.channel_id,
            guild_id=route.guild_id,
            parent_channel_id=route.channel_id,
            initial_request=_initial_request(card),
            project_context=_project_context(card, route.channel_id),
            request_id=route.top_level_message_id,
            source_message_id=route.top_level_message_id,
            board_slug=route.board or None,
        )
        return replace(route, board=board.slug, board_public_url=board.public_url)
    except Exception as exc:
        log.warning("self-improvement Discord approval board ensure failed: %s", exc)
        error = str(exc)
        if route.error:
            error = f"{route.error}; board: {error}"
        return replace(route, error=error)


def activate_approved_proposal(
    card: dict[str, Any],
    route: DiscordApprovalRoute | None,
) -> DiscordApprovalRoute | None:
    """Mark the Discord worker board active after its Kanban task exists."""

    if route is None or not route.thread_id or not route.board:
        return route
    try:
        from hermes_cli.discord_worker_boards import mark_dispatch_dirty, start_planner_request

        criteria = _acceptance_criteria(card)
        board = start_planner_request(
            thread_id=route.thread_id,
            request=_initial_request(card),
            chat_id=route.channel_id,
            guild_id=route.guild_id,
            parent_channel_id=route.channel_id,
            project_context=_project_context(card, route.channel_id),
            request_id=route.top_level_message_id,
            board_slug=route.board,
            created_by="self-improvement",
            acceptance_criteria=criteria,
        )
        mark_dispatch_dirty(board=board.slug, reason="self-improvement-approved")
        _update_feature_embed(route.channel_id, route.top_level_message_id, card, board.public_url)
        return replace(route, board=board.slug, board_public_url=board.public_url)
    except Exception as exc:
        log.warning("self-improvement Discord worker activation failed: %s", exc)
        error = str(exc)
        if route.error:
            error = f"{route.error}; activation: {error}"
        return replace(route, error=error)


def _initial_request(card: dict[str, Any]) -> str:
    title = str(card.get("title") or "Self-improvement proposal").strip()
    summary = str(card.get("summary") or "").strip()
    body = str(card.get("body") or "").strip()
    annotation_context = _operator_annotation_context(card)
    return "\n\n".join(part for part in (title, summary, body, annotation_context) if part).strip()


def _operator_annotation_context(card: dict[str, Any]) -> str:
    proposal_id = str(card.get("proposal_id") or "").strip()
    if not proposal_id:
        return ""
    try:
        from hermes_cli import command_center_annotations

        return command_center_annotations.operator_context_block(f"self-improvement:{proposal_id}")
    except Exception:
        return ""


def _project_context(card: dict[str, Any], channel_id: str) -> dict[str, Any]:
    project = str(card.get("project") or "self-improvement").strip()
    mapping = _project_mapping_for_key(project)
    if not mapping or not str(mapping.get("project_path") or "").strip():
        channel_mapping = _project_mapping_for_channel(channel_id)
        if channel_mapping:
            mapping = {**mapping, **channel_mapping}
    context = {
        "project_name": str(mapping.get("project_name") or project),
        "project_path": mapping.get("project_path"),
        "project_github_url": mapping.get("github_url"),
        "project_channel_id": str(channel_id or mapping.get("channel_id") or ""),
        "project_mapping_source": mapping.get("source"),
        "project_mapping_resolved": bool(mapping),
        "self_improvement_project": project,
        "self_improvement_prong": str(card.get("prong") or ""),
    }
    return {key: value for key, value in context.items() if value is not None}


def _project_mapping_for_channel(channel_id: object) -> dict[str, Any]:
    channel = str(channel_id or "").strip()
    if not channel:
        return {}
    try:
        cfg = load_config_readonly()
    except Exception as exc:
        log.debug("self-improvement Discord channel mapping lookup failed: %s", exc)
        return {}
    raw = cfg_get(cfg if isinstance(cfg, dict) else {}, "discord", "channel_cwds", default={})
    if not isinstance(raw, dict):
        return {}
    configured = raw.get(channel) or raw.get(str(channel))
    if not configured:
        return {}
    project_path = Path(str(configured)).expanduser()
    if not project_path.is_absolute() or not project_path.is_dir():
        return {}
    project_path = project_path.resolve()
    return {
        "project_key": project_path.name,
        "project_name": project_path.name,
        "project_path": str(project_path),
        "source": "configured_channel_cwd",
        "channel_id": channel,
    }


def _acceptance_criteria(card: dict[str, Any]) -> list[str]:
    return _explicit_acceptance_criteria(card)


def _explicit_acceptance_criteria(card: dict[str, Any]) -> list[str]:
    task = card.get("kanban_task") if isinstance(card.get("kanban_task"), dict) else {}
    candidates = (
        task.get("acceptance_criteria"),
        card.get("acceptance_criteria"),
        card.get("criteria"),
    )
    for candidate in candidates:
        criteria = _coerce_criteria(candidate)
        if criteria:
            return criteria
    return []


def _coerce_criteria(value: object) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    criteria: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("criterion") or item.get("body") or "").strip()
        else:
            text = str(item or "").strip()
        if text and text not in criteria:
            criteria.append(text)
    return criteria


def _feature_embed(card: dict[str, Any], board_public_url: str = "") -> dict[str, Any]:
    title = _truncate(str(card.get("title") or "Self-improvement proposal").strip(), 256)
    description = _truncate(str(card.get("summary") or card.get("body") or "").strip(), 4096)
    fields = [
        {"name": "Project", "value": _field(card.get("project")), "inline": True},
        {"name": "Prong", "value": _field(card.get("prong")), "inline": True},
        {"name": "Priority", "value": _field(card.get("priority") or "medium"), "inline": True},
        {"name": "Proposal ID", "value": _field(card.get("proposal_id")), "inline": False},
    ]
    board_public_url = str(board_public_url or "").strip().rstrip("/")
    if board_public_url:
        fields.append({"name": "Worker board", "value": _truncate(board_public_url, 1024), "inline": False})
    rationale = str(card.get("rationale") or "").strip()
    if rationale:
        fields.append({"name": "Rationale", "value": _truncate(rationale, 1024), "inline": False})
    embed = {
        "title": title,
        "description": description or "Approved self-improvement proposal.",
        "color": 0x22C55E,
        "fields": fields,
    }
    if board_public_url:
        embed["url"] = board_public_url
    return embed


def _update_feature_embed(channel_id: str, message_id: str, card: dict[str, Any], board_public_url: str) -> None:
    board_public_url = str(board_public_url or "").strip()
    if not channel_id or not message_id or not board_public_url:
        return
    try:
        from tools.discord_tool import _discord_request, _get_bot_token

        token = _get_bot_token()
        if not token:
            return
        _discord_request(
            "PATCH",
            f"/channels/{channel_id}/messages/{message_id}",
            token,
            body={"embeds": [_feature_embed(card, board_public_url)]},
        )
    except Exception as exc:
        log.debug("self-improvement Discord approval embed update failed: %s", exc)


def _add_reaction(token: str, channel_id: str, message_id: str, emoji: str) -> None:
    try:
        from urllib.parse import quote

        from tools.discord_tool import _discord_request

        _discord_request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji, safe='')}/@me",
            token,
        )
    except Exception as exc:
        log.debug("self-improvement Discord initial reaction failed: %s", exc)


def _field(value: object) -> str:
    text = str(value or "unspecified").strip() or "unspecified"
    return _truncate(text, 1024)


def _thread_name(card: dict[str, Any]) -> str:
    raw = str(card.get("title") or "Self-improvement proposal").strip()
    return _truncate(re.sub(r"\s+", " ", raw), 90) or "Self-improvement proposal"


def _thread_url(guild_id: str, thread_id: str) -> str:
    if not guild_id or not thread_id:
        return ""
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def _truncate(text: str, limit: int) -> str:
    text = str(text or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
