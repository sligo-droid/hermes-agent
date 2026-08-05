"""Loopback-only resolver for access-protected Sligo trace links."""

from __future__ import annotations

import argparse
import base64
import binascii
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import os
import re
from typing import Any, Optional
from urllib.parse import urlsplit

from .config import Config
from .publisher import Publisher
from .state import State

_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_-]{22,128}$")
_AUTH_MODE_BASIC = "basic"
_AUTH_MODE_CLOUDFLARE_ACCESS = "cloudflare-access"
_VALID_AUTH_MODES = {_AUTH_MODE_BASIC, _AUTH_MODE_CLOUDFLARE_ACCESS}
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "X-Robots-Tag": "noindex, nofollow",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
    ),
}


def credentials() -> tuple[Optional[str], Optional[str]]:
    traces_username = os.environ.get("HERMES_TRACES_AUTH_USERNAME")
    traces_password = os.environ.get("HERMES_TRACES_AUTH_PASSWORD")
    if traces_username is not None or traces_password is not None:
        return traces_username, traces_password
    return (
        os.environ.get("HERMES_DASHBOARD_USERNAME"),
        os.environ.get("HERMES_DASHBOARD_PASSWORD"),
    )


def auth_mode() -> str:
    mode = os.environ.get("HERMES_TRACES_AUTH_MODE", _AUTH_MODE_BASIC)
    return mode.strip().lower().replace("_", "-")


def _basic_authorized(header: str) -> bool:
    expected_username, expected_password = credentials()
    if not expected_username or not expected_password:
        return False
    try:
        scheme, encoded = header.split(" ", 1)
        if scheme.lower() != "basic":
            return False
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        supplied_username, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError, binascii.Error):
        return False
    return hmac.compare_digest(supplied_username, expected_username) and hmac.compare_digest(
        supplied_password, expected_password
    )


def _cloudflare_access_authorized(headers: Any) -> bool:
    """Trust identity headers only because the resolver is loopback-only.

    Cloudflare Tunnel is the only public path to this service. Access verifies
    the assertion at the edge and forwards both headers to the origin.
    """
    assertion = headers.get("Cf-Access-Jwt-Assertion", "").strip()
    email = headers.get("Cf-Access-Authenticated-User-Email", "").strip()
    return bool(assertion and email)


def _handler_class(
    state: State,
    publisher: Publisher,
    authentication_mode: str,
) -> type[BaseHTTPRequestHandler]:
    class ResolverHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *args: Any) -> None:
            return

        def _respond(
            self,
            status: int,
            body: bytes = b"",
            *,
            content_type: str = "text/plain; charset=utf-8",
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            for name, value in _SECURITY_HEADERS.items():
                self.send_header(name, value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD" and body:
                self.wfile.write(body)

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self.do_GET()

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            if authentication_mode == _AUTH_MODE_CLOUDFLARE_ACCESS:
                if not _cloudflare_access_authorized(self.headers):
                    self._respond(403, b"Forbidden.\n")
                    return
            elif not _basic_authorized(self.headers.get("Authorization", "")):
                self._respond(
                    401,
                    headers={"WWW-Authenticate": 'Basic realm="Hermes Traces"'},
                )
                return

            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                self._respond(404, b"Not found.\n")
                return
            if parsed.path == "/healthz":
                self._respond(200, b"ok\n")
                return
            if not parsed.path.startswith("/traces/"):
                self._respond(404, b"Not found.\n")
                return

            slug = parsed.path.removeprefix("/traces/")
            if not _SLUG_PATTERN.fullmatch(slug):
                self._respond(404, b"Not found.\n")
                return
            record = state.get_slug(slug)
            if not record:
                self._respond(404, b"Not found.\n")
                return

            if record.get("status") == "ready" and Publisher._valid_url(
                record.get("shared_url")
            ):
                destination = record["shared_url"].rstrip("/")
                if not destination.endswith("/full"):
                    destination += "/full"
                self._respond(
                    303,
                    headers={"Location": destination},
                )
                return
            if record.get("status") == "pending":
                publisher.enqueue(record["key"])
                self._respond(
                    200,
                    (
                        b"<!doctype html><meta charset=utf-8>"
                        b"<meta http-equiv=refresh content=3>"
                        b"<title>Agent trace</title>"
                        b"<p>Trace publication is still in progress.</p>"
                    ),
                    content_type="text/html; charset=utf-8",
                    headers={"Refresh": "3"},
                )
                return
            self._respond(503, b"Trace temporarily unavailable.\n")

    return ResolverHandler


class ResolverServer(ThreadingHTTPServer):
    daemon_threads = True


def serve(
    host: str = "127.0.0.1",
    port: int = 8788,
    config: Optional[Config] = None,
    *,
    state: Optional[State] = None,
    publisher: Optional[Publisher] = None,
) -> ResolverServer:
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError as exc:
        raise ValueError("resolver host must be a loopback IP address") from exc
    if not is_loopback:
        raise ValueError("resolver must bind to a loopback address")
    if not 0 <= port <= 65_535:
        raise ValueError("resolver port is out of range")

    config = config or Config.from_env()
    state = state or State(config.index_path)
    publisher = publisher or Publisher(config, state)
    authentication_mode = auth_mode()
    if authentication_mode not in _VALID_AUTH_MODES:
        raise ValueError("unsupported HERMES_TRACES_AUTH_MODE")
    return ResolverServer(
        (host, port),
        _handler_class(state, publisher, authentication_mode),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Serve protected Sligo links for Hermes agent traces."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    arguments = parser.parse_args()
    serve(arguments.host, arguments.port).serve_forever()


if __name__ == "__main__":
    main()
