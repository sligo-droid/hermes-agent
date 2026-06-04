from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_work_state_is_only_command_center_navigation_filter():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    assert "function WorkStatePanel" in source
    assert "Issue Pulse" not in source
    work_state_source = source.split("function WorkStatePanel", 1)[1].split("function ActionButton", 1)[0]
    assert "Work State" in work_state_source
    assert "Command Center lanes" in work_state_source
    for lane in ("Overview", "Inbox", "Work", "Archive", "Workers"):
        assert lane in work_state_source
    assert "Work states by status" not in source
    assert "onSelectStatus" not in source
    assert "activeStatus" not in source
    assert "status bar" not in source.lower()
    assert "role=\"list\"" not in work_state_source
    assert "<button" not in work_state_source
    assert 'href={lane.href} key={lane.key} rel="noopener noreferrer" target="_blank"' in work_state_source
    assert 'if (normalized.includes("/runs")) return "runs";' in source
    assert "opens in a new tab" in work_state_source


def test_command_center_worker_and_ticket_links_open_new_tabs():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    worker_link_markers = source.count('href={item.execution.worker_url} rel="noopener noreferrer" target="_blank"')
    ticket_link_markers = source.count('href={item.execution.task_url} rel="noopener noreferrer" target="_blank"')
    assert worker_link_markers >= 2
    assert ticket_link_markers >= 2


def test_sligo_shell_has_no_duplicate_top_tab_navigation():
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    shell_source = source.split("function SligoSurfaceShell", 1)[1].split("export default function App", 1)[0]

    assert "Sligo operator navigation" not in shell_source
    assert "<SligoNavLink" not in shell_source
    assert "Refresh Command Center" in shell_source


def test_modal_behavior_traps_focus_and_restores_safely():
    source = (ROOT / "web/src/hooks/useModalBehavior.ts").read_text(encoding="utf-8")

    assert "const modalStack" in source
    assert "topModal()?.id !== id" in source
    assert "e.key !== \"Tab\"" in source
    assert "e.key === \"Escape\"" in source
    assert "requestAnimationFrame" in source
    assert "[data-autofocus]" in source
    assert "isFocusableElement(entry.restoreTarget)" in source


def test_first_party_custom_modals_use_focus_behavior():
    expected = {
        "web/src/pages/CronPage.tsx": "data-autofocus",
        "web/src/pages/ProfilesPage.tsx": "data-autofocus",
        "web/src/pages/ModelsPage.tsx": "useModalBehavior({ open: true, onClose })",
        "web/src/components/ModelPickerDialog.tsx": "useModalBehavior({ open: true, onClose })",
        "web/src/components/OAuthLoginModal.tsx": "useModalBehavior({ open: true, onClose: handleClose })",
    }

    for relative, marker in expected.items():
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "useModalBehavior" in source
        assert marker in source
