"""Narrow Notion REST client for immutable client-source archival."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

import httpx


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2026-03-11"


class NotionArchiveError(RuntimeError):
    error_class = "notion_error"
    retryable = False
    quarantine = True


class NotionRetryableError(NotionArchiveError):
    error_class = "notion_retryable"
    retryable = True
    quarantine = False


class NotionStaticConfigError(NotionArchiveError):
    error_class = "notion_static_config"


class NotionSchemaError(NotionArchiveError):
    error_class = "notion_schema"


class NotionPermissionError(NotionArchiveError):
    error_class = "notion_permission"


class NotionAmbiguousRecoveryError(NotionArchiveError):
    error_class = "notion_ambiguous_recovery"


class NotionIncompleteEvidenceError(NotionRetryableError):
    error_class = "notion_incomplete_evidence"


@dataclass(frozen=True, slots=True)
class NotionListEvidence:
    items: tuple[dict[str, Any], ...]
    page_count: int


class NotionClient:
    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        sleep=time.sleep,
    ) -> None:
        key = str(api_key or "").strip()
        if not key:
            raise NotionStaticConfigError("notion API key is not configured")
        self._client = httpx.Client(
            base_url=NOTION_API_BASE,
            timeout=max(1.0, float(timeout)),
            transport=transport,
            headers={
                "Authorization": f"Bearer {key}",
                "Notion-Version": NOTION_API_VERSION,
            },
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "NotionClient":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise NotionRetryableError("notion transport failed") from exc
        if response.status_code == 429:
            try:
                delay = min(60.0, max(0.0, float(response.headers.get("Retry-After", "0"))))
            except ValueError:
                delay = 0
            if delay:
                self._sleep(delay)
            raise NotionRetryableError("notion rate limited")
        if response.status_code in {500, 502, 503, 504, 529}:
            raise NotionRetryableError("notion service failure")
        if response.status_code in {401, 403, 404}:
            raise NotionPermissionError("notion target is unavailable")
        if response.status_code >= 400:
            raise NotionSchemaError("notion rejected a static request")
        try:
            payload = response.json()
        except ValueError as exc:
            raise NotionRetryableError("notion returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise NotionRetryableError("notion returned an invalid object")
        return payload

    def retrieve_data_source(self, data_source_id: str) -> dict[str, Any]:
        return self._request("GET", f"/data_sources/{data_source_id}")

    def update_data_source_properties(
        self, data_source_id: str, properties: Mapping[str, Any]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH", f"/data_sources/{data_source_id}", json={"properties": dict(properties)}
        )

    def query_data_source(
        self, data_source_id: str, *, property_name: str, value: str
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "POST",
            f"/data_sources/{data_source_id}/query",
            json={
                "filter": {"property": property_name, "rich_text": {"equals": value}},
                "page_size": 10,
            },
        )
        self._require_complete_page(payload)
        results = payload.get("results")
        if not isinstance(results, list):
            raise NotionIncompleteEvidenceError("notion query results are incomplete")
        if payload.get("has_more") is not False:
            raise NotionIncompleteEvidenceError("notion query result set is not bounded")
        return [item for item in results if isinstance(item, dict)]

    def create_page(
        self,
        data_source_id: str,
        *,
        properties: Mapping[str, Any],
        children: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/pages",
            json={
                "parent": {"data_source_id": data_source_id},
                "properties": dict(properties),
                "children": [dict(item) for item in children],
            },
        )

    def retrieve_page(self, page_id: str) -> dict[str, Any]:
        return self._request("GET", f"/pages/{page_id}")

    def list_block_children(
        self,
        block_id: str,
        *,
        max_items: int = 1000,
        heartbeat: Callable[[], None] | None = None,
    ) -> NotionListEvidence:
        return self._paginate(
            f"/blocks/{block_id}/children",
            max_items=max_items,
            heartbeat=heartbeat,
        )

    def append_block_children(
        self, block_id: str, children: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        return self._request(
            "PATCH",
            f"/blocks/{block_id}/children",
            json={"children": [dict(item) for item in children]},
        )

    def create_file_upload(
        self,
        *,
        filename: str,
        content_type: str,
        mode: str,
        number_of_parts: int,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "mode": mode,
            "filename": filename,
            "content_type": content_type,
        }
        if mode == "multi_part":
            body["number_of_parts"] = int(number_of_parts)
        return self._request("POST", "/file_uploads", json=body)

    def retrieve_file_upload(self, upload_id: str) -> dict[str, Any]:
        return self._request("GET", f"/file_uploads/{upload_id}")

    def list_file_uploads(
        self, *, max_items: int = 5000, heartbeat: Callable[[], None] | None = None
    ) -> NotionListEvidence:
        return self._paginate(
            "/file_uploads",
            max_items=max_items,
            require_request_status=True,
            heartbeat=heartbeat,
        )

    def send_file_upload(
        self,
        upload_id: str,
        *,
        filename: str,
        content_type: str,
        content: Any,
        part_number: int | None = None,
    ) -> dict[str, Any]:
        data = {"part_number": str(part_number)} if part_number is not None else None
        return self._request(
            "POST",
            f"/file_uploads/{upload_id}/send",
            data=data,
            files={"file": (filename, content, content_type)},
        )

    def complete_file_upload(self, upload_id: str) -> dict[str, Any]:
        return self._request("POST", f"/file_uploads/{upload_id}/complete", json={})

    @staticmethod
    def _require_complete_page(payload: Mapping[str, Any]) -> None:
        request_status = payload.get("request_status")
        if isinstance(request_status, Mapping) and request_status.get("type") != "complete":
            raise NotionIncompleteEvidenceError("notion result set is incomplete")

    def _paginate(
        self,
        path: str,
        *,
        max_items: int,
        require_request_status: bool = False,
        heartbeat: Callable[[], None] | None = None,
    ) -> NotionListEvidence:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        page_count = 0
        while True:
            if heartbeat is not None:
                heartbeat()
            params: dict[str, Any] = {"page_size": 100}
            if cursor is not None:
                params["start_cursor"] = cursor
            payload = self._request("GET", path, params=params)
            page_count += 1
            if require_request_status and not isinstance(
                payload.get("request_status"), Mapping
            ):
                raise NotionIncompleteEvidenceError(
                    "notion list completion evidence is missing"
                )
            self._require_complete_page(payload)
            results = payload.get("results")
            if not isinstance(results, list):
                raise NotionIncompleteEvidenceError("notion list is incomplete")
            for item in results:
                if isinstance(item, dict):
                    items.append(item)
                    if len(items) > max_items:
                        raise NotionIncompleteEvidenceError("notion list exceeds evidence bound")
            has_more = payload.get("has_more")
            next_cursor = payload.get("next_cursor")
            if has_more is False:
                break
            if has_more is not True or not isinstance(next_cursor, str) or not next_cursor:
                raise NotionIncompleteEvidenceError("notion pagination is inconclusive")
            if next_cursor in seen:
                raise NotionIncompleteEvidenceError("notion pagination cursor repeated")
            seen.add(next_cursor)
            cursor = next_cursor
        return NotionListEvidence(tuple(items), page_count)


__all__ = [
    "NOTION_API_VERSION",
    "NotionAmbiguousRecoveryError",
    "NotionArchiveError",
    "NotionClient",
    "NotionIncompleteEvidenceError",
    "NotionListEvidence",
    "NotionPermissionError",
    "NotionRetryableError",
    "NotionSchemaError",
    "NotionStaticConfigError",
]
