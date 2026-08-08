"""Persisted-only replacement of one fully undecided synthesis."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

from agent.plugin_llm import PluginLlm

from .derived import DerivedStore, canonical_json, versioned_identity
from .extraction import EXTRACTOR_VERSION, EXTRACTION_LIMITS_VERSION, REDACTION_VERSION
from .review import ProjectReviewConfig, ReviewFailure, _render_notification
from .store import IntakeStore
from .synthesis import (
    PROMPT_VERSION,
    SCHEMA_VERSION,
    SYNTHESIS_VERSION,
    SynthesisFailure,
    SynthesisSettings,
    item_identity,
    run_synthesis_model,
    validate_synthesis,
)


class SynthesisRegenerationFailure(ValueError):
    pass


def regenerate_undecided_synthesis(
    synthesis_id: str,
    *,
    store: IntakeStore,
    derived: DerivedStore,
    llm: PluginLlm,
    config: Mapping[str, Any],
) -> str:
    """Replace one confirmed, fully undecided review without replaying sources."""
    try:
        preflight = store.preflight_synthesis_regeneration(synthesis_id)
    except ValueError as exc:
        raise SynthesisRegenerationFailure("synthesis regeneration preflight failed") from exc
    if preflight["mode"] == "existing":
        return str(preflight["replacement_synthesis_id"])
    source = preflight["synthesis"]
    extraction_row = preflight["extraction"]
    extraction = derived.read_json(
        "extractions",
        extraction_row["extraction_id"],
        extraction_row["output_sha256"],
        extraction_row["output_bytes"],
    )
    artifact = store.get_artifact(str(source["artifact_id"]))
    if (
        artifact is None
        or not isinstance(extraction, Mapping)
        or extraction.get("artifact_id") != artifact.artifact_id
        or extraction.get("source_sha256") != artifact.content_sha256
        or extraction.get("object_version") != EXTRACTOR_VERSION
        or extraction.get("limits_version") != EXTRACTION_LIMITS_VERSION
        or extraction.get("redaction_version") != REDACTION_VERSION
        or str(source["notion_ref"] or "") != str(
            (store.get_completed_stage_receipt(artifact.artifact_id, "notion_archived") or {}).get(
                "receipt_id"
            )
            or ""
        )
    ):
        raise SynthesisRegenerationFailure("persisted synthesis provenance is invalid")
    settings = SynthesisSettings.from_config(config)
    replacement_id = versioned_identity(
        "client-knowledge-synthesis-regeneration",
        synthesis_id,
        str(extraction_row["extraction_id"]),
        SYNTHESIS_VERSION,
        SCHEMA_VERSION,
        PROMPT_VERSION,
    )
    try:
        orphan = derived.read_json("syntheses", replacement_id)
    except FileNotFoundError:
        orphan = None
    if orphan is None:
        synthesis, attribution, usage = run_synthesis_model(
            llm=llm,
            project_key=artifact.project_key,
            extraction=extraction,
            settings=settings,
        )
        value = {
            "object_version": SYNTHESIS_VERSION,
            "synthesis_id": replacement_id,
            "extraction_id": extraction_row["extraction_id"],
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "parent_synthesis_id": synthesis_id,
            "synthesis": synthesis,
            "attribution": attribution,
            "usage": usage,
        }
    else:
        if (
            not isinstance(orphan, Mapping)
            or orphan.get("object_version") != SYNTHESIS_VERSION
            or orphan.get("synthesis_id") != replacement_id
            or orphan.get("extraction_id") != extraction_row["extraction_id"]
            or orphan.get("parent_synthesis_id") != synthesis_id
        ):
            raise SynthesisRegenerationFailure("replacement synthesis orphan is invalid")
        synthesis = validate_synthesis(
            orphan.get("synthesis"), extraction, max_output_bytes=settings.max_output_bytes
        )
        attribution = orphan.get("attribution")
        usage = orphan.get("usage")
        if not isinstance(attribution, Mapping) or not isinstance(usage, Mapping):
            raise SynthesisRegenerationFailure("replacement synthesis orphan is invalid")
        value = dict(orphan)
    if not all(
        str(attribution.get(key) or "")
        for key in ("actual_provider", "actual_model", "model_tier", "route_fingerprint")
    ):
        raise SynthesisRegenerationFailure("replacement synthesis attribution is missing")
    items = []
    for position, learning in enumerate(synthesis["learnings"], start=1):
        item_id, digest = item_identity(
            replacement_id,
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
    synthesis_row = {
        "synthesis_id": replacement_id,
        "artifact_id": artifact.artifact_id,
        "extraction_id": extraction_row["extraction_id"],
        "project_key": artifact.project_key,
        "notion_ref": source["notion_ref"],
        "synthesis_version": f"{SYNTHESIS_VERSION}+regeneration:{synthesis_id[:16]}",
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        **{key: str(attribution.get(key) or "") for key in (
            "actual_provider", "actual_model", "selected_provider", "selected_model",
            "model_tier", "route_fingerprint",
        )},
        **{key: int(usage.get(key) or 0) for key in (
            "input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
            "cache_write_tokens",
        )},
        "base_git_head": "publication-time",
        "state": "review_pending",
        "parent_synthesis_id": synthesis_id,
    }
    prospective_bytes = canonical_json(value)
    synthesis_row.update({
        "output_sha256": hashlib.sha256(prospective_bytes).hexdigest(),
        "output_bytes": len(prospective_bytes),
    })
    try:
        project = ProjectReviewConfig.from_config(config, artifact.project_key)
        _render_notification(synthesis_row, items, extraction, project)
    except ReviewFailure as exc:
        raise SynthesisRegenerationFailure(
            "replacement synthesis is not deliverable to Discord"
        ) from exc

    def persist_derived() -> dict[str, Any]:
        record = derived.put_json("syntheses", replacement_id, value)
        return {
            "derived_storage_id": record.storage_id,
            "derived_object_key": record.object_key,
            "output_sha256": record.sha256,
            "output_bytes": record.byte_size,
        }

    try:
        return store.replace_undecided_synthesis(
            source_synthesis_id=synthesis_id,
            synthesis=synthesis_row,
            items=items,
            persist_derived=persist_derived,
        )
    except ValueError as exc:
        raise SynthesisRegenerationFailure("synthesis replacement transaction failed") from exc


__all__ = ["SynthesisRegenerationFailure", "regenerate_undecided_synthesis"]
