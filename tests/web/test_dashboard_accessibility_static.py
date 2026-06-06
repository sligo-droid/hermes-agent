from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_work_state_is_only_command_center_navigation_filter():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    assert "function WorkStatePanel" in source
    assert "function ProjectTabs" in source
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


def test_command_center_project_tabs_render_above_work_state_and_preserve_lanes():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    page_source = source.split("export default function CommandCenterPage", 1)[1]
    render_source = page_source.split("return (", 1)[1]

    assert "Command Center projects" in source
    assert "command-center-project-tabs" in source
    assert "projects={snapshot?.projects ?? []}" in source
    assert "to={{ pathname, search: tabSearch(project.key) }}" in source
    assert "to={{ pathname: lane.href, search }}" in source
    assert render_source.index("<ProjectTabs") < render_source.index("<WorkStatePanel")
    for forbidden in ("Operator Surface", "KPI", "status distribution"):
        assert forbidden not in source
    assert "Sligo operator navigation" not in source


def test_command_center_worker_rows_and_ticket_links_open_new_tabs():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert 'window.open(workerUrl, "_blank", "noopener,noreferrer")' in card_source
    assert 'role={workerUrl ? "link" : undefined}' in card_source
    assert 'tabIndex={workerUrl ? 0 : undefined}' in card_source
    assert 'href={item.execution.worker_url}' not in card_source
    assert 'href={item.execution.task_url} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank"' in card_source


def test_command_center_running_rows_have_live_indicator():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "web/src/index.css").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert "function runningDescriptor" in source
    assert "function RunningIndicator" in source
    assert "const running = isRunningWorkItem(item);" in card_source
    assert "command-center-card-running" in card_source
    assert "Running work item:" in source
    assert "command-center-running-chip" in source
    assert "command-center-live-scan" in styles
    assert ".sligo-light .command-center-card-running" in styles


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
    action_source = source.split("function availableActionKinds", 1)[1].split("function actionSet", 1)[0]
    row_rail = source.split('className="mt-4 flex flex-wrap items-center gap-2 border-t border-white/[0.08] pt-3"', 1)[1].split("</div>", 1)[0]

    assert action_source.index('actions.push("approve", "reject")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("resume")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("pause")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("undo")') < action_source.index('actions.push("archive")')
    assert "actions.map((kind)" in row_rail
    assert "kind={kind}" in row_rail


def test_command_center_row_actions_lock_during_refresh_settle():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]
    handle_source = source.split("const handleAction = useCallback", 1)[1].split("return (", 1)[0]

    assert "const ACTION_SETTLE_MS = 600" in source
    assert "const actionDisabled = (kind: ActionKind) => (Boolean(activeAction) && !actionBusy(kind)) || (selectionActive && (!selected || !multiSelectActionCommon.has(kind)));" in card_source
    assert "aria-busy={rowBusy || undefined}" in card_source
    assert "<div aria-busy={rowBusy}" in card_source
    assert "aria-live=\"polite\"" in card_source
    assert "const disabled = actionDisabled(kind);" in card_source
    assert "disabled={disabled}" in card_source
    assert "if (activeAction) return;" in handle_source
    assert "delayBeforeApplyMs: Math.max(0, ACTION_SETTLE_MS - (Date.now() - startedAt))" in handle_source
    assert "settleAfterApplyMs: ACTION_SETTLE_MS" in handle_source
    assert "preserveMissingWorkItemId" not in source


def test_command_center_worker_url_is_row_action_without_worker_button():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert "const workerUrl = item.execution?.worker_url || null" in card_source
    assert "Open worker board for" in card_source
    assert "Worker <ExternalLink" not in card_source
    assert "function DetailPanel" not in source
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
    assert 'href={discordUrl} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank"' in source


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
