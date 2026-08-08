from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from agent.plugin_llm import PluginLlmStructuredResult, PluginLlmUsage
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.store import IntakeStore, JobClaim
from plugins.client_knowledge_gbrain.synthesis import (
    SYNTHESIS_SCHEMA,
    SynthesisFailure,
    SynthesisSettings,
    SynthesisWorker,
    validate_synthesis,
)


EXTRACTION = {
    "object_version": "client-knowledge-extractor/v1",
    "limits_version": "client-knowledge-extraction-limits/v1",
    "redaction_version": "client-knowledge-redaction/v1",
    "artifact_id": "a" * 64,
    "source_sha256": "b" * 64,
    "segments": [
        {
            "segment_id": "body-0001",
            "kind": "body_plain",
            "label": "Email body",
            "text": "Send a concise status report every Monday. Use the existing approval flow.",
        }
    ],
}


def _learning(statement="Send a concise status report every Monday."):
    quote = "Send a concise status report every Monday."
    return {
        "statement": statement,
        "evidence": [{
            "segment_id": "body-0001",
            "start": 0,
            "end": len(quote),
            "quote": quote,
        }],
    }


def test_synthesis_schema_is_plain_bounded_and_has_no_taxonomy_or_publication_fields():
    assert SYNTHESIS_SCHEMA["required"] == ["learnings"]
    assert SYNTHESIS_SCHEMA["properties"]["learnings"]["minItems"] == 1
    assert SYNTHESIS_SCHEMA["properties"]["learnings"]["maxItems"] == 3
    item = SYNTHESIS_SCHEMA["properties"]["learnings"]["items"]
    assert set(item["properties"]) == {"statement", "evidence"}
    forbidden = {
        "requirement", "learning", "preference", "fact", "kind", "category",
        "operation", "target_slug", "final_markdown", "honcho_projection",
    }
    assert forbidden.isdisjoint(str(SYNTHESIS_SCHEMA))


def test_synthesis_accepts_exact_evidence_and_repairs_one_unique_offset():
    parsed = {"learnings": [_learning()]}
    parsed["learnings"][0]["evidence"][0]["start"] = 1
    parsed["learnings"][0]["evidence"][0]["end"] += 1
    value = validate_synthesis(parsed, EXTRACTION, max_output_bytes=100_000)
    assert value["learnings"][0]["evidence"][0]["start"] == 0


@pytest.mark.parametrize("count", [0, 4])
def test_synthesis_rejects_outside_one_to_three(count):
    with pytest.raises(SynthesisFailure, match="synthesis_schema_mismatch"):
        validate_synthesis(
            {"learnings": [_learning(f"Learning {index}") for index in range(count)]},
            EXTRACTION,
            max_output_bytes=100_000,
        )


def test_synthesis_rejects_overlapping_statements_and_ambiguous_evidence():
    with pytest.raises(SynthesisFailure, match="synthesis_statements_overlap"):
        validate_synthesis(
            {"learnings": [
                _learning("Send a status report every Monday."),
                _learning("Send a status report every Monday. Include blockers."),
            ]},
            EXTRACTION,
            max_output_bytes=100_000,
        )
    duplicate = dict(EXTRACTION)
    duplicate["segments"] = [{**EXTRACTION["segments"][0], "text": "Monday Monday"}]
    item = _learning("Use Monday as the reporting day.")
    item["evidence"][0] = {
        "segment_id": "body-0001", "start": 99, "end": 105, "quote": "Monday"
    }
    with pytest.raises(SynthesisFailure, match="synthesis_evidence_mismatch"):
        validate_synthesis({"learnings": [item]}, duplicate, max_output_bytes=100_000)


class _Derived:
    def __init__(self, extraction):
        self.extraction = extraction
        self.saved = {}

    def read_json(self, kind, *_args):
        if kind == "extractions":
            return self.extraction
        if kind == "syntheses":
            raise FileNotFoundError
        raise AssertionError(kind)

    def put_json(self, kind, object_id, value):
        from plugins.client_knowledge_gbrain.derived import DerivedRecord, canonical_json

        data = canonical_json(value)
        self.saved[object_id] = value
        return DerivedRecord(
            object_id, kind, "storage", f"{kind}/{object_id}",
            hashlib.sha256(data).hexdigest(), len(data), SimpleNamespace(),
        )


def test_worker_uses_one_synthesis_model_call_and_persists_bounded_attribution(monkeypatch):
    artifact = IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-1",
        content=b"source",
    )
    extraction = {**EXTRACTION, "artifact_id": artifact.artifact_id, "source_sha256": artifact.content_sha256}
    captured = {}

    class Store:
        def get_synthesis_for_artifact(self, _):
            return None

        def get_extraction_for_synthesis_claim(self, claim):
            return artifact, {
                "extraction_id": "e" * 64,
                "output_sha256": "f" * 64,
                "output_bytes": 1,
            }, "notion:page:source"

        def get_synthesis(self, _):
            return None

        def require_synthesis_review(self, claim, *, synthesis, items):
            captured["synthesis"] = synthesis
            captured["items"] = list(items)

    class Llm:
        def complete_structured(self, **kwargs):
            captured["call"] = kwargs
            return PluginLlmStructuredResult(
                text="{}", parsed={"learnings": [_learning()]}, content_type="json",
                provider="provider", model="model", agent_id="agent",
                audit={
                    "selected_provider": "provider", "selected_model": "model",
                    "model_tier": "advanced", "route_fingerprint": "route",
                },
                usage=PluginLlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
            )

    client = SimpleNamespace(
        settings=SimpleNamespace(source_checkout=None, source_branch="main", timeout_seconds=10),
        assert_source_checkout=lambda: SimpleNamespace(),
    )
    worker = SynthesisWorker(
        Store(), _Derived(extraction), Llm(), client,
        SynthesisSettings(True, 1, 300, 60, 180, 4096, 600_000, 100_000),
    )
    synthesis_id = worker.process_claim(
        JobClaim("j" * 32, artifact.artifact_id, "synthesized", "token", 1, "host", 100, 1)
    )
    assert synthesis_id == captured["synthesis"]["synthesis_id"]
    assert captured["call"]["task"] == "client_knowledge_synthesize"
    assert len(captured["items"]) == 1
    assert captured["synthesis"]["total_tokens"] == 15
    assert captured["synthesis"]["base_git_head"] == "publication-time"
    assert "operation" not in str(captured["call"]["json_schema"])


def test_worker_rejects_aggregate_discord_overflow_before_persistence():
    artifact = IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-overflow",
        content=b"source",
    )
    segments = [
        {
            "segment_id": f"body-{index:04d}",
            "kind": "body_plain",
            "label": "Email body",
            "text": "*" * 800,
        }
        for index in range(1, 4)
    ]
    extraction = {
        **EXTRACTION,
        "artifact_id": artifact.artifact_id,
        "source_sha256": artifact.content_sha256,
        "segments": segments,
    }
    derived = _Derived(extraction)

    class Store:
        def get_synthesis_for_artifact(self, _):
            return None

        def get_extraction_for_synthesis_claim(self, _claim):
            return artifact, {
                "extraction_id": "e" * 64,
                "output_sha256": "f" * 64,
                "output_bytes": 1,
            }, "notion:page:source"

        def get_synthesis(self, _):
            return None

        def require_synthesis_review(self, *_args, **_kwargs):
            raise AssertionError("undeliverable synthesis must not be persisted")

    evidence = [
        {
            "segment_id": segment["segment_id"],
            "start": 0,
            "end": 800,
            "quote": "*" * 800,
        }
        for segment in segments
    ]
    llm = SimpleNamespace(complete_structured=lambda **_kwargs: PluginLlmStructuredResult(
        text="{}",
        parsed={"learnings": [{"statement": "x" * 2000, "evidence": evidence}]},
        content_type="json",
        provider="provider",
        model="model",
        agent_id="agent",
        audit={
            "selected_provider": "provider", "selected_model": "model",
            "model_tier": "advanced", "route_fingerprint": "route",
        },
        usage=PluginLlmUsage(input_tokens=10, output_tokens=5, total_tokens=15),
    ))
    worker = SynthesisWorker(
        Store(), derived, llm, SimpleNamespace(),
        SynthesisSettings(True, 1, 300, 60, 180, 4096, 600_000, 100_000),
    )
    with pytest.raises(SynthesisFailure, match="review_payload_undeliverable"):
        worker.process_claim(
            JobClaim("j" * 32, artifact.artifact_id, "synthesized", "token", 1, "host", 100, 1)
        )
    assert derived.saved == {}
