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
    with pytest.raises(AssimilationFailure, match="assimilation_markdown_mismatch"):
        validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )


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
    with pytest.raises(AssimilationFailure, match="projection_policy_invalid"):
        validate_proposal(
            _proposal(op), artifact_id="a" * 64, interpretation_id="b" * 64,
            project_key="pid", notion_ref="notion:page:new", current_pages={},
            interpretation=_interpretation(op["claim"]), source_root=tmp_path,
            max_output_bytes=500_000,
        )


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
    assert reason == "confirmation_finding_grounding_mismatch"
    rendered = parsed["operations"][0]
    assert rendered["timeline_entry"].startswith("- original timeline")
    assert "erase history" not in rendered["final_markdown"]


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
