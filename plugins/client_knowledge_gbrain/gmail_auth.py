"""Dedicated exact-scope OAuth credentials for Gmail intake."""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from hermes_constants import get_hermes_home


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
TOKENINFO_URI = "https://oauth2.googleapis.com/tokeninfo"
DEFAULT_TOKEN_RELATIVE_PATH = "secrets/client-knowledge/gmail-readonly-token.json"
_MAX_CREDENTIAL_BYTES = 64 * 1024
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,512}$")


class GmailAuthError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class GmailAccessToken:
    value: str
    expires_at: float


def resolve_token_path(configured: Any) -> Path:
    value = str(configured or "").strip()
    path = Path(value).expanduser() if value else get_hermes_home() / DEFAULT_TOKEN_RELATIVE_PATH
    if not path.is_absolute():
        raise GmailAuthError("Gmail token path must be absolute")
    return path


def _reject_symlinks(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    for part in path.parts[1:] if path.is_absolute() else path.parts:
        current = current / part
        if current.is_symlink():
            raise GmailAuthError("Gmail credential path is unsafe")


def _read_private_json(path: Path) -> dict[str, Any]:
    _reject_symlinks(path)
    if not path.exists() or path.is_symlink():
        raise GmailAuthError("Gmail credential is unavailable")
    parent = path.parent
    if parent.stat().st_mode & 0o077:
        raise GmailAuthError("Gmail credential directory is not private")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise GmailAuthError("Gmail credential is unavailable") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise GmailAuthError("Gmail credential file is not private")
        raw = os.read(fd, _MAX_CREDENTIAL_BYTES + 1)
    finally:
        os.close(fd)
    if len(raw) > _MAX_CREDENTIAL_BYTES:
        raise GmailAuthError("Gmail credential file is too large")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GmailAuthError("Gmail credential file is invalid") from exc
    if not isinstance(payload, dict):
        raise GmailAuthError("Gmail credential file is invalid")
    return payload


def _scope_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {item for item in value.split() if item}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {item.strip() for item in value if item.strip()}
    return set()


def _expiry(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _client_id(value: Any) -> str:
    client_id = str(value or "")
    if not _CLIENT_ID_RE.fullmatch(client_id):
        raise GmailAuthError("Gmail OAuth client ID is missing or invalid")
    return client_id


def _atomic_save(path: Path, payload: Mapping[str, Any]) -> None:
    _reject_symlinks(path)
    encoded = json.dumps(dict(payload), sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _MAX_CREDENTIAL_BYTES:
        raise GmailAuthError("Gmail credential file is too large")
    temp = path.parent / f".{path.name}.tmp-{secrets.token_hex(12)}"
    fd = -1
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(fd, "wb", closefd=True) as handle:
            fd = -1
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise GmailAuthError("Gmail credential refresh could not be persisted") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


class GmailOAuth:
    def __init__(
        self,
        token_path: Path,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.token_path = token_path
        self.transport = transport
        self.timeout = max(1.0, float(timeout))

    def access_token(self, *, now: float | None = None) -> GmailAccessToken:
        timestamp = time.time() if now is None else float(now)
        payload = _read_private_json(self.token_path)
        if _scope_set(payload.get("scopes")) != {GMAIL_READONLY_SCOPE}:
            raise GmailAuthError("Gmail credential scope is not exactly gmail.readonly")
        token_uri = str(payload.get("token_uri") or DEFAULT_TOKEN_URI).strip()
        if token_uri != DEFAULT_TOKEN_URI:
            raise GmailAuthError("Gmail credential token endpoint is not approved")
        client_id = _client_id(payload.get("client_id"))
        token = str(payload.get("token") or "").strip()
        expires_at = _expiry(payload.get("expiry"))
        if not token or expires_at <= timestamp + 60:
            token, expires_at = self._refresh(payload, timestamp)
            self._introspect(token, client_id)
            payload["token"] = token
            payload["expiry"] = datetime.fromtimestamp(expires_at, timezone.utc).isoformat()
            _atomic_save(self.token_path, payload)
        else:
            self._introspect(token, client_id)
        return GmailAccessToken(token, expires_at)

    def _refresh(self, payload: Mapping[str, Any], now: float) -> tuple[str, float]:
        required = {
            "client_id": _client_id(payload.get("client_id")),
            "client_secret": str(payload.get("client_secret") or "").strip(),
            "refresh_token": str(payload.get("refresh_token") or "").strip(),
        }
        if not all(required.values()):
            raise GmailAuthError("Gmail credential refresh fields are incomplete")
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.post(
                    DEFAULT_TOKEN_URI,
                    data={**required, "grant_type": "refresh_token"},
                )
        except httpx.HTTPError as exc:
            raise GmailAuthError("Gmail OAuth refresh failed") from exc
        if response.status_code >= 500 or response.status_code == 429:
            raise GmailAuthError("Gmail OAuth refresh is temporarily unavailable")
        if response.status_code != 200 or len(response.content) > _MAX_CREDENTIAL_BYTES:
            raise GmailAuthError("Gmail OAuth refresh was rejected")
        try:
            refreshed = response.json()
        except ValueError as exc:
            raise GmailAuthError("Gmail OAuth refresh response is invalid") from exc
        if not isinstance(refreshed, Mapping):
            raise GmailAuthError("Gmail OAuth refresh response is invalid")
        returned_scopes = _scope_set(refreshed.get("scope"))
        if returned_scopes and returned_scopes != {GMAIL_READONLY_SCOPE}:
            raise GmailAuthError("Gmail OAuth refresh broadened the scope")
        token = str(refreshed.get("access_token") or "").strip()
        try:
            expires_in = int(refreshed.get("expires_in"))
        except (TypeError, ValueError) as exc:
            raise GmailAuthError("Gmail OAuth refresh expiry is invalid") from exc
        if not token or not 60 <= expires_in <= 86_400:
            raise GmailAuthError("Gmail OAuth refresh response is invalid")
        return token, now + expires_in

    def _introspect(self, token: str, client_id: str) -> None:
        try:
            with httpx.Client(timeout=self.timeout, transport=self.transport) as client:
                response = client.get(TOKENINFO_URI, params={"access_token": token})
        except httpx.HTTPError as exc:
            raise GmailAuthError("Gmail OAuth scope verification failed") from exc
        if response.status_code != 200 or len(response.content) > _MAX_CREDENTIAL_BYTES:
            raise GmailAuthError("Gmail OAuth scope verification failed")
        try:
            details = response.json()
        except ValueError as exc:
            raise GmailAuthError("Gmail OAuth scope verification failed") from exc
        if not isinstance(details, Mapping) or _scope_set(details.get("scope")) != {GMAIL_READONLY_SCOPE}:
            raise GmailAuthError("Gmail access token scope is not exactly gmail.readonly")
        aud = str(details.get("aud") or "").strip()
        issued_to = str(details.get("issued_to") or "").strip()
        if not aud and not issued_to:
            raise GmailAuthError("Gmail access token audience is missing")
        if aud and issued_to and aud != issued_to:
            raise GmailAuthError("Gmail access token audience evidence conflicts")
        audience = aud or issued_to
        if audience != client_id:
            raise GmailAuthError("Gmail access token audience does not match the OAuth client")


__all__ = [
    "DEFAULT_TOKEN_RELATIVE_PATH",
    "GMAIL_READONLY_SCOPE",
    "GmailAccessToken",
    "GmailAuthError",
    "GmailOAuth",
    "resolve_token_path",
]
