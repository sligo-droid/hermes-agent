"""Discord message and board helper for Kanban coding workers."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import sys
import threading
import time
from typing import Any, Sequence
from urllib import error, parse, request

from tools.discord_tool import DiscordAPIError, _discord_request


_BROKER: "_ReadBroker | None" = None
_ALLOWED_DISCORD_METHODS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def _token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Discord worker read broker is not configured; "
            "HERMES_DISCORD_WORKER_READ_URL is required"
        )
    return token


class _ReadBroker:
    def __init__(self, discord_token: str):
        self.discord_token = discord_token
        self.access_token = secrets.token_urlsafe(32)
        broker = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle(self)

            def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle(self)

            def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle(self)

            def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle(self)

            def do_DELETE(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                broker._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="discord-worker-read-broker",
            daemon=True,
        )
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _handle(self, handler: BaseHTTPRequestHandler) -> None:
        if handler.headers.get("Authorization") != f"Bearer {self.access_token}":
            self._write(handler, 403, {"error": "forbidden"})
            return

        method = str(getattr(handler, "command", "GET") or "GET").upper()
        if method not in _ALLOWED_DISCORD_METHODS:
            self._write(handler, 405, {"error": "unsupported Discord method"})
            return

        parsed = parse.urlsplit(handler.path)
        path = parsed.path
        parts = path.strip("/").split("/")
        is_single = len(parts) == 4 and parts[0] == "channels" and parts[2] == "messages"
        is_history = len(parts) == 3 and parts[0] == "channels" and parts[2] == "messages"
        if method == "GET" and not (is_single or is_history) and not _valid_discord_api_path(path):
            self._write(handler, 404, {"error": "unsupported Discord read endpoint"})
            return
        if method != "GET" and not _valid_discord_api_path(path):
            self._write(handler, 404, {"error": "unsupported Discord mutation endpoint"})
            return

        query = parse.parse_qs(parsed.query, keep_blank_values=False)
        params = {key: values[0] for key, values in query.items() if values and values[0]}
        if method == "GET" and is_history:
            try:
                limit = int((query.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            params["limit"] = str(max(1, min(limit, 100)))
            for key in ("before", "after"):
                values = query.get(key)
                if values and values[0]:
                    params[key] = values[0]

        body = None
        length = int(handler.headers.get("Content-Length") or 0)
        if length > 0:
            try:
                body = json.loads(handler.rfile.read(length).decode("utf-8"))
            except Exception as exc:
                self._write(handler, 400, {"error": f"invalid JSON body: {exc}"})
                return

        try:
            payload = _discord_request(
                method,
                path,
                self.discord_token,
                params=params or None,
                body=body,
            )
        except DiscordAPIError as exc:
            self._write(handler, 502, {"error": str(exc)})
            return
        self._write(handler, 200, payload)

    def _write(self, handler: BaseHTTPRequestHandler, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def start_read_broker(discord_token: str) -> tuple[str, str]:
    """Start a local Discord proxy and return (base_url, bearer)."""
    global _BROKER
    token = discord_token.strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required to start Discord read broker")
    if _BROKER is None or _BROKER.discord_token != token:
        if _BROKER is not None:
            _BROKER.shutdown()
        _BROKER = _ReadBroker(token)
    return _BROKER.base_url, _BROKER.access_token


def start_worker_broker(discord_token: str) -> tuple[str, str]:
    """Alias with a less restrictive name for new worker callers."""
    return start_read_broker(discord_token)


def _shutdown_read_broker_for_tests() -> None:
    global _BROKER
    if _BROKER is not None:
        _BROKER.shutdown()
        _BROKER = None


def _valid_discord_api_path(path: str) -> bool:
    if not path.startswith("/") or path.startswith("//"):
        return False
    if not path.strip("/"):
        return False
    if "://" in path or "\x00" in path:
        return False
    return True


def _worker_read_only() -> bool:
    return os.getenv("HERMES_DISCORD_WORKER_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}


def _require_control_access(action: str) -> None:
    if _worker_read_only():
        raise RuntimeError(
            f"Discord worker helper is read-only in this role; {action} is reserved for the finalizer/operator"
        )


def _broker_request(
    path: str,
    params: dict[str, str] | None = None,
    *,
    method: str = "GET",
    body: Any = None,
) -> Any:
    method = method.upper()
    control_url = os.getenv("HERMES_DISCORD_WORKER_CONTROL_URL", "").strip()
    control_token = os.getenv("HERMES_DISCORD_WORKER_CONTROL_TOKEN", "").strip()
    read_url = os.getenv("HERMES_DISCORD_WORKER_READ_URL", "").strip()
    read_token = os.getenv("HERMES_DISCORD_WORKER_READ_TOKEN", "").strip()
    if method != "GET":
        _require_control_access("Discord REST mutation")
        base_url = control_url.rstrip("/")
        bearer = control_token
        token_name = "HERMES_DISCORD_WORKER_CONTROL_TOKEN"
    elif control_url and control_token:
        base_url = control_url.rstrip("/")
        bearer = control_token
        token_name = "HERMES_DISCORD_WORKER_CONTROL_TOKEN"
    elif read_url:
        base_url = read_url.rstrip("/")
        bearer = read_token
        token_name = "HERMES_DISCORD_WORKER_READ_TOKEN"
    elif control_url:
        base_url = control_url.rstrip("/")
        bearer = control_token
        token_name = "HERMES_DISCORD_WORKER_CONTROL_TOKEN"
    else:
        return None
    if not base_url:
        return None
    if not bearer:
        raise RuntimeError(f"{token_name} is required for Discord worker access")
    query = f"?{parse.urlencode(params)}" if params else ""
    data = None
    headers = {"Authorization": f"Bearer {bearer}"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = request.Request(
        f"{base_url}{path}{query}",
        data=data,
        headers=headers,
        method=method.upper(),
    )
    try:
        with request.urlopen(req, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord worker read broker failed: {detail}") from exc


def fetch_message(channel_id: str, message_id: str) -> Any:
    broker_payload = _broker_request(f"/channels/{channel_id}/messages/{message_id}")
    if broker_payload is not None:
        return broker_payload
    return _discord_request(
        "GET",
        f"/channels/{channel_id}/messages/{message_id}",
        _token(),
    )


def discord_request(
    method: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    body: Any = None,
) -> Any:
    method = str(method or "GET").upper()
    if method not in _ALLOWED_DISCORD_METHODS:
        raise ValueError(f"unsupported Discord method: {method}")
    if not _valid_discord_api_path(path):
        raise ValueError("Discord API path must be an absolute API path")
    if method != "GET":
        _require_control_access("Discord REST mutation")
    broker_payload = _broker_request(path, params=params, method=method, body=body)
    if broker_payload is not None:
        return broker_payload
    return _discord_request(method, path, _token(), params=params, body=body)


def _parse_jsonish(value: str) -> Any:
    text = str(value or "")
    try:
        return json.loads(text)
    except Exception:
        return text


def _parse_set_pairs(values: list[str] | None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for raw in values or []:
        if "=" not in raw:
            raise ValueError(f"expected KEY=VALUE, got {raw!r}")
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError("metadata key is required")
        parsed[key] = _parse_jsonish(value)
    return parsed


def update_board(
    board: str,
    *,
    goal_status: str | None = None,
    phase: str | None = None,
    blocked_reason: str | None = None,
    clear_blocked_reason: bool = False,
    concise_outcome: str | None = None,
    summary_title: str | None = None,
    pr_url: str | None = None,
    pr_number: str | None = None,
    set_values: dict[str, Any] | None = None,
    delete_keys: list[str] | None = None,
    sync_summary: bool = False,
    sync_reaction: bool = False,
    persist_summary: bool = False,
    dispatch_reason: str = "worker-board-update",
) -> dict[str, Any]:
    _require_control_access("board metadata mutation")
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import (
        mark_completion_notice_pending_on_done_transition,
        mark_dispatch_dirty,
        persist_board_run_summary,
        _mutate_worker_metadata,
    )
    from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY

    board = str(board or "").strip()
    if not board:
        raise ValueError("board is required")
    existing_worker = dict(kanban_db.read_board_metadata(board).get(DISCORD_WORKER_META_KEY) or {})
    if existing_worker.get("kind") != "discord_worker_board":
        raise KeyError(f"unknown Discord worker board: {board}")

    updates = dict(set_values or {})
    for key, value in (
        ("goal_status", goal_status),
        ("phase", phase),
        ("blocked_reason", blocked_reason),
        ("concise_outcome", concise_outcome),
        ("summary_title", summary_title),
        ("pr_url", pr_url),
        ("pr_number", pr_number),
    ):
        if value is not None:
            updates[key] = str(value)
    if clear_blocked_reason:
        updates["blocked_reason"] = ""
    if sync_summary:
        updates["terminal_summary_sync_pending"] = True
    if sync_reaction:
        updates["terminal_reaction_sync_pending"] = True
    def mutate(metadata: dict[str, Any], worker: dict[str, Any]) -> bool:
        if worker.get("kind") != "discord_worker_board":
            return False
        previous = dict(worker)
        for key, value in updates.items():
            worker[str(key)] = value
        for key in delete_keys or []:
            worker.pop(str(key), None)
        mark_completion_notice_pending_on_done_transition(worker, previous)
        worker["updated_at"] = int(time.time())
        return True

    written = _mutate_worker_metadata(board, mutate, warning_action="update Discord worker read metadata")
    if written is None:
        raise TimeoutError(f"timed out acquiring metadata lock for board {board}")
    if persist_summary:
        persist_board_run_summary(board)
    marker = mark_dispatch_dirty(board=board, reason=dispatch_reason)
    updated = dict(kanban_db.read_board_metadata(board).get(DISCORD_WORKER_META_KEY) or {})
    return {
        "success": True,
        "board": board,
        "goal_status": updated.get("goal_status"),
        "phase": updated.get("phase"),
        "summary_message_id": updated.get("summary_message_id"),
        "dispatch_dirty_marker": str(marker),
    }


def update_task_status(
    board: str,
    task_id: str,
    status: str,
    *,
    summary: str | None = None,
    result: str | None = None,
    block_reason: str | None = None,
) -> dict[str, Any]:
    _require_control_access("task status mutation")
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import mark_dispatch_dirty

    board = str(board or "").strip()
    task_id = str(task_id or "").strip()
    status = str(status or "").strip().lower()
    if not board or not task_id or not status:
        raise ValueError("board, task_id, and status are required")
    conn = kanban_db.connect(board=board)
    try:
        ok = kanban_db.move_task_status(
            conn,
            task_id,
            status,
            result=result,
            summary=summary,
            block_reason=block_reason,
            source="discord-worker-helper",
        )
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    marker = mark_dispatch_dirty(board=board, reason="worker-task-status")
    return {
        "success": bool(ok),
        "board": board,
        "task_id": task_id,
        "status": getattr(task, "status", None) if task is not None else None,
        "dispatch_dirty_marker": str(marker),
    }


def _clip(value: Any, limit: int, default: str = "") -> str:
    text = " ".join(str(value or "").split()) or default
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _summary_status_label(state: str) -> str:
    return {
        "done": "Complete",
        "blocked": "Blocked",
        "errored": "Failed",
        "running": "Running",
        "active": "In progress",
    }.get(str(state or "").strip().lower(), str(state or "Pending"))


def _summary_color(state: str) -> int:
    return {
        "done": 0x22C55E,
        "blocked": 0xF59E0B,
        "errored": 0xEF4444,
        "running": 0x3B82F6,
        "active": 0x3B82F6,
    }.get(str(state or "").strip().lower(), 0x3B82F6)


def _summary_embed_payload(
    snapshot: dict[str, Any],
    *,
    status: str | None = None,
    title: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    state = str(status or snapshot.get("state") or "active")
    fields = [
        {"name": "Status", "value": _clip(_summary_status_label(state), 1024, "Pending"), "inline": True},
        {"name": "Concise Outcome", "value": _clip(outcome or snapshot.get("outcome"), 1024, "Pending"), "inline": False},
    ]
    if snapshot.get("branch"):
        fields.append({"name": "Branch", "value": _clip(snapshot.get("branch"), 1024), "inline": True})
    if snapshot.get("pr_url"):
        fields.append({"name": "GitHub PR", "value": _clip(snapshot.get("pr_url"), 1024), "inline": False})
    if snapshot.get("public_url"):
        fields.append({"name": "Kanban Board", "value": _clip(snapshot.get("public_url"), 1024), "inline": False})
    embed = {
        "title": _clip(title or snapshot.get("title") or snapshot.get("fallback_title"), 240, "Discord Worker Feature"),
        "color": _summary_color(state),
        "fields": fields,
    }
    return {"embeds": [embed]}


def sync_summary_message(
    board: str,
    *,
    status: str | None = None,
    title: str | None = None,
    outcome: str | None = None,
) -> dict[str, Any]:
    _require_control_access("Discord summary mutation")
    from hermes_cli.discord_worker_boards import feature_summary_snapshot, mark_thread_status_synced

    board = str(board or "").strip()
    if not board:
        raise ValueError("board is required")
    snapshot = feature_summary_snapshot(board)
    channel_id = str(snapshot.get("thread_id") or snapshot.get("chat_id") or "").strip()
    message_id = str(snapshot.get("message_id") or "").strip()
    if not channel_id or not message_id:
        raise ValueError("board does not have a summary message handle")
    payload = _summary_embed_payload(snapshot, status=status, title=title, outcome=outcome)
    result = discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        body=payload,
    )
    mark_thread_status_synced(board, summary=True)
    return {
        "success": True,
        "board": board,
        "channel_id": channel_id,
        "message_id": message_id,
        "discord_result": result,
    }


def fetch_messages(
    channel_id: str,
    limit: int,
    *,
    before: str | None = None,
    after: str | None = None,
) -> Any:
    params = {"limit": str(max(1, min(int(limit), 100)))}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    broker_payload = _broker_request(f"/channels/{channel_id}/messages", params=params)
    if broker_payload is not None:
        return broker_payload
    return _discord_request(
        "GET",
        f"/channels/{channel_id}/messages",
        _token(),
        params=params,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read and mutate Discord worker board state."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("fetch-message", help="Fetch one Discord message")
    single.add_argument("--channel-id", required=True)
    single.add_argument("--message-id", required=True)

    history = subparsers.add_parser("fetch-messages", help="Fetch Discord channel history")
    history.add_argument("--channel-id", required=True)
    history.add_argument("--limit", required=True, type=int)
    history.add_argument("--before")
    history.add_argument("--after")

    api = subparsers.add_parser("discord-request", help="Call the Discord REST API")
    api.add_argument("--method", required=True, choices=sorted(_ALLOWED_DISCORD_METHODS))
    api.add_argument("--path", required=True, help="Discord API path, e.g. /channels/.../messages/...")
    api.add_argument("--body-json")
    api.add_argument("--param", action="append", default=[], help="Query parameter as KEY=VALUE")

    board = subparsers.add_parser("update-board", help="Update Discord worker board metadata")
    board.add_argument("--board", required=True)
    board.add_argument("--goal-status")
    board.add_argument("--phase")
    board.add_argument("--blocked-reason")
    board.add_argument("--clear-blocked-reason", action="store_true")
    board.add_argument("--concise-outcome")
    board.add_argument("--summary-title")
    board.add_argument("--pr-url")
    board.add_argument("--pr-number")
    board.add_argument("--set", action="append", default=[], help="Worker metadata update as KEY=JSON_OR_TEXT")
    board.add_argument("--delete", action="append", default=[], help="Worker metadata key to delete")
    board.add_argument("--sync-summary", action="store_true")
    board.add_argument("--sync-reaction", action="store_true")
    board.add_argument("--persist-summary", action="store_true")
    board.add_argument("--dispatch-reason", default="worker-board-update")

    task = subparsers.add_parser("task-status", help="Move a task on any accessible board")
    task.add_argument("--board", required=True)
    task.add_argument("--task-id", required=True)
    task.add_argument("--status", required=True)
    task.add_argument("--summary")
    task.add_argument("--result")
    task.add_argument("--block-reason")

    summary = subparsers.add_parser("sync-summary", help="Patch the Discord feature summary message")
    summary.add_argument("--board", required=True)
    summary.add_argument("--status")
    summary.add_argument("--title")
    summary.add_argument("--outcome")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch-message":
            payload = fetch_message(args.channel_id, args.message_id)
        elif args.command == "fetch-messages":
            payload = fetch_messages(
                args.channel_id,
                args.limit,
                before=args.before,
                after=args.after,
            )
        elif args.command == "discord-request":
            payload = discord_request(
                args.method,
                args.path,
                params=_parse_set_pairs(args.param),
                body=_parse_jsonish(args.body_json) if args.body_json else None,
            )
        elif args.command == "update-board":
            payload = update_board(
                args.board,
                goal_status=args.goal_status,
                phase=args.phase,
                blocked_reason=args.blocked_reason,
                clear_blocked_reason=args.clear_blocked_reason,
                concise_outcome=args.concise_outcome,
                summary_title=args.summary_title,
                pr_url=args.pr_url,
                pr_number=args.pr_number,
                set_values=_parse_set_pairs(args.set),
                delete_keys=args.delete,
                sync_summary=args.sync_summary,
                sync_reaction=args.sync_reaction,
                persist_summary=args.persist_summary,
                dispatch_reason=args.dispatch_reason,
            )
        elif args.command == "task-status":
            payload = update_task_status(
                args.board,
                args.task_id,
                args.status,
                summary=args.summary,
                result=args.result,
                block_reason=args.block_reason,
            )
        else:
            payload = sync_summary_message(
                args.board,
                status=args.status,
                title=args.title,
                outcome=args.outcome,
            )
    except DiscordAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
