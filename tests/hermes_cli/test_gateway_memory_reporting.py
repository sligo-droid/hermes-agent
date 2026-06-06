from gateway import memory_telemetry as mt
from hermes_cli import gateway


def test_gateway_memory_lines_render_sanitized_child(monkeypatch):
    telemetry = mt.GatewayMemoryTelemetry(
        gateway_pids=(100,),
        gateway_rss_kb=1024,
        child_rss_kb=4096,
        top_children=(
            mt.ChildMemory(
                pid=200,
                rss_kb=4096,
                kind="lsp",
                label="server --api-key=[redacted]",
                unit="hermes-gateway-child-lsp-session-server.scope",
            ),
        ),
        source="systemd+/proc",
    )
    monkeypatch.setattr(
        "gateway.memory_telemetry.collect_gateway_memory_telemetry",
        lambda pids: telemetry,
    )

    lines = gateway._gateway_memory_lines((100,))

    assert lines[0] == "Gateway RSS: 1.0 MiB"
    assert lines[1] == "Isolated child/helper RSS: 4.0 MiB"
    assert "PID 200 lsp unit=hermes-gateway-child-lsp-session-server.scope" in lines[3]
    assert "api-key=[redacted]" in lines[3]
