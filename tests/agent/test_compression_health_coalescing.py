import logging
from types import SimpleNamespace

from agent.conversation_compression import check_compression_model_feasibility


class _CompressionAgent:
    compression_enabled = True
    provider = "auto"
    _custom_providers = {}
    _aux_compression_context_length_config = None

    def __init__(self):
        self._compression_warning = None
        self.statuses = []
        self.context_compressor = SimpleNamespace(threshold_tokens=100_000)

    def _current_main_runtime(self):
        return {}

    def _emit_status(self, msg):
        self.statuses.append(msg)


def test_repeated_compression_feasibility_unavailable_is_coalesced(monkeypatch, caplog):
    import agent.auxiliary_client as aux

    now = 5000.0
    monkeypatch.setattr(aux, "_aux_time", lambda: now)
    monkeypatch.setattr(aux, "get_text_auxiliary_client", lambda *args, **kwargs: (None, None))
    monkeypatch.setattr(
        aux,
        "_resolve_task_provider_model",
        lambda task: ("auto", "", None, None, None),
    )

    agents = [_CompressionAgent() for _ in range(3)]
    with caplog.at_level(logging.WARNING):
        for agent in agents:
            check_compression_model_feasibility(agent)

    messages = [rec.message for rec in caplog.records]
    warning = "No auxiliary LLM provider for compression"
    assert sum(warning in msg for msg in messages) == 1
    assert "task=compression" in next(msg for msg in messages if warning in msg)
    assert "failure_class=no_provider" in next(msg for msg in messages if warning in msg)
    assert "final_state=degraded" in next(msg for msg in messages if warning in msg)
    assert all(agent.statuses for agent in agents)
    assert all(agent._compression_warning for agent in agents)
