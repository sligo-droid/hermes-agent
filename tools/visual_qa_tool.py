"""Stable agent-facing tool for bounded trusted visual assertions."""

from __future__ import annotations

import json
from typing import Any

from tools.registry import registry
from tools.visual_assertion_runner import run_visual_assertions


_LOCATOR_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "by": {"type": "string", "enum": ["test_id", "role", "css"]},
        "value": {"type": "string", "maxLength": 200},
        "name": {"type": "string", "maxLength": 120},
    },
    "required": ["by", "value"],
}

_VIEWPORT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "description": {"type": "string", "maxLength": 120},
        "width": {"type": "integer", "minimum": 200, "maximum": 7680},
        "height": {"type": "integer", "minimum": 200, "maximum": 4320},
    },
    "required": ["description"],
}

VISUAL_QA_SCHEMA = {
    "name": "visual_qa",
    "description": (
        "Run bounded declarative visual assertions against the existing task browser session. "
        "This tool is callable in any agent turn; it does not require a preceding UI change or "
        "an active visual requirement. Formulate the smallest relevant target, prepared/current page "
        "state, viewport assumptions, and concrete assertion intent from the full accepted "
        "request and code understanding. This tool accepts only "
        "trusted assertion kinds; it does not accept JavaScript, CDP commands, shell commands, "
        "URLs, screenshot inputs, cookies, or headers. Request up to four meaningful human-facing "
        "artifacts from the screenshots used by QA; focused target plus surrounding context is the "
        "default when artifacts are omitted. Reuse every host-provided opaque assertion ID "
        "with its exact required kind only for a legacy requirement; new orchestrator-owned "
        "contracts receive host-generated opaque assertion IDs. Missing, duplicate, substituted, "
        "unrelated, or incomplete coverage is rejected. Only an explicit passed receipt satisfies an enforced visual gate; "
        "failed, blocked, uncertain, malformed, and timed-out checks do not. Include "
        "`no_new_diagnostics` only when a browser tool returned an exact host-issued diagnostic cursor. "
        "Use `viewport_contained` only when the selected control or region must fit fully inside "
        "all four viewport edges; do not use it for full pages, long columns, document roots, or "
        "intentionally vertical scroll surfaces. Use `no_horizontal_overflow` on the selected "
        "page root or container when vertical scrolling is allowed but horizontal scrolling is not."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "target": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "description": {"type": "string", "maxLength": 160},
                    "locator": _LOCATOR_SCHEMA,
                },
                "required": ["description"],
            },
            "page": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "state": {
                        "type": "string",
                        "enum": ["already_open", "prepared"],
                    },
                    "description": {"type": "string", "maxLength": 160},
                },
                "required": ["state", "description"],
            },
            "viewport": _VIEWPORT_SCHEMA,
            "state": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "description": (
                    "Required current page assumptions, separate from page.state; "
                    "for example data loaded, menu open, or authenticated shell visible."
                ),
                "items": {"type": "string", "maxLength": 160},
            },
            "artifacts": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["focused", "context", "responsive"],
                        },
                        "description": {"type": "string", "maxLength": 160},
                        "locator": _LOCATOR_SCHEMA,
                        "viewport": _VIEWPORT_SCHEMA,
                    },
                    "required": ["kind", "description"],
                },
            },
            "assertions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {"type": "string", "maxLength": 48},
                        "kind": {
                            "type": "string",
                            "enum": [
                                "exists",
                                "not_exists",
                                "visible",
                                "viewport_contained",
                                "no_horizontal_overflow",
                                "count",
                                "text_present",
                                "screenshot_appearance",
                                "no_new_diagnostics",
                            ],
                        },
                        "locator": _LOCATOR_SCHEMA,
                        "min": {"type": "integer", "minimum": 0, "maximum": 10000},
                        "max": {"type": "integer", "minimum": 0, "maximum": 10000},
                        "text": {"type": "string", "maxLength": 80},
                        "policy": {"type": "string", "enum": ["literal_request_text"]},
                        "expectation": {"type": "string", "maxLength": 240},
                        "cursor": {
                            "type": "string",
                            "pattern": "^dcur_[0-9]+_[0-9a-f]{24}$",
                            "maxLength": 96,
                            "description": (
                                "Exact host-issued diagnostic cursor from the active browser. "
                                "Omit the no_new_diagnostics assertion when no cursor was returned."
                            ),
                        },
                    },
                    "required": ["kind"],
                },
            },
        },
        "required": ["target", "page", "viewport", "state", "assertions"],
    },
}


async def _visual_qa_handler(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        from agent.auxiliary_client import get_runtime_main
        from agent.visual_qa import get_active_visual_requirement
        from hermes_cli.config import load_config

        cfg = load_config()
        runtime = get_runtime_main()
        requirement = get_active_visual_requirement()
    except Exception:
        cfg = {}
        runtime = {}
        requirement = {"level": "none", "target": "", "assertions": []}
    from tools.browser_tool import _read_only_browser_task_id

    task_id = _read_only_browser_task_id(
        str(kwargs.get("task_id") or "default"),
        kwargs.get("runtime_mode"),
    )
    result = await run_visual_assertions(
        task_id=str(task_id or "default"),
        requirement=requirement,
        contract=args,
        config=cfg,
        provider=str(runtime.get("provider") or ""),
        model=str(runtime.get("model") or ""),
        base_url=str(runtime.get("base_url") or ""),
        api_key=str(runtime.get("api_key") or ""),
        api_mode=str(runtime.get("api_mode") or ""),
    )
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))


registry.register(
    name="visual_qa",
    toolset="browser",
    schema=VISUAL_QA_SCHEMA,
    handler=_visual_qa_handler,
    is_async=True,
    emoji="🔎",
    max_result_size_chars=6000,
    effect="read_only",
)


__all__ = ["VISUAL_QA_SCHEMA"]
