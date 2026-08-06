"""Immutable, validated contracts for client-knowledge intake.

The intake lane deliberately stores metadata and hashes, never raw provider
payloads.  Provider identity is part of the artifact identity: a byte-identical
document received from two providers is still two distinct intake artifacts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from .scope import ClientKnowledgeValidationError, validate_project_key


_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32,128}$")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_MIME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}/"
    r"[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}$"
)
VALID_PROVIDERS = frozenset({"gmail", "discord"})
VALID_SOURCE_TYPES = frozenset({"email", "attachment"})
UNMAPPED_PROJECT_KEY = "unmapped"
VALID_STAGES = frozenset(
    {
        "discovered",
        "raw_preserved",
        "notion_archived",
        "extracted",
        "interpreted",
        "assimilated",
        "honcho_projected",
        "complete",
        "needs_mapping",
        "needs_review",
        "quarantined",
    }
)


def _text(value: Any, name: str, *, max_length: int = 500) -> str:
    result = str(value or "").strip()
    if not result or len(result) > max_length or _CONTROL_RE.search(result):
        raise ClientKnowledgeValidationError(f"{name} must be a bounded non-empty value")
    return result


def _identity(value: Any, name: str) -> str:
    result = _text(value, name, max_length=128)
    if not _IDENTITY_RE.fullmatch(result):
        raise ClientKnowledgeValidationError(
            f"{name} must contain only ASCII identity characters"
        )
    return result


def validate_stage(value: Any) -> str:
    stage = _text(value, "stage", max_length=64).lower()
    if stage not in VALID_STAGES:
        raise ClientKnowledgeValidationError("stage is not part of the intake state machine")
    return stage


def _sha256(value: Any, name: str = "content_sha256") -> str:
    result = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(result):
        raise ClientKnowledgeValidationError(f"{name} must be a lowercase SHA-256 hex digest")
    return result


def _timestamp(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ClientKnowledgeValidationError(f"{name} must be a finite timestamp") from exc
    if not math.isfinite(result) or result < 0:
        raise ClientKnowledgeValidationError(f"{name} must be a finite timestamp")
    return result


def _optional_text(value: Any, name: str, *, max_length: int) -> str:
    if value is None or value == "":
        return ""
    return _text(value, name, max_length=max_length)


def _provenance_json(value: Any) -> str:
    if value in (None, ""):
        parsed: Any = {}
    elif isinstance(value, Mapping):
        parsed = dict(value)
    elif isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ClientKnowledgeValidationError("provenance_json must be valid JSON") from exc
    else:
        raise ClientKnowledgeValidationError("provenance_json must be a JSON object")
    if not isinstance(parsed, dict):
        raise ClientKnowledgeValidationError("provenance_json must be a JSON object")
    try:
        result = json.dumps(parsed, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ClientKnowledgeValidationError("provenance_json must be JSON serializable") from exc
    if len(result) > 32_000:
        raise ClientKnowledgeValidationError("provenance_json is too large")
    return result


def artifact_identity(
    provider_id: str,
    provider_artifact_id: str,
    project_key: str = "",
) -> str:
    """Return a stable opaque artifact id for one provider identity."""
    provider = _identity(provider_id, "provider_id")
    external_id = _text(provider_artifact_id, "provider_artifact_id")
    if project_key:
        validate_project_key(project_key)
    payload = f"client-knowledge-artifact\0{provider}\0{external_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def storage_key(provider_id: str, provider_artifact_id: str, project_key: str = "") -> str:
    """Return an opaque, traversal-safe raw-spool key.

    The key is a digest rather than a sanitized provider value, so original
    provider identifiers and filenames cannot leak through the filesystem.
    """
    provider = _identity(provider_id, "provider_id")
    if provider not in VALID_PROVIDERS:
        raise ClientKnowledgeValidationError("provider_id is not supported")
    external_id = _text(provider_artifact_id, "provider_artifact_id")
    if project_key:
        validate_project_key(project_key)
    payload = f"client-knowledge-spool\0{provider}\0{external_id}".encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class IntakeArtifact:
    """Immutable metadata for one provider-scoped raw artifact."""

    project_key: str
    provider_id: str
    provider_artifact_id: str
    content_sha256: str
    byte_size: int
    source_type: str = "email"
    parent_artifact_id: str = ""
    provider_message_id: str = ""
    provider_attachment_id: str = ""
    occurred_at: float = 0
    actor_display: str = ""
    actor_id: str = ""
    delivered_alias: str = ""
    original_filename: str = ""
    mime_type: str = "application/octet-stream"
    source_url: str = ""
    text_context: str = ""
    provenance_json: str | Mapping[str, Any] = "{}"
    spool_key: str = ""
    artifact_id: str = ""
    received_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        project = validate_project_key(self.project_key)
        provider = _identity(self.provider_id, "provider_id")
        if provider not in VALID_PROVIDERS:
            raise ClientKnowledgeValidationError("provider_id is not supported")
        provider_artifact = _text(self.provider_artifact_id, "provider_artifact_id")
        source_type = _identity(self.source_type, "source_type")
        if source_type not in VALID_SOURCE_TYPES:
            raise ClientKnowledgeValidationError("source_type is not supported")
        digest = _sha256(self.content_sha256)
        if isinstance(self.byte_size, bool) or not isinstance(self.byte_size, int):
            raise ClientKnowledgeValidationError("byte_size must be a non-negative integer")
        size = self.byte_size
        if size < 0:
            raise ClientKnowledgeValidationError("byte_size must be a non-negative integer")
        key = self.spool_key.strip() if isinstance(self.spool_key, str) else ""
        if key and not _STORAGE_KEY_RE.fullmatch(key):
            raise ClientKnowledgeValidationError("spool_key must be an opaque hexadecimal key")
        expected_spool_key = storage_key(provider, provider_artifact, project)
        if key and key != expected_spool_key:
            raise ClientKnowledgeValidationError("spool_key does not match provider identity")
        artifact = self.artifact_id.strip() if isinstance(self.artifact_id, str) else ""
        expected_artifact = artifact_identity(provider, provider_artifact, project)
        if artifact and artifact != expected_artifact:
            raise ClientKnowledgeValidationError("artifact_id does not match provider identity")
        parent = self.parent_artifact_id.strip() if isinstance(self.parent_artifact_id, str) else ""
        if parent and not _SHA256_RE.fullmatch(parent):
            raise ClientKnowledgeValidationError("parent_artifact_id is not canonical")
        if source_type == "attachment" and not parent:
            raise ClientKnowledgeValidationError("attachment artifacts require parent_artifact_id")
        if parent == expected_artifact:
            raise ClientKnowledgeValidationError("artifact cannot be its own parent")
        message_id = _optional_text(
            self.provider_message_id or (provider_artifact if source_type == "email" else ""),
            "provider_message_id",
            max_length=500,
        )
        attachment_id = _optional_text(
            self.provider_attachment_id,
            "provider_attachment_id",
            max_length=500,
        )
        if source_type == "attachment" and not attachment_id:
            raise ClientKnowledgeValidationError(
                "attachment artifacts require provider_attachment_id"
            )
        received = _timestamp(self.received_at, "received_at")
        occurred = received if self.occurred_at in (None, 0) else _timestamp(
            self.occurred_at,
            "occurred_at",
        )
        actor_display = _optional_text(self.actor_display, "actor_display", max_length=500)
        actor_id = _optional_text(self.actor_id, "actor_id", max_length=500)
        delivered_alias = _optional_text(
            self.delivered_alias,
            "delivered_alias",
            max_length=320,
        )
        original_filename = _optional_text(
            self.original_filename,
            "original_filename",
            max_length=255,
        )
        mime_type = str(self.mime_type or "application/octet-stream").strip().lower()
        if not _MIME_RE.fullmatch(mime_type):
            raise ClientKnowledgeValidationError("mime_type is not canonical")
        source_url = _optional_text(self.source_url, "source_url", max_length=2048)
        text_context = _optional_text(self.text_context, "text_context", max_length=16_000)
        provenance = _provenance_json(self.provenance_json)
        object.__setattr__(self, "project_key", project)
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "provider_artifact_id", provider_artifact)
        object.__setattr__(self, "source_type", source_type)
        object.__setattr__(self, "parent_artifact_id", parent)
        object.__setattr__(self, "provider_message_id", message_id)
        object.__setattr__(self, "provider_attachment_id", attachment_id)
        object.__setattr__(self, "occurred_at", occurred)
        object.__setattr__(self, "actor_display", actor_display)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "delivered_alias", delivered_alias)
        object.__setattr__(self, "original_filename", original_filename)
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "source_url", source_url)
        object.__setattr__(self, "text_context", text_context)
        object.__setattr__(self, "provenance_json", provenance)
        object.__setattr__(self, "content_sha256", digest)
        object.__setattr__(self, "byte_size", size)
        object.__setattr__(self, "spool_key", key or expected_spool_key)
        object.__setattr__(self, "artifact_id", expected_artifact)
        object.__setattr__(self, "received_at", received)

    @classmethod
    def from_bytes(
        cls,
        *,
        project_key: str,
        provider_id: str,
        provider_artifact_id: str,
        content: bytes,
        source_type: str = "email",
        parent_artifact_id: str = "",
        provider_message_id: str = "",
        provider_attachment_id: str = "",
        occurred_at: float | None = None,
        actor_display: str = "",
        actor_id: str = "",
        delivered_alias: str = "",
        original_filename: str = "",
        mime_type: str = "application/octet-stream",
        source_url: str = "",
        text_context: str = "",
        provenance_json: str | Mapping[str, Any] = "{}",
        received_at: float | None = None,
    ) -> "IntakeArtifact":
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise ClientKnowledgeValidationError("content must be bytes-like")
        payload = bytes(content)
        return cls(
            project_key=project_key,
            provider_id=provider_id,
            provider_artifact_id=provider_artifact_id,
            source_type=source_type,
            parent_artifact_id=parent_artifact_id,
            provider_message_id=provider_message_id,
            provider_attachment_id=provider_attachment_id,
            occurred_at=0 if occurred_at is None else occurred_at,
            actor_display=actor_display,
            actor_id=actor_id,
            delivered_alias=delivered_alias,
            original_filename=original_filename,
            mime_type=mime_type,
            source_url=source_url,
            text_context=text_context,
            provenance_json=provenance_json,
            content_sha256=hashlib.sha256(payload).hexdigest(),
            byte_size=len(payload),
            received_at=time.time() if received_at is None else received_at,
        )

    @property
    def provider_identity(self) -> str:
        return f"{self.provider_id}:{self.provider_artifact_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "project_key": self.project_key,
            "provider_id": self.provider_id,
            "provider_artifact_id": self.provider_artifact_id,
            "source_type": self.source_type,
            "parent_artifact_id": self.parent_artifact_id,
            "provider_message_id": self.provider_message_id,
            "provider_attachment_id": self.provider_attachment_id,
            "occurred_at": self.occurred_at,
            "actor_display": self.actor_display,
            "actor_id": self.actor_id,
            "delivered_alias": self.delivered_alias,
            "original_filename": self.original_filename,
            "mime_type": self.mime_type,
            "source_url": self.source_url,
            "text_context": self.text_context,
            "provenance_json": self.provenance_json,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "spool_key": self.spool_key,
            "received_at": self.received_at,
        }


@dataclass(frozen=True, slots=True)
class IntakeStage:
    """Immutable identifier for an artifact processing stage."""

    artifact_id: str
    stage: str

    def __post_init__(self) -> None:
        artifact = _text(self.artifact_id, "artifact_id", max_length=128)
        stage = validate_stage(self.stage)
        if not _SHA256_RE.fullmatch(artifact):
            raise ClientKnowledgeValidationError("stage identity is not canonical")
        object.__setattr__(self, "artifact_id", artifact)
        object.__setattr__(self, "stage", stage)


@dataclass(frozen=True, slots=True)
class StageReceipt:
    """Immutable receipt produced by one completed stage."""

    artifact_id: str
    stage: str
    receipt_id: str
    output_sha256: str = ""
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        stage = IntakeStage(self.artifact_id, self.stage)
        receipt = _text(self.receipt_id, "receipt_id", max_length=128)
        output = self.output_sha256.strip().lower() if isinstance(self.output_sha256, str) else ""
        if output and not _SHA256_RE.fullmatch(output):
            raise ClientKnowledgeValidationError("output_sha256 must be a lowercase SHA-256 hex digest")
        object.__setattr__(self, "artifact_id", stage.artifact_id)
        object.__setattr__(self, "stage", stage.stage)
        object.__setattr__(self, "receipt_id", receipt)
        object.__setattr__(self, "output_sha256", output)
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at, "recorded_at"))


@dataclass(frozen=True, slots=True)
class ExternalReceipt:
    """Immutable idempotency receipt for an external provider operation."""

    provider_id: str
    external_id: str
    artifact_id: str
    receipt_kind: str = ""
    recorded_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        provider = _identity(self.provider_id, "provider_id")
        external = _text(self.external_id, "external_id")
        artifact = _text(self.artifact_id, "artifact_id", max_length=128)
        if not _SHA256_RE.fullmatch(artifact):
            raise ClientKnowledgeValidationError("artifact_id is not canonical")
        kind = self.receipt_kind.strip() if isinstance(self.receipt_kind, str) else ""
        if kind and not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", kind):
            raise ClientKnowledgeValidationError("receipt_kind is not canonical")
        object.__setattr__(self, "provider_id", provider)
        object.__setattr__(self, "external_id", external)
        object.__setattr__(self, "artifact_id", artifact)
        object.__setattr__(self, "receipt_kind", kind)
        object.__setattr__(self, "recorded_at", _timestamp(self.recorded_at, "recorded_at"))


# Friendly aliases used by early consumers and future PRs.
Artifact = IntakeArtifact
ArtifactStage = IntakeStage
IntakeArtifactRecord = IntakeArtifact


__all__ = [
    "Artifact",
    "ArtifactStage",
    "ExternalReceipt",
    "IntakeArtifact",
    "IntakeArtifactRecord",
    "IntakeStage",
    "StageReceipt",
    "UNMAPPED_PROJECT_KEY",
    "VALID_PROVIDERS",
    "VALID_SOURCE_TYPES",
    "VALID_STAGES",
    "artifact_identity",
    "storage_key",
    "validate_stage",
]
