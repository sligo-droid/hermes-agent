import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.conversation_loop import _new_turn_runtime_stats
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent(tmp_path) -> AIAgent:
    dashboard = tmp_path / "dashboard"
    dashboard.mkdir()
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (dashboard / "package.json").write_text(
        json.dumps({
            "scripts": {
                "dev": "vite dev",
                "preview": "vite preview",
                "qa:auth": "node scripts/authenticated-qa-smoke.mjs",
            }
        })
    )
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("terminal", "process", "browser_navigate"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent.session_cwd = str(tmp_path)
    agent.terminal_cwd = str(tmp_path)
    agent.visual_qa_requirement = {"level": "none", "target": "", "assertions": []}
    agent._turn_runtime_stats = _new_turn_runtime_stats(time.perf_counter())
    return agent


def _tool_call(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _execute(agent: AIAgent, name: str, args: dict) -> list[dict]:
    call = _tool_call(name, args)
    messages: list[dict] = []
    agent._execute_tool_calls_sequential(
        SimpleNamespace(content="", tool_calls=[call]),
        messages,
        "task-preview",
    )
    return messages


def test_runtime_blocks_live_equivalent_retries_but_allows_native_qa_and_closeout(
    tmp_path,
):
    agent = _make_agent(tmp_path)
    executed: list[tuple[str, str]] = []

    def fake_handle(name, args, task_id, **kwargs):
        executed.append((name, str(args.get("command") or args.get("action") or "")))
        if name == "terminal" and args.get("background"):
            return json.dumps({
                "output": "Background process started",
                "session_id": "proc_live",
                "exit_code": 0,
            })
        if name == "process":
            return json.dumps({
                "status": "running",
                "session_id": "proc_live",
                "output_preview": "outside of Vite serving allow list",
            })
        return json.dumps({"output": "ok", "exit_code": 0})

    workdir = str(tmp_path)
    with (
        patch("run_agent.handle_function_call", side_effect=fake_handle) as handler,
        patch(
            "tools.process_registry.process_registry.kill_process",
            return_value={"status": "killed"},
        ) as kill,
    ):
        _execute(
            agent,
            "terminal",
            {
                "command": "pnpm --dir dashboard dev --host 127.0.0.1 --port 5174",
                "background": True,
                "workdir": workdir,
            },
        )
        process_messages = _execute(
            agent, "process", {"action": "poll", "session_id": "proc_live"}
        )
        blocked_messages = _execute(
            agent,
            "terminal",
            {
                "command": "pnpm --dir dashboard preview --host 127.0.0.1 --port 5174",
                "background": True,
                "workdir": workdir,
            },
        )
        native_messages = _execute(
            agent,
            "terminal",
            {"command": "pnpm --dir dashboard qa:auth", "workdir": workdir},
        )
        closeout_messages = _execute(
            agent,
            "terminal",
            {
                "command": "bash scripts/local_lifecycle/closeout.sh --source .",
                "workdir": workdir,
            },
        )

    kill.assert_called_once()
    assert handler.call_count == 4
    assert executed == [
        ("terminal", "pnpm --dir dashboard dev --host 127.0.0.1 --port 5174"),
        ("process", "poll"),
        ("terminal", "pnpm --dir dashboard qa:auth"),
        ("terminal", "bash scripts/local_lifecycle/closeout.sh --source ."),
    ]
    assert "vite_fs_allow" in process_messages[0]["content"]
    assert "preview_local_budget_exhausted" in blocked_messages[0]["content"]
    assert "preview_readiness" in native_messages[0]["content"]
    assert json.loads(closeout_messages[0]["content"])["exit_code"] == 0
    assert agent._tool_guardrail_halt_decision is None
    assert len(agent._turn_runtime_stats["preview_readiness_events"]) <= 6
