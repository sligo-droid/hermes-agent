from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from agent.plugin_llm import PluginLlmStructuredResult, PluginLlmUsage
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.store import IntakeStore, JobClaim
from plugins.client_knowledge_gbrain.synthesis import (
    SYNTHESIS_INSTRUCTIONS,
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


def _learnings(count):
    statements = [
        "Schedule weekly status delivery for Monday mornings.",
        "Route client-facing changes through the existing approval workflow.",
        "Keep interface copy concise for executive readers.",
        "Record launch blockers in the delivery checklist.",
        "Assign one owner to production incident communication.",
        "Preserve audit history when project permissions change.",
        "Show deadline reminders beside open governance proposals.",
        "Use accessible color contrast for dashboard warnings.",
        "Retain delegate voting power during balance recalculation.",
        "Document rollout dependencies before implementation starts.",
    ]
    return [_learning(statement) for statement in statements[:count]]


def test_synthesis_schema_is_plain_bounded_and_has_no_taxonomy_or_publication_fields():
    assert SYNTHESIS_SCHEMA["required"] == ["learnings"]
    assert SYNTHESIS_SCHEMA["properties"]["learnings"]["minItems"] == 3
    assert SYNTHESIS_SCHEMA["properties"]["learnings"]["maxItems"] == 10
    item = SYNTHESIS_SCHEMA["properties"]["learnings"]["items"]
    assert set(item["properties"]) == {"statement", "evidence"}
    forbidden = {
        "requirement", "learning", "preference", "fact", "kind", "category",
        "operation", "target_slug", "final_markdown", "honcho_projection",
    }
    assert forbidden.isdisjoint(str(SYNTHESIS_SCHEMA))


def test_synthesis_accepts_exact_evidence_and_repairs_one_unique_offset():
    parsed = {"learnings": _learnings(3)}
    parsed["learnings"][0]["evidence"][0]["start"] = 1
    parsed["learnings"][0]["evidence"][0]["end"] += 1
    value = validate_synthesis(parsed, EXTRACTION, max_output_bytes=100_000)
    assert value["learnings"][0]["evidence"][0]["start"] == 0


@pytest.mark.parametrize("count", [3, 4, 7, 10])
def test_synthesis_accepts_full_natural_count_range(count):
    assert len(validate_synthesis(
        {"learnings": _learnings(count)}, EXTRACTION, max_output_bytes=100_000
    )["learnings"]) == count


@pytest.mark.parametrize("count", [0, 1, 2, 11])
def test_synthesis_rejects_outside_three_to_ten(count):
    with pytest.raises(SynthesisFailure, match="synthesis_schema_mismatch"):
        validate_synthesis(
            {"learnings": [_learning(f"Learning {index}") for index in range(count)]},
            EXTRACTION,
            max_output_bytes=100_000,
        )


def test_synthesis_prompt_requires_natural_distinct_count_without_padding():
    folded = SYNTHESIS_INSTRUCTIONS.casefold()
    assert "natural count from 3 to 10" in folded
    assert "durable source richness" in folded
    assert "never pad" in folded
    assert "restate the same underlying idea" in folded
    assert "mutually distinct" in folded


def test_synthesis_rejects_semantic_restatement_and_ambiguous_evidence():
    with pytest.raises(SynthesisFailure, match="synthesis_statements_overlap"):
        validate_synthesis(
            {"learnings": [
                _learning(
                    "Agora permission changes must preserve the project role model and governance workflow."
                ),
                _learning(
                    "When implementing Agora, reuse project permission roles and protect the governance workflow."
                ),
                _learning("Send a concise status report every Monday."),
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
        validate_synthesis(
            {"learnings": [item, *_learnings(2)]},
            duplicate,
            max_output_bytes=100_000,
        )


def test_synthesis_allows_distinct_actions_that_share_project_terms():
    value = validate_synthesis(
        {"learnings": [
            _learning("Configure Agora proposal notifications with actionable deadline reminders."),
            _learning("Preserve Agora delegate voting power during token balance recalculation."),
            _learning("Document Agora rollout ownership in the weekly project status report."),
        ]},
        EXTRACTION,
        max_output_bytes=100_000,
    )
    assert len(value["learnings"]) == 3


def test_synthesis_rejects_live_political_positioning_restatement():
    quote = (
        "I would love to avoid labeling ourselves as a Political Site because I believe "
        "we want to be more in spite of the momentary shock value of the rhetoric and theater."
    )
    extraction = {
        **EXTRACTION,
        "segments": [{
            "segment_id": "body-0001",
            "kind": "body_plain",
            "label": "Email body",
            "text": quote,
        }],
    }
    evidence = [{
        "segment_id": "body-0001",
        "start": 0,
        "end": len(quote),
        "quote": quote,
    }]
    with pytest.raises(SynthesisFailure, match="synthesis_statements_overlap"):
        validate_synthesis(
            {"learnings": [
                {
                    "statement": (
                        "Agora is conceived as a public signal index whose scope extends "
                        "beyond campaign politics."
                    ),
                    "evidence": evidence,
                },
                {
                    "statement": (
                        "John Iwaniec prefers to avoid labeling Agora as a Political Site."
                    ),
                    "evidence": evidence,
                },
                _learning("Send a concise status report every Monday."),
            ]},
            extraction,
            max_output_bytes=100_000,
        )


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
                text="{}", parsed={"learnings": _learnings(3)}, content_type="json",
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
    assert len(captured["items"]) == 3
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
        parsed={"learnings": [
            {"statement": "x" * 2000, "evidence": evidence},
            {
                "statement": "Schedule weekly executive reporting for Monday morning.",
                "evidence": [evidence[0]],
            },
            {
                "statement": "Preserve the existing approval workflow for client-facing changes.",
                "evidence": [evidence[1]],
            },
        ]},
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
