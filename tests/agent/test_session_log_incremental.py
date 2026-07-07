import json
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from run_agent import AIAgent


def _agent(tmp_path, session_id="session-log-incremental"):
    agent = AIAgent.__new__(AIAgent)
    agent.logs_dir = tmp_path
    agent.session_id = session_id
    agent.session_log_file = tmp_path / f"session_{session_id}.json"
    agent.session_start = datetime(2026, 1, 1, 0, 0, 0)
    agent.model = "test-model"
    agent.base_url = "https://example.invalid/v1"
    agent.platform = "test"
    agent._cached_system_prompt = ""
    agent.tools = []
    agent._session_messages = []
    agent.verbose_logging = False
    agent._session_log_written_count = -1
    agent._session_log_written_path = None
    agent._session_log_written_stat = None
    return agent


def _messages(count):
    return [
        {"role": "user" if idx % 2 == 0 else "assistant", "content": f"message {idx}"}
        for idx in range(count)
    ]


def _write_log(path, count):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "existing",
                "message_count": count,
                "messages": _messages(count),
            }
        ),
        encoding="utf-8",
    )


def _read_log(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_fresh_process_performs_read_back_guard(tmp_path):
    agent = _agent(tmp_path)
    _write_log(agent.session_log_file, 1)

    with patch.object(Path, "read_text", autospec=True, wraps=Path.read_text) as read_text:
        agent._save_session_log(_messages(1))

    assert read_text.call_count == 1
    assert agent._session_log_written_count == 1
    assert agent._session_log_written_path == agent.session_log_file


def test_same_path_save_skips_read_back_but_still_writes(tmp_path):
    agent = _agent(tmp_path)
    _write_log(agent.session_log_file, 1)

    with (
        patch.object(Path, "read_text", autospec=True, wraps=Path.read_text) as read_text,
        patch("run_agent.atomic_json_write") as atomic_json_write,
    ):
        agent._save_session_log(_messages(1))
        changed_same_count = [{"role": "user", "content": "changed content"}]
        agent._save_session_log(changed_same_count)

    assert read_text.call_count == 1
    assert atomic_json_write.call_count == 2
    second_payload = atomic_json_write.call_args_list[1].args[1]
    assert second_payload["messages"] == changed_same_count
    assert second_payload["message_count"] == 1


def test_path_change_reenables_read_back_guard(tmp_path):
    agent = _agent(tmp_path / "old")
    agent._save_session_log(_messages(1))

    new_logs_dir = tmp_path / "new"
    new_log_file = new_logs_dir / f"session_{agent.session_id}.json"
    _write_log(new_log_file, 1)
    agent.logs_dir = new_logs_dir

    with patch.object(Path, "read_text", autospec=True, wraps=Path.read_text) as read_text:
        agent._save_session_log(_messages(1))

    assert read_text.call_count == 1
    assert agent.session_log_file == new_log_file
    assert agent._session_log_written_path == new_log_file


def test_fresh_process_guard_prevents_clobbering_fuller_log(tmp_path):
    session_id = "resume-session"
    agent_a = _agent(tmp_path, session_id=session_id)
    agent_a._save_session_log(_messages(5))

    agent_b = _agent(tmp_path, session_id=session_id)
    agent_b._save_session_log(_messages(2))

    data = _read_log(agent_a.session_log_file)
    assert data["message_count"] == 5
    assert len(data["messages"]) == 5
    assert agent_b._session_log_written_count == -1


def test_external_writer_reenables_guard_and_prevents_clobber(tmp_path):
    agent = _agent(tmp_path)
    agent._save_session_log(_messages(2))

    # Another process (e.g. restart-handoff overlap) writes a FULLER log to
    # the same path after our last write: the stat fingerprint must force the
    # read-back guard, which then refuses to clobber the fuller log.
    _write_log(agent.session_log_file, 6)

    with patch.object(Path, "read_text", autospec=True, wraps=Path.read_text) as read_text:
        agent._save_session_log(_messages(3))

    assert read_text.call_count == 1
    data = _read_log(agent.session_log_file)
    assert data["message_count"] == 6
    assert len(data["messages"]) == 6
