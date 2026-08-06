"""Present-forward Gmail polling and raw source admission."""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from hermes_cli.config import load_config

from .gmail_api import (
    GmailClient,
    GmailHistoryExpired,
    GmailInvalidResponse,
    GmailMessageGone,
)
from .gmail_auth import GmailOAuth, resolve_token_path
from .gmail_mime import AliasResolution, cleanup_attachments, parse_attachments, resolve_alias
from .gmail_state import GmailState
from .models import IntakeArtifact, UNMAPPED_PROJECT_KEY
from .scope import validate_project_key
from .spool import RawSpool
from .store import IntakeStore


@dataclass(frozen=True, slots=True)
class GmailSettings:
    enabled: bool
    mailbox: str
    token_path: Path
    aliases: tuple[tuple[str, str], ...]
    request_timeout: float
    max_response_bytes: int
    reconcile_lookback_days: int
    max_reconcile_messages: int
    max_message_bytes: int
    max_invalid_attempts: int
    server_clock_skew_seconds: int
    mime: dict[str, int | float]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "GmailSettings":
        client_knowledge = config.get("client_knowledge")
        raw = client_knowledge.get("gmail") if isinstance(client_knowledge, Mapping) else None
        raw = raw if isinstance(raw, Mapping) else {}
        historical = str(raw.get("historical_processing") or "disabled").strip().lower()
        if historical != "disabled":
            raise ValueError("Gmail historical processing must remain disabled")
        mailbox = str(raw.get("mailbox") or "").strip().lower()
        if bool(raw.get("enabled", False)) and (not mailbox or "@" not in mailbox):
            raise ValueError("Gmail mailbox is required when polling is enabled")
        aliases = []
        projects = config.get("projects")
        if isinstance(projects, Mapping):
            seen: dict[str, str] = {}
            for project_key, project in projects.items():
                if not isinstance(project, Mapping):
                    continue
                project_name = validate_project_key(project_key)
                configured = project.get("aliases", [])
                if not isinstance(configured, list):
                    raise ValueError("project aliases must be a list")
                for value in configured:
                    alias = str(value or "").strip().lower()
                    if not alias or "@" not in alias or len(alias) > 320:
                        raise ValueError("project alias is invalid")
                    previous = seen.get(alias)
                    if previous and previous != project_name:
                        raise ValueError("Gmail alias belongs to multiple projects")
                    seen[alias] = project_name
            aliases = sorted(seen.items())
        if bool(raw.get("enabled", False)) and not aliases:
            raise ValueError("Gmail polling requires at least one project alias")

        def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
            value = int(raw.get(name, default))
            if not minimum <= value <= maximum:
                raise ValueError(f"Gmail {name} is outside its allowed bound")
            return value

        mime = {
            "max_header_bytes": bounded("max_header_bytes", 256 * 1024, 1024, 4 * 1024 * 1024),
            "max_header_count": bounded("max_header_count", 2000, 1, 10_000),
            "max_header_line_bytes": bounded("max_header_line_bytes", 64 * 1024, 128, 1024 * 1024),
            "max_mime_parts": bounded("max_mime_parts", 512, 1, 5000),
            "max_mime_depth": bounded("max_mime_depth", 20, 1, 100),
            "max_attachments": bounded("max_attachments", 100, 0, 1000),
            "max_attachment_bytes": bounded("max_attachment_bytes", 32 * 1024 * 1024, 1, 512 * 1024 * 1024),
            "max_total_attachment_bytes": bounded("max_total_attachment_bytes", 64 * 1024 * 1024, 1, 1024 * 1024 * 1024),
            "timeout_seconds": bounded("mime_parse_timeout_seconds", 30, 1, 300),
            "memory_bytes": bounded("mime_parser_memory_bytes", 256 * 1024 * 1024, 64 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
            "cpu_seconds": bounded("mime_parser_cpu_seconds", 20, 1, 300),
        }
        max_message_bytes = bounded("max_message_bytes", 64 * 1024 * 1024, 1, 256 * 1024 * 1024)
        max_response_bytes = bounded("max_http_response_bytes", 96 * 1024 * 1024, 1024, 512 * 1024 * 1024)
        if max_response_bytes < math.ceil(max_message_bytes * 4 / 3) + 64 * 1024:
            raise ValueError("Gmail HTTP response bound cannot contain the configured raw message bound")
        return cls(
            enabled=bool(raw.get("enabled", False)),
            mailbox=mailbox,
            token_path=resolve_token_path(raw.get("token_path")),
            aliases=tuple(aliases),
            request_timeout=max(1.0, float(raw.get("request_timeout_seconds", 30))),
            max_response_bytes=max_response_bytes,
            reconcile_lookback_days=bounded("reconcile_lookback_days", 14, 1, 90),
            max_reconcile_messages=bounded("max_reconcile_messages", 500, 1, 5000),
            max_message_bytes=max_message_bytes,
            max_invalid_attempts=bounded("max_consistent_invalid_raw_attempts", 3, 2, 10),
            server_clock_skew_seconds=bounded("server_clock_skew_seconds", 300, 1, 3600),
            mime=mime,
        )

    @property
    def alias_map(self) -> dict[str, str]:
        return dict(self.aliases)

    @property
    def config_hash(self) -> str:
        value = {
            "mailbox": self.mailbox,
            "aliases": self.aliases,
            "lookback": self.reconcile_lookback_days,
            "reconcile_max": self.max_reconcile_messages,
            "message_max": self.max_message_bytes,
            "invalid_max": self.max_invalid_attempts,
            "mime": self.mime,
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class GmailPoller:
    def __init__(
        self,
        store: IntakeStore,
        spool: RawSpool,
        client: GmailClient,
        settings: GmailSettings,
        *,
        now=time.time,
    ) -> None:
        self.store = store
        self.spool = spool
        self.client = client
        self.settings = settings
        self.state = GmailState(store)
        self.now = now

    def run_once(self) -> dict[str, Any]:
        state = self.state.get_mailbox(self.settings.mailbox)
        if state is None:
            return self._initialize()
        profile = self.client.profile()
        if str(profile.payload.get("emailAddress") or "").strip().lower() != state.mailbox:
            raise ValueError("authenticated Gmail mailbox does not match initialized state")
        batch = self.state.get_or_create_batch(
            state.mailbox,
            state.cursor_history_id,
            config_hash=self.settings.config_hash,
            alias_count=len(self.settings.aliases),
        )
        batch_id = str(batch["batch_id"])
        self._discover_history(batch_id, state)
        self._discover_reconciliation(batch_id)
        processed = 0
        for item in self.state.pending_items(batch_id):
            self._process_item(item, state)
            processed += 1
        cursor = self.state.finalize(batch_id, spool=self.spool)
        return {"mode": str(self.state.get_batch(batch_id)["mode"]), "batch_id": batch_id, "processed": processed, "cursor": cursor}

    def _initialize(self) -> dict[str, Any]:
        local_now = self.now()
        first = self.client.profile()
        second = self.client.profile()
        first_mailbox = str(first.payload.get("emailAddress") or "").strip().lower()
        second_mailbox = str(second.payload.get("emailAddress") or "").strip().lower()
        first_history = str(first.payload.get("historyId") or "")
        second_history = str(second.payload.get("historyId") or "")
        if first_mailbox != self.settings.mailbox or second_mailbox != self.settings.mailbox:
            raise ValueError("authenticated Gmail mailbox does not match configuration")
        if not first_history.isdigit() or not second_history.isdigit() or int(second_history) < int(first_history):
            raise ValueError("Gmail profile history bracket is invalid")
        if second.server_ms < first.server_ms:
            raise ValueError("Gmail server time moved backward")
        if abs(first.server_ms / 1000 - local_now) > self.settings.server_clock_skew_seconds or abs(second.server_ms / 1000 - local_now) > self.settings.server_clock_skew_seconds:
            raise ValueError("Gmail server time is outside the allowed skew")
        state = self.state.initialize_mailbox(
            self.settings.mailbox,
            cutover_history_id=second_history,
            bracket_start_server_ms=first.server_ms,
            bracket_end_server_ms=second.server_ms,
            admit_after_server_ms=second.server_ms + 1000,
        )
        return {"mode": "initialized", "cursor": state.cursor_history_id, "processed": 0}

    def _discover_history(self, batch_id: str, mailbox_state) -> None:
        with self.store._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM gmail_history_completion WHERE batch_id=?", (batch_id,)
            ).fetchone():
                return
        next_page = self.state.next_history_page(batch_id)
        if next_page is None:
            self.state.complete_history(batch_id)
            return
        while next_page is not None:
            ordinal, page_token = next_page
            try:
                response = self.client.history(mailbox_state.cursor_history_id, page_token)
            except GmailHistoryExpired:
                if ordinal != 0:
                    raise
                profile = self.client.profile()
                if str(profile.payload.get("emailAddress") or "").strip().lower() != mailbox_state.mailbox:
                    raise ValueError("authenticated Gmail mailbox changed")
                self.state.complete_history_recovery(
                    batch_id, str(profile.payload.get("historyId") or "")
                )
                return
            history = response.payload.get("history", [])
            if not isinstance(history, list):
                raise GmailInvalidResponse("Gmail history list is invalid", fingerprint=response.fingerprint)
            candidates = []
            previous_id = -1
            for record in history:
                if not isinstance(record, Mapping) or not str(record.get("id") or "").isdigit():
                    raise GmailInvalidResponse("Gmail history record is invalid", fingerprint=response.fingerprint)
                addition_id = str(record["id"])
                if int(addition_id) < previous_id:
                    raise GmailInvalidResponse("Gmail history order is invalid", fingerprint=response.fingerprint)
                previous_id = int(addition_id)
                additions = record.get("messagesAdded", [])
                if not isinstance(additions, list):
                    raise GmailInvalidResponse("Gmail history additions are invalid", fingerprint=response.fingerprint)
                for addition in additions:
                    message = addition.get("message") if isinstance(addition, Mapping) else None
                    message_id = str(message.get("id") or "") if isinstance(message, Mapping) else ""
                    if not message_id:
                        raise GmailInvalidResponse("Gmail history message is invalid", fingerprint=response.fingerprint)
                    candidates.append((message_id, addition_id))
            next_token = str(response.payload.get("nextPageToken") or "")
            response_history_id = str(response.payload.get("historyId") or "")
            self.state.register_history_page(
                batch_id,
                page_ordinal=ordinal,
                request_token=page_token,
                next_token=next_token,
                response_history_id=response_history_id,
                candidates=candidates,
            )
            if not next_token:
                self.state.complete_history(batch_id)
                return
            next_page = (ordinal + 1, next_token)

    def _discover_reconciliation(self, batch_id: str) -> None:
        batch = self.state.get_batch(batch_id)
        if not batch.get("target_cursor"):
            raise ValueError("Gmail history discovery is incomplete")
        with self.store._connect() as conn:
            total = int(conn.execute(
                "SELECT COALESCE(SUM(observation_count), 0) FROM gmail_reconciliation_pages "
                "WHERE batch_id=?", (batch_id,)
            ).fetchone()[0])
        for alias_ordinal, (alias, _project) in enumerate(self.settings.aliases):
            next_page = self.state.next_reconciliation_page(batch_id, alias_ordinal)
            while next_page is not None:
                page_ordinal, page_token = next_page
                query = (
                    f"{{deliveredto:{alias} to:{alias} cc:{alias}}} "
                    f"newer_than:{self.settings.reconcile_lookback_days}d"
                )
                response = self.client.list_messages(query, page_token)
                messages = response.payload.get("messages", [])
                if not isinstance(messages, list):
                    raise GmailInvalidResponse("Gmail reconciliation list is invalid", fingerprint=response.fingerprint)
                ids = []
                for item in messages:
                    message_id = str(item.get("id") or "") if isinstance(item, Mapping) else ""
                    if not message_id:
                        raise GmailInvalidResponse("Gmail reconciliation message is invalid", fingerprint=response.fingerprint)
                    ids.append(message_id)
                total += len(ids)
                if total > self.settings.max_reconcile_messages:
                    raise ValueError("Gmail reconciliation exceeded its configured bound")
                next_token = str(response.payload.get("nextPageToken") or "")
                self.state.register_reconciliation_page(
                    batch_id,
                    alias_ordinal=alias_ordinal,
                    page_ordinal=page_ordinal,
                    request_token=page_token,
                    next_token=next_token,
                    message_ids=ids,
                )
                next_page = (page_ordinal + 1, next_token) if next_token else None
        self.state.complete_reconciliation(
            batch_id, max_observations=self.settings.max_reconcile_messages
        )

    def _process_item(self, item: Mapping[str, Any], mailbox_state) -> None:
        kind = str(item["item_kind"])
        item_id = str(item["candidate_id"] if kind == "candidate" else item["observation_id"])
        message_id = str(item["message_id"])
        if kind == "candidate" and int(item["addition_history_id"]) <= int(mailbox_state.cutover_history_id):
            self.state.terminal_disposition(kind, item_id, "rejected_pre_cutover")
            return
        if item.get("raw_spool_key"):
            receipt = self.spool.verify(
                str(item["raw_spool_key"]),
                storage_id=str(item.get("raw_storage_id") or ""),
                expected_sha256=str(item["raw_sha256"]),
                expected_size=int(item["raw_byte_size"]),
            )
            with self.spool.read(receipt.storage_key) as handle:
                raw = handle.read(self.settings.max_message_bytes + 1)
            metadata = {
                "internalDate": item.get("internal_date_ms"),
                "historyId": item.get("message_history_id"),
            }
        else:
            try:
                response = self.client.raw_message(message_id)
            except GmailMessageGone:
                self.state.terminal_disposition(kind, item_id, "gone", error_class="gmail_message_gone")
                return
            except GmailInvalidResponse as exc:
                if exc.fingerprint:
                    terminal = self.state.record_invalid_raw(
                        kind,
                        item_id,
                        fingerprint=exc.fingerprint,
                        error_class="gmail_invalid_response",
                        threshold=self.settings.max_invalid_attempts,
                    )
                    if terminal:
                        return
                raise
            metadata = response.payload
            if str(metadata.get("id") or "") != message_id:
                terminal = self.state.record_invalid_raw(
                    kind,
                    item_id,
                    fingerprint=response.fingerprint,
                    error_class="gmail_message_identity_mismatch",
                    threshold=self.settings.max_invalid_attempts,
                )
                if not terminal:
                    raise GmailInvalidResponse(
                        "Gmail message identity is invalid", fingerprint=response.fingerprint
                    )
                return
            try:
                raw = self._decode_raw(metadata.get("raw"), self.settings.max_message_bytes)
            except ValueError as exc:
                fingerprint = response.fingerprint
                terminal = self.state.record_invalid_raw(
                    kind,
                    item_id,
                    fingerprint=fingerprint,
                    error_class="gmail_invalid_provider_raw",
                    threshold=self.settings.max_invalid_attempts,
                )
                if not terminal:
                    raise GmailInvalidResponse(
                        "Gmail raw payload is invalid", fingerprint=fingerprint
                    ) from exc
                return
            except OverflowError:
                self.state.terminal_disposition(
                    kind, item_id, "rejected_oversize_actual", error_class="gmail_raw_bytes_limit"
                )
                return
            try:
                internal_date = int(metadata.get("internalDate"))
                message_history = int(metadata.get("historyId"))
            except (TypeError, ValueError) as exc:
                terminal = self.state.record_invalid_raw(
                    kind,
                    item_id,
                    fingerprint=response.fingerprint,
                    error_class="gmail_invalid_message_metadata",
                    threshold=self.settings.max_invalid_attempts,
                )
                if not terminal:
                    raise GmailInvalidResponse("Gmail message metadata is invalid", fingerprint=response.fingerprint) from exc
                return
            if kind == "observation" and message_history <= int(mailbox_state.cutover_history_id):
                self.state.terminal_disposition(kind, item_id, "rejected_pre_cutover")
                return
            if internal_date < mailbox_state.admit_after_server_ms:
                disposition = (
                    "rejected_cutover_precision"
                    if internal_date >= mailbox_state.bracket_end_server_ms
                    else "rejected_pre_cutover"
                )
                self.state.terminal_disposition(kind, item_id, disposition)
                return
            provider_artifact_id = self._message_identity(message_id)
            receipt = self.spool.put(
                provider_id="gmail", provider_artifact_id=provider_artifact_id, source=[raw]
            )
            self.state.record_raw(
                kind,
                item_id,
                spool_key=receipt.storage_key,
                storage_id=receipt.storage_id,
                sha256=receipt.sha256,
                byte_size=receipt.byte_size,
                message_history_id=str(message_history),
                internal_date_ms=internal_date,
            )
        routing_error = ""
        try:
            resolution = resolve_alias(
                raw,
                self.settings.alias_map,
                max_header_bytes=int(self.settings.mime["max_header_bytes"]),
            )
        except ValueError:
            resolution = AliasResolution("unmapped", "", "")
            routing_error = "gmail_routing_headers_invalid"
        project = resolution.project_key
        occurred = int(metadata["internalDate"]) / 1000
        provider_artifact_id = self._message_identity(message_id)
        parent = IntakeArtifact.from_bytes(
            project_key=project,
            provider_id="gmail",
            provider_artifact_id=provider_artifact_id,
            provider_message_id=message_id,
            occurred_at=occurred,
            delivered_alias=resolution.delivered_alias,
            original_filename="message.eml",
            mime_type="message/rfc822",
            source_url=f"https://mail.google.com/mail/u/0/#all/{message_id}",
            provenance_json={
                "mailbox": self.settings.mailbox,
                "routing_tier": resolution.tier,
                "routing_conflict": resolution.conflict,
                "routing_error": routing_error,
            },
            content=raw,
        )
        attachments, parse_error = parse_attachments(receipt.path, self.settings.mime)
        next_stage = "needs_mapping" if project == UNMAPPED_PROJECT_KEY else (
            "quarantined" if parse_error else "notion_archived"
        )
        self.store.admit_raw_artifact(self.spool, parent, [raw], defer_stages=True)
        if not parse_error:
            try:
                for attachment in attachments:
                    path = Path(str(attachment["path"]))
                    with path.open("rb") as handle:
                        content = handle.read(int(attachment["byte_size"]) + 1)
                    child = IntakeArtifact.from_bytes(
                        project_key=project,
                        provider_id="gmail",
                        provider_artifact_id=f"{provider_artifact_id}:attachment:part:{attachment['part_path']}",
                        source_type="attachment",
                        parent_artifact_id=parent.artifact_id,
                        provider_message_id=message_id,
                        provider_attachment_id=f"part:{attachment['part_path']}",
                        occurred_at=parent.occurred_at,
                        delivered_alias=parent.delivered_alias,
                        original_filename=str(attachment["filename"]),
                        mime_type=str(attachment["mime_type"]),
                        provenance_json={"mailbox": self.settings.mailbox, "mime_part": attachment["part_path"]},
                        content=content,
                    )
                    self.store.admit_raw_artifact(self.spool, child, [content])
            finally:
                cleanup_attachments(attachments)
        self.store.ensure_job(parent.artifact_id, next_stage)
        disposition = "needs_mapping" if project == UNMAPPED_PROJECT_KEY else (
            "quarantined" if parse_error else "admitted"
        )
        self.state.terminal_disposition(
            kind,
            item_id,
            disposition,
            error_class=routing_error or parse_error,
            artifact_id=parent.artifact_id,
        )

    def _message_identity(self, message_id: str) -> str:
        return f"{self.settings.mailbox}:message:{message_id}"

    @staticmethod
    def _decode_raw(value: Any, max_bytes: int) -> bytes:
        if not isinstance(value, str) or not value:
            raise ValueError("raw field is missing")
        # Decode in quanta so only actual decoded bytes determine the limit.
        output = io.BytesIO()
        carry = ""
        for index in range(0, len(value), 64 * 1024):
            carry += value[index:index + 64 * 1024]
            usable = len(carry) - (len(carry) % 4)
            if usable:
                chunk, carry = carry[:usable], carry[usable:]
                try:
                    decoded = base64.b64decode(chunk, altchars=b"-_", validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ValueError("invalid base64url") from exc
                output.write(decoded)
                if output.tell() > max_bytes:
                    raise OverflowError("raw bytes exceed configured bound")
        if carry:
            padded = carry + "=" * ((4 - len(carry) % 4) % 4)
            try:
                output.write(base64.b64decode(padded, altchars=b"-_", validate=True))
            except (binascii.Error, ValueError) as exc:
                raise ValueError("invalid base64url") from exc
        if output.tell() > max_bytes:
            raise OverflowError("raw bytes exceed configured bound")
        return output.getvalue()


def run_gmail_once(
    *,
    store: IntakeStore | None = None,
    spool: RawSpool | None = None,
    config: Mapping[str, Any] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    resolved = dict(config or load_config() or {})
    settings = GmailSettings.from_config(resolved)
    if not settings.enabled:
        return {"mode": "disabled", "processed": 0}
    oauth = GmailOAuth(
        settings.token_path, transport=transport, timeout=settings.request_timeout
    )
    token = oauth.access_token()
    with GmailClient(
        token.value,
        transport=transport,
        timeout=settings.request_timeout,
        max_response_bytes=settings.max_response_bytes,
    ) as client:
        return GmailPoller(
            store or IntakeStore(), spool or RawSpool(), client, settings
        ).run_once()


__all__ = ["GmailPoller", "GmailSettings", "run_gmail_once"]
