from types import SimpleNamespace

import pytest


def _llm_response(text: str, model: str = "test-model"):
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], model=model)


def test_dashboard_inference_search_query_uses_or_prefix_terms():
    from hermes_cli.web_server import _dashboard_inference_search_query

    query = _dashboard_inference_search_query(
        "Can you show me Discord voice message work?"
    )

    assert query == "discord* OR voice* OR message* OR work*"


def test_dashboard_inference_rejects_empty_question():
    from hermes_cli.web_server import _run_dashboard_inference

    with pytest.raises(ValueError, match="Question is required"):
        _run_dashboard_inference("   ")


def test_dashboard_inference_answers_from_retrieved_context(monkeypatch):
    from hermes_cli import web_server

    monkeypatch.setattr(
        web_server,
        "_dashboard_inference_context",
        lambda question: (
            [
                {
                    "session_id": "s1",
                    "role": "assistant",
                    "snippet": ">>>Discord<<< voice message trigger",
                    "source": "discord",
                    "model": "openai/gpt-5.4",
                    "session_started": 123.0,
                }
            ],
            [
                {
                    "id": "s1",
                    "title": "Discord voice messages",
                    "source": "discord",
                    "model": "openai/gpt-5.4",
                    "started_at": 123.0,
                    "last_active": 124.0,
                    "message_count": 3,
                    "preview": "voice message trigger",
                }
            ],
        ),
    )
    monkeypatch.setattr(
        "agent.auxiliary_client.call_llm",
        lambda *args, **kwargs: _llm_response("Session s1 covered voice messages."),
    )

    result = web_server._run_dashboard_inference("What happened with voice messages?")

    assert result["answer"] == "Session s1 covered voice messages."
    assert result["model"] == "test-model"
    assert result["matches"][0]["session_id"] == "s1"
