from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from plugins.client_knowledge_gbrain.assimilation import (
    ASSIMILATION_VERSION,
    AssimilationFailure,
    _canonical_markdown,
    _verify_synced_pages,
    validate_proposal,
)
from plugins.client_knowledge_gbrain.publisher import (
    GitSourcePublisher,
    PublicationFailure,
    PublicationFile,
)


def _frontmatter(refs=None):
    return {
        "project": "pid", "status": "current", "kind": "requirement",
        "effective_at": "2026-08-04", "updated_at": "prior",
        "source_refs": refs or ["notion:page:old"], "supersedes": [],
        "confidence": "high", "sensitivity": "internal", "impact": "ordinary",
        "honcho_projection": "eligible",
    }


def _operation(operation="add", *, prior="", claim="Weekly report is due Monday."):
    value = {
        "operation": operation,
        "target_slug": "requirements/reporting",
        "title": "Reporting",
        "kind": "requirement",
        "status": "current",
        "confidence": "high",
        "sensitivity": "internal",
        "impact": "ordinary",
        "honcho_projection": "eligible",
        "effective_at": "2026-08-04",
        "source_refs": ["notion:page:new"],
        "supersedes": [],
        "claim": claim,
        "timeline_entry": "- **2026-08-04** | Confirmed. [Source: notion:page:new]",
        "expected_prior_sha256": prior,
        "finding_id": "requirement-reporting",
        "evidence_ids": ["evidence-001"],
        "final_markdown": "",
    }
    value["final_markdown"] = _canonical_markdown(value, project_key="pid")
    return value


def _proposal(operation):
    return {
        "artifact_id": "a" * 64,
        "interpretation_id": "b" * 64,
        "project_key": "pid",
        "operations": [operation],
    }


def _interpretation(claim="Weekly report is due Monday."):
    return {
        "candidate_learnings": [],
        "decisions": [],
        "requirements": [{
            "id": "requirement-reporting",
            "text": claim,
            "confidence": "high",
            "sensitivity": "internal",
            "evidence_ids": ["evidence-001"],
        }],
        "preferences": [],
        "evidence": [{
            "id": "evidence-001",
            "segment_id": "body-0001",
            "start": 0,
            "end": len(claim),
            "quote": claim,
        }],
    }


def test_closed_allowlist_routes_all_content_changes_to_review(tmp_path):
    for operation_name in (
        "add", "refine", "supersede", "contradict", "mark_tentative", "needs_review"
    ):
        op = _operation(operation_name)
        current_pages = {}
        if operation_name != "add":
            path = tmp_path / "projects/pid/requirements/reporting.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("old", encoding="utf-8")
            op["expected_prior_sha256"] = hashlib.sha256(b"old").hexdigest()
            if operation_name == "contradict":
                op["status"] = "disputed"
                op["honcho_projection"] = "ineligible"
            elif operation_name == "mark_tentative":
                op["status"] = "tentative"
                op["honcho_projection"] = "ineligible"
            elif operation_name == "supersede":
                op["status"] = "current"
                op["target_slug"] = "requirements/reporting-v2"
                op["supersedes"] = ["projects/pid/requirements/reporting"]
                op["expected_prior_sha256"] = ""
                current_pages = {
                    "requirements/reporting": {
                        "title": "Reporting", "compiled_truth": "old",
                        "timeline": "- prior",
                        "markdown_sha256": hashlib.sha256(b"old").hexdigest(),
                        "frontmatter": _frontmatter(),
                    }
                }
            op["final_markdown"] = _canonical_markdown(op, project_key="pid")
        parsed, review, reason = validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages=current_pages,
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )
        assert parsed["operations"][0]["operation"] == operation_name
        assert review is True
        assert reason


@pytest.mark.parametrize(
    "claim",
    [
        "The production root credential is hunter2.",
        "Sligo must transfer EUR 500000 tomorrow.",
        "Disable authentication for production users.",
        "This email amends the signed contract.",
        "The client prefers bypassing legal review.",
        "Ordinary fact\u200b with hidden text.",
    ],
)
def test_adversarial_ordinary_fact_requirement_preference_never_auto_publishes(tmp_path, claim):
    op = _operation("add", claim=claim)
    if any(
        token in claim.lower()
        for token in ("credential", "authentication", "contract", "legal")
    ):
        op["impact"] = "high"
        op["honcho_projection"] = "ineligible"
        op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    _parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation(claim), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason in {"outside_auto_publication_allowlist", "high_impact_claim"}


def test_exact_confirmation_is_the_only_write_auto_allowlist(tmp_path):
    path = tmp_path / "projects/pid/requirements/reporting.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prior")
    prior = hashlib.sha256(b"prior").hexdigest()
    op = _operation("confirm", prior=prior)
    op["source_refs"] = ["notion:page:old", "notion:page:new"]
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    current = {
        "requirements/reporting": {
            "title": "Reporting", "compiled_truth": op["claim"],
            "timeline": "- prior",
            "frontmatter": _frontmatter(),
        }
    }
    _parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages=current,
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is False
    assert reason == "closed_allowlist_exact_confirmation"

    op["claim"] += " Changed."
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    _parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages=current,
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason == "confirmation_changes_truth"


def test_project_citation_and_markdown_boundaries_fail_closed(tmp_path):
    op = _operation("add")
    op["source_refs"] = ["notion:page:invented"]
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    with pytest.raises(AssimilationFailure, match="assimilation_citation_invalid"):
        validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )
    op = _operation("add")
    op["target_slug"] = "projects/decoy/canary"
    with pytest.raises(Exception):
        validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )
    op = _operation("add")
    op["final_markdown"] += "model-added"
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    rendered = parsed["operations"][0]
    assert review is True
    assert reason == "finding_grounding_mismatch"
    assert rendered["final_markdown"] == _canonical_markdown(rendered, project_key="pid")
    assert "model-added" not in rendered["final_markdown"]


class _Client:
    def __init__(self, root):
        self.root = Path(root)
        self.settings = type("S", (), {
            "source_branch": "main", "source_checkout": self.root,
            "timeout_seconds": 10,
        })()

    def assert_source_checkout(self):
        return self.root


def _repo(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    (root / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=root, check=True, capture_output=True)
    return root


def _publish(root, *, expected_head=None):
    expected_head = expected_head or subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
    ).strip()
    with GitSourcePublisher(_Client(root), project_key="pid") as publisher:
        return publisher.publish(
            artifact_id="a" * 64, assimilation_id="b" * 64,
            assimilation_version=ASSIMILATION_VERSION, proposal_sha256="c" * 64,
            expected_head=expected_head, authored_at=1,
            files=[PublicationFile("requirements/reporting", b"content\n")],
            interpretation_id="d" * 64,
        )


@pytest.mark.parametrize("dirty", ["tracked", "staged", "untracked", "ignored"])
def test_publisher_requires_clean_index_worktree_and_no_untracked(tmp_path, dirty):
    root = _repo(tmp_path)
    if dirty == "tracked":
        (root / "seed.txt").write_text("dirty\n")
    elif dirty == "staged":
        (root / "seed.txt").write_text("dirty\n")
        subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
    elif dirty == "untracked":
        (root / "new.txt").write_text("untracked")
    else:
        (root / ".gitignore").write_text("ignored.txt\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "ignore"], cwd=root, check=True, capture_output=True)
        (root / "ignored.txt").write_text("ignored")
    with pytest.raises(PublicationFailure):
        _publish(root)
    assert (root / "new.txt").exists() if dirty == "untracked" else True
    assert (root / "ignored.txt").exists() if dirty == "ignored" else True


def test_publisher_rejects_symlink_source_before_git(tmp_path):
    root = _repo(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(Exception, match="symlink"):
        GitSourcePublisher(_Client(alias), project_key="pid")


def test_publisher_cas_rejects_concurrent_head_and_audits_paths(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    expected = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original = GitSourcePublisher._workspace_snapshot
    calls = 0

    def race(self, head, **kwargs):
        nonlocal calls
        calls += 1
        result = original(self, head, **kwargs)
        if calls == 2:
            subprocess.run(["git", "commit", "--allow-empty", "-m", "race"], cwd=root, check=True, capture_output=True)
        return result

    monkeypatch.setattr(GitSourcePublisher, "_workspace_snapshot", race)
    with pytest.raises(PublicationFailure, match="git_cas_failed"):
        _publish(root, expected_head=expected)
    assert not (root / "projects/pid/requirements/reporting.md").exists()


def test_publisher_creates_one_exact_project_commit_and_recovers(tmp_path):
    root = _repo(tmp_path)
    result = _publish(root)
    changed = subprocess.check_output(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", result.commit_sha],
        cwd=root, text=True,
    ).splitlines()
    assert changed == ["projects/pid/requirements/reporting.md"]
    count = int(subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=root, text=True))
    assert count == 2
    assert subprocess.check_output(["git", "status", "--porcelain", "--ignored"], cwd=root, text=True) == ""


def test_publisher_recovers_crash_after_cas_without_duplicate_commit(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original = GitSourcePublisher._materialize_commit
    crashed = False

    def crash_once(self, **kwargs):
        nonlocal crashed
        if not crashed:
            crashed = True
            raise PublicationFailure("simulated_crash_after_cas")
        return original(self, **kwargs)

    monkeypatch.setattr(GitSourcePublisher, "_materialize_commit", crash_once)
    with pytest.raises(PublicationFailure, match="simulated_crash_after_cas"):
        _publish(root, expected_head=base)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert commit != base
    assert not (root / "projects/pid/requirements/reporting.md").exists()

    recovered = _publish(root, expected_head=base)
    assert recovered.commit_sha == commit
    assert (root / "projects/pid/requirements/reporting.md").read_bytes() == b"content\n"
    assert int(subprocess.check_output(["git", "rev-list", "--count", "HEAD"], cwd=root, text=True)) == 2


def test_publisher_rejects_symlink_in_target_parent_and_git_metadata(tmp_path):
    root = _repo(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "projects").mkdir()
    (root / "projects/pid").symlink_to(outside, target_is_directory=True)
    with pytest.raises(PublicationFailure, match="publication_path_unsafe"):
        _publish(root)

    second = tmp_path / "second"
    second.mkdir()
    root = _repo(second)
    info = root / ".git" / "objects" / "info"
    (info / "canary").symlink_to(outside)
    with pytest.raises(PublicationFailure, match="git_metadata_symlink_present"):
        _publish(root)


def test_high_impact_and_unsafe_projection_metadata_fail_closed(tmp_path):
    op = _operation("add", claim="The signed contract requires a payment tomorrow.")
    _parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason == "outside_auto_publication_allowlist"
    op["impact"] = "high"
    op["honcho_projection"] = "ineligible"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    _parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason == "high_impact_claim"

    op["honcho_projection"] = "eligible"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason == "finding_grounding_mismatch"
    assert parsed["operations"][0]["honcho_projection"] == "ineligible"


def test_confirmation_requires_persisted_finding_and_host_preserves_timeline(tmp_path):
    path = tmp_path / "projects/pid/requirements/reporting.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prior")
    prior = hashlib.sha256(b"prior").hexdigest()
    current = {
        "requirements/reporting": {
            "title": "Reporting",
            "compiled_truth": "Weekly report is due Monday.",
            "timeline": "- original timeline",
            "frontmatter": _frontmatter(),
        }
    }
    op = _operation("confirm", prior=prior)
    op["source_refs"] = ["notion:page:old", "notion:page:new"]
    op["timeline_entry"] = "erase history"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages=current,
        interpretation=_interpretation("Unrelated finding."), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is True
    assert reason == "finding_grounding_mismatch"
    rendered = parsed["operations"][0]
    assert rendered["timeline_entry"].startswith("- original timeline")
    assert "erase history" not in rendered["final_markdown"]


def test_non_confirmation_grounding_mismatch_is_host_normalized_for_review(tmp_path):
    op = _operation("add", claim="Paraphrased reporting requirement.")
    op["evidence_ids"] = ["evidence-999"]
    op["kind"] = "decision"
    op["confidence"] = "low"
    op["sensitivity"] = "confidential"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation("Weekly report is due Monday."),
        source_root=tmp_path, max_output_bytes=500_000,
    )
    rendered = parsed["operations"][0]
    assert review is True
    assert reason == "finding_grounding_mismatch"
    assert rendered["claim"] == "Weekly report is due Monday."
    assert rendered["evidence_ids"] == ["evidence-001"]
    assert rendered["kind"] == "requirement"
    assert rendered["confidence"] == "high"
    assert rendered["sensitivity"] == "internal"
    assert "Paraphrased reporting requirement." not in rendered["final_markdown"]
    assert rendered["final_markdown"] == _canonical_markdown(rendered, project_key="pid")


@pytest.mark.parametrize(("operation", "model_status", "host_status"), [
    ("add", "tentative", "current"),
    ("refine", "disputed", "current"),
    ("contradict", "current", "disputed"),
    ("mark_tentative", "current", "tentative"),
    ("supersede", "archived", "current"),
])
def test_operation_status_is_host_normalized_for_review(
    tmp_path, operation, model_status, host_status
):
    op = _operation(operation)
    op["status"] = model_status
    current = {}
    if operation in {"refine", "contradict", "mark_tentative"}:
        path = tmp_path / "projects/pid/requirements/reporting.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("prior", encoding="utf-8")
        op["expected_prior_sha256"] = hashlib.sha256(b"prior").hexdigest()
        current = {
            "requirements/reporting": {
                "title": "Reporting", "compiled_truth": "prior", "timeline": "- prior",
                "markdown_sha256": hashlib.sha256(b"prior").hexdigest(),
                "frontmatter": _frontmatter(),
            }
        }
    if operation == "supersede":
        op["target_slug"] = "requirements/reporting-v2"
        op["supersedes"] = ["projects/pid/requirements/reporting"]
        current = {
            "requirements/reporting": {
                "title": "Reporting", "compiled_truth": "prior", "timeline": "- prior",
                "markdown_sha256": hashlib.sha256(b"prior").hexdigest(),
                "frontmatter": _frontmatter(),
            }
        }
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages=current,
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    rendered = parsed["operations"][0]
    assert review is True
    assert reason == "finding_grounding_mismatch"
    assert rendered["status"] == host_status
    if host_status in {"disputed", "tentative"}:
        assert rendered["honcho_projection"] == "ineligible"
    assert rendered["final_markdown"] == _canonical_markdown(rendered, project_key="pid")


@pytest.mark.parametrize(("field", "value"), [
    ("impact", "high"),
    ("sensitivity", "confidential"),
    ("sensitivity", "restricted"),
])
def test_projection_ineligibility_is_host_normalized_for_review(tmp_path, field, value):
    op = _operation("add")
    op[field] = value
    op["honcho_projection"] = "eligible"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    interpretation = _interpretation(op["claim"])
    if field == "sensitivity":
        interpretation["requirements"][0]["sensitivity"] = value
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=interpretation, source_root=tmp_path,
        max_output_bytes=500_000,
    )
    rendered = parsed["operations"][0]
    assert review is True
    assert reason == "finding_grounding_mismatch"
    assert rendered["honcho_projection"] == "ineligible"
    assert rendered["final_markdown"] == _canonical_markdown(rendered, project_key="pid")


def test_assimilation_schema_rejects_invalid_classification_values():
    op = _operation("add")
    for key, value in (
        ("status", "active"),
        ("impact", "medium"),
        ("honcho_projection", "project_requirement"),
    ):
        invalid = _proposal({**op, key: value})
        with pytest.raises(AssimilationFailure, match="schema_mismatch"):
            validate_proposal(
                invalid, artifact_id="a" * 64, interpretation_id="b" * 64,
                project_key="pid", notion_ref="notion:page:new", current_pages={},
                interpretation=_interpretation(op["claim"]), source_root=Path("/tmp"),
                max_output_bytes=500_000,
            )


def test_non_transient_operation_rejects_empty_classification_values():
    op = _operation("add")
    for key in (
        "kind", "status", "confidence", "sensitivity", "impact",
        "honcho_projection",
    ):
        invalid = _proposal({**op, key: ""})
        with pytest.raises(AssimilationFailure, match="schema_mismatch"):
            validate_proposal(
                invalid, artifact_id="a" * 64, interpretation_id="b" * 64,
                project_key="pid", notion_ref="notion:page:new", current_pages={},
                interpretation=_interpretation(op["claim"]), source_root=Path("/tmp"),
                max_output_bytes=500_000,
            )


def test_operation_schema_requires_grounded_date_and_empty_transient_content():
    op = _operation("add")
    with pytest.raises(AssimilationFailure, match="schema_mismatch"):
        validate_proposal(
            _proposal({**op, "effective_at": ""}),
            artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=Path("/tmp"),
            max_output_bytes=500_000,
        )
    transient = {key: "" for key in (
        "target_slug", "title", "kind", "status", "confidence", "sensitivity",
        "impact", "honcho_projection", "effective_at", "claim", "timeline_entry",
        "expected_prior_sha256", "finding_id", "final_markdown",
    )}
    transient.update({
        "operation": "ignore_transient", "source_refs": ["notion:page:new"],
        "supersedes": [], "evidence_ids": [],
    })
    with pytest.raises(AssimilationFailure, match="schema_mismatch"):
        validate_proposal(
            _proposal(transient), artifact_id="a" * 64,
            interpretation_id="b" * 64, project_key="pid",
            notion_ref="notion:page:new", current_pages={}, interpretation={
                "candidate_learnings": [], "decisions": [], "requirements": [],
                "preferences": [], "evidence": [],
            }, source_root=Path("/tmp"), max_output_bytes=500_000,
        )


def test_grounded_transient_may_reference_exact_finding_evidence(tmp_path):
    transient = {key: "" for key in (
        "target_slug", "title", "kind", "status", "confidence", "sensitivity",
        "impact", "honcho_projection", "effective_at", "claim", "timeline_entry",
        "expected_prior_sha256", "final_markdown",
    )}
    transient.update({
        "operation": "ignore_transient", "finding_id": "requirement-reporting",
        "source_refs": [], "supersedes": [], "evidence_ids": ["evidence-001"],
    })
    parsed, review, reason = validate_proposal(
        _proposal(transient), artifact_id="a" * 64,
        interpretation_id="b" * 64, project_key="pid",
        notion_ref="notion:page:new", current_pages={},
        interpretation=_interpretation("Transient test instruction."),
        source_root=tmp_path, max_output_bytes=500_000,
    )
    assert parsed["operations"] == [transient]
    assert review is False
    assert reason == "closed_allowlist_ignore_transient"


def test_confirmation_preserves_timeline_beyond_model_context_truncation(tmp_path):
    path = tmp_path / "projects/pid/requirements/reporting.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"prior")
    timeline = "x" * 5000
    current = {
        "requirements/reporting": {
            "title": "Reporting", "compiled_truth": "Weekly report is due Monday.",
            "timeline": timeline, "frontmatter": _frontmatter(),
        }
    }
    op = _operation("confirm", prior=hashlib.sha256(b"prior").hexdigest())
    op["source_refs"] = ["notion:page:old", "notion:page:new"]
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages=current,
        interpretation=_interpretation(op["claim"]), source_root=tmp_path,
        max_output_bytes=500_000,
    )
    assert review is False
    assert reason == "closed_allowlist_exact_confirmation"
    assert timeline in parsed["operations"][0]["final_markdown"]


def test_proposal_requires_one_operation_for_each_grounded_finding(tmp_path):
    first = _operation("add")
    second = _operation("add", claim="Invoices are due Friday.")
    second.update({
        "target_slug": "requirements/invoicing", "finding_id": "requirement-invoicing",
        "evidence_ids": ["evidence-002"],
    })
    second["final_markdown"] = _canonical_markdown(second, project_key="pid")
    interpretation = _interpretation(first["claim"])
    interpretation["requirements"].append({
        "id": "requirement-invoicing", "text": second["claim"], "confidence": "high",
        "sensitivity": "internal", "evidence_ids": ["evidence-002"],
    })
    interpretation["evidence"].append({
        "id": "evidence-002", "segment_id": "body-0002", "start": 0,
        "end": len(second["claim"]), "quote": second["claim"],
    })
    proposal = _proposal(first)
    with pytest.raises(AssimilationFailure, match="findings_incomplete"):
        validate_proposal(
            proposal, artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=interpretation, source_root=tmp_path, max_output_bytes=500_000,
        )
    proposal["operations"].append(second)
    parsed, review, reason = validate_proposal(
        proposal, artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation=interpretation, source_root=tmp_path, max_output_bytes=500_000,
    )
    assert len(parsed["operations"]) == 2
    assert review is True
    assert reason == "outside_auto_publication_allowlist"


def test_empty_interpretation_accepts_one_empty_transient_operation(tmp_path):
    op = {key: "" for key in (
        "target_slug", "title", "kind", "status", "confidence", "sensitivity",
        "impact", "honcho_projection", "effective_at", "claim", "timeline_entry",
        "expected_prior_sha256", "finding_id", "final_markdown",
    )}
    op.update({
        "operation": "ignore_transient", "source_refs": [], "supersedes": [],
        "evidence_ids": [],
    })
    parsed, review, reason = validate_proposal(
        _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
        project_key="pid", notion_ref="notion:page:new", current_pages={},
        interpretation={
            "candidate_learnings": [], "decisions": [], "requirements": [],
            "preferences": [], "evidence": [],
        },
        source_root=tmp_path, max_output_bytes=500_000,
    )
    assert parsed["operations"] == [op]
    assert review is False
    assert reason == "closed_allowlist_ignore_transient"


def test_frontmatter_scalar_injection_fails_closed(tmp_path):
    op = _operation("add")
    op["effective_at"] = "2026-08-04\nstatus: current"
    op["final_markdown"] = _canonical_markdown(op, project_key="pid")
    with pytest.raises(AssimilationFailure, match="effective_at_invalid"):
        validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )


def test_publisher_rechecks_target_immediately_before_materialization(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "projects/pid/requirements/reporting.md"
    original = GitSourcePublisher._materialization_state_is_recoverable

    def race(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"concurrent edit\n")
        return result

    monkeypatch.setattr(GitSourcePublisher, "_materialization_state_is_recoverable", race)
    with pytest.raises(PublicationFailure, match="target_changed_before_materialization"):
        _publish(root)
    assert target.read_bytes() == b"concurrent edit\n"


def test_publisher_never_overwrites_target_created_during_atomic_install(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "projects/pid/requirements/reporting.md"
    original = os.link

    def race(source, destination, *args, **kwargs):
        if Path(destination) == target:
            target.write_bytes(b"concurrent edit\n")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", race)
    with pytest.raises(PublicationFailure, match="target_changed_before_materialization"):
        _publish(root)
    assert target.read_bytes() == b"concurrent edit\n"


def test_publisher_preserves_concurrent_edit_during_atomic_exchange(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "projects/pid/requirements/reporting.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior\n")
    subprocess.run(["git", "add", str(target.relative_to(root))], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "prior"], cwd=root, check=True, capture_output=True)
    expected_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original = GitSourcePublisher._exchange_paths

    def race(left, right):
        if Path(right) == target:
            target.write_bytes(b"concurrent edit\n")
        return original(left, right)

    monkeypatch.setattr(GitSourcePublisher, "_exchange_paths", staticmethod(race))
    with GitSourcePublisher(_Client(root), project_key="pid") as publisher:
        with pytest.raises(PublicationFailure, match="target_changed_before_materialization"):
            publisher.publish(
                artifact_id="a" * 64, assimilation_id="b" * 64,
                assimilation_version=ASSIMILATION_VERSION, proposal_sha256="c" * 64,
                expected_head=expected_head, authored_at=1,
                files=[PublicationFile(
                    "requirements/reporting", b"content\n",
                    expected_prior_sha256=hashlib.sha256(b"prior\n").hexdigest(),
                )],
                interpretation_id="d" * 64,
            )
    assert target.read_bytes() == b"concurrent edit\n"
    assert not any(target.parent.glob(".reporting.md.tmp-*"))


def test_publisher_preserves_concurrent_edit_when_exchange_rollback_fails(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "projects/pid/requirements/reporting.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior\n")
    subprocess.run(["git", "add", str(target.relative_to(root))], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "prior"], cwd=root, check=True, capture_output=True)
    expected_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original = GitSourcePublisher._exchange_paths
    calls = 0

    def fail_rollback(left, right):
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_bytes(b"concurrent edit\n")
            original(left, right)
            return
        raise OSError("rollback failed")

    monkeypatch.setattr(GitSourcePublisher, "_exchange_paths", staticmethod(fail_rollback))
    with GitSourcePublisher(_Client(root), project_key="pid") as publisher:
        with pytest.raises(PublicationFailure, match="exchange_recovery_failed"):
            publisher.publish(
                artifact_id="a" * 64, assimilation_id="b" * 64,
                assimilation_version=ASSIMILATION_VERSION, proposal_sha256="c" * 64,
                expected_head=expected_head, authored_at=1,
                files=[PublicationFile(
                    "requirements/reporting", b"content\n",
                    expected_prior_sha256=hashlib.sha256(b"prior\n").hexdigest(),
                )],
                interpretation_id="d" * 64,
            )
    assert target.read_bytes() == b"content\n"
    sidecars = list(target.parent.glob(".reporting.md.tmp-*"))
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == b"concurrent edit\n"


def test_publisher_recovers_crash_after_atomic_exchange(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    target = root / "projects/pid/requirements/reporting.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"prior\n")
    subprocess.run(["git", "add", str(target.relative_to(root))], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "prior"], cwd=root, check=True, capture_output=True)
    expected_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    original_unlink = Path.unlink

    def crash_on_sidecar_cleanup(path, *args, **kwargs):
        if path.name.startswith(".reporting.md.tmp-"):
            raise RuntimeError("simulated crash after exchange")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", crash_on_sidecar_cleanup)
    with GitSourcePublisher(_Client(root), project_key="pid") as publisher:
        with pytest.raises(RuntimeError, match="simulated crash after exchange"):
            publisher.publish(
                artifact_id="a" * 64, assimilation_id="b" * 64,
                assimilation_version=ASSIMILATION_VERSION, proposal_sha256="c" * 64,
                expected_head=expected_head, authored_at=1,
                files=[PublicationFile(
                    "requirements/reporting", b"content\n",
                    expected_prior_sha256=hashlib.sha256(b"prior\n").hexdigest(),
                )],
                interpretation_id="d" * 64,
            )
    assert target.read_bytes() == b"content\n"
    assert any(target.parent.glob(".reporting.md.tmp-*"))

    monkeypatch.setattr(Path, "unlink", original_unlink)
    with GitSourcePublisher(_Client(root), project_key="pid") as publisher:
        recovered = publisher.publish(
            artifact_id="a" * 64, assimilation_id="b" * 64,
            assimilation_version=ASSIMILATION_VERSION, proposal_sha256="c" * 64,
            expected_head=expected_head, authored_at=1,
            files=[PublicationFile(
                "requirements/reporting", b"content\n",
                expected_prior_sha256=hashlib.sha256(b"prior\n").hexdigest(),
            )],
            interpretation_id="d" * 64,
        )
    assert recovered.commit_sha == subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True,
    ).strip()
    assert target.read_bytes() == b"content\n"
    assert not any(target.parent.glob(".reporting.md.tmp-*"))


def test_post_sync_verifies_exact_page_and_blob(tmp_path):
    slug = "projects/pid/requirements/reporting"
    content = _canonical_markdown(_operation("add"), project_key="pid").encode()
    target = tmp_path / f"{slug}.md"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    expected = _operation("add")

    class Client:
        settings = type("Settings", (), {"source_id": "client-knowledge"})()

        def assert_runtime_ready(self):
            return tmp_path

        def get_page(self, _slug):
            return {
                "source_id": "client-knowledge", "slug": slug, "title": expected["title"],
                "frontmatter": {**_frontmatter(["notion:page:new"]), "impact": "ordinary", "honcho_projection": "eligible"},
                "compiled_truth": expected["claim"], "timeline": expected["timeline_entry"],
            }

    _verify_synced_pages(
        Client(), project_key="pid", expected_pages={slug: expected},
        expected_content={slug: content},
    )
    target.write_bytes(b"stale")
    with pytest.raises(AssimilationFailure, match="blob_verification_failed"):
        _verify_synced_pages(
            Client(), project_key="pid", expected_pages={slug: expected},
            expected_content={slug: content},
        )
