from gateway.session_context import clear_session_vars, set_session_vars
from tools.terminal_tool import _get_env_config


def test_terminal_config_prefers_gateway_session_cwd(monkeypatch, tmp_path):
    env_cwd = tmp_path / "env"
    session_cwd = tmp_path / "session"
    env_cwd.mkdir()
    session_cwd.mkdir()
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(env_cwd))

    tokens = set_session_vars(session_cwd=str(session_cwd))
    try:
        config = _get_env_config()
    finally:
        clear_session_vars(tokens)

    assert config["cwd"] == str(session_cwd)
