from agent.runtime_phase_classification import classify_runtime_phase


def test_runtime_phase_classification_is_deterministic():
    assert classify_runtime_phase("model_attempt") == "model"
    assert classify_runtime_phase("tool", tool_name="browser_snapshot") == "browser"
    assert classify_runtime_phase("tool", tool_name="visual_qa") == "vision"
    assert classify_runtime_phase("tool", tool_name="delegate_coding_task") == "coding_worker"
    assert classify_runtime_phase("github_ci_poll") == "ci"
    assert classify_runtime_phase("code_review") == "review"
    assert classify_runtime_phase("deployment") == "deployment"
    assert classify_runtime_phase("production_qa") == "production_qa"
    assert classify_runtime_phase("canonical_sync") == "canonical_sync"
    assert classify_runtime_phase("gateway_restart") == "restart"
    assert classify_runtime_phase("durable_handoff") == "gateway_handoff"
    assert classify_runtime_phase("unknown") == "overhead"


def test_all_browser_operations_route_to_browser_except_visual_capture():
    for operation in (
        "browser_navigate",
        "browser_back",
        "browser_console",
        "browser_cdp",
    ):
        assert classify_runtime_phase(operation) == "browser"
        assert classify_runtime_phase("tool", tool_name=operation) == "browser"

    for operation in ("browser_vision", "browser_capture", "browser_screenshot"):
        assert classify_runtime_phase(operation) == "vision"
        assert classify_runtime_phase("tool", tool_name=operation) == "vision"
