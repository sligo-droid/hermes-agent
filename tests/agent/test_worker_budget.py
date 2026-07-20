from types import SimpleNamespace

from agent.worker_budget import remaining_nested_worker_budget


def test_nested_worker_budget_caps_requested_timeout(monkeypatch):
    monkeypatch.setattr("agent.worker_budget.time.monotonic", lambda: 100.0)
    parent = SimpleNamespace(_nested_worker_deadline_monotonic=145.0)

    assert remaining_nested_worker_budget(parent, 600.0) == 45.0
    assert remaining_nested_worker_budget(parent, 30.0) == 30.0


def test_nested_worker_budget_reports_exhaustion(monkeypatch):
    monkeypatch.setattr("agent.worker_budget.time.monotonic", lambda: 200.0)
    parent = SimpleNamespace(_nested_worker_deadline_monotonic=199.0)

    assert remaining_nested_worker_budget(parent, 600.0) == 0.0
