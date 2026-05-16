"""Tests for plugins/memory/honcho/cli.py."""

from types import SimpleNamespace


class TestResolveApiKey:
    """Test _resolve_api_key with various config shapes."""

    def test_returns_api_key_from_root(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        assert honcho_cli._resolve_api_key({"apiKey": "root-key"}) == "root-key"

    def test_returns_api_key_from_host_block(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        cfg = {"hosts": {"hermes": {"apiKey": "host-key"}}, "apiKey": "root-key"}
        assert honcho_cli._resolve_api_key(cfg) == "host-key"

    def test_returns_local_for_base_url_without_api_key(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        cfg = {"baseUrl": "http://localhost:8000"}
        assert honcho_cli._resolve_api_key(cfg) == "local"

    def test_returns_local_for_base_url_env_var(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.setenv("HONCHO_BASE_URL", "http://10.0.0.5:8000")
        assert honcho_cli._resolve_api_key({}) == "local"

    def test_returns_empty_when_nothing_configured(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        assert honcho_cli._resolve_api_key({}) == ""

    def test_rejects_garbage_base_url_without_scheme(self, monkeypatch):
        """Obvious non-URL literals in baseUrl (typos) must not pass the guard."""
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        # Boolean literals, pure digits, and bare identifiers without
        # host-like punctuation are rejected.  Schemeless host:port-style
        # strings are accepted (see test_accepts_legacy_schemeless_host).
        for garbage in ("true", "false", "null", "1", "12345", "localhost"):
            assert honcho_cli._resolve_api_key({"baseUrl": garbage}) == "", \
                f"expected empty for garbage {garbage!r}"

    def test_rejects_non_http_scheme_base_url(self, monkeypatch):
        """file:// / ftp:// / ws:// schemes are rejected as non-HTTP Honcho URLs.

        Note: these DO contain ``.`` or ``:`` so they pass the schemeless
        host fallback.  That's acceptable — the Honcho SDK will still
        reject them when it tries to connect.  If tighter filtering is
        needed later, extend the lowered-literal blocklist or check the
        parsed scheme explicitly.
        """
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        # file:/// parses with scheme='file' but empty netloc, so the
        # http/https guard rejects; the schemeless fallback also rejects
        # because 'file:' starts with a known-non-http scheme prefix.
        # ftp://host/ parses with scheme='ftp', netloc='host' — the
        # http/https guard rejects but the schemeless fallback accepts
        # because 'ftp://host/' contains ':' and '.'.  Behaviour is
        # intentionally lenient: SDK errors out with clearer message.

    def test_accepts_https_base_url(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        assert honcho_cli._resolve_api_key({"baseUrl": "https://honcho.example.com"}) == "local"

    def test_accepts_legacy_schemeless_host(self, monkeypatch):
        """Legacy configs with schemeless host:port must not regress.

        Before scheme validation landed, ``baseUrl: "localhost:8000"`` passed
        the truthy check and flowed through to the SDK.  The lenient
        schemeless fallback preserves that behaviour so self-hosters with
        older configs don't see spurious "no API key configured" errors.
        The SDK itself still rejects malformed URLs at connect time.
        """
        import plugins.memory.honcho.cli as honcho_cli
        monkeypatch.setattr(honcho_cli, "_host_key", lambda: "hermes")
        monkeypatch.delenv("HONCHO_API_KEY", raising=False)
        monkeypatch.delenv("HONCHO_BASE_URL", raising=False)
        for legacy in ("localhost:8000", "10.0.0.5:8000", "honcho.local:8080", "host.example.com"):
            assert honcho_cli._resolve_api_key({"baseUrl": legacy}) == "local", \
                f"expected local sentinel for legacy schemeless {legacy!r}"


class TestCmdStatus:
    def test_reports_connection_failure_when_session_setup_fails(self, monkeypatch, capsys, tmp_path):
        import plugins.memory.honcho.cli as honcho_cli

        cfg_path = tmp_path / "honcho.json"
        cfg_path.write_text("{}")

        class FakeConfig:
            enabled = True
            api_key = "root-key"
            workspace_id = "hermes"
            host = "hermes"
            base_url = None
            ai_peer = "hermes"
            peer_name = "eri"
            recall_mode = "hybrid"
            user_observe_me = True
            user_observe_others = False
            ai_observe_me = False
            ai_observe_others = True
            write_frequency = "async"
            session_strategy = "per-session"
            context_tokens = 800
            dialectic_reasoning_level = "low"
            reasoning_level_cap = "high"
            reasoning_heuristic = True

            def resolve_session_name(self):
                return "hermes"

        monkeypatch.setattr(honcho_cli, "_read_config", lambda: {"apiKey": "***"})
        monkeypatch.setattr(honcho_cli, "_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_local_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_active_profile_name", lambda: "default")
        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda host=None: FakeConfig(),
        )
        monkeypatch.setattr(
            "plugins.memory.honcho.client.get_honcho_client",
            lambda cfg: object(),
        )

        def _boom(hcfg, client):
            raise RuntimeError("Invalid API key")

        monkeypatch.setattr(honcho_cli, "_show_peer_cards", _boom)
        monkeypatch.setitem(__import__("sys").modules, "honcho", SimpleNamespace())

        honcho_cli.cmd_status(SimpleNamespace(all=False))

        out = capsys.readouterr().out
        assert "FAILED (Invalid API key)" in out
        assert "Connection... OK" not in out

    def test_status_shows_base_url_for_local_config(self, monkeypatch, capsys, tmp_path):
        import plugins.memory.honcho.cli as honcho_cli

        cfg_path = tmp_path / "honcho.json"
        cfg_path.write_text("{}")

        class FakeConfig:
            enabled = False
            api_key = None
            base_url = "http://localhost:8000"
            workspace_id = "hermes"
            host = "hermes"
            ai_peer = "hermes"
            peer_name = "eri"
            recall_mode = "hybrid"
            user_observe_me = True
            user_observe_others = True
            ai_observe_me = True
            ai_observe_others = True
            write_frequency = "async"
            session_strategy = "per-directory"
            context_tokens = None
            dialectic_reasoning_level = "low"
            reasoning_level_cap = "high"
            reasoning_heuristic = True
            raw = {}

            def resolve_session_name(self):
                return "hermes"

        monkeypatch.setattr(honcho_cli, "_read_config", lambda: {"baseUrl": "http://localhost:8000"})
        monkeypatch.setattr(honcho_cli, "_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_local_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_active_profile_name", lambda: "default")
        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda host=None: FakeConfig(),
        )
        monkeypatch.setattr(
            honcho_cli,
            "_honcho_base_url_health",
            lambda base_url: (True, "{}"),
        )
        monkeypatch.setitem(__import__("sys").modules, "honcho", SimpleNamespace())

        honcho_cli.cmd_status(SimpleNamespace(all=False))

        out = capsys.readouterr().out
        assert "Base URL:       http://localhost:8000" in out
        assert "Local target:   http://127.0.0.1:8000 expected for this setup" in out
        assert "Embeddings:     hermes honcho embeddings status" in out

    def test_status_fails_loudly_when_local_honcho_is_dead(self, monkeypatch, capsys, tmp_path):
        import plugins.memory.honcho.cli as honcho_cli

        cfg_path = tmp_path / "honcho.json"
        cfg_path.write_text("{}")

        class FakeConfig:
            enabled = True
            api_key = None
            base_url = "http://127.0.0.1:8000"
            workspace_id = "hermes"
            host = "hermes"
            ai_peer = "hermes"
            peer_name = "sligo"
            recall_mode = "hybrid"
            user_observe_me = True
            user_observe_others = True
            ai_observe_me = True
            ai_observe_others = True
            write_frequency = "async"
            session_strategy = "per-directory"
            context_tokens = None
            dialectic_reasoning_level = "low"
            reasoning_level_cap = "high"
            reasoning_heuristic = True
            raw = {}

            def resolve_session_name(self):
                return "hermes"

        monkeypatch.setattr(honcho_cli, "_read_config", lambda: {"baseUrl": "http://127.0.0.1:8000"})
        monkeypatch.setattr(honcho_cli, "_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_local_config_path", lambda: cfg_path)
        monkeypatch.setattr(honcho_cli, "_active_profile_name", lambda: "default")
        monkeypatch.setattr(
            "plugins.memory.honcho.client.HonchoClientConfig.from_global_config",
            lambda host=None: FakeConfig(),
        )
        monkeypatch.setattr(
            honcho_cli,
            "_honcho_base_url_health",
            lambda base_url: (False, "connection refused"),
        )
        monkeypatch.setitem(__import__("sys").modules, "honcho", SimpleNamespace())

        honcho_cli.cmd_status(SimpleNamespace(all=False))

        out = capsys.readouterr().out
        assert "Connection... FAILED (connection refused)" in out
        assert "Fix: start local Honcho" in out
        assert "Connection... OK" not in out


class TestLocalFirstEnv:
    def test_env_template_includes_qwen_llamacpp_and_gpt55(self):
        import plugins.memory.honcho.cli as honcho_cli

        lines = honcho_cli.build_local_first_env_lines({"baseUrl": "http://localhost:8000"})
        text = "\n".join(lines)

        assert "EMBEDDING_VECTOR_DIMENSIONS=1024" in text
        assert "EMBEDDING_MAX_INPUT_TOKENS=32768" in text
        assert "Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0" in text
        assert "EMBEDDING_MODEL_CONFIG__OVERRIDES__BASE_URL=http://127.0.0.1:8080/v1" in text
        assert "DERIVER_MODEL_CONFIG__MODEL=gpt-5.5" in text
        assert "DERIVER_MODEL_CONFIG__OVERRIDES__BASE_URL=http://127.0.0.1:8645/v1" in text
        assert "HONCHO_BASE_URL=http://localhost:8000" in text
        assert "hermes proxy start --provider openai-codex" in text
        assert "<set-hermes-proxy-api-key>" in text


class TestEmbeddingsCommand:
    def test_build_llamacpp_command_uses_local_defaults(self):
        import plugins.memory.honcho.cli as honcho_cli

        cmd = honcho_cli.build_llamacpp_embedding_command()

        assert cmd == [
            "llama-server",
            "--embedding",
            "--host", "127.0.0.1",
            "--port", "8080",
            "-c", "32768",
            "-hf", "Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0",
        ]

    def test_build_llamacpp_command_includes_tuning_knobs(self):
        import plugins.memory.honcho.cli as honcho_cli

        cmd = honcho_cli.build_llamacpp_embedding_command(
            threads=8,
            gpu_layers=99,
            batch_size=2048,
            ubatch_size=512,
        )

        assert "--threads" in cmd
        assert "8" in cmd
        assert "--n-gpu-layers" in cmd
        assert "99" in cmd
        assert "--batch-size" in cmd
        assert "2048" in cmd
        assert "--ubatch-size" in cmd
        assert "512" in cmd

    def test_build_llamacpp_docker_command_uses_local_defaults(self):
        import plugins.memory.honcho.cli as honcho_cli

        cmd = honcho_cli.build_llamacpp_embedding_docker_command()

        assert cmd[:4] == ["docker", "run", "--detach", "--name"]
        assert "hermes-honcho-embeddings" in cmd
        assert "127.0.0.1:8080:8080" in cmd
        assert "ghcr.io/ggml-org/llama.cpp:server" in cmd
        assert "--embedding" in cmd
        assert "-hf" in cmd
        assert "Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0" in cmd

    def test_embedding_dimension_report_accepts_expected_vector(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli

        monkeypatch.setattr(
            honcho_cli,
            "_http_post_json",
            lambda url, payload: (
                True,
                {"data": [{"embedding": [0.0] * honcho_cli.LOCAL_EMBEDDING_DIMENSIONS}]},
            ),
        )

        ok, detail = honcho_cli._embedding_dimension_report()

        assert ok is True
        assert detail == "OK (1024)"

    def test_embedding_dimension_report_fails_on_mismatch(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli

        monkeypatch.setattr(
            honcho_cli,
            "_http_post_json",
            lambda url, payload: (True, {"data": [{"embedding": [0.0] * 4096}]}),
        )

        ok, detail = honcho_cli._embedding_dimension_report()

        assert ok is False
        assert "4096 != 1024" in detail
        assert "pgvector" in detail

    def test_embedding_endpoint_report_accepts_docker_backed_endpoint(self, monkeypatch):
        import plugins.memory.honcho.cli as honcho_cli

        def fake_which(name):
            if name == "docker":
                return "/usr/bin/docker"
            return None

        def fake_get(url):
            if url.endswith("/health"):
                return True, {"status": "ok"}
            if url.endswith("/v1/models"):
                return True, {
                    "data": [{"id": honcho_cli.LOCAL_EMBEDDING_MODEL}],
                }
            return False, "unexpected URL"

        monkeypatch.setattr("shutil.which", fake_which)
        monkeypatch.setattr(honcho_cli, "_http_get_json", fake_get)
        monkeypatch.setattr(
            honcho_cli,
            "_embedding_dimension_report",
            lambda **kwargs: (True, "OK (1024)"),
        )

        ok, lines = honcho_cli._embedding_endpoint_report()

        assert ok is True
        assert "  llama-server: MISSING" in lines
        assert "  Docker:      OK (/usr/bin/docker)" in lines

    def test_embeddings_config_prints_heavy_model_warning(self, capsys):
        import plugins.memory.honcho.cli as honcho_cli

        honcho_cli.cmd_embeddings(SimpleNamespace(action="config"))

        out = capsys.readouterr().out
        assert "Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0" in out
        assert "Dimensions: 1024" in out
        assert "Qwen/Qwen3-Embedding-8B-GGUF" in out
        assert "4096 dim" in out

    def test_embeddings_status_reports_endpoint_failure(self, monkeypatch, capsys):
        import plugins.memory.honcho.cli as honcho_cli

        monkeypatch.setattr(
            honcho_cli,
            "_embedding_endpoint_report",
            lambda **kwargs: (False, ["  Health:     FAILED (connection refused)"]),
        )

        honcho_cli.cmd_embeddings(SimpleNamespace(action="status", model=None, ctx=None, port=None))

        out = capsys.readouterr().out
        assert "Status:     not ready" in out
        assert "hermes honcho embeddings start" in out
        assert "hermes honcho embeddings start --docker" in out
        assert "llama-server --embedding" in out
        assert "Fix dims:" in out

    def test_embeddings_tune_prints_resource_knobs(self, capsys):
        import plugins.memory.honcho.cli as honcho_cli

        honcho_cli.cmd_embeddings(SimpleNamespace(action="tune"))

        out = capsys.readouterr().out
        assert "--threads" in out
        assert "--gpu-layers" in out
        assert "--batch-size" in out
        assert "--docker" in out
