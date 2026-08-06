"""Deterministic, bounded extraction for archived client-knowledge sources."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from email import policy
from email.header import decode_header
from email.message import Message
from email.parser import BytesFeedParser
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence

from agent.redact import redact_sensitive_text
from hermes_cli.config import load_config
from tools.read_extract import DocumentExtractionLimits, ExtractionError, extract_document_text

from .derived import DerivedRecord, DerivedStore, canonical_json, versioned_identity
from .models import IntakeArtifact
from .spool import RawSpool
from .store import DEFAULT_LEASE_SECONDS, IntakeStore, JobClaim

EXTRACTOR_VERSION = "client-knowledge-extractor/v1"
EXTRACTION_LIMITS_VERSION = "client-knowledge-extraction-limits/v1"
REDACTION_VERSION = "client-knowledge-redaction/v1"

_SELECTED_HEADERS = (
    "Message-ID",
    "Date",
    "From",
    "Reply-To",
    "To",
    "Cc",
    "Delivered-To",
    "X-Original-To",
    "Subject",
)
_DOC_MIMES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
_PLAIN_ATTACHMENT_MIMES = frozenset(
    {
        "text/plain",
        "text/csv",
        "text/tab-separated-values",
        "text/markdown",
        "text/x-markdown",
    }
)
_SECRET_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?-----END[A-Z ]*PRIVATE KEY-----")),
    ("authorization_header", re.compile(r"((?:Proxy-)?Authorization:\s*)(?:[A-Za-z][\w.+-]*\s+)?([^\s\"']+)", re.I)),
    ("api_key_header", re.compile(r"((?:x-api-key|x-goog-api-key|api-key|apikey|x-api-token|x-auth-token|x-access-token)\s*:\s*)(\S+)", re.I)),
    ("known_provider_token", re.compile(r"(?<![A-Za-z0-9_-])(?:sk-[A-Za-z0-9_-]{10,}|gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{30,}|ntn_[A-Za-z0-9]{10,}|AKIA[A-Z0-9]{16})(?![A-Za-z0-9_-])")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_=-]{4,}){0,2}")),
    ("credential_assignment", re.compile(r"(?im)(\b[A-Za-z0-9_.-]*(?:api[_.-]?key|token|secret|password|passwd|credential)[A-Za-z0-9_.-]*\s*[:=]\s*)([^\s,&]+)")),
    ("database_url_password", re.compile(r"((?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp)://[^:\s]+:)([^@\s]+)(@)", re.I)),
    ("url_credential", re.compile(r"((?:[A-Za-z][A-Za-z0-9+.-]*:)?//)([^/\s?#@]+)@")),
)


class ExtractionFailure(ValueError):
    def __init__(self, error_class: str, *, quarantine: bool = True) -> None:
        super().__init__(error_class)
        self.error_class = error_class
        self.quarantine = quarantine


@dataclass(frozen=True, slots=True)
class ExtractionSettings:
    enabled: bool
    max_jobs_per_run: int
    lease_seconds: float
    retry_delay_seconds: float
    max_message_bytes: int
    max_part_bytes: int
    max_total_decoded_bytes: int
    max_attachment_reconciliation_bytes: int
    max_total_attachment_reconciliation_bytes: int
    max_mime_parts: int
    max_mime_depth: int
    max_header_chars: int
    max_segment_chars: int
    max_output_chars: int
    document_limits: DocumentExtractionLimits

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "ExtractionSettings":
        from .gmail_poller import GmailSettings

        ck = config.get("client_knowledge", {})
        raw = ck.get("extraction", {}) if isinstance(ck, Mapping) else {}
        if not isinstance(raw, Mapping):
            raise ExtractionFailure("static_extraction_config_invalid")
        try:
            gmail_mime = GmailSettings.from_config(config).mime
            limits = DocumentExtractionLimits(
                max_input_bytes=int(raw.get("max_document_bytes", 32 * 1024 * 1024)),
                max_zip_members=int(raw.get("max_zip_members", 1024)),
                max_member_bytes=int(raw.get("max_zip_member_bytes", 8 * 1024 * 1024)),
                max_expanded_bytes=int(raw.get("max_zip_expanded_bytes", 32 * 1024 * 1024)),
                max_compression_ratio=int(raw.get("max_zip_compression_ratio", 100)),
                max_output_chars=int(raw.get("max_document_output_chars", 300_000)),
                max_notebook_cells=int(raw.get("max_notebook_cells", 2000)),
                max_notebook_source_items=int(raw.get("max_notebook_source_items", 5000)),
                max_sheets=int(raw.get("max_xlsx_sheets", 64)),
                max_rows_per_sheet=int(raw.get("max_xlsx_rows_per_sheet", 5000)),
                max_cols=int(raw.get("max_xlsx_cols", 256)),
                max_shared_strings=int(raw.get("max_xlsx_shared_strings", 100_000)),
            )
            values = cls(
                enabled=bool(raw.get("enabled", False)),
                max_jobs_per_run=max(1, min(100, int(raw.get("max_jobs_per_run", 10)))),
                lease_seconds=max(5.0, float(raw.get("lease_seconds", DEFAULT_LEASE_SECONDS))),
                retry_delay_seconds=max(0.0, float(raw.get("retry_delay_seconds", 60))),
                max_message_bytes=int(raw.get("max_message_bytes", 16 * 1024 * 1024)),
                max_part_bytes=int(raw.get("max_part_bytes", 4 * 1024 * 1024)),
                max_total_decoded_bytes=int(raw.get("max_total_decoded_bytes", 16 * 1024 * 1024)),
                max_attachment_reconciliation_bytes=int(gmail_mime["max_attachment_bytes"]),
                max_total_attachment_reconciliation_bytes=int(
                    gmail_mime["max_total_attachment_bytes"]
                ),
                max_mime_parts=int(raw.get("max_mime_parts", 512)),
                max_mime_depth=int(raw.get("max_mime_depth", 20)),
                max_header_chars=int(raw.get("max_header_chars", 32_000)),
                max_segment_chars=int(raw.get("max_segment_chars", 300_000)),
                max_output_chars=int(raw.get("max_output_chars", 600_000)),
                document_limits=limits,
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ExtractionFailure("static_extraction_config_invalid") from exc
        integer_values = (
            values.max_message_bytes,
            values.max_part_bytes,
            values.max_total_decoded_bytes,
            values.max_attachment_reconciliation_bytes,
            values.max_total_attachment_reconciliation_bytes,
            values.max_mime_parts,
            values.max_mime_depth,
            values.max_header_chars,
            values.max_segment_chars,
            values.max_output_chars,
        )
        if any(value <= 0 for value in integer_values):
            raise ExtractionFailure("static_extraction_config_invalid")
        return values


def _redact(value: str) -> tuple[str, dict[str, int]]:
    counts: Counter[str] = Counter()
    text = value
    for category, pattern in _SECRET_PATTERNS:
        def replacement(match: re.Match[str]) -> str:
            counts[category] += 1
            if category in {"authorization_header", "api_key_header", "credential_assignment"}:
                return match.group(1) + f"[REDACTED:{category}]"
            if category == "database_url_password":
                return match.group(1) + f"[REDACTED:{category}]" + match.group(3)
            if category == "url_credential":
                return match.group(1) + f"[REDACTED:{category}]@"
            return f"[REDACTED:{category}]"
        text = pattern.sub(replacement, text)
    before_generic = text
    text = redact_sensitive_text(
        text, force=True, file_read=True, redact_url_credentials=True
    ) or ""
    if text != before_generic:
        new_sentinels = max(
            0,
            text.count("«redacted:") - before_generic.count("«redacted:"),
        )
        counts["other_secret"] += max(1, new_sentinels)
    return text, dict(sorted(counts.items()))


class _VisibleHTML(HTMLParser):
    _HIDDEN = {"head", "script", "style", "template", "svg", "form", "object", "embed", "iframe"}
    _BLOCK = {"p", "div", "section", "article", "header", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "table", "blockquote", "pre"}

    def __init__(self, limit: int) -> None:
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.parts: list[str] = []
        self.size = 0
        self.hidden = 0

    def _add(self, value: str) -> None:
        if self.size + len(value) > self.limit:
            raise ExtractionFailure("html_output_limit")
        self.parts.append(value)
        self.size += len(value)

    def handle_starttag(self, tag: str, _attrs) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN:
            self.hidden += 1
        elif not self.hidden and tag in self._BLOCK | {"br", "td", "th"}:
            self._add("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._HIDDEN and self.hidden:
            self.hidden -= 1
        elif not self.hidden and tag in self._BLOCK | {"td", "th"}:
            self._add("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self._add(data)

    def text(self) -> str:
        value = "".join(self.parts).replace("\r\n", "\n").replace("\r", "\n")
        value = re.sub(r"[ \t]+", " ", value)
        value = re.sub(r" *\n *", "\n", value)
        return re.sub(r"\n{3,}", "\n\n", value).strip()


def _decode_header(value: str, limit: int) -> str:
    out: list[str] = []
    size = 0
    for part, charset in decode_header(value):
        text = part.decode(charset or "utf-8", errors="replace") if isinstance(part, bytes) else part
        if size + len(text) > limit:
            raise ExtractionFailure("header_character_limit")
        out.append(text)
        size += len(text)
    return " ".join(out)


def _read_limited(handle: BinaryIO, expected_size: int, limit: int) -> bytes:
    if expected_size > limit:
        raise ExtractionFailure("source_byte_limit")
    data = bytearray()
    while len(data) <= limit:
        chunk = handle.read(min(64 * 1024, limit + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
    if len(data) > limit:
        raise ExtractionFailure("source_byte_limit")
    if len(data) != expected_size:
        raise ExtractionFailure("source_size_mismatch")
    return bytes(data)


def _parse_message(raw: bytes, settings: ExtractionSettings) -> Message:
    count = 0
    def factory(*args, **kwargs):
        nonlocal count
        from email.message import EmailMessage
        count += 1
        if count > settings.max_mime_parts:
            raise ExtractionFailure("mime_part_limit")
        return EmailMessage(*args, **kwargs)
    parser = BytesFeedParser(policy=policy.default.clone(message_factory=factory))
    for offset in range(0, len(raw), 64 * 1024):
        parser.feed(raw[offset:offset + 64 * 1024])
    return parser.close()


def _depth(part: Message, current: int, limit: int) -> None:
    if current > limit:
        raise ExtractionFailure("mime_depth_limit")
    if part.is_multipart():
        for child in part.iter_parts():
            _depth(child, current + 1, limit)


def _part_text(part: Message, settings: ExtractionSettings, total: list[int]) -> tuple[str, str]:
    try:
        payload = part.get_payload(decode=True) or b""
    except Exception as exc:
        raise ExtractionFailure("mime_decode_invalid") from exc
    if len(payload) > settings.max_part_bytes:
        raise ExtractionFailure("mime_part_bytes_limit")
    total[0] += len(payload)
    if total[0] > settings.max_total_decoded_bytes:
        raise ExtractionFailure("mime_total_decoded_bytes_limit")
    charset = part.get_content_charset() or "utf-8"
    try:
        text = payload.decode(charset, errors="replace")
    except LookupError:
        text = payload.decode("utf-8", errors="replace")
    if len(text) > settings.max_segment_chars:
        raise ExtractionFailure("segment_character_limit")
    if part.get_content_type() == "text/html":
        parser = _VisibleHTML(settings.max_segment_chars)
        parser.feed(text)
        parser.close()
        return parser.text(), "body_html"
    return text.replace("\r\n", "\n").replace("\r", "\n").strip(), "body_plain"


def _body_parts(part: Message, settings: ExtractionSettings, total: list[int]) -> list[tuple[str, str]]:
    if part.get_content_disposition() == "attachment" or part.get_filename():
        return []
    if part.get_content_type() == "message/rfc822":
        return []
    if not part.is_multipart():
        if part.get_content_type() in {"text/plain", "text/html"}:
            text, kind = _part_text(part, settings, total)
            return [(kind, text)] if text else []
        return []
    children = list(part.iter_parts())
    if part.get_content_subtype() == "alternative":
        plain: list[tuple[str, str]] = []
        html: list[tuple[str, str]] = []
        for child in children:
            found = _body_parts(child, settings, total)
            plain.extend(item for item in found if item[0] == "body_plain")
            html.extend(item for item in found if item[0] == "body_html")
        return plain or html[:1]
    result: list[tuple[str, str]] = []
    for child in children:
        result.extend(_body_parts(child, settings, total))
    return result


def _parent_attachment_parts(
    message: Message, settings: ExtractionSettings
) -> dict[str, dict[str, Any]]:
    parts: dict[str, dict[str, Any]] = {}
    stack: list[tuple[Message, int, str]] = [(message, 0, "1")]
    total_bytes = 0
    while stack:
        part, depth, part_path = stack.pop()
        if depth > settings.max_mime_depth:
            raise ExtractionFailure("mime_depth_limit")
        if part.is_multipart():
            children = list(part.iter_parts())
            for index, child in reversed(list(enumerate(children, 1))):
                stack.append((child, depth + 1, f"{part_path}.{index}"))
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename() or ""
        if disposition != "attachment" and not filename:
            continue
        identity = f"part:{part_path}"
        if identity in parts:
            raise ExtractionFailure("attachment_reconciliation_failed")
        try:
            payload = part.get_payload(decode=True) or b""
        except Exception as exc:
            raise ExtractionFailure("attachment_reconciliation_failed") from exc
        if len(payload) > settings.max_attachment_reconciliation_bytes:
            raise ExtractionFailure("mime_attachment_bytes_limit")
        total_bytes += len(payload)
        if total_bytes > settings.max_total_attachment_reconciliation_bytes:
            raise ExtractionFailure("mime_total_attachment_bytes_limit")
        parts[identity] = {
            "provider_attachment_id": identity,
            "filename": str(filename),
            "mime_type": part.get_content_type().lower(),
            "byte_size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    return parts


def _reconcile_attachments(
    message: Message,
    children: Sequence[IntakeArtifact],
    settings: ExtractionSettings,
) -> None:
    parent_parts = _parent_attachment_parts(message, settings)
    durable: dict[str, IntakeArtifact] = {}
    for child in children:
        identity = child.provider_attachment_id
        if not identity.startswith("part:") or identity in durable:
            raise ExtractionFailure("attachment_reconciliation_failed")
        durable[identity] = child
    if set(parent_parts) != set(durable):
        raise ExtractionFailure("attachment_reconciliation_failed")
    for identity, expected in parent_parts.items():
        child = durable[identity]
        if (
            child.content_sha256 != expected["sha256"]
            or child.byte_size != expected["byte_size"]
            or child.mime_type != expected["mime_type"]
            or child.original_filename != expected["filename"]
        ):
            raise ExtractionFailure("attachment_reconciliation_failed")


def _segment(segment_id: str, kind: str, label: str, value: str) -> tuple[dict[str, Any], dict[str, int]]:
    redacted, counts = _redact(value)
    return {
        "segment_id": segment_id,
        "kind": kind,
        "label": label,
        "text": redacted,
        "character_count": len(redacted),
    }, counts


def _attachment_kind(artifact: IntakeArtifact) -> str:
    suffix = Path(artifact.original_filename).suffix.lower()
    if suffix in _DOC_MIMES and artifact.mime_type == _DOC_MIMES[suffix]:
        return suffix[1:]
    if suffix == ".ipynb" and artifact.mime_type in {"application/json", "application/x-ipynb+json"}:
        return "ipynb"
    if artifact.mime_type == "text/html" and suffix in {".html", ".htm"}:
        return "html"
    if artifact.mime_type in _PLAIN_ATTACHMENT_MIMES and suffix not in {".zip", ".pdf", ".html", ".htm"}:
        return "text"
    if artifact.mime_type == "application/pdf" or suffix == ".pdf":
        return "unsupported_pdf_v1"
    return "unsupported_binary_v1"


class ExtractionWorker:
    def __init__(
        self,
        store: IntakeStore,
        spool: RawSpool,
        derived: DerivedStore,
        settings: ExtractionSettings,
    ) -> None:
        self.store = store
        self.spool = spool
        self.derived = derived
        self.settings = settings

    def process_claim(self, claim: JobClaim) -> str:
        if claim.stage != "extracted":
            raise ExtractionFailure("extraction_wrong_stage")
        parent, children = self.store.get_artifact_family_for_claim(claim)
        manifest = [{"artifact_id": item.artifact_id, "sha256": item.content_sha256, "size": item.byte_size} for item in (parent, *children)]
        source_manifest_sha256 = hashlib.sha256(canonical_json(manifest)).hexdigest()
        extraction_id = versioned_identity(
            "client-knowledge-extraction",
            parent.artifact_id,
            source_manifest_sha256,
            EXTRACTOR_VERSION,
            EXTRACTION_LIMITS_VERSION,
            REDACTION_VERSION,
        )
        existing = self.store.get_extraction(extraction_id)
        if existing:
            self.derived.read_json(
                "extractions", extraction_id, str(existing["output_sha256"]), int(existing["output_bytes"])
            )
            self.store.complete_extraction(claim, existing)
            return extraction_id
        segments: list[dict[str, Any]] = []
        counts: Counter[str] = Counter()
        unsupported: list[dict[str, Any]] = []
        with self.spool.read_verified(
            parent.spool_key,
            storage_id=self.spool.storage_id,
            expected_sha256=parent.content_sha256,
            expected_size=parent.byte_size,
        ) as handle:
            raw = _read_limited(handle, parent.byte_size, self.settings.max_message_bytes)
        message = _parse_message(raw, self.settings)
        _depth(message, 0, self.settings.max_mime_depth)
        _reconcile_attachments(message, children, self.settings)
        for index, name in enumerate(_SELECTED_HEADERS, 1):
            values = message.get_all(name, [])
            for repeat, value in enumerate(values, 1):
                text = _decode_header(str(value), self.settings.max_header_chars)
                item, redactions = _segment(
                    f"header-{index:02d}-{repeat:02d}", "header", name, text
                )
                segments.append(item)
                counts.update(redactions)
        decoded_total = [0]
        for index, (kind, text) in enumerate(_body_parts(message, self.settings, decoded_total), 1):
            item, redactions = _segment(f"body-{index:04d}", kind, "Email body", text)
            segments.append(item)
            counts.update(redactions)
        for index, child in enumerate(children, 1):
            kind = _attachment_kind(child)
            metadata = {
                "artifact_id": child.artifact_id,
                "provider_attachment_id": child.provider_attachment_id,
                "mime_type": child.mime_type,
                "filename": child.original_filename,
                "byte_size": child.byte_size,
                "sha256": child.content_sha256,
                "support": kind,
            }
            item, redactions = _segment(
                f"attachment-{index:04d}-metadata",
                "attachment_metadata",
                "Attachment metadata",
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            )
            segments.append(item)
            counts.update(redactions)
            if kind.startswith("unsupported_"):
                unsupported.append({"artifact_id": child.artifact_id, "reason_code": kind})
                continue
            with self.spool.read_verified(
                child.spool_key,
                storage_id=self.spool.storage_id,
                expected_sha256=child.content_sha256,
                expected_size=child.byte_size,
            ) as handle:
                if kind in {"text", "html"}:
                    data = _read_limited(
                        handle, child.byte_size, self.settings.document_limits.max_input_bytes
                    )
                elif child.byte_size > self.settings.document_limits.max_input_bytes:
                    raise ExtractionFailure("source_byte_limit")
            if kind in {"text", "html"}:
                decoded = data.decode("utf-8", errors="replace")
                if kind == "html":
                    parser = _VisibleHTML(self.settings.document_limits.max_output_chars)
                    parser.feed(decoded)
                    parser.close()
                    text = parser.text()
                else:
                    text = decoded
                if len(text) > self.settings.document_limits.max_output_chars:
                    raise ExtractionFailure("document_output_limit")
            else:
                try:
                    text = extract_document_text(
                        str(self.spool.path_for_key(child.spool_key)),
                        self.settings.document_limits,
                        extension=Path(child.original_filename).suffix.lower(),
                    )
                except ExtractionError as exc:
                    raise ExtractionFailure("structured_document_invalid") from exc
            item, redactions = _segment(
                f"attachment-{index:04d}-text", kind, f"Attachment {index}", text
            )
            segments.append(item)
            counts.update(redactions)
        total_chars = sum(int(item["character_count"]) for item in segments)
        if total_chars > self.settings.max_output_chars:
            raise ExtractionFailure("extraction_output_limit")
        status = "extracted" if any(item["text"].strip() for item in segments) else "empty"
        value = {
            "object_version": EXTRACTOR_VERSION,
            "extraction_id": extraction_id,
            "artifact_id": parent.artifact_id,
            "project_key": parent.project_key,
            "source_sha256": parent.content_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "limits_version": EXTRACTION_LIMITS_VERSION,
            "redaction_version": REDACTION_VERSION,
            "status": status,
            "redaction_counts": dict(sorted(counts.items())),
            "segments": segments,
            "unsupported_attachments": unsupported,
        }
        record = self.derived.put_json("extractions", extraction_id, value)
        row = {
            "extraction_id": extraction_id,
            "artifact_id": parent.artifact_id,
            "source_sha256": parent.content_sha256,
            "source_manifest_sha256": source_manifest_sha256,
            "extractor_version": EXTRACTOR_VERSION,
            "limits_version": EXTRACTION_LIMITS_VERSION,
            "redaction_version": REDACTION_VERSION,
            "status": status,
            "derived_storage_id": record.storage_id,
            "derived_object_key": record.object_key,
            "output_sha256": record.sha256,
            "output_bytes": record.byte_size,
            "output_characters": total_chars,
            "redaction_counts_json": json.dumps(dict(sorted(counts.items())), sort_keys=True),
        }
        self.store.complete_extraction(claim, row)
        return extraction_id


def run_extraction_once(
    *,
    store: IntakeStore,
    spool: RawSpool,
    derived: DerivedStore,
    config: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    settings = ExtractionSettings.from_config(dict(config or load_config() or {}))
    result = {"processed": 0, "succeeded": 0, "failed": 0, "quarantined": 0, "operator_blocked": 0}
    if not settings.enabled:
        return result
    worker = ExtractionWorker(store, spool, derived, settings)
    for _ in range(settings.max_jobs_per_run):
        claim = store.claim_next(stage="extracted", spool=spool, lease_seconds=settings.lease_seconds)
        if claim is None:
            break
        result["processed"] += 1
        try:
            worker.process_claim(claim)
            result["succeeded"] += 1
        except ExtractionFailure as exc:
            if store.fail_stage(
                claim.job_id,
                claim.claim_token,
                error_class=exc.error_class,
                retry_delay=settings.retry_delay_seconds,
                quarantine=exc.quarantine,
            ):
                result["quarantined" if exc.quarantine else "failed"] += 1
        except Exception:
            if store.fail_stage(
                claim.job_id,
                claim.claim_token,
                error_class="extraction_internal_error",
                quarantine=True,
            ):
                result["quarantined"] += 1
    return result


__all__ = [
    "EXTRACTION_LIMITS_VERSION",
    "EXTRACTOR_VERSION",
    "ExtractionFailure",
    "ExtractionSettings",
    "ExtractionWorker",
    "REDACTION_VERSION",
    "run_extraction_once",
]
