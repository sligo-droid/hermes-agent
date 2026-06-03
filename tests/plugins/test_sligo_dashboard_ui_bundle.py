from pathlib import Path


UI_BUNDLE = Path("plugins/sligo/dashboard/dist/index.js")


def _bundle() -> str:
    return UI_BUNDLE.read_text(encoding="utf-8")


def test_self_improvement_bundle_loads_board_and_groups_prongs():
    src = _bundle()

    assert 'fetchJSON(`${API}/projects`)' in src
    assert 'fetchJSON(`${API}/runs?limit=100`)' in src
    assert 'fetchJSON(`${API}/proposals?limit=500`)' in src
    assert 'byProng.set(`${project.id}:${prong.id}`' in src
    assert "project.prongs" in src
    assert "data-testid\": \"self-improvement-board" in src


def test_self_improvement_bundle_hides_rejected_by_default_and_exposes_history_filter():
    src = _bundle()

    assert 'value: "active", label: "Active"' in src
    assert 'value: "rejected", label: "Rejected history"' in src
    assert 'filters.status === "active" && proposal.status === "rejected"' in src


def test_self_improvement_bundle_has_drawer_actions_loading_errors_and_links():
    src = _bundle()

    assert 'role: "dialog"' in src
    assert 'setError(messageFromError(err))' in src
    assert 'props.action === `approve:${proposal.card_id}` ? "Approving..."' in src
    assert 'props.action === `reject:${proposal.card_id}` ? "Rejecting..."' in src
    assert 'props.action === `edit:${props.proposal.card_id}` ? "Saving..."' in src
    assert "View source output" in src
    assert "View worker" in src
    assert "Generated worker prompt" in src
    assert "Feedback and action history" in src
    assert "proposal.linked_worker_url" in src
    assert "`/kanban?board=${encodeURIComponent(board)}&task=${encodeURIComponent(task)}`" in src
    assert "/workers?board=" not in src


def test_self_improvement_bundle_calls_proposal_mutation_endpoints():
    src = _bundle()

    assert '}/approve`, { method: "POST" })' in src
    assert '}/reject`, {' in src
    assert 'method: "PATCH"' in src
    assert "refreshSelected(data.proposal)" in src
