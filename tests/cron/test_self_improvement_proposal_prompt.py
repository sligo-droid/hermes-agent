from cron.scheduler import _build_job_prompt
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
