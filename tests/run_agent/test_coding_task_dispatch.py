from __future__ import annotations

import json

from run_agent import AIAgent
from tools import coding_worker_tool as cwt


def test_dispatch_coding_task_forwards_route_decision(monkeypatch):
    captured = {}

    def fake_delegate_coding_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True})

    monkeypatch.setattr(cwt, "delegate_coding_task", fake_delegate_coding_task)
    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        api_mode="chat_completions",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    route_decision = {
        "route": "ui_visual_specialist",
        "confidence": 0.91,
        "rationale": "visual implementation",
    }

    result = agent._dispatch_coding_task(
        {
            "task": "polish the AI budget dashboard",
            "context": "Focus on visual implementation.",
            "cwd": "/tmp/project",
            "turn_timeout_seconds": 600,
            "route_decision": route_decision,
        }
    )

    assert json.loads(result) == {"success": True}
    assert captured["route_decision"] is route_decision
    assert captured["parent_agent"] is agent
