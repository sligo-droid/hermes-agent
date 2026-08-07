"""Narrow read-only Gmail REST client."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Mapping

import httpx


GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
# Gmail accepts inbound messages up to 50 MiB. A raw API response base64url-
# encodes those bytes, so a configured small admission cap must not make a
# provider-valid oversized message fail at the transport layer forever.
GMAIL_PROVIDER_RAW_RESPONSE_BYTES = 72 * 1024 * 1024


class GmailAPIError(RuntimeError):
    retryable = False


class GmailRetryableError(GmailAPIError):
    retryable = True


class GmailHistoryExpired(GmailAPIError):
    pass


class GmailMessageGone(GmailAPIError):
    pass


class GmailInvalidResponse(GmailAPIError):
    def __init__(self, message: str, *, fingerprint: str = "", complete: bool = True) -> None:
        super().__init__(message)
        self.fingerprint = fingerprint
        self.complete = complete


@dataclass(frozen=True, slots=True)
class GmailResponse:
    payload: dict[str, Any]
    server_ms: int
    fingerprint: str


class GmailClient:
    def __init__(
        self,
        access_token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        max_response_bytes: int = 96 * 1024 * 1024,
    ) -> None:
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._client = httpx.Client(
            base_url=GMAIL_API_BASE,
            timeout=max(1.0, float(timeout)),
            transport=transport,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GmailClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _get(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        kind: str,
        max_response_bytes: int | None = None,
    ) -> GmailResponse:
        digest = hashlib.sha256()
        chunks = []
        size = 0
        response_limit = self.max_response_bytes if max_response_bytes is None else max(
            self.max_response_bytes, int(max_response_bytes)
        )
        try:
            with self._client.stream("GET", path, params=params) as response:
                status = response.status_code
                server_ms = self._server_ms(
                    response.headers.get("Date", ""), required=(kind == "profile")
                )
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > response_limit:
                        raise GmailRetryableError("Gmail response exceeded the transport bound")
                    digest.update(chunk)
                    chunks.append(chunk)
        except GmailAPIError:
            raise
        except httpx.HTTPError as exc:
            raise GmailRetryableError("Gmail transport failed") from exc
        fingerprint = digest.hexdigest()
        if status == 404 and kind == "history":
            raise GmailHistoryExpired("Gmail history cursor expired")
        if status == 404 and kind == "message":
            raise GmailMessageGone("Gmail message no longer exists")
        if status == 429 or status >= 500:
            raise GmailRetryableError("Gmail service is temporarily unavailable")
        if status in {401, 403}:
            raise GmailAPIError("Gmail authorization failed")
        if status == 204 and kind == "list":
            return GmailResponse({}, server_ms, fingerprint)
        if status != 200:
            raise GmailAPIError("Gmail request was rejected")
        raw = b"".join(chunks)
        try:
            payload = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailInvalidResponse(
                "Gmail returned invalid JSON", fingerprint=fingerprint
            ) from exc
        if not isinstance(payload, dict):
            raise GmailInvalidResponse("Gmail returned an invalid object", fingerprint=fingerprint)
        return GmailResponse(payload, server_ms, fingerprint)

    @staticmethod
    def _server_ms(value: str, *, required: bool) -> int:
        if not value and not required:
            return 0
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError) as exc:
            raise GmailInvalidResponse("Gmail server time is invalid") from exc
        if parsed is None:
            raise GmailInvalidResponse("Gmail server time is missing")
        return int(parsed.timestamp()) * 1000

    def profile(self) -> GmailResponse:
        return self._get(
            "/users/me/profile", params={"fields": "emailAddress,historyId"}, kind="profile"
        )

    def history(self, start_history_id: str, page_token: str = "") -> GmailResponse:
        params: dict[str, Any] = {
            "startHistoryId": start_history_id,
            "historyTypes": "messageAdded",
            "maxResults": 500,
            "fields": "history(id,messagesAdded(message(id))),nextPageToken,historyId",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get("/users/me/history", params=params, kind="history")

    def list_messages(self, query: str, page_token: str = "") -> GmailResponse:
        params: dict[str, Any] = {
            "q": query,
            "maxResults": 500,
            "fields": "messages(id),nextPageToken",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get("/users/me/messages", params=params, kind="list")

    def raw_message(self, message_id: str) -> GmailResponse:
        return self._get(
            f"/users/me/messages/{message_id}",
            params={
                "format": "raw",
                "fields": "id,threadId,historyId,internalDate,sizeEstimate,raw",
            },
            kind="message",
            max_response_bytes=GMAIL_PROVIDER_RAW_RESPONSE_BYTES,
        )


__all__ = [
    "GMAIL_API_BASE",
    "GmailAPIError",
    "GmailClient",
    "GmailHistoryExpired",
    "GmailInvalidResponse",
    "GmailMessageGone",
    "GmailResponse",
    "GmailRetryableError",
]
