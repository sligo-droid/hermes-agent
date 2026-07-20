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
    agent = object.__new__(AIAgent)
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
            "model_tier": "advanced",
            "reasoning_effort": "high",
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
    assert captured["model_tier"] == "advanced"
    assert captured["reasoning_effort"] == "high"
    assert "worker_tier" not in captured
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


def test_dispatch_coding_task_preserves_background_from_owned_preflight(monkeypatch, tmp_path):
    agent = object.__new__(AIAgent)
    agent.session_cwd = str(tmp_path)
    preflight = cwt.preflight_delegate_coding_task(
        {"task": "long change", "background": True},
        agent,
    )
    captured = {}

    def fake_dispatch(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True, "background": True})

    monkeypatch.setattr(cwt, "_dispatch_background_coding_task", fake_dispatch)

    result = agent._dispatch_coding_task(preflight.args)

    assert json.loads(result)["background"] is True
    assert captured["call_kwargs"]["route_decision"] is None
