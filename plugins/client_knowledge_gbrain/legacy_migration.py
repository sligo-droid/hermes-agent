"""Narrow persisted-only migration for one qualifying legacy review."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .derived import DerivedStore, canonical_json, versioned_identity
from .store import IntakeStore
from .synthesis import PROMPT_VERSION, SCHEMA_VERSION, SYNTHESIS_VERSION, item_identity


class LegacyMigrationFailure(ValueError):
    pass


def _finding_maps(interpretation: Mapping[str, Any]) -> tuple[dict[str, Mapping[str, Any]], dict[str, Mapping[str, Any]]]:
    evidence = {
        str(item.get("id") or ""): item
        for item in interpretation.get("evidence", [])
        if isinstance(item, Mapping) and item.get("id")
    }
    findings: dict[str, Mapping[str, Any]] = {}
    for category in (
        "candidate_learnings", "decisions", "requirements", "preferences", "risks",
        "stakeholders", "deadlines", "open_questions", "suggested_actions",
    ):
        for item in interpretation.get(category, []):
            if isinstance(item, Mapping) and item.get("id"):
                identifier = str(item["id"])
                if identifier in findings:
                    raise LegacyMigrationFailure("legacy interpretation has duplicate findings")
                findings[identifier] = item
    return findings, evidence


def _resolve_evidence(
    operation: Mapping[str, Any],
    *,
    finding: Mapping[str, Any],
    evidence: Mapping[str, Mapping[str, Any]],
    extraction: Mapping[str, Any],
) -> list[dict[str, Any]]:
    evidence_ids = [str(value) for value in operation.get("evidence_ids", [])]
    if evidence_ids != [str(value) for value in finding.get("evidence_ids", [])]:
        raise LegacyMigrationFailure("legacy operation evidence does not match interpretation")
    segments = {
        str(item.get("segment_id")): str(item.get("text") or "")
        for item in extraction.get("segments", [])
        if isinstance(item, Mapping) and item.get("segment_id")
    }
    resolved = []
    for evidence_id in evidence_ids:
        item = evidence.get(evidence_id)
        if item is None:
            raise LegacyMigrationFailure("legacy operation evidence is missing")
        segment_id = str(item.get("segment_id") or "")
        segment = segments.get(segment_id)
        start, end, quote = int(item.get("start") or 0), int(item.get("end") or 0), str(item.get("quote") or "")
        if segment is None or not 0 <= start < end <= len(segment) or segment[start:end] != quote:
            raise LegacyMigrationFailure("legacy evidence no longer resolves against extraction")
        resolved.append({
            "segment_id": segment_id,
            "start": start,
            "end": end,
            "quote": quote,
        })
    if not resolved:
        raise LegacyMigrationFailure("legacy operation has no evidence")
    return resolved


def migrate_legacy_review(
    review_id: str,
    *,
    store: IntakeStore,
    derived: DerivedStore,
) -> str:
    try:
        review = store.preflight_legacy_review_migration(review_id)
    except ValueError as exc:
        raise LegacyMigrationFailure("legacy review preflight failed") from exc
    assimilation = store.get_assimilation(str(review["assimilation_id"]))
    if assimilation is None:
        raise LegacyMigrationFailure("legacy assimilation is missing")
    assimilation_value = derived.read_json(
        "assimilations",
        assimilation["assimilation_id"],
        assimilation["output_sha256"],
        assimilation["output_bytes"],
    )
    proposal = assimilation_value.get("proposal") if isinstance(assimilation_value, Mapping) else None
    interpretation_row = store.get_interpretation(str(assimilation["interpretation_id"]))
    if interpretation_row is None:
        raise LegacyMigrationFailure("legacy interpretation is missing")
    interpretation_value = derived.read_json(
        "interpretations",
        interpretation_row["interpretation_id"],
        interpretation_row["output_sha256"],
        interpretation_row["output_bytes"],
    )
    interpretation = interpretation_value.get("interpretation") if isinstance(interpretation_value, Mapping) else None
    extraction_row = store.get_extraction(str(interpretation_row["extraction_id"]))
    if extraction_row is None:
        raise LegacyMigrationFailure("legacy extraction is missing")
    extraction = derived.read_json(
        "extractions",
        extraction_row["extraction_id"],
        extraction_row["output_sha256"],
        extraction_row["output_bytes"],
    )
    if not all(isinstance(value, Mapping) for value in (proposal, interpretation, extraction)):
        raise LegacyMigrationFailure("legacy derived provenance is invalid")
    if hashlib.sha256(canonical_json(proposal)).hexdigest() != str(review["proposal_sha256"]):
        raise LegacyMigrationFailure("legacy proposal hash mismatch")
    operations = proposal.get("operations")
    if not isinstance(operations, list):
        raise LegacyMigrationFailure("legacy proposal operations are invalid")
    selected = [
        operation for operation in operations
        if isinstance(operation, Mapping) and operation.get("operation") != "ignore_transient"
    ]
    if not 1 <= len(selected) <= 3:
        raise LegacyMigrationFailure("legacy review must have one to three publication operations")
    findings, evidence = _finding_maps(interpretation)
    learnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for operation in selected:
        finding_id = str(operation.get("finding_id") or "")
        finding = findings.get(finding_id)
        if finding is None or finding_id in seen:
            raise LegacyMigrationFailure("legacy operation finding is invalid")
        seen.add(finding_id)
        claim = " ".join(str(operation.get("claim") or "").split())
        if not claim:
            raise LegacyMigrationFailure("legacy operation claim is empty")
        learnings.append({
            "statement": claim,
            "evidence": _resolve_evidence(
                operation, finding=finding, evidence=evidence, extraction=extraction
            ),
        })
    synthesis_id = versioned_identity(
        "client-knowledge-legacy-synthesis",
        review_id,
        assimilation["assimilation_id"],
        extraction_row["extraction_id"],
        SYNTHESIS_VERSION,
    )
    value = {
        "object_version": SYNTHESIS_VERSION,
        "synthesis_id": synthesis_id,
        "extraction_id": extraction_row["extraction_id"],
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "source_legacy_review_id": review_id,
        "synthesis": {"learnings": learnings},
        "attribution": {
            "actual_provider": assimilation["actual_provider"],
            "actual_model": assimilation["actual_model"],
            "selected_provider": assimilation["selected_provider"],
            "selected_model": assimilation["selected_model"],
            "model_tier": assimilation["model_tier"],
            "route_fingerprint": assimilation["route_fingerprint"],
        },
        "usage": {
            "input_tokens": interpretation_row["input_tokens"],
            "output_tokens": interpretation_row["output_tokens"],
            "total_tokens": interpretation_row["total_tokens"],
            "cache_read_tokens": interpretation_row["cache_read_tokens"],
            "cache_write_tokens": interpretation_row["cache_write_tokens"],
        },
    }
    synthesis = {
        "synthesis_id": synthesis_id,
        "artifact_id": review["artifact_id"],
        "extraction_id": extraction_row["extraction_id"],
        "project_key": review["project_key"],
        "notion_ref": str(
            (store.get_completed_stage_receipt(review["artifact_id"], "notion_archived") or {}).get("receipt_id") or ""
        ),
        "synthesis_version": f"{SYNTHESIS_VERSION}+legacy-migration",
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "actual_provider": assimilation["actual_provider"],
        "actual_model": assimilation["actual_model"],
        "selected_provider": assimilation["selected_provider"],
        "selected_model": assimilation["selected_model"],
        "model_tier": assimilation["model_tier"],
        "route_fingerprint": assimilation["route_fingerprint"],
        "input_tokens": interpretation_row["input_tokens"],
        "output_tokens": interpretation_row["output_tokens"],
        "total_tokens": interpretation_row["total_tokens"],
        "cache_read_tokens": interpretation_row["cache_read_tokens"],
        "cache_write_tokens": interpretation_row["cache_write_tokens"],
        "base_git_head": "publication-time",
        "source_legacy_review_id": review_id,
        "state": "review_pending",
    }
    if not synthesis["notion_ref"].startswith("notion:page:"):
        raise LegacyMigrationFailure("legacy Notion provenance is missing")
    items = []
    for position, learning in enumerate(learnings, start=1):
        item_id, digest = item_identity(
            synthesis_id,
            position=position,
            revision_number=0,
            statement=learning["statement"],
            evidence=learning["evidence"],
        )
        items.append({
            "item_id": item_id,
            "position": position,
            "statement": learning["statement"],
            "evidence_json": canonical_json(learning["evidence"]).decode("utf-8"),
            "item_sha256": digest,
        })
    from .review import ReviewFailure, validate_item_review_deliverability

    try:
        for item in items:
            validate_item_review_deliverability({**item, "revision_number": 0})
    except ReviewFailure as exc:
        raise LegacyMigrationFailure(
            "legacy review payload is not deliverable to Discord"
        ) from exc

    def persist_derived() -> dict[str, Any]:
        record = derived.put_json("syntheses", synthesis_id, value)
        return {
            "derived_storage_id": record.storage_id,
            "derived_object_key": record.object_key,
            "output_sha256": record.sha256,
            "output_bytes": record.byte_size,
        }

    return store.migrate_legacy_review_to_synthesis(
        legacy_review_id=review_id,
        synthesis=synthesis,
        items=items,
        persist_derived=persist_derived,
    )


__all__ = ["LegacyMigrationFailure", "migrate_legacy_review"]
