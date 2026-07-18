from unittest.mock import patch

from agent.vision_capabilities import (
    resolve_vision_capabilities,
    transport_supports_image_tool_results,
)


def test_custom_codex_responses_transport_accepts_image_tool_results():
    assert transport_supports_image_tool_results(
        "custom", "gpt-5.6-sol", api_mode="codex_responses"
    ) is True


def test_transport_and_model_capability_are_resolved_separately():
    with patch("agent.vision_capabilities._lookup_models_dev_support", return_value=False):
        resolution = resolve_vision_capabilities(
            "custom",
            "text-only-model",
            {},
            api_mode="codex_responses",
        )

    assert resolution.transport_supports_image_results is True
    assert resolution.model_supports_image_input is False
    assert resolution.native_tool_result_supported is False


def test_config_override_precedence_is_preserved():
    cfg = {
        "model": {"provider": "named", "supports_vision": "false"},
        "providers": {
            "named": {
                "models": {"vlm": {"supports_vision": True}},
            }
        },
    }

    resolution = resolve_vision_capabilities(
        "custom", "vlm", cfg, api_mode="codex_responses"
    )

    assert resolution.model_supports_image_input is False
    assert resolution.model_capability_source == "config:model.supports_vision"
    assert resolution.native_tool_result_supported is False


def test_per_provider_override_handles_runtime_custom_name_rewrite():
    cfg = {
        "model": {"provider": "named"},
        "providers": {
            "named": {
                "models": {"vlm": {"supports_vision": "true"}},
            }
        },
    }

    resolution = resolve_vision_capabilities(
        "custom", "vlm", cfg, api_mode="codex_responses"
    )

    assert resolution.model_supports_image_input is True
    assert resolution.model_capability_source.startswith("config:providers.")
    assert resolution.native_tool_result_supported is True


def test_unknown_capability_fails_closed_with_bounded_diagnostic():
    with patch("agent.vision_capabilities._lookup_models_dev_support", return_value=None):
        resolution = resolve_vision_capabilities("unknown", "new-model", {})

    assert resolution.native_tool_result_supported is False
    assert resolution.diagnostic_code == "model_capability_unknown"
    assert len(resolution.diagnostic_code) < 80
