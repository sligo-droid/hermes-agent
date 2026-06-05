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


def test_command_center_archive_action_is_one_click_without_removing_other_prompts():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    archive_branch = source.split('if (kind === "archive") {', 1)[1].split('} else if (proposalId && kind === "approve")', 1)[0]

    assert "window.confirm" not in archive_branch
    assert "archiveKanbanBoard" in archive_branch
    assert "haltSelfImprovementProposal" in archive_branch
    assert 'window.prompt("Reject reason for future prong feedback?"' in source
    assert 'window.prompt("Reason for revert follow-up?"' in source


def test_command_center_archive_action_renders_last_in_row_rail():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    row_rail = source.split('className="mt-4 flex flex-wrap items-center gap-2 border-t border-white/[0.08] pt-3"', 1)[1].split("</div>", 1)[0]

    assert row_rail.index('kind="approve"') < row_rail.index('kind="reject"')
    assert row_rail.index('kind="resume"') < row_rail.index('kind="archive"')
    assert row_rail.index('kind="pause"') < row_rail.index('kind="archive"')
    assert row_rail.index('kind="undo"') < row_rail.index('kind="archive"')


def test_command_center_row_actions_lock_during_refresh_settle():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]
    handle_source = source.split("const handleAction = useCallback", 1)[1].split("const selectedWorkId", 1)[0]

    assert "const ACTION_SETTLE_MS = 600" in source
    assert "const actionDisabled = (kind: ActionKind) => Boolean(activeAction) && !actionBusy(kind);" in card_source
    assert "aria-busy={rowBusy || undefined}" in card_source
    assert "<div aria-busy={rowBusy}" in card_source
    assert "aria-live=\"polite\"" in card_source
    for kind in ("approve", "reject", "resume", "pause", "undo", "archive"):
        assert f'disabled={{actionDisabled("{kind}")}}' in card_source
    assert "if (activeAction) return;" in handle_source
    assert "delayBeforeApplyMs: Math.max(0, ACTION_SETTLE_MS - (Date.now() - startedAt))" in handle_source
    assert "settleAfterApplyMs: ACTION_SETTLE_MS" in handle_source
    assert "preserveMissingWorkItemId: item.id" in handle_source


def test_command_center_worker_pill_has_no_workers_fallback():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]
    detail_source = source.split("function DetailPanel", 1)[1].split("function KeyValue", 1)[0]

    assert "item.execution?.worker_url || discordUrl ?" in card_source
    assert "item.execution?.worker_url &&" in card_source
    assert "item.execution?.worker_url &&" in detail_source
    assert 'href="/workers"' not in card_source
    assert 'to="/workers"' not in card_source


def test_sligo_shell_has_no_duplicate_top_tab_navigation():
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    shell_source = source.split("function SligoSurfaceShell", 1)[1].split("export default function App", 1)[0]

    assert "Sligo operator navigation" not in shell_source
    assert "<SligoNavLink" not in shell_source
    assert "Refresh Command Center" in shell_source


def test_sligo_shell_refresh_feedback_and_light_toggle():
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    shell_source = source.split("function SligoSurfaceShell", 1)[1].split("export default function App", 1)[0]

    assert "sligo-command-center-color-mode" in source
    assert "data-sligo-theme={sligoMode}" in shell_source
    assert "Switch Command Center to light mode" in shell_source
    assert "Switch Command Center to dark mode" in shell_source
    assert "aria-busy={isRefreshing}" in shell_source
    assert "animate-spin" in shell_source
    assert "Refreshing" in shell_source
    assert "await window.__commandCenterRefresh()" in shell_source


def test_command_center_discord_links_open_new_tabs():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    assert "function discordSourceUrl" in source
    assert "discord_thread" in source
    assert "discord_url" in source
    assert "discord_thread_url" in source
    assert "Open Discord source for" in source
    assert 'href={discordUrl} rel="noopener noreferrer" target="_blank"' in source


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
