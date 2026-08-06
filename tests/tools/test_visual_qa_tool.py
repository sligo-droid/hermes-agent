import json
from pathlib import Path

import pytest

from tools.registry import _module_registers_tools, registry


def _schema_property_names(value):
    if isinstance(value, dict):
        names = set((value.get("properties") or {}).keys())
        for item in value.values():
            names.update(_schema_property_names(item))
        return names
    if isinstance(value, list):
        names = set()
        for item in value:
            names.update(_schema_property_names(item))
        return names
    return set()


def test_visual_qa_is_statically_discoverable_and_always_registered():
    module_path = Path(__file__).resolve().parents[2] / "tools" / "visual_qa_tool.py"
    assert _module_registers_tools(module_path) is True

    import tools.visual_qa_tool  # noqa: F401

    entry = registry.get_entry("visual_qa")
    assert entry is not None
    assert entry.toolset == "browser"
    assert entry.is_async is True
    assert entry.check_fn is None


def test_visual_qa_is_in_stable_direct_action_toolsets():
    from toolsets import TOOLSETS, _HERMES_CORE_TOOLS

    assert "visual_qa" in _HERMES_CORE_TOOLS
    assert "visual_qa" in TOOLSETS["browser"]["tools"]
    assert "browser_authenticate" in _HERMES_CORE_TOOLS
    assert "browser_authenticate" in TOOLSETS["browser"]["tools"]
    assert "browser_authenticate" in TOOLSETS["coding"]["tools"]
    assert "browser_authenticate" in TOOLSETS["hermes-acp"]["tools"]
    assert "browser_authenticate" in TOOLSETS["hermes-api-server"]["tools"]


def test_visual_qa_schema_has_no_arbitrary_execution_or_protected_inputs():
    from tools.visual_qa_tool import VISUAL_QA_SCHEMA

    assert VISUAL_QA_SCHEMA["parameters"]["additionalProperties"] is False
    property_names = {name.lower() for name in _schema_property_names(VISUAL_QA_SCHEMA)}
    assert VISUAL_QA_SCHEMA["parameters"]["required"] == [
        "target",
        "page",
        "viewport",
        "state",
        "assertions",
    ]
    assert {"target", "page", "viewport", "state", "artifacts", "assertions"} <= property_names
    assert VISUAL_QA_SCHEMA["parameters"]["properties"]["artifacts"]["maxItems"] == 4
    assert "receipt_assertions" not in property_names
    assert property_names.isdisjoint(
        {
            "javascript",
            "js",
            "cdp",
            "command",
            "shell",
            "url",
            "screenshot",
            "image",
            "cookie",
            "cookies",
            "header",
            "headers",
            "credential",
            "credentials",
            "authorization",
            "token",
        }
    )


def test_visual_qa_description_distinguishes_containment_from_scroll_overflow():
    from tools.visual_qa_tool import VISUAL_QA_SCHEMA

    description = VISUAL_QA_SCHEMA["description"]
    assert "fit fully inside all four viewport edges" in description
    assert "do not use it for full pages" in description
    assert "Use `no_horizontal_overflow`" in description
    assert "vertical scrolling is allowed" in description


@pytest.mark.asyncio
async def test_visual_qa_uses_read_only_browser_namespace(monkeypatch):
    from tools import visual_qa_tool

    captured = {}

    async def run(**kwargs):
        captured.update(kwargs)
        return {"status": "passed"}

    monkeypatch.setattr(visual_qa_tool, "run_visual_assertions", run)

    result = await visual_qa_tool._visual_qa_handler(
        {},
        task_id="turn-7",
        runtime_mode="read_only",
    )

    assert json.loads(result)["status"] == "passed"
    assert captured["task_id"] == "turn-7::read-only"
