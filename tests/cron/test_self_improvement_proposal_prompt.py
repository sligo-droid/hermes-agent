from cron.scheduler import _build_job_prompt
from self_improvement import proposal_storage
from self_improvement.proposals import CONTRACT_VERSION


def test_cron_job_prompt_includes_self_improvement_proposal_guidance():
    prompt = _build_job_prompt(
        {
            "id": "abc123def456",
            "name": "Daily PID admin dogfood UX bugfix",
            "prompt": "Review yesterday's PID admin dogfood notes.",
            "self_improvement_proposal": {
                "project": "pid",
                "prong": "admin_dogfood_ux_bugfix",
            },
        }
    )

    assert CONTRACT_VERSION in prompt
    assert "```json" in prompt
    assert "at most 5 proposal cards" in prompt
    assert "Do not create Kanban tasks" in prompt
    assert "Review yesterday's PID admin dogfood notes." in prompt


def test_cron_job_prompt_includes_scoped_feedback_context(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = {
        "contract_version": CONTRACT_VERSION,
        "project": "pid",
        "prong": "airflow_scraper_doctor",
        "run": {
            "run_id": "cron-run-1",
            "cron_job_id": "job-1",
            "created_at": "2026-06-04T02:00:00Z",
        },
        "generated_at": "2026-06-04T02:04:10Z",
        "human_markdown": "Two cards.",
        "cards": [
            {
                "idempotency_key": "accept-me",
                "title": "Accepted feedback card",
                "summary": "Approved idea to preserve.",
                "body": "Full accepted body should not be injected.",
                "rationale": "Useful.",
                "priority": "high",
                "status": "proposed",
                "created_at": "2026-06-04T02:04:10Z",
                "source_excerpts": [],
                "kanban_task": {"title": "Accepted task", "body": "Do it."},
            },
            {
                "idempotency_key": "reject-me",
                "title": "Rejected feedback card",
                "summary": "Rejected idea to avoid.",
                "body": "Full rejected body should not be injected.",
                "rationale": "Not useful.",
                "priority": "medium",
                "status": "proposed",
                "created_at": "2026-06-04T02:04:11Z",
                "source_excerpts": [],
                "kanban_task": {"title": "Rejected task", "body": "Do not do it."},
            },
        ],
    }
    import json

    proposal_storage.ingest_proposal_output(json.dumps(payload))
    cards = proposal_storage.grouped_cards()["projects"][0]["prongs"][0]["cards"]
    for card in cards:
        if card["title"].startswith("Accepted"):
            proposal_storage.record_approval(card["proposal_id"], kanban_task_id="t_accept", worker_url="/workers?task=t_accept")
        else:
            proposal_storage.record_rejection(card["proposal_id"], reason="not enough signal")

    prompt = _build_job_prompt(
        {
            "prompt": "Review PID scraper notes.",
            "self_improvement_proposal": {
                "project": "pid",
                "prong": "airflow_scraper_doctor",
            },
        }
    )

    assert "Recent Proposal Feedback" in prompt
    assert "Accepted feedback card" in prompt
    assert "t_accept" in prompt
    assert "Rejected feedback card" in prompt
    assert "not enough signal" in prompt
    assert "Full accepted body should not be injected" not in prompt
