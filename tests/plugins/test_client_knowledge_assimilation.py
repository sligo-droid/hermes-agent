from __future__ import annotations

import hashlib
import json
import subprocess
import time
from types import SimpleNamespace

import pytest

from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.publisher import (
    GitSourcePublisher,
    PublicationFailure,
    PublicationFile,
    PublicationResult,
)
from plugins.client_knowledge_gbrain.store import IntakeStore, JobClaim
from plugins.client_knowledge_gbrain.synthesis import (
    SynthesisSettings,
    SynthesisWorker,
    render_learning_markdown,
)


def _repo(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    (root / "README.md").write_text("source\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)
    return root


def _artifact():
    return IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-1",
        occurred_at=1786089600, content=b"source",
    )


def _item(item_id, state, statement):
    quote = "Send a concise status report every Monday."
    return {
        "item_id": item_id,
        "state": state,
        "statement": statement,
        "evidence_json": json.dumps([{
            "segment_id": "body-0001", "start": 0, "end": len(quote), "quote": quote,
        }], sort_keys=True, separators=(",", ":")),
        "item_sha256": hashlib.sha256(statement.encode()).hexdigest(),
    }


def _worker(store, client):
    return SynthesisWorker(
        store, SimpleNamespace(), SimpleNamespace(), client,
        SynthesisSettings(True, 1, 300, 60, 180, 4096, 600_000, 100_000),
    )


def test_host_rendered_page_has_deterministic_provenance_and_no_honcho():
    content = render_learning_markdown(
        project_key="pid",
        statement="Send a concise status report every Monday.",
        evidence=[{
            "segment_id": "body-0001", "start": 0, "end": 42,
            "quote": "Send a concise status report every Monday.",
        }],
        notion_ref="notion:page:source",
        source_artifact_id="a" * 64,
        source_date="2026-08-07",
    ).decode()
    assert "honcho" not in content.lower()
    assert "notion:page:source" in content
    assert "body-0001:0-42" in content
    assert "title: Client learning" in content
    assert "<!-- timeline -->" in content
    assert "# Client learning" not in content


def test_all_rejected_completes_without_git_publication():
    artifact = _artifact()
    completed = {}

    class Store:
        def get_synthesis_for_artifact(self, _):
            return {"state": "ready"}

        def get_synthesis_for_publication_claim(self, claim):
            return artifact, {
                "synthesis_id": "s" * 64,
                "output_sha256": "o" * 64,
            }, [_item("1" * 64, "rejected", "Rejected")]

        def complete_synthesis(self, claim, **kwargs):
            completed.update(kwargs)

    worker = _worker(Store(), SimpleNamespace())
    assert worker.process_claim(JobClaim("j" * 32, artifact.artifact_id, "synthesized", "t", 1, "h", 99, 1)) == "s" * 64
    assert completed == {
        "synthesis_id": "s" * 64,
        "commit_sha": "none",
        "output_sha256": "o" * 64,
        "sync_verified": False,
    }


def test_new_completion_has_no_honcho_or_legacy_complete_stage(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    now = time.time()
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e" * 64, artifact.artifact_id, artifact.content_sha256, "m" * 64,
             "ev", "lv", "rv", "extracted", "storage", "object", "x" * 64, 1, 1, "{}", now),
        )
        synthesis = {
            "synthesis_id": "s" * 64, "artifact_id": artifact.artifact_id,
            "extraction_id": "e" * 64, "project_key": "pid", "notion_ref": "notion:page:source",
            "synthesis_version": "v1", "schema_version": "sv", "prompt_version": "pv",
            "derived_storage_id": "storage", "derived_object_key": "object",
            "output_sha256": "0" * 64, "output_bytes": 1, "actual_provider": "provider",
            "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
            "model_tier": "advanced", "route_fingerprint": "route", "base_git_head": "head",
            "state": "ready",
        }
        store._insert_synthesis_locked(conn, synthesis, [{
            "item_id": "1" * 64, "position": 1, "statement": "Rejected.",
            "evidence_json": _item("1" * 64, "rejected", "Rejected.")["evidence_json"],
            "item_sha256": "a" * 64, "state": "rejected",
        }], now=now)
        conn.execute(
            "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, claim_token, "
            "owner_pid, owner_host, lease_expires_at, created_at, updated_at) "
            "VALUES(?,?,?,'running',3,'token',1,'host',?,?,?)",
            ("j" * 32, artifact.artifact_id, "synthesized", now + 60, now, now),
        )
    claim = JobClaim("j" * 32, artifact.artifact_id, "synthesized", "token", 1, "host", now + 60, 1)
    store.complete_synthesis(
        claim, synthesis_id="s" * 64, commit_sha="none", output_sha256="0" * 64,
        sync_verified=False,
    )
    with store._connect() as conn:
        stages = dict(conn.execute(
            "SELECT stage, receipt_id FROM stage_receipts WHERE artifact_id=?",
            (artifact.artifact_id,),
        ).fetchall())
        jobs = dict(conn.execute(
            "SELECT stage, status FROM jobs WHERE artifact_id=?",
            (artifact.artifact_id,),
        ).fetchall())
    assert stages == {"synthesized": "synthesis:" + "s" * 64 + ":none"}
    assert jobs == {"synthesized": "succeeded"}


def test_final_resolution_publishes_only_approved_items_atomically(tmp_path, monkeypatch):
    artifact = _artifact()
    root = _repo(tmp_path)
    completed = {}
    published = {}
    synthesis = {
        "synthesis_id": "s" * 64,
        "synthesis_version": "v1",
        "output_sha256": "o" * 64,
        "notion_ref": "notion:page:source",
        "base_git_head": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
        ).stdout.strip(),
    }

    class Store:
        def get_synthesis_for_artifact(self, _):
            return {"state": "ready"}

        def get_synthesis_for_publication_claim(self, claim):
            return artifact, synthesis, [
                _item("1" * 64, "approved", "Approved statement."),
                _item("2" * 64, "rejected", "Rejected statement."),
            ]

        def record_synthesis_publication(self, **kwargs):
            published.update(kwargs)

        def complete_synthesis(self, claim, **kwargs):
            completed.update(kwargs)

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, source_id="client-knowledge",
            timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

        def assert_runtime_ready(self):
            return root

        def sync_no_pull(self):
            return None

        def get_page(self, slug):
            return {
                "source_id": "client-knowledge", "slug": slug, "title": "Client learning",
                "frontmatter": {
                    "project": "pid", "status": "current", "effective_at": "2026-08-07",
                    "updated_at": "synthesis-managed", "source_refs": ["notion:page:source"],
                    "supersedes": [], "confidence": "high", "sensitivity": "internal",
                },
                "compiled_truth": "Approved statement.", "timeline": "",
            }

        def parse_markdown(self, _content, *, file_path, expected_slug):
            assert file_path == f"{expected_slug}.md"
            return {
                "errors": [], "slug": expected_slug, "title": "Client learning",
                "compiled_truth": "Approved statement.", "timeline": "## Timeline\nentry",
            }

    original_publish = __import__(
        "plugins.client_knowledge_gbrain.publisher", fromlist=["GitSourcePublisher"]
    ).GitSourcePublisher.publish

    def capture_publish(self, **kwargs):
        assert len(kwargs["files"]) == 1
        assert b"Approved statement." in kwargs["files"][0].content
        assert b"Rejected statement." not in kwargs["files"][0].content
        return original_publish(self, **kwargs)

    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.publisher.GitSourcePublisher.publish",
        capture_publish,
    )
    worker = _worker(Store(), Client())
    worker.process_claim(JobClaim("j" * 32, artifact.artifact_id, "synthesized", "t", 1, "h", 99, 1))
    assert published["state"] == "committed"
    assert completed["sync_verified"] is True


def test_sequential_syntheses_use_publication_time_head(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    expected_heads = []
    original_publish = __import__(
        "plugins.client_knowledge_gbrain.publisher", fromlist=["GitSourcePublisher"]
    ).GitSourcePublisher.publish

    def capture_publish(self, **kwargs):
        expected_heads.append(kwargs["expected_head"])
        return original_publish(self, **kwargs)

    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.publisher.GitSourcePublisher.publish",
        capture_publish,
    )

    class Store:
        def __init__(self):
            self.index = 0
            self.publications = {}

        def get_synthesis_for_artifact(self, _):
            return {"state": "ready"}

        def get_synthesis_for_publication_claim(self, claim):
            self.index += 1
            artifact = IntakeArtifact.from_bytes(
                project_key="pid", provider_id="gmail",
                provider_artifact_id=f"message-{self.index}",
                occurred_at=1786089600, content=f"source-{self.index}".encode(),
            )
            synthesis_id = str(self.index) * 64
            return artifact, {
                "synthesis_id": synthesis_id, "synthesis_version": "v1",
                "output_sha256": "o" * 64, "notion_ref": "notion:page:source",
                "base_git_head": "stale",
            }, [_item(str(self.index) * 64, "approved", f"Statement {self.index}.")]

        def get_synthesis_publication(self, synthesis_id):
            return self.publications.get(synthesis_id)

        def record_synthesis_publication(self, **kwargs):
            self.publications[kwargs["synthesis_id"]] = kwargs

        def complete_synthesis(self, _claim, **_kwargs):
            return None

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, source_id="client-knowledge",
            timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

        def assert_runtime_ready(self):
            return root

        def sync_no_pull(self):
            return None

        def parse_markdown(self, _content, *, expected_slug, **_kwargs):
            index = len(expected_heads) + 1
            return {
                "errors": [], "slug": expected_slug, "title": "Client learning",
                "compiled_truth": f"Statement {index}.", "timeline": "## Timeline",
            }

        def get_page(self, slug):
            index = 1 if slug.endswith("1" * 16) else 2
            return {
                "source_id": "client-knowledge", "slug": slug, "title": "Client learning",
                "frontmatter": {
                    "project": "pid", "status": "current", "effective_at": "2026-08-07",
                    "updated_at": "synthesis-managed", "source_refs": ["notion:page:source"],
                    "supersedes": [], "confidence": "high", "sensitivity": "internal",
                },
                "compiled_truth": f"Statement {index}.", "timeline": "",
            }

    store = Store()
    worker = _worker(store, Client())
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    for index in range(1, 3):
        worker.process_claim(JobClaim(
            str(index) * 32, "artifact", "synthesized", "token", 1, "host", 99, 1
        ))
    assert expected_heads[0] == first_head
    assert expected_heads[1] != first_head
    assert expected_heads[1] == subprocess.run(
        ["git", "rev-parse", "HEAD^"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()


def test_publication_recovers_after_cas_before_materialization(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

    class Recorder:
        def __init__(self):
            self.rows = []

        def record_publication(self, **kwargs):
            self.rows.append(kwargs)

    recorder = Recorder()
    publisher = GitSourcePublisher(Client(), project_key="pid", store=recorder)
    original = publisher._materialize_commit
    monkeypatch.setattr(
        publisher,
        "_materialize_commit",
        lambda **_kwargs: (_ for _ in ()).throw(PublicationFailure("simulated_crash")),
    )
    with publisher:
        with pytest.raises(PublicationFailure, match="simulated_crash"):
            publisher.publish(
                artifact_id="a" * 64,
                assimilation_id="s" * 64,
                assimilation_version="v1",
                proposal_sha256="p" * 64,
                expected_head=expected_head,
                authored_at=1786089600,
                files=[PublicationFile("learnings/recovery", b"recovered\n")],
                trailer_label="Synthesis",
            )
    committed_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()
    assert committed_head != expected_head
    assert not (root / "projects" / "pid" / "learnings" / "recovery.md").exists()

    recovered = GitSourcePublisher(Client(), project_key="pid", store=recorder)
    monkeypatch.setattr(recovered, "_materialize_commit", original.__get__(recovered))
    with recovered:
        result = recovered.publish(
            artifact_id="a" * 64,
            assimilation_id="s" * 64,
            assimilation_version="v1",
            proposal_sha256="p" * 64,
            expected_head=expected_head,
            authored_at=1786089600,
            files=[PublicationFile("learnings/recovery", b"recovered\n")],
            trailer_label="Synthesis",
        )
    assert result.commit_sha == committed_head
    assert (root / "projects" / "pid" / "learnings" / "recovery.md").read_bytes() == b"recovered\n"
    assert recorder.rows[-1]["state"] == "committed"


def test_committed_publication_verifies_after_later_branch_advance(tmp_path):
    root = _repo(tmp_path)
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

    first_file = PublicationFile("learnings/first", b"first\n")
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        first = publisher.publish(
            artifact_id="a" * 64, assimilation_id="1" * 64,
            assimilation_version="v1", proposal_sha256="p" * 64,
            expected_head=first_head, authored_at=1786089600,
            files=[first_file], trailer_label="Synthesis",
        )
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        publisher.publish(
            artifact_id="b" * 64, assimilation_id="2" * 64,
            assimilation_version="v1", proposal_sha256="q" * 64,
            expected_head=first.commit_sha, authored_at=1786089601,
            files=[PublicationFile(
                "learnings/second",
                b"second\n",
            )],
            trailer_label="Synthesis",
        )
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        verified = publisher.verify_committed(
            expected_head=first_head,
            commit_sha=first.commit_sha,
            manifest_json=first.manifest_json,
            files=[first_file],
        )
    assert verified.commit_sha == first.commit_sha


def test_prepared_publication_adopts_ancestor_after_later_branch_advance(tmp_path):
    root = _repo(tmp_path)
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

    first_file = PublicationFile("learnings/first", b"first\n")
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        first = publisher.publish(
            artifact_id="a" * 64, assimilation_id="1" * 64,
            assimilation_version="v1", proposal_sha256="p" * 64,
            expected_head=first_head, authored_at=1786089600,
            files=[first_file], trailer_label="Synthesis",
        )
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        publisher.publish(
            artifact_id="b" * 64, assimilation_id="2" * 64,
            assimilation_version="v1", proposal_sha256="q" * 64,
            expected_head=first.commit_sha, authored_at=1786089601,
            files=[PublicationFile("learnings/second", b"second\n")],
            trailer_label="Synthesis",
        )
    with GitSourcePublisher(Client(), project_key="pid") as publisher:
        adopted = publisher.publish(
            artifact_id="a" * 64, assimilation_id="1" * 64,
            assimilation_version="v1", proposal_sha256="p" * 64,
            expected_head=first_head, authored_at=1786089600,
            files=[first_file], trailer_label="Synthesis",
        )
    assert adopted.commit_sha == first.commit_sha


def test_prepared_cas_loser_rebases_after_unrelated_winner(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

    class Recorder:
        def __init__(self):
            self.row = None

        def record_publication(self, **kwargs):
            identity = tuple(kwargs[key] for key in (
                "artifact_id", "assimilation_version", "proposal_sha256",
                "branch_ref", "expected_head", "manifest_json",
            ))
            if self.row is not None:
                existing = tuple(self.row[key] for key in (
                    "artifact_id", "assimilation_version", "proposal_sha256",
                    "branch_ref", "expected_head", "manifest_json",
                ))
                assert existing == identity
            self.row = {**(self.row or {}), **kwargs}

        def reset_publication(self, **kwargs):
            if (
                self.row is None
                or self.row["state"] != "prepared"
                or self.row.get("commit_sha")
                or self.row["expected_head"] != kwargs["old_expected_head"]
                or self.row["manifest_json"] != kwargs["old_manifest_json"]
            ):
                return False
            self.row.update({
                "expected_head": kwargs["new_expected_head"],
                "manifest_json": kwargs["new_manifest_json"],
            })
            return True

    recorder = Recorder()
    loser = GitSourcePublisher(Client(), project_key="pid", store=recorder)
    original_git = loser._git
    raced = False

    def race_update_ref(*args, **kwargs):
        nonlocal raced
        if args and args[0] == "update-ref" and not raced:
            raced = True
            with GitSourcePublisher(Client(), project_key="pid") as winner:
                winner.publish(
                    artifact_id="b" * 64,
                    assimilation_id="2" * 64,
                    assimilation_version="v1",
                    proposal_sha256="q" * 64,
                    expected_head=first_head,
                    authored_at=1786089601,
                    files=[PublicationFile("learnings/winner", b"winner\n")],
                    trailer_label="Synthesis",
                )
        return original_git(*args, **kwargs)

    monkeypatch.setattr(loser, "_git", race_update_ref)
    with loser:
        with pytest.raises(PublicationFailure, match="git_cas_failed"):
            loser.publish(
                artifact_id="a" * 64,
                assimilation_id="1" * 64,
                assimilation_version="v1",
                proposal_sha256="p" * 64,
                expected_head=first_head,
                authored_at=1786089600,
                files=[PublicationFile("learnings/loser", b"loser\n")],
                trailer_label="Synthesis",
            )
    assert recorder.row["state"] == "prepared"
    assert recorder.row["expected_head"] == first_head

    with GitSourcePublisher(Client(), project_key="pid", store=recorder) as retry:
        result = retry.publish(
            artifact_id="a" * 64,
            assimilation_id="1" * 64,
            assimilation_version="v1",
            proposal_sha256="p" * 64,
            expected_head=first_head,
            authored_at=1786089600,
            files=[PublicationFile("learnings/loser", b"loser\n")],
            trailer_label="Synthesis",
        )
    assert recorder.row["state"] == "committed"
    assert recorder.row["expected_head"] != first_head
    assert recorder.row["commit_sha"] == result.commit_sha
    assert (root / "projects" / "pid" / "learnings" / "winner.md").read_bytes() == b"winner\n"
    assert (root / "projects" / "pid" / "learnings" / "loser.md").read_bytes() == b"loser\n"


def test_prepared_cas_loser_does_not_rebase_across_changed_target_blob(tmp_path):
    root = _repo(tmp_path)
    first_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, text=True, capture_output=True
    ).stdout.strip()

    class Client:
        settings = SimpleNamespace(
            source_branch="main", source_checkout=root, timeout_seconds=10,
        )

        def assert_source_checkout(self):
            return root

    class Recorder:
        def __init__(self):
            self.row = None
            self.reset_called = False

        def record_publication(self, **kwargs):
            if self.row is None:
                self.row = dict(kwargs)

        def reset_publication(self, **_kwargs):
            self.reset_called = True
            return True

    recorder = Recorder()
    target = PublicationFile("learnings/shared", b"loser\n")
    with GitSourcePublisher(Client(), project_key="pid", store=recorder) as prepared:
        prepared.store.record_publication(
            assimilation_id="1" * 64,
            artifact_id="a" * 64,
            assimilation_version="v1",
            proposal_sha256="p" * 64,
            branch_ref=prepared.branch_ref,
            expected_head=first_head,
            manifest_json=json.dumps(
                prepared._manifest([target], first_head)[0],
                sort_keys=True,
                separators=(",", ":"),
            ),
            state="prepared",
        )
    with GitSourcePublisher(Client(), project_key="pid") as winner:
        winner.publish(
            artifact_id="b" * 64,
            assimilation_id="2" * 64,
            assimilation_version="v1",
            proposal_sha256="q" * 64,
            expected_head=first_head,
            authored_at=1786089601,
            files=[PublicationFile("learnings/shared", b"winner\n")],
            trailer_label="Synthesis",
        )
    with GitSourcePublisher(Client(), project_key="pid", store=recorder) as retry:
        with pytest.raises(PublicationFailure, match="publication_prior_hash_mismatch"):
            retry.publish(
                artifact_id="a" * 64,
                assimilation_id="1" * 64,
                assimilation_version="v1",
                proposal_sha256="p" * 64,
                expected_head=first_head,
                authored_at=1786089600,
                files=[target],
                trailer_label="Synthesis",
            )
    assert recorder.reset_called is False
    assert recorder.row["expected_head"] == first_head
    assert (root / "projects" / "pid" / "learnings" / "shared.md").read_bytes() == b"winner\n"
