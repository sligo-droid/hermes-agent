from __future__ import annotations

import json
from pathlib import Path

import pytest

from plugins.client_knowledge_gbrain.client import (
    GBrainClient,
    GBrainSettings,
    PINNED_GBRAIN_COMMIT,
    PINNED_GBRAIN_TAG,
    PINNED_BUN_VERSION,
    client_knowledge_environment,
    load_settings,
)
from plugins.client_knowledge_gbrain.scope import (
    ClientKnowledgeValidationError,
    full_project_slug,
    validate_frontmatter,
    validate_project_key,
    validate_search_results,
)
from plugins.client_knowledge_gbrain.tools import (
    handle_client_knowledge_get,
    handle_client_knowledge_search,
)


def _frontmatter(project="pid"):
    return {
        "project": project,
        "status": "current",
        "kind": "requirement",
        "effective_at": "2026-08-04",
        "updated_at": "2026-08-04T12:00:00Z",
        "source_refs": ["notion:page:synthetic-pid-001"],
        "supersedes": [],
        "confidence": "high",
        "sensitivity": "internal",
    }


@pytest.mark.parametrize("value", ["../pid", "PID", "pid/other", "pid\\other", "píd", "-pid"])
def test_project_key_rejects_noncanonical_values(value):
    with pytest.raises(ClientKnowledgeValidationError):
        validate_project_key(value)


def test_full_project_slug_is_constructed_not_accepted_absolute():
    assert full_project_slug("pid", "requirements/reporting") == "projects/pid/requirements/reporting"
    with pytest.raises(ClientKnowledgeValidationError):
        full_project_slug("pid", "projects/decoy/requirements/canary")
    with pytest.raises(ClientKnowledgeValidationError):
        full_project_slug("pid", "/".join(["segment"] * 17))


def test_frontmatter_requires_matching_project_and_notion_citations():
    validate_frontmatter(
        _frontmatter(),
        project_key="pid",
        slug="projects/pid/requirements/reporting",
    )
    bad = _frontmatter("decoy")
    with pytest.raises(ClientKnowledgeValidationError):
        validate_frontmatter(
            bad,
            project_key="pid",
            slug="projects/pid/requirements/reporting",
        )


def test_search_results_filter_foreign_but_fail_on_missing_identity():
    accepted, foreign = validate_search_results(
        [
            {"source_id": "client-knowledge", "slug": "projects/pid/requirements/reporting"},
            {"source_id": "client-knowledge", "slug": "projects/decoy/requirements/canary"},
        ],
        project_key="pid",
        source_id="client-knowledge",
    )
    assert [item["slug"] for item in accepted] == ["projects/pid/requirements/reporting"]
    assert foreign == 1
    with pytest.raises(ClientKnowledgeValidationError):
        validate_search_results([{"slug": "projects/pid/x"}], project_key="pid", source_id="client-knowledge")
    with pytest.raises(ClientKnowledgeValidationError):
        validate_search_results(
            [{"source_id": "client-knowledge", "slug": "projects/pid/../decoy/canary"}],
            project_key="pid",
            source_id="client-knowledge",
        )


def test_allowlisted_environment_excludes_provider_and_host_paths(monkeypatch, tmp_path):
    binary = tmp_path / "gbrain"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    home = tmp_path / "gbrain-home"
    home.mkdir()
    settings = GBrainSettings(binary, home)
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("GBRAIN_CHAT_MODEL", "provider:model")
    monkeypatch.setenv("HERMES_HOME", "/real/hermes")

    env = client_knowledge_environment(settings)

    assert env["GBRAIN_HOME"] == str(home)
    assert env["HOME"] == str(home)
    assert env["PATH"] == str(binary.parent)
    assert "OPENAI_API_KEY" not in env
    assert "GBRAIN_CHAT_MODEL" not in env
    assert "HERMES_HOME" not in env


def test_pin_constants_match_approved_upstream():
    assert PINNED_GBRAIN_TAG == "v0.42.73.1"
    assert PINNED_GBRAIN_COMMIT == "aecb33e795cc4806f760446c55ab1c350194ddc8"
    assert PINNED_BUN_VERSION == "1.3.14"


def test_bundled_plugin_is_opt_in_and_registers_only_bounded_tools(monkeypatch, tmp_path):
    from hermes_cli.plugins import PluginManager

    config_path = tmp_path / "config.yaml"
    config_path.write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    manager = PluginManager()
    manager.discover_and_load(force=True)
    plugin = manager._plugins["client-knowledge-gbrain"]
    assert plugin.enabled is False
    assert plugin.error and "not enabled" in plugin.error

    config_path.write_text(
        "plugins:\n  enabled:\n    - client-knowledge-gbrain\n",
        encoding="utf-8",
    )
    manager.discover_and_load(force=True)
    plugin = manager._plugins["client-knowledge-gbrain"]
    assert plugin.enabled is True
    assert plugin.tools_registered == ["client_knowledge_search", "client_knowledge_get"]


def test_checkout_pin_rejects_wrong_commit(monkeypatch, tmp_path):
    checkout = tmp_path / "gbrain"
    checkout.mkdir()
    (checkout / "package.json").write_text('{"version":"0.42.73.1"}\n', encoding="utf-8")
    binary = tmp_path / "bun"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    client = GBrainClient(GBrainSettings(binary, home, checkout=checkout))

    completed = type("Completed", (), {"returncode": 0, "stdout": "wrong-commit\n"})()
    monkeypatch.setattr("plugins.client_knowledge_gbrain.client.subprocess.run", lambda *a, **k: completed)

    with pytest.raises(RuntimeError, match="requires GBrain"):
        client.assert_pinned_checkout()


def test_load_settings_accepts_only_exact_source_launcher_shape(tmp_path):
    checkout = tmp_path / "gbrain"
    source_cli = checkout / "src" / "cli.ts"
    source_cli.parent.mkdir(parents=True)
    source_cli.write_text("", encoding="utf-8")
    executable = tmp_path / "bun"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    home = tmp_path / "brain-home"
    home.mkdir()
    block = {
        "client_knowledge": {
            "gbrain": {
                "executable": str(executable),
                "args": [str(source_cli)],
                "home": str(home),
                "checkout": str(checkout),
            }
        }
    }

    settings = load_settings(block)

    assert settings.executable == executable.resolve()
    assert settings.checkout == checkout.resolve()
    assert settings.args == (str(source_cli.resolve()),)

    block["client_knowledge"]["gbrain"]["args"] = []
    with pytest.raises(ClientKnowledgeValidationError, match="src/cli.ts"):
        load_settings(block)


def test_keyword_only_requires_db_plane_readback(monkeypatch, tmp_path):
    binary = tmp_path / "bun"
    binary.write_text("", encoding="utf-8")
    binary.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    client = GBrainClient(GBrainSettings(binary, home))
    completed = type(
        "Completed",
        (),
        {"returncode": 0, "stdout": "true\n", "stderr": "source: file plane\n"},
    )()
    monkeypatch.setattr("plugins.client_knowledge_gbrain.client.subprocess.run", lambda *a, **k: completed)

    with pytest.raises(RuntimeError, match="must read back as true"):
        client.assert_keyword_only()


def test_tool_handlers_enforce_project_scope(monkeypatch):
    class FakeClient:
        settings = type("Settings", (), {"source_id": "client-knowledge", "max_context_chars": 8000})()

        def search(self, query, *, limit):
            return [
                {
                    "source_id": "client-knowledge",
                    "slug": "projects/pid/requirements/reporting",
                    "title": "Reporting",
                    "type": "requirement",
                    "chunk_text": "Weekly reports.",
                    "evidence": "X" * 100_000,
                },
                {
                    "source_id": "client-knowledge",
                    "slug": "projects/decoy/requirements/canary",
                    "title": "CANARY",
                    "chunk_text": "LEAK",
                },
            ]

        def get_page(self, slug):
            return {
                "source_id": "client-knowledge",
                "slug": slug,
                "title": "Reporting",
                "frontmatter": {**_frontmatter(), "untrusted_extra": "X" * 100_000},
                "compiled_truth": "Weekly reports.",
                "timeline": "2026-08-04 created.",
            }

    monkeypatch.setattr("plugins.client_knowledge_gbrain.tools._client", lambda: FakeClient())
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.tools.get_trusted_project_key",
        lambda: "pid",
    )
    search = json.loads(
        handle_client_knowledge_search({"project_key": "pid", "query": "reporting"})
    )
    assert search["result_count"] == 1
    assert search["foreign_results_filtered"] == 1
    assert "LEAK" not in json.dumps(search)
    assert "evidence" not in search["results"][0]

    page = json.loads(
        handle_client_knowledge_get({"project_key": "pid", "slug": "requirements/reporting"})
    )
    assert page["reference"] == "gbrain:projects/pid/requirements/reporting"
    assert page["source_refs"] == ["notion:page:synthetic-pid-001"]
    assert "untrusted_extra" not in page["frontmatter"]


@pytest.mark.parametrize("mapped", ["", "decoy"])
def test_tool_handlers_reject_unmapped_or_foreign_project(monkeypatch, mapped):
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.tools.get_trusted_project_key",
        lambda: mapped,
    )

    result = json.loads(
        handle_client_knowledge_search({"project_key": "pid", "query": "reporting"})
    )

    assert set(result) == {"error"}
    assert "mapped project" in result["error"]
