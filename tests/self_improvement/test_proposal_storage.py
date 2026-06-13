import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from self_improvement import proposal_storage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "self_improvement"


def _fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _fenced(name: str) -> str:
    return "Human summary before JSON.\n```json\n" + _fixture_text(name) + "\n```"


def _valid_payload() -> dict:
    return json.loads(_fixture_text("proposal_run_pid_valid.json"))


def test_parse_payload_reads_entire_fenced_json_block_with_nested_values():
    source = """Human summary before JSON.
```json
{
  "schema": "self_improvement.proposal_run.v1",
  "cards": [
    {
      "evidence_basis": {
        "missing_live_evidence": [],
        "supporting_signals": [
          {"kind": "log", "details": {"nested": true}}
        ]
      },
      "source_excerpts": [
        {
          "path": "cron/logs/example.log",
          "lines": [1, 2],
          "excerpt": {"text": "nested object"}
        }
      ],
      "kanban_task": {
        "id": "task-1",
        "labels": ["self-improvement"],
        "metadata": {"priority": "normal"}
      }
    }
  ]
}
```
Human summary after JSON.
"""

    payload = proposal_storage._parse_payload(source)

    card = payload["cards"][0]
    assert card["evidence_basis"]["supporting_signals"][0]["details"]["nested"] is True
    assert card["source_excerpts"][0]["excerpt"]["text"] == "nested object"
    assert card["kanban_task"]["metadata"]["priority"] == "normal"


def test_parse_payload_skips_non_json_fences_before_payload():
    source = """Human summary before JSON.
```text
not json and not the proposal payload
```
```json
{"contract_version":"self_improvement.proposal_run.v1","cards":[]}
```
"""

    payload = proposal_storage._parse_payload(source)

    assert payload["contract_version"] == "self_improvement.proposal_run.v1"
    assert payload["cards"] == []


def test_parse_payload_prefers_later_proposal_fence_over_bad_candidates():
    source = """Human summary before JSON.
```json
{"not_the_proposal": true}
```
```json
{"not_the_proposal":
```
```json
{"contract_version":"self_improvement.proposal_run.v1","cards":[]}
```
"""

    payload = proposal_storage._parse_payload(source)

    assert payload["contract_version"] == "self_improvement.proposal_run.v1"
    assert payload["cards"] == []


def test_profile_safe_db_path_and_initialization(tmp_path, monkeypatch):
    home = tmp_path / "profile-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    proposal_storage.init_db()

    assert proposal_storage.proposals_db_path() == home / "self_improvement" / "proposals.db"
    assert proposal_storage.proposals_db_path().exists()


def test_ingest_valid_run_is_idempotent_and_replaces_cards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    first = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    second = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))

    assert first["status"] == "valid"
    assert first["run_id"] == second["run_id"]
    assert second["card_count"] == 1

    conn = proposal_storage.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM proposal_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM proposal_cards").fetchone()[0] == 1
    finally:
        conn.close()


def test_ingest_empty_run_records_empty_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    result = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_empty.json"))

    run = proposal_storage.get_run(result["run_id"])

    assert result["status"] == "empty"
    assert run["status"] == "empty"
    assert run["cards"] == []


def test_ingest_malformed_run_persists_parse_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    result = proposal_storage.ingest_proposal_output(
        _fenced("proposal_run_malformed.json"),
        source={"run_id": "run-1", "cron_output_path": "/tmp/out.md"},
    )
    failures = proposal_storage.list_parse_failures()

    assert result["status"] == "malformed"
    assert "run.cron_job_id" in result["parse_error"]
    assert len(failures["failures"]) == 1
    assert failures["failures"][0]["source_ref"]["cron_output_path"] == "/tmp/out.md"


def test_ingest_run_with_audit_metadata_uses_trusted_source_run_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = _valid_payload()
    payload["run"] = {
        "id": "audit-run-alias",
        "status": "completed",
        "delegated_workers": ["worker-1"],
    }
    payload["cards"][0]["idempotency_key"] = {"finding": "alias-shaped-card", "date": "2026-06-04"}
    payload["cards"][0]["priority"] = "P1"
    payload["cards"][0]["severity"] = "high"
    payload["cards"][0]["source_excerpts"] = [
        "plain string excerpt",
        {"excerpt": "dict excerpt", "path": "src/example.py", "lines": "3-5"},
    ]

    result = proposal_storage.ingest_proposal_output(
        "Human summary before JSON.\n```json\n" + json.dumps(payload) + "\n```",
        source={
            "run_id": "source-run-id",
            "cron_job_id": "job-from-source",
            "cron_job_name": "Source cron job",
            "cron_output_path": "/tmp/out.md",
            "generated_at": "2026-06-04T02:04:11Z",
        },
    )
    run = proposal_storage.get_run(result["run_id"])
    assert run is not None

    assert result["status"] == "valid"
    assert result["card_count"] == 1
    assert run["run_id"] == "source-run-id"
    assert run["cron_job_id"] == "job-from-source"
    assert run["created_at"] == "2026-06-04T02:04:11Z"
    card = run["cards"][0]
    assert card["proposal_id"]
    assert card["idempotency_key"] == '{"date":"2026-06-04","finding":"alias-shaped-card"}'
    assert card["priority"] == "critical"
    assert card["severity"] == "major"
    assert card["source_excerpts"][0]["text"] == "plain string excerpt"
    assert card["source_excerpts"][1]["text"] == "dict excerpt"
    assert card["source_excerpts"][1]["label"] == "src/example.py"
    assert card["source_excerpts"][1]["line_start"] == 3
    assert card["source_excerpts"][1]["line_end"] == 5


def test_ingest_run_id_alias_is_used_when_source_has_no_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = _valid_payload()
    payload["run"] = {"id": "audit-run-alias"}

    result = proposal_storage.ingest_proposal_output(
        "Human summary before JSON.\n```json\n" + json.dumps(payload) + "\n```",
        source={
            "cron_job_id": "job-from-source",
            "generated_at": "2026-06-04T02:04:11Z",
        },
    )
    run = proposal_storage.get_run(result["run_id"])
    assert run is not None

    assert result["status"] == "valid"
    assert run["run_id"] == "audit-run-alias"


def test_ingest_run_id_alias_keeps_distinct_source_keys_without_source_run_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload_one = _valid_payload()
    payload_one["run"] = {"id": "alias-run-one"}
    payload_two = _valid_payload()
    payload_two["run"] = {"id": "alias-run-two"}
    source = {"cron_job_id": "same-job", "generated_at": "2026-06-04T02:04:11Z"}

    first = proposal_storage.ingest_proposal_output(json.dumps(payload_one), source=source)
    second = proposal_storage.ingest_proposal_output(json.dumps(payload_two), source=source)

    assert first["run_id"] != second["run_id"]
    conn = proposal_storage.connect()
    try:
        rows = conn.execute("SELECT source_key FROM proposal_runs ORDER BY id").fetchall()
    finally:
        conn.close()
    assert [row["source_key"] for row in rows] == ["same-job:alias-run-one", "same-job:alias-run-two"]


def test_ingest_run_missing_metadata_stays_malformed_when_source_is_insufficient(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = _valid_payload()
    payload["run"] = {"status": "completed"}

    result = proposal_storage.ingest_proposal_output(
        "Human summary before JSON.\n```json\n" + json.dumps(payload) + "\n```",
        source={"cron_job_id": "job-from-source", "cron_output_path": "/tmp/out.md"},
    )

    assert result["status"] == "malformed"
    assert "run.run_id" in result["parse_error"]


def test_grouped_reads_card_details_and_run_source(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    result = proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))

    grouped = proposal_storage.grouped_cards()
    prong = grouped["projects"][0]["prongs"][0]
    card = prong["cards"][0]
    detail = proposal_storage.get_card(card["proposal_id"])
    run = proposal_storage.get_run(result["run_id"])

    assert grouped["projects"][0]["project"] == "pid"
    assert prong["prong"] == "airflow_scraper_doctor"
    assert detail["kanban_task"]["title"] == "Instrument PID scraper timeout retry backoff"
    assert run["source_markdown"].startswith("Human summary before JSON")
    assert run["cards"][0]["proposal_id"] == card["proposal_id"]


def test_dashboard_read_only_routes_return_proposal_shapes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    proposal_storage.ingest_proposal_output(
        "not json",
        source={"source_key": "bad-output", "cron_output_path": "/tmp/bad.md"},
    )

    from plugins.kanban.dashboard.plugin_api import router

    app = FastAPI()
    app.include_router(router, prefix="/api/plugins/kanban")
    client = TestClient(app)

    grouped = client.get("/api/plugins/kanban/self-improvement/proposals")
    assert grouped.status_code == 200
    card = grouped.json()["projects"][0]["prongs"][0]["cards"][0]

    detail = client.get(f"/api/plugins/kanban/self-improvement/proposals/{card['proposal_id']}")
    assert detail.status_code == 200
    assert detail.json()["card"]["proposal_id"] == card["proposal_id"]

    run = client.get(f"/api/plugins/kanban/self-improvement/runs/{card['run_db_id']}")
    assert run.status_code == 200
    assert run.json()["run"]["source_markdown"]

    failures = client.get("/api/plugins/kanban/self-improvement/parse-failures")
    assert failures.status_code == 200
    assert failures.json()["failures"][0]["source_ref"]["cron_output_path"] == "/tmp/bad.md"

    snapshot = client.get("/api/plugins/kanban/command-center/snapshot")
    assert snapshot.status_code == 200
    payload = snapshot.json()
    assert payload["schema_version"] == 1
    assert payload["summary"].startswith("Sources create canonical Work Items")
    assert payload["work_items"][0]["source"]["kind"] == "self_improvement"
    assert payload["work_items"][0]["source"]["id"].startswith("source:self-improvement-proposal:")


def test_approval_and_rejection_state_are_persisted_and_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    card = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"][0]

    approved = proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id="t_123",
        worker_url="/workers?task=t_123",
        actor="operator",
    )

    assert approved["status"] == "approved"
    assert approved["kanban_task_id"] == "t_123"
    assert approved["worker_url"] == "/workers?task=t_123"
    events = proposal_storage.list_audit_events(card["proposal_id"])
    assert events[0]["action"] == "approved"
    assert events[0]["actor"] == "operator"

    rejected = proposal_storage.record_rejection(card["proposal_id"], reason="too broad", actor="operator")
    assert rejected["status"] == "rejected"
    assert rejected["rejected_reason"] == "too broad"
    assert proposal_storage.grouped_cards()["projects"] == []
    events = proposal_storage.list_audit_events(card["proposal_id"])
    assert [event["action"] for event in events] == ["approved", "rejected"]


def test_reingesting_approved_card_preserves_review_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = _fenced("proposal_run_pid_valid.json")
    proposal_storage.ingest_proposal_output(source)
    card = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"][0]
    approved = proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id="t_approved",
        worker_url="/workers?task=t_approved",
        actor="operator",
    )

    proposal_storage.ingest_proposal_output(source)

    reingested = proposal_storage.get_card(card["proposal_id"])
    assert reingested["status"] == "approved"
    assert reingested["kanban_task_id"] == "t_approved"
    assert reingested["worker_url"] == "/workers?task=t_approved"
    assert reingested["run_db_id"] == approved["run_db_id"]
    assert proposal_storage.list_audit_events(card["proposal_id"])[0]["action"] == "approved"
    summary = proposal_storage.summarize_feedback_history(project="pid", prong="airflow_scraper_doctor")
    assert summary["projects"][0]["prongs"][0]["accepted"][0]["kanban_task_id"] == "t_approved"


def test_reingesting_rejected_card_preserves_review_state_and_hidden_grouping(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    source = _fenced("proposal_run_pid_valid.json")
    proposal_storage.ingest_proposal_output(source)
    card = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"][0]
    rejected = proposal_storage.record_rejection(card["proposal_id"], reason="not actionable", actor="operator")

    proposal_storage.ingest_proposal_output(source)

    reingested = proposal_storage.get_card(card["proposal_id"])
    assert reingested["status"] == "rejected"
    assert reingested["rejected_reason"] == "not actionable"
    assert reingested["archived_at"] == rejected["archived_at"]
    assert reingested["run_db_id"] == rejected["run_db_id"]
    assert proposal_storage.grouped_cards()["projects"] == []
    assert proposal_storage.list_audit_events(card["proposal_id"])[0]["action"] == "rejected"
    summary = proposal_storage.summarize_feedback_history(project="pid", prong="airflow_scraper_doctor")
    assert summary["projects"][0]["prongs"][0]["rejected"][0]["reason"] == "not actionable"


def test_feedback_summary_empty_history(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))

    summary = proposal_storage.summarize_feedback_history(project="pid", prong="airflow_scraper_doctor")

    assert summary["projects"] == []


def test_feedback_summary_groups_filters_and_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = json.loads(_fixture_text("proposal_run_pid_valid.json"))
    payload["cards"] = []
    for idx in range(3):
        card = json.loads(_fixture_text("proposal_run_pid_valid.json"))["cards"][0]
        card["idempotency_key"] = f"accepted-{idx}"
        card["title"] = f"Accepted card {idx}"
        card["summary"] = "Accepted summary " + ("x" * 80)
        payload["cards"].append(card)
    for idx in range(2):
        card = json.loads(_fixture_text("proposal_run_pid_valid.json"))["cards"][0]
        card["idempotency_key"] = f"rejected-{idx}"
        card["title"] = f"Rejected card {idx}"
        card["summary"] = "Rejected summary " + ("y" * 80)
        payload["cards"].append(card)
    proposal_storage.ingest_proposal_output(json.dumps(payload))
    cards = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"]

    for card in cards[:3]:
        proposal_storage.record_approval(card["proposal_id"], kanban_task_id=f"t_{card['title'][-1]}", worker_url="/workers")
    for card in cards[3:]:
        proposal_storage.record_rejection(card["proposal_id"], reason="duplicate and too broad " + ("z" * 80))

    summary = proposal_storage.summarize_feedback_history(
        project="pid",
        prong="airflow_scraper_doctor",
        max_items_per_kind=2,
        max_text_chars=40,
    )
    prong = summary["projects"][0]["prongs"][0]

    assert summary["projects"][0]["project"] == "pid"
    assert prong["prong"] == "airflow_scraper_doctor"
    assert len(prong["accepted"]) == 2
    assert len(prong["rejected"]) == 2
    assert prong["accepted"][0]["outcome"] == "accepted"
    assert prong["rejected"][0]["outcome"] == "rejected"
    assert len(prong["accepted"][0]["summary"]) <= 40
    assert len(prong["rejected"][0]["reason"]) <= 40

    other = proposal_storage.summarize_feedback_history(project="pid", prong="admin_dogfood_ux_bugfix")
    assert other["projects"] == []


def test_feedback_context_formatter_is_compact(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    proposal_storage.ingest_proposal_output(_fenced("proposal_run_pid_valid.json"))
    card = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"][0]
    proposal_storage.record_rejection(card["proposal_id"], reason="already handled elsewhere")

    context = proposal_storage.format_feedback_history_context(
        proposal_storage.summarize_feedback_history(project="pid", prong="airflow_scraper_doctor")
    )

    assert "Recent Proposal Feedback" in context
    assert "Rejected recently" in context
    assert "already handled elsewhere" in context
    assert card["body"] not in context


def test_json_fence_parser_accepts_plain_json(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = json.loads(_fixture_text("proposal_run_pid_empty.json"))

    result = proposal_storage.ingest_proposal_output(json.dumps(payload))

    assert result["status"] == "empty"
