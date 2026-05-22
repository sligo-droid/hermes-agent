"""Read-only Discord message helper for Kanban coding workers."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import sys
import threading
from typing import Any, Sequence
from urllib import error, parse, request

from tools.discord_tool import DiscordAPIError, _discord_request


_BROKER: "_ReadBroker | None" = None


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

        parsed = parse.urlsplit(handler.path)
        path = parsed.path
        parts = path.strip("/").split("/")
        is_single = len(parts) == 4 and parts[0] == "channels" and parts[2] == "messages"
        is_history = len(parts) == 3 and parts[0] == "channels" and parts[2] == "messages"
        if not (is_single or is_history):
            self._write(handler, 404, {"error": "unsupported Discord read endpoint"})
            return

        params = None
        if is_history:
            query = parse.parse_qs(parsed.query, keep_blank_values=False)
            params = {}
            try:
                limit = int((query.get("limit") or ["50"])[0])
            except ValueError:
                limit = 50
            params["limit"] = str(max(1, min(limit, 100)))
            for key in ("before", "after"):
                values = query.get(key)
                if values and values[0]:
                    params[key] = values[0]

        try:
            payload = _discord_request("GET", path, self.discord_token, params=params)
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
    """Start a local read-only Discord proxy and return (base_url, bearer)."""
    global _BROKER
    token = discord_token.strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required to start Discord read broker")
    if _BROKER is None or _BROKER.discord_token != token:
        if _BROKER is not None:
            _BROKER.shutdown()
        _BROKER = _ReadBroker(token)
    return _BROKER.base_url, _BROKER.access_token


def _shutdown_read_broker_for_tests() -> None:
    global _BROKER
    if _BROKER is not None:
        _BROKER.shutdown()
        _BROKER = None


def _broker_request(path: str, params: dict[str, str] | None = None) -> Any:
    base_url = os.getenv("HERMES_DISCORD_WORKER_READ_URL", "").strip().rstrip("/")
    bearer = os.getenv("HERMES_DISCORD_WORKER_READ_TOKEN", "").strip()
    if not base_url:
        return None
    if not bearer:
        raise RuntimeError("HERMES_DISCORD_WORKER_READ_TOKEN is required for Discord worker reads")
    query = f"?{parse.urlencode(params)}" if params else ""
    req = request.Request(
        f"{base_url}{path}{query}",
        headers={"Authorization": f"Bearer {bearer}"},
        method="GET",
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
        description="Read Discord messages through the worker read broker."
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch-message":
            payload = fetch_message(args.channel_id, args.message_id)
        else:
            payload = fetch_messages(
                args.channel_id,
                args.limit,
                before=args.before,
                after=args.after,
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
