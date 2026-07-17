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
    parallel_group = {"group_id": "group-1", "base_cwd": "/tmp/project"}

    result = agent._dispatch_coding_task(
        {
            "task": "polish the AI budget dashboard",
            "context": "Focus on visual implementation.",
            "cwd": "/tmp/project",
            "turn_timeout_seconds": 600,
            "worker_tier": "thorough",
            "relevant_files": [{"path": "src/app.py", "note": "entry point"}],
            "approach": "Keep the change local.",
            "constraints": "Do not change the API.",
            "verification": "Run the focused test.",
            "scope_paths": ["src/app.py"],
            "route_decision": route_decision,
            "_parallel_group": parallel_group,
        }
    )

    assert json.loads(result) == {"success": True}
    assert captured["worker_tier"] == "thorough"
    assert captured["relevant_files"] == [
        {"path": "src/app.py", "note": "entry point"}
    ]
    assert captured["approach"] == "Keep the change local."
    assert captured["constraints"] == "Do not change the API."
    assert captured["verification"] == "Run the focused test."
    assert captured["scope_paths"] == ["src/app.py"]
    assert captured["route_decision"] is route_decision
    assert captured["_parallel_group"] is parallel_group
    assert captured["parent_agent"] is agent
