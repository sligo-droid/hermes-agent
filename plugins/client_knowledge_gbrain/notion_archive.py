"""Claim-fenced Notion source archive worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from typing import Any, BinaryIO, Callable, Iterable, Mapping

import httpx

from hermes_cli.config import load_config

from .models import IntakeArtifact, StageReceipt
from .notion import (
    NotionAmbiguousRecoveryError,
    NotionArchiveError,
    NotionClient,
    NotionIncompleteEvidenceError,
    NotionRetryableError,
    NotionStaticConfigError,
)
from .spool import RawSpool
from .store import DEFAULT_LEASE_SECONDS, IntakeStore, JobClaim


MACHINE_PROPERTIES = {
    "source_id": ("Source ID", {"rich_text": {}}),
    "source_type": ("Source Type", {"rich_text": {}}),
    "source_hash": ("Source Hash", {"rich_text": {}}),
    "source_url": ("Source URL", {"url": {}}),
}
DEFAULT_PROPERTY_NAMES = {
    "title": "Feedback",
    "status": "Status",
    "received_at": "Date Received",
    "source_label": "Client / Source",
    "category": "Category",
    "priority": "Priority",
    **{key: value[0] for key, value in MACHINE_PROPERTIES.items()},
}
UPLOAD_STATES = (
    "upload-created",
    "bytes-sent",
    "multipart-completed",
    "page-attached",
    "receipt-verified",
)
_EXTENSIONS = {
    "message/rfc822": "eml",
    "application/pdf": "pdf",
    "text/plain": "txt",
    "image/png": "png",
    "image/jpeg": "jpg",
    "application/zip": "zip",
}
_MARKER_RE = re.compile(r"^ckfu-v1-[0-9a-f]{12}-[0-9a-f]{12}-[0-9a-f]{12}$")
MAX_RENDER_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 100_000
MAX_RENDER_BLOCKS = 80
TRUNCATION_NOTICE = "\n\n[Content truncated for Notion display; raw .eml remains canonical.]"


@dataclass(frozen=True, slots=True)
class NotionArchiveSettings:
    enabled: bool
    api_key: str
    timeout: float
    max_file_bytes: int
    multipart_part_bytes: int
    max_jobs_per_run: int
    lease_seconds: float
    heartbeat_seconds: float

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "NotionArchiveSettings":
        raw = config.get("client_knowledge", {})
        raw = raw.get("notion", {}) if isinstance(raw, Mapping) else {}
        env_name = str(raw.get("api_key_env") or "NOTION_API_KEY")
        multipart_part_bytes = int(
            raw.get("multipart_part_bytes", 10 * 1024 * 1024)
        )
        if not 5 * 1024 * 1024 <= multipart_part_bytes <= 20 * 1024 * 1024:
            raise NotionStaticConfigError(
                "notion multipart part size must be between 5 and 20 MiB"
            )
        return cls(
            enabled=bool(raw.get("enabled", False)),
            api_key=os.getenv(env_name, "").strip(),
            timeout=max(1.0, float(raw.get("request_timeout_seconds", 30))),
            max_file_bytes=max(0, int(raw.get("max_file_bytes", 0))),
            multipart_part_bytes=multipart_part_bytes,
            max_jobs_per_run=max(1, min(100, int(raw.get("max_jobs_per_run", 10)))),
            lease_seconds=max(5.0, float(raw.get("lease_seconds", DEFAULT_LEASE_SECONDS))),
            heartbeat_seconds=max(1.0, float(raw.get("claim_heartbeat_seconds", 30))),
        )


@dataclass(frozen=True, slots=True)
class ProjectNotionConfig:
    project_key: str
    database_id: str
    data_source_id: str
    properties: dict[str, str]
    sandbox: bool = False
    allow_synthetic_fixture_writes: bool = False

    @classmethod
    def from_config(cls, config: Mapping[str, Any], project_key: str) -> "ProjectNotionConfig":
        projects = config.get("projects")
        project = projects.get(project_key) if isinstance(projects, Mapping) else None
        notion = project.get("notion") if isinstance(project, Mapping) else None
        if not isinstance(notion, Mapping):
            raise NotionStaticConfigError("project notion configuration is missing")
        properties = dict(DEFAULT_PROPERTY_NAMES)
        configured = notion.get("properties")
        if isinstance(configured, Mapping):
            for key in properties:
                value = configured.get(key)
                if value is not None:
                    text = str(value).strip()
                    if not text:
                        raise NotionStaticConfigError("project property mapping is incomplete")
                    properties[key] = text
        database_id = str(notion.get("database_id") or "").strip()
        data_source_id = str(notion.get("data_source_id") or "").strip()
        if not database_id or not data_source_id:
            raise NotionStaticConfigError("project notion IDs are incomplete")
        return cls(
            project_key=project_key,
            database_id=database_id,
            data_source_id=data_source_id,
            properties=properties,
            sandbox=bool(notion.get("sandbox", False)),
            allow_synthetic_fixture_writes=bool(notion.get("allow_synthetic_fixture_writes", False)),
        )


class _ReadableHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if value:
            self.parts.append(value)


def _chunks(value: str, limit: int = 1900) -> Iterable[str]:
    text = str(value or "")
    if not text:
        yield ""
        return
    for index in range(0, len(text), limit):
        yield text[index:index + limit]


def _paragraphs(label: str, value: str) -> list[dict[str, Any]]:
    blocks = [{
        "object": "block",
        "type": "heading_2",
        "heading_2": {"rich_text": [{"type": "text", "text": {"content": label}}]},
    }]
    for chunk in _chunks(value or "None"):
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk or "None"}}]},
        })
    return blocks


def _email_content(raw: bytes) -> tuple[str, str, dict[str, str]]:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    headers = {}
    for name in ("Subject", "From", "To", "Cc", "Date", "Message-ID", "Delivered-To", "X-Original-To"):
        value = message.get(name)
        if value:
            headers[name] = str(value)
    plain: list[str] = []
    html_parts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_disposition() == "attachment":
            continue
        try:
            content = part.get_content()
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        if part.get_content_type() == "text/plain":
            plain.append(content)
        elif part.get_content_type() == "text/html":
            html_parts.append(content)
    if plain:
        body = "\n\n".join(item.strip() for item in plain if item.strip())
    else:
        parser = _ReadableHTML()
        parser.feed("\n".join(html_parts))
        body = "\n".join(parser.parts)
    return headers.get("Subject", "Untitled client source"), body or "No readable body.", headers


def _marker(artifact: IntakeArtifact, attempt_id: str) -> str:
    value = f"ckfu-v1-{artifact.artifact_id[:12]}-{artifact.content_sha256[:12]}-{attempt_id[:12]}"
    if not _MARKER_RE.fullmatch(value):
        raise ValueError("upload marker is invalid")
    return value


def _multipart_plan(byte_size: int, preferred_part_size: int) -> tuple[int, int]:
    part_size = int(preferred_part_size)
    minimum_part_size = 5 * 1024 * 1024
    part_count = max(1, math.ceil(int(byte_size) / part_size))
    if part_count > 1000:
        part_size = max(minimum_part_size, math.ceil(int(byte_size) / 1000))
        if part_size > 20 * 1024 * 1024:
            raise NotionStaticConfigError("notion multipart upload exceeds the part limit")
        part_count = math.ceil(int(byte_size) / part_size)
    if not 1 <= part_count <= 1000:
        raise NotionStaticConfigError("notion multipart upload exceeds the part limit")
    return part_size, part_count


def _file_block(upload_id: str, marker: str, artifact: IntakeArtifact) -> dict[str, Any]:
    caption = f"{marker} | sha256:{artifact.content_sha256} | source:{artifact.source_type}"
    if artifact.original_filename:
        caption += f" | original:{artifact.original_filename}"
    return {
        "object": "block",
        "type": "file",
        "file": {
            "type": "file_upload",
            "file_upload": {"id": upload_id},
            "caption": [{"type": "text", "text": {"content": caption[:1900]}}],
        },
    }


class _HeartbeatReader:
    def __init__(self, handle: BinaryIO, heartbeat: Callable[[], None], interval: float) -> None:
        self.handle = handle
        self.heartbeat = heartbeat
        self.interval = interval
        self.last = time.monotonic()

    def read(self, size: int = -1) -> bytes:
        if time.monotonic() - self.last >= self.interval:
            self.heartbeat()
            self.last = time.monotonic()
        return self.handle.read(size)


class _PartReader:
    def __init__(self, handle: BinaryIO, remaining: int, heartbeat: Callable[[], None], interval: float) -> None:
        self.reader = _HeartbeatReader(handle, heartbeat, interval)
        self.remaining = remaining

    def read(self, size: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        if size < 0 or size > self.remaining:
            size = self.remaining
        chunk = self.reader.read(size)
        self.remaining -= len(chunk)
        return chunk


class NotionArchiveWorker:
    def __init__(
        self,
        store: IntakeStore,
        spool: RawSpool,
        client: NotionClient,
        settings: NotionArchiveSettings,
        config: Mapping[str, Any],
    ) -> None:
        self.store = store
        self.spool = spool
        self.client = client
        self.settings = settings
        self.config = config
        self._schema_types: dict[str, dict[str, str]] = {}

    def _renew(self, claim: JobClaim) -> None:
        if not self.store.renew_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id,
            lease_seconds=self.settings.lease_seconds,
        ):
            raise PermissionError("notion job claim was lost")

    def preflight(self, project: ProjectNotionConfig, *, apply_schema: bool = False) -> dict[str, Any]:
        before = self.client.retrieve_data_source(project.data_source_id)
        parent = before.get("parent")
        if isinstance(parent, Mapping):
            parent_database_id = str(parent.get("database_id") or "")
            if parent_database_id and parent_database_id != project.database_id:
                raise NotionStaticConfigError("notion data source parent does not match configured database")
        properties = before.get("properties")
        if not isinstance(properties, Mapping):
            raise NotionStaticConfigError("notion data source has no property schema")
        expected_types = {
            "title": {"title"},
            "status": {"status", "select"},
            "received_at": {"date"},
            "source_label": {"rich_text"},
            "category": {"select"},
            "priority": {"select"},
        }
        for key, allowed in expected_types.items():
            item = properties.get(project.properties[key])
            if not isinstance(item, Mapping) or item.get("type") not in allowed:
                raise NotionStaticConfigError("notion business property schema does not match")
        self._schema_types[project.project_key] = {
            key: str(properties[project.properties[key]]["type"])
            for key in expected_types
        }
        self._require_options(properties[project.properties["status"]], {"New", "In Progress", "Resolved"})
        self._require_options(properties[project.properties["category"]], {"Other"})
        missing: dict[str, Any] = {}
        for key, (_default, definition) in MACHINE_PROPERTIES.items():
            name = project.properties[key]
            item = properties.get(name)
            expected_type = next(iter(definition))
            if item is None:
                missing[name] = definition
            elif not isinstance(item, Mapping) or item.get("type") != expected_type:
                raise NotionStaticConfigError("notion machine property has an incompatible type")
        if apply_schema and missing:
            self.client.update_data_source_properties(project.data_source_id, missing)
            after = self.client.retrieve_data_source(project.data_source_id)
            after_properties = after.get("properties")
            if not isinstance(after_properties, Mapping):
                raise NotionStaticConfigError("notion schema verification failed")
            for name, item in properties.items():
                if after_properties.get(name) != item:
                    raise NotionStaticConfigError("notion existing schema changed during additive preflight")
            for name, definition in missing.items():
                expected_type = next(iter(definition))
                added = after_properties.get(name)
                if not isinstance(added, Mapping) or added.get("type") != expected_type:
                    raise NotionStaticConfigError("notion additive schema update was not verified")
        if not missing or apply_schema:
            self.client.query_data_source(
                project.data_source_id,
                property_name=project.properties["source_id"],
                value="hermes-preflight-no-import-v1",
            )
        return {"project": project.project_key, "missing_properties": sorted(missing), "applied": bool(apply_schema and missing)}

    @staticmethod
    def _require_options(property_schema: Mapping[str, Any], required: set[str]) -> None:
        value = property_schema.get(str(property_schema.get("type") or ""))
        options = value.get("options") if isinstance(value, Mapping) else None
        names = {str(item.get("name")) for item in options or () if isinstance(item, Mapping)}
        if not required.issubset(names):
            raise NotionStaticConfigError("notion business property options are incomplete")

    def process_claim(self, claim: JobClaim) -> str:
        if claim.stage != "notion_archived":
            raise ValueError("notion worker received a different stage")
        artifact = self.store.get_artifact_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )
        if artifact.parent_artifact_id:
            raise NotionStaticConfigError("attachment artifacts do not own notion archive jobs")
        project = ProjectNotionConfig.from_config(self.config, artifact.project_key)
        self.preflight(project, apply_schema=False)
        page_id = self._ensure_page(claim, artifact, project)
        family = [artifact, *self.store.list_child_artifacts_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )]
        verified: list[str] = []
        for item in family:
            verified.append(self._ensure_file(claim, claim.artifact_id, item, page_id))
        digest = hashlib.sha256(
            json.dumps({"page_id": page_id, "attempts": verified}, sort_keys=True).encode()
        ).hexdigest()
        receipt = StageReceipt(
            artifact.artifact_id, "notion_archived", f"notion:page:{page_id}", digest
        )
        self._renew(claim)
        if not self.store.complete_stage(claim.job_id, claim.claim_token, receipt):
            raise PermissionError("notion job claim was lost before completion")
        return page_id

    def _ensure_page(
        self, claim: JobClaim, artifact: IntakeArtifact, project: ProjectNotionConfig
    ) -> str:
        operation = self.store.get_notion_operation(artifact.artifact_id, "page")
        page_id = str((operation or {}).get("page_id") or "")
        if page_id:
            self._verify_page(self.client.retrieve_page(page_id), artifact, project)
            self._ensure_source_blocks(claim, artifact, page_id)
            return page_id
        matches = self.client.query_data_source(
            project.data_source_id,
            property_name=project.properties["source_id"],
            value=artifact.artifact_id,
        )
        if len(matches) > 1:
            raise NotionAmbiguousRecoveryError("multiple notion source pages exist")
        if matches:
            page = matches[0]
            self._verify_page(page, artifact, project)
        else:
            subject, _body, _headers = self._source_content(artifact)
            self._renew(claim)
            page = self.client.create_page(
                project.data_source_id,
                properties=self._page_properties(artifact, project, subject),
                children=(),
            )
        page_id = str(page.get("id") or "")
        if not page_id:
            raise NotionArchiveError("notion page identity is missing")
        self._verify_page(self.client.retrieve_page(page_id), artifact, project)
        self._renew(claim)
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "page", "page-created",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id,
        )
        self._ensure_source_blocks(claim, artifact, page_id)
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "page", "page-verified",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id,
        )
        return page_id

    def _source_content(
        self, artifact: IntakeArtifact
    ) -> tuple[str, str, dict[str, str]]:
        with self.spool.read_verified(
            artifact.spool_key,
            storage_id=self.spool.storage_id,
            expected_sha256=artifact.content_sha256,
            expected_size=artifact.byte_size,
        ) as handle:
            raw = handle.read(MAX_RENDER_BYTES + 1)
        truncated = len(raw) > MAX_RENDER_BYTES
        subject, body, headers = _email_content(raw[:MAX_RENDER_BYTES])
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS]
            truncated = True
        if truncated:
            body = body.rstrip() + TRUNCATION_NOTICE
        return subject, body, headers

    def _ensure_source_blocks(
        self, claim: JobClaim, artifact: IntakeArtifact, page_id: str
    ) -> None:
        _subject, body, headers = self._source_content(artifact)
        blocks: list[dict[str, Any]] = []
        blocks.extend(_paragraphs(
            "Source headers",
            "\n".join(f"{key}: {value}" for key, value in headers.items()),
        ))
        blocks.extend(_paragraphs("Faithful body", body))
        metadata = {
            "artifact_id": artifact.artifact_id,
            "source_type": artifact.source_type,
            "sha256": artifact.content_sha256,
            "received_at": artifact.occurred_at,
            "delivered_alias": artifact.delivered_alias,
            "source_url": artifact.source_url,
            "processing_status": "New",
        }
        blocks.extend(_paragraphs(
            "Processing metadata",
            json.dumps(metadata, sort_keys=True, ensure_ascii=False),
        ))
        if len(blocks) > MAX_RENDER_BLOCKS:
            blocks = blocks[: MAX_RENDER_BLOCKS - 2]
            blocks.extend(_paragraphs("Display limit", TRUNCATION_NOTICE.strip()))
        digest = hashlib.sha256(
            json.dumps(blocks, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:12]
        evidence = self.client.list_block_children(
            page_id, heartbeat=lambda: self._renew(claim)
        )
        existing = json.dumps(evidence.items, sort_keys=True, ensure_ascii=False)
        for batch_index, offset in enumerate(range(0, len(blocks), 99)):
            marker = f"ckpage-v1-{artifact.artifact_id[:12]}-{digest}-{batch_index:04d}"
            if marker in existing:
                continue
            marker_block = {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": marker}}]
                },
            }
            self._renew(claim)
            self.client.append_block_children(
                page_id, [marker_block, *blocks[offset:offset + 99]]
            )

    def _page_properties(
        self, artifact: IntakeArtifact, project: ProjectNotionConfig, subject: str
    ) -> dict[str, Any]:
        names = project.properties
        status_type = self._schema_types.get(project.project_key, {}).get("status", "status")
        return {
            names["title"]: {"title": [{"text": {"content": subject[:1900]}}]},
            names["status"]: {status_type: {"name": "New"}},
            names["received_at"]: {"date": {"start": datetime.fromtimestamp(artifact.occurred_at, timezone.utc).isoformat()}},
            names["source_label"]: {"rich_text": [{"text": {"content": (artifact.actor_display or artifact.actor_id or artifact.provider_id)[:1900]}}]},
            names["category"]: {"select": {"name": "Other"}},
            names["source_id"]: {"rich_text": [{"text": {"content": artifact.artifact_id}}]},
            names["source_type"]: {"rich_text": [{"text": {"content": artifact.source_type}}]},
            names["source_hash"]: {"rich_text": [{"text": {"content": artifact.content_sha256}}]},
            names["source_url"]: {"url": artifact.source_url or None},
        }

    @staticmethod
    def _property_plain(page: Mapping[str, Any], name: str) -> str:
        item = page.get("properties", {}).get(name, {}) if isinstance(page.get("properties"), Mapping) else {}
        for kind in ("rich_text", "title"):
            values = item.get(kind) if isinstance(item, Mapping) else None
            if isinstance(values, list):
                return "".join(str(value.get("plain_text") or value.get("text", {}).get("content") or "") for value in values if isinstance(value, Mapping))
        return str(item.get("url") or "") if isinstance(item, Mapping) else ""

    def _verify_page(
        self, page: Mapping[str, Any], artifact: IntakeArtifact, project: ProjectNotionConfig
    ) -> None:
        if page.get("in_trash") is True:
            raise NotionAmbiguousRecoveryError("notion source page is in trash")
        parent = page.get("parent")
        if isinstance(parent, Mapping):
            remote_parent = str(parent.get("data_source_id") or "")
            if remote_parent and remote_parent != project.data_source_id:
                raise NotionAmbiguousRecoveryError("notion source page parent conflicts")
        if self._property_plain(page, project.properties["source_id"]) != artifact.artifact_id:
            raise NotionAmbiguousRecoveryError("notion source page identity conflicts")
        if self._property_plain(page, project.properties["source_hash"]) != artifact.content_sha256:
            raise NotionAmbiguousRecoveryError("notion source page hash conflicts")
        if self._property_plain(page, project.properties["source_type"]) != artifact.source_type:
            raise NotionAmbiguousRecoveryError("notion source page type conflicts")

    def _ensure_file(
        self, claim: JobClaim, claimed_artifact_id: str, artifact: IntakeArtifact, page_id: str
    ) -> str:
        operation = self.store.get_notion_operation(artifact.artifact_id, "file")
        attempts = self.store.get_upload_attempts(artifact.artifact_id)
        active_id = str((operation or {}).get("active_upload_attempt_id") or "")
        attempt = next((item for item in attempts if item["attempt_id"] == active_id), None)
        if attempt is None and attempts:
            attempt = attempts[-1]
        if operation and operation.get("state") == "receipt-verified" and attempt:
            return str(attempt["attempt_id"])
        if artifact.byte_size > self.settings.max_file_bytes or self.settings.max_file_bytes <= 0:
            raise NotionStaticConfigError("notion file admission cap blocks the artifact")
        if artifact.byte_size > 5 * 1024 * 1024 * 1024:
            raise NotionStaticConfigError("notion file exceeds the supported API maximum")
        if attempt is None:
            attempt_id = secrets.token_hex(16)
            marker = _marker(artifact, attempt_id)
            extension = _EXTENSIONS.get(artifact.mime_type, "bin")
            mode = "single_part" if artifact.byte_size <= 20 * 1024 * 1024 else "multi_part"
            part_size, part_count = _multipart_plan(
                artifact.byte_size, self.settings.multipart_part_bytes
            )
            attempt = self.store.reserve_upload_attempt(
                claim.job_id, claim.claim_token, artifact,
                attempt_id=attempt_id, opaque_marker=marker,
                remote_filename=f"{marker}.{extension}", upload_mode=mode,
                expected_part_count=part_count, expected_part_size=part_size,
                claimed_artifact_id=claimed_artifact_id,
            )
            self.store.select_active_upload_attempt(
                claim.job_id, claim.claim_token, claimed_artifact_id, artifact, attempt_id
            )
        attempt_id = str(attempt["attempt_id"])
        marker = str(attempt["opaque_marker"])
        upload_id = str(attempt.get("remote_upload_id") or "")
        if not upload_id:
            upload_id = self._create_or_recover_upload(claim, claimed_artifact_id, artifact, attempt)
            self.store.advance_notion_operation(
                claim.job_id, claim.claim_token, artifact.artifact_id, "file", "upload-created",
                expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
                expected_mime_type=artifact.mime_type, page_id=page_id,
                active_upload_attempt_id=attempt_id, claimed_artifact_id=claimed_artifact_id,
            )
        remote = self.client.retrieve_file_upload(upload_id)
        if remote.get("status") in {"expired", "failed"}:
            disposition = str(remote["status"])
            self.store.set_upload_attempt_disposition(
                claim.job_id, claim.claim_token, claimed_artifact_id,
                artifact.artifact_id, attempt_id, disposition,
                remote_upload_id=upload_id,
            )
            replacement_id = secrets.token_hex(16)
            replacement_marker = _marker(artifact, replacement_id)
            replacement = self.store.reserve_upload_attempt(
                claim.job_id, claim.claim_token, artifact,
                attempt_id=replacement_id, opaque_marker=replacement_marker,
                remote_filename=f"{replacement_marker}.{_EXTENSIONS.get(artifact.mime_type, 'bin')}",
                upload_mode=str(attempt["upload_mode"]),
                expected_part_count=int(attempt["expected_part_count"]),
                expected_part_size=int(attempt.get("expected_part_size") or 0),
                replaces_attempt_id=attempt_id,
                replacement_reason=f"{disposition}_before_attachment",
                claimed_artifact_id=claimed_artifact_id,
            )
            self.store.set_upload_attempt_disposition(
                claim.job_id, claim.claim_token, claimed_artifact_id,
                artifact.artifact_id, attempt_id, "superseded",
                remote_upload_id=upload_id,
            )
            self.store.select_active_upload_attempt(
                claim.job_id,
                claim.claim_token,
                claimed_artifact_id,
                artifact,
                replacement_id,
                state="upload-created",
            )
            attempt = replacement
            attempt_id = replacement_id
            marker = replacement_marker
            upload_id = self._create_or_recover_upload(
                claim, claimed_artifact_id, artifact, replacement
            )
            remote = self.client.retrieve_file_upload(upload_id)
        self._validate_remote_upload(remote, artifact, attempt)
        if remote.get("status") != "uploaded":
            self._send_bytes(claim, claimed_artifact_id, artifact, attempt, upload_id)
            remote = self.client.retrieve_file_upload(upload_id)
            self._validate_remote_upload(remote, artifact, attempt)
            if remote.get("status") != "uploaded":
                raise NotionIncompleteEvidenceError("notion upload bytes are not verified")
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "file", "bytes-sent",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id,
            active_upload_attempt_id=attempt_id, claimed_artifact_id=claimed_artifact_id,
        )
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "file", "multipart-completed",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id,
            active_upload_attempt_id=attempt_id, claimed_artifact_id=claimed_artifact_id,
        )
        block_id = self._ensure_attachment_block(
            claim, claimed_artifact_id, artifact, page_id, upload_id, marker, attempt_id
        )
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "file", "page-attached",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id, block_id=block_id,
            active_upload_attempt_id=attempt_id, claimed_artifact_id=claimed_artifact_id,
        )
        remote = self.client.retrieve_file_upload(upload_id)
        self._validate_remote_upload(remote, artifact, attempt)
        if remote.get("status") != "uploaded":
            raise NotionIncompleteEvidenceError("notion upload receipt is not verified")
        self.store.append_upload_attempt_event(
            claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
            "receipt_verified", claimed_artifact_id=claimed_artifact_id,
            remote_upload_id=upload_id, remote_state="uploaded", page_id=page_id, block_id=block_id,
        )
        self.store.advance_notion_operation(
            claim.job_id, claim.claim_token, artifact.artifact_id, "file", "receipt-verified",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type, page_id=page_id, block_id=block_id,
            active_upload_attempt_id=attempt_id, claimed_artifact_id=claimed_artifact_id,
        )
        return attempt_id

    @staticmethod
    def _validate_remote_upload(
        remote: Mapping[str, Any], artifact: IntakeArtifact, attempt: Mapping[str, Any]
    ) -> None:
        expected_upload_id = str(attempt.get("remote_upload_id") or "")
        if expected_upload_id and str(remote.get("id") or "") != expected_upload_id:
            raise NotionAmbiguousRecoveryError("notion upload identity conflicts")
        if remote.get("filename") not in {None, attempt["remote_filename"]}:
            raise NotionAmbiguousRecoveryError("notion upload marker conflicts")
        if remote.get("content_type") not in {None, artifact.mime_type}:
            raise NotionAmbiguousRecoveryError("notion upload content type conflicts")
        if remote.get("content_length") not in {None, artifact.byte_size}:
            raise NotionAmbiguousRecoveryError("notion upload size conflicts")
        parts = remote.get("number_of_parts") or {}
        if (
            attempt["upload_mode"] == "multi_part"
            and parts.get("total") not in {None, int(attempt["expected_part_count"])}
        ):
            raise NotionAmbiguousRecoveryError("notion multipart identity conflicts")

    def _complete_scan(self, claim: JobClaim, artifact_id: str, attempt_id: str, role: str):
        evidence = self.client.list_file_uploads(
            heartbeat=lambda: self._renew(claim)
        )
        scan_id = self.store.publish_upload_scan(
            claim.job_id, claim.claim_token, artifact_id, attempt_id,
            scan_role=role, page_count=evidence.page_count, items=list(evidence.items),
            claimed_artifact_id=claim.artifact_id,
        )
        return scan_id, evidence

    def _create_or_recover_upload(
        self, claim: JobClaim, claimed_artifact_id: str, artifact: IntakeArtifact,
        attempt: Mapping[str, Any],
    ) -> str:
        attempt_id = str(attempt["attempt_id"])
        baseline_id = str(attempt.get("baseline_scan_id") or "")
        existing_events = self.store.get_upload_attempt_events(attempt_id)
        create_started = any(item["event_type"] == "create_started" for item in existing_events)
        if baseline_id and create_started:
            return self._recover_uncertain_creation(
                claim, claimed_artifact_id, artifact, attempt, baseline_id
            )
        if not baseline_id:
            baseline_id, _ = self._complete_scan(claim, artifact.artifact_id, attempt_id, "baseline")
            attempt = self.store.get_upload_attempts(artifact.artifact_id)[-1]
        self.store.append_upload_attempt_event(
            claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
            "create_started", claimed_artifact_id=claimed_artifact_id,
        )
        self._renew(claim)
        try:
            remote = self.client.create_file_upload(
                filename=str(attempt["remote_filename"]),
                content_type=artifact.mime_type,
                mode=str(attempt["upload_mode"]),
                number_of_parts=int(attempt["expected_part_count"]),
            )
        except (httpx.HTTPError, NotionRetryableError):
            self.store.append_upload_attempt_event(
                claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
                "create_result_uncertain", claimed_artifact_id=claimed_artifact_id,
            )
            return self._recover_uncertain_creation(claim, claimed_artifact_id, artifact, attempt, baseline_id)
        upload_id = str(remote.get("id") or "")
        if not upload_id:
            self.store.append_upload_attempt_event(
                claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
                "create_result_uncertain", claimed_artifact_id=claimed_artifact_id,
            )
            return self._recover_uncertain_creation(claim, claimed_artifact_id, artifact, attempt, baseline_id)
        self._renew(claim)
        self.store.record_upload_remote_identity(
            claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id, upload_id,
            evidence_identity="direct-create-response", claimed_artifact_id=claimed_artifact_id,
        )
        return upload_id

    def _recover_uncertain_creation(
        self, claim: JobClaim, claimed_artifact_id: str, artifact: IntakeArtifact,
        attempt: Mapping[str, Any], baseline_id: str,
    ) -> str:
        reconciliation_id, evidence = self._complete_scan(
            claim, artifact.artifact_id, str(attempt["attempt_id"]), "reconciliation"
        )
        baseline = {item["remote_upload_id"] for item in self.store.get_upload_scan_items(baseline_id)}
        delta = [item for item in evidence.items if str(item.get("id") or "") not in baseline]
        eligible = []
        for item in delta:
            parts = item.get("number_of_parts") or {}
            if (
                item.get("filename") == attempt["remote_filename"]
                and item.get("content_type") in {None, artifact.mime_type}
                and (item.get("content_length") in {None, artifact.byte_size})
                and (
                    attempt["upload_mode"] != "multi_part"
                    or parts.get("total") in {None, int(attempt["expected_part_count"])}
                )
            ):
                eligible.append(item)
        if not eligible:
            raise NotionIncompleteEvidenceError(
                "notion upload creation is not yet visible in complete evidence"
            )
        if len(eligible) != 1:
            raise NotionAmbiguousRecoveryError("notion upload creation cannot be attributed safely")
        upload_id = str(eligible[0]["id"])
        evidence_identity = hashlib.sha256(
            f"{baseline_id}\0{reconciliation_id}\0{attempt['opaque_marker']}\0{upload_id}".encode()
        ).hexdigest()
        self.store.record_upload_remote_identity(
            claim.job_id, claim.claim_token, artifact.artifact_id,
            str(attempt["attempt_id"]), upload_id,
            evidence_identity=evidence_identity, claimed_artifact_id=claimed_artifact_id,
        )
        return upload_id

    def _send_bytes(
        self, claim: JobClaim, claimed_artifact_id: str, artifact: IntakeArtifact,
        attempt: Mapping[str, Any], upload_id: str,
    ) -> None:
        attempt_id = str(attempt["attempt_id"])
        mode = str(attempt["upload_mode"])
        remote = self.client.retrieve_file_upload(upload_id)
        remote_parts = remote.get("number_of_parts") or {}
        sent_parts = int(remote_parts.get("sent") or 0)
        total_parts = int(remote_parts.get("total") or attempt["expected_part_count"])
        if total_parts != int(attempt["expected_part_count"]):
            raise NotionAmbiguousRecoveryError("notion multipart part count conflicts")
        part_size = int(attempt.get("expected_part_size") or 0)
        if mode == "multi_part" and part_size <= 0:
            raise NotionAmbiguousRecoveryError(
                "notion multipart partition size is not durably known"
            )
        with self.spool.read_verified(
            artifact.spool_key, storage_id=self.spool.storage_id,
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
        ) as handle:
            if mode == "single_part":
                self._renew(claim)
                self.client.send_file_upload(
                    upload_id, filename=str(attempt["remote_filename"]),
                    content_type=artifact.mime_type,
                    content=_HeartbeatReader(handle, lambda: self._renew(claim), self.settings.heartbeat_seconds),
                )
            else:
                if sent_parts < 0 or sent_parts > int(attempt["expected_part_count"]):
                    raise NotionAmbiguousRecoveryError("notion multipart progress conflicts")
                if sent_parts:
                    handle.seek(sent_parts * part_size)
                for part_number in range(sent_parts + 1, int(attempt["expected_part_count"]) + 1):
                    if not 1 <= part_number <= 1000:
                        raise NotionStaticConfigError("notion part number is outside the API limit")
                    remaining = min(
                        part_size,
                        artifact.byte_size - handle.tell(),
                    )
                    self._renew(claim)
                    self.client.send_file_upload(
                        upload_id, filename=str(attempt["remote_filename"]),
                        content_type=artifact.mime_type,
                        content=_PartReader(handle, remaining, lambda: self._renew(claim), self.settings.heartbeat_seconds),
                        part_number=part_number,
                    )
                    self._renew(claim)
                    self.store.append_upload_attempt_event(
                        claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
                        "part_acknowledged", claimed_artifact_id=claimed_artifact_id,
                        remote_upload_id=upload_id, remote_parts_sent=part_number,
                    )
                self._renew(claim)
                self.client.complete_file_upload(upload_id)
                self._renew(claim)
                self.store.append_upload_attempt_event(
                    claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
                    "multipart_complete_observed", claimed_artifact_id=claimed_artifact_id,
                    remote_upload_id=upload_id,
                )

    def _ensure_attachment_block(
        self, claim: JobClaim, claimed_artifact_id: str, artifact: IntakeArtifact,
        page_id: str, upload_id: str, marker: str, attempt_id: str,
    ) -> str:
        evidence = self.client.list_block_children(
            page_id, heartbeat=lambda: self._renew(claim)
        )
        matches = []
        for block in evidence.items:
            content = block.get(block.get("type"), {}) if isinstance(block, Mapping) else {}
            caption = content.get("caption") if isinstance(content, Mapping) else None
            plain = "".join(str(item.get("plain_text") or item.get("text", {}).get("content") or "") for item in caption or () if isinstance(item, Mapping))
            block_upload_id = str(
                content.get("file_upload", {}).get("id")
                if isinstance(content, Mapping)
                and isinstance(content.get("file_upload"), Mapping)
                else ""
            )
            if marker in plain and (not block_upload_id or block_upload_id == upload_id):
                matches.append(block)
            elif marker in plain:
                raise NotionAmbiguousRecoveryError(
                    "notion attachment marker points to another upload"
                )
        if len(matches) > 1:
            raise NotionAmbiguousRecoveryError("multiple notion attachment blocks match")
        if matches:
            block_id = str(matches[0].get("id") or "")
        else:
            self._renew(claim)
            response = self.client.append_block_children(
                page_id, [_file_block(upload_id, marker, artifact)]
            )
            results = response.get("results")
            block_id = str(results[0].get("id") or "") if isinstance(results, list) and results else ""
        if not block_id:
            raise NotionIncompleteEvidenceError("notion attachment block identity is missing")
        self._renew(claim)
        self.store.append_upload_attempt_event(
            claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
            "attachment_observed", claimed_artifact_id=claimed_artifact_id,
            remote_upload_id=upload_id, page_id=page_id, block_id=block_id,
        )
        return block_id


def run_notion_once(
    *,
    store: IntakeStore,
    spool: RawSpool,
    config: Mapping[str, Any] | None = None,
    client: NotionClient | None = None,
) -> dict[str, int]:
    cfg = dict(config or load_config() or {})
    settings = NotionArchiveSettings.from_config(cfg)
    if not settings.enabled:
        return {"processed": 0, "succeeded": 0, "failed": 0, "quarantined": 0}
    owns_client = client is None
    client = client or NotionClient(settings.api_key, timeout=settings.timeout)
    worker = NotionArchiveWorker(store, spool, client, settings, cfg)
    result = {"processed": 0, "succeeded": 0, "failed": 0, "quarantined": 0}
    try:
        for _ in range(settings.max_jobs_per_run):
            claim = store.claim_next(
                stage="notion_archived", spool=spool, lease_seconds=settings.lease_seconds
            )
            if claim is None:
                break
            result["processed"] += 1
            try:
                worker.process_claim(claim)
                result["succeeded"] += 1
            except PermissionError:
                continue
            except NotionArchiveError as exc:
                changed = store.fail_stage(
                    claim.job_id, claim.claim_token, error_class=exc.error_class,
                    retry_delay=60 if exc.retryable else 0, quarantine=exc.quarantine,
                )
                if changed:
                    result["quarantined" if exc.quarantine else "failed"] += 1
            except Exception:
                if store.fail_stage(
                    claim.job_id, claim.claim_token,
                    error_class="notion_internal_error", quarantine=True,
                ):
                    result["quarantined"] += 1
    finally:
        if owns_client:
            client.close()
    return result


def _assert_sandbox_target(
    config: Mapping[str, Any], project: ProjectNotionConfig
) -> None:
    if project.project_key == "pid" or not project.sandbox:
        raise NotionStaticConfigError("synthetic fixtures require a non-PID sandbox project")
    if not project.allow_synthetic_fixture_writes:
        raise NotionStaticConfigError("synthetic fixture writes are not enabled")
    projects = config.get("projects")
    if not isinstance(projects, Mapping):
        raise NotionStaticConfigError("PID target must be configured for sandbox separation proof")
    try:
        pid = ProjectNotionConfig.from_config(config, "pid")
    except NotionStaticConfigError as exc:
        raise NotionStaticConfigError(
            "valid PID Notion IDs are required for sandbox separation proof"
        ) from exc
    if not pid.database_id or not pid.data_source_id:
        raise NotionStaticConfigError(
            "valid PID Notion IDs are required for sandbox separation proof"
        )
    if pid.database_id == project.database_id or pid.data_source_id == project.data_source_id:
        raise NotionStaticConfigError("sandbox Notion target overlaps PID")
    for key, value in projects.items():
        notion = value.get("notion") if isinstance(value, Mapping) else None
        if not isinstance(notion, Mapping) or str(key) == project.project_key:
            continue
        if bool(notion.get("sandbox", False)):
            continue
        if (
            str(notion.get("database_id") or "") == project.database_id
            or str(notion.get("data_source_id") or "") == project.data_source_id
        ):
            raise NotionStaticConfigError("sandbox Notion target overlaps a production project")


def run_fixed_sandbox_fixtures(
    *,
    store: IntakeStore,
    spool: RawSpool,
    client: NotionClient,
    config: Mapping[str, Any],
    project_key: str,
) -> dict[str, Any]:
    """Archive only internally generated, fixed synthetic fixture bytes."""
    settings = NotionArchiveSettings.from_config(config)
    project = ProjectNotionConfig.from_config(config, project_key)
    _assert_sandbox_target(config, project)
    large_size = 20 * 1024 * 1024 + 1
    if settings.max_file_bytes < large_size:
        raise NotionStaticConfigError("sandbox admission cap does not admit multipart fixture")
    raw = (
        b"From: fixture@example.invalid\r\n"
        b"To: archive@example.invalid\r\n"
        b"Subject: Hermes Notion archive sandbox fixture\r\n"
        b"Message-ID: <hermes-notion-fixture-v1@example.invalid>\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Fixed synthetic client-knowledge source fixture.\r\n"
    )
    parent = IntakeArtifact.from_bytes(
        project_key=project_key,
        provider_id="gmail",
        provider_artifact_id="hermes-notion-sandbox-fixture-v1",
        provider_message_id="hermes-notion-sandbox-fixture-v1",
        actor_display="Hermes synthetic fixture",
        delivered_alias="archive@example.invalid",
        original_filename="fixture.eml",
        mime_type="message/rfc822",
        source_url="https://example.invalid/hermes-notion-fixture-v1",
        content=raw,
        received_at=1_700_000_000,
    )
    small = b"Hermes fixed synthetic attachment fixture.\n"
    small_attachment = IntakeArtifact.from_bytes(
        project_key=project_key,
        provider_id="gmail",
        provider_artifact_id="hermes-notion-sandbox-fixture-v1:small",
        provider_message_id=parent.provider_message_id,
        provider_attachment_id="small",
        source_type="attachment",
        parent_artifact_id=parent.artifact_id,
        original_filename="fixture.txt",
        mime_type="text/plain",
        content=small,
        received_at=1_700_000_000,
    )
    large = b"H" * large_size
    large_attachment = IntakeArtifact.from_bytes(
        project_key=project_key,
        provider_id="gmail",
        provider_artifact_id="hermes-notion-sandbox-fixture-v1:multipart",
        provider_message_id=parent.provider_message_id,
        provider_attachment_id="multipart",
        source_type="attachment",
        parent_artifact_id=parent.artifact_id,
        original_filename="fixture.bin",
        mime_type="application/octet-stream",
        content=large,
        received_at=1_700_000_000,
    )
    store.admit_raw_artifact(
        spool, parent, [raw], next_stages=("notion_archived",)
    )
    store.admit_raw_artifact(spool, small_attachment, [small])
    store.admit_raw_artifact(spool, large_attachment, [large])
    existing_page = store.get_notion_operation(parent.artifact_id, "page")
    completed = store.get_completed_stage_receipt(
        parent.artifact_id, "notion_archived"
    )
    if completed:
        if not existing_page or not existing_page.get("page_id"):
            raise NotionIncompleteEvidenceError(
                "sandbox fixture completion has no durable page identity"
            )
        page_id = str(existing_page["page_id"])
        page = client.retrieve_page(page_id)
        worker = NotionArchiveWorker(store, spool, client, settings, config)
        worker.preflight(project, apply_schema=False)
        worker._verify_page(page, parent, project)
        blocks = client.list_block_children(page_id)
        rendered = json.dumps(blocks.items, sort_keys=True, ensure_ascii=False)
        for artifact in (parent, small_attachment, large_attachment):
            operation = store.get_notion_operation(artifact.artifact_id, "file")
            attempts = store.get_upload_attempts(artifact.artifact_id)
            if not operation or operation.get("state") != "receipt-verified" or not attempts:
                raise NotionIncompleteEvidenceError(
                    "sandbox fixture archive is incomplete and requires operator retry"
                )
            attempt = next(
                (
                    item for item in attempts
                    if item["attempt_id"] == operation.get("active_upload_attempt_id")
                ),
                None,
            )
            if attempt is None or not attempt.get("remote_upload_id"):
                raise NotionIncompleteEvidenceError(
                    "sandbox fixture upload identity is incomplete"
                )
            remote = client.retrieve_file_upload(str(attempt["remote_upload_id"]))
            worker._validate_remote_upload(remote, artifact, attempt)
            if remote.get("status") != "uploaded" or str(attempt["opaque_marker"]) not in rendered:
                raise NotionIncompleteEvidenceError(
                    "sandbox fixture remote receipt is incomplete"
                )
        return {
            "project": project_key,
            "page_id": page_id,
            "fixture_count": 3,
            "multipart_exercised": True,
            "recovered": True,
        }
    store.reconcile()
    claim = store.claim_next(
        stage="notion_archived", spool=spool, lease_seconds=settings.lease_seconds
    )
    if claim is None or claim.artifact_id != parent.artifact_id:
        raise NotionStaticConfigError("sandbox fixture job is unavailable")
    page_id = NotionArchiveWorker(store, spool, client, settings, config).process_claim(claim)
    return {
        "project": project_key,
        "page_id": page_id,
        "fixture_count": 3,
        "multipart_exercised": True,
        "recovered": False,
    }


__all__ = [
    "NotionArchiveSettings",
    "NotionArchiveWorker",
    "ProjectNotionConfig",
    "UPLOAD_STATES",
    "run_fixed_sandbox_fixtures",
    "run_notion_once",
]
