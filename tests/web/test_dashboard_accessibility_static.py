from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_work_state_is_only_command_center_navigation_filter():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")

    assert "function WorkStatePanel" in source
    assert "function ProjectTabs" in source
    assert "Issue Pulse" not in source
    work_state_source = source.split("function WorkStatePanel", 1)[1].split("function ProjectTabs", 1)[0]
    assert "Work State" in work_state_source
    assert "Command Center lanes" in work_state_source
    for lane in ("Overview", "Inbox", "Active", "Completed", "Archive"):
        assert lane in work_state_source
    lane_order = [work_state_source.index(f'label: "{lane}"') for lane in ("Overview", "Inbox", "Active", "Completed", "Archive")]
    assert lane_order == sorted(lane_order)
    assert "Work states by status" not in source
    assert "onSelectStatus" not in source
    assert "activeStatus" not in source
    assert "status bar" not in source.lower()
    assert "role=\"list\"" not in work_state_source
    assert "<button" not in work_state_source
    assert 'key: "workers"' not in work_state_source
    assert 'href="/workers"' not in work_state_source
    assert 'if (normalized.includes("/runs")) return "runs";' in source
    assert 'if (normalized.includes("/completed") || normalized.includes("/rejected")) return "completed";' in source


def test_command_center_project_tabs_render_above_work_state_and_preserve_lanes():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    page_source = source.split("export default function CommandCenterPage", 1)[1]
    render_source = page_source.split("return (", 1)[1]

    assert "Command Center projects" in source
    assert "command-center-project-tabs" in source
    assert "command-center-project-tab-row" in source
    assert "projects={snapshot?.projects ?? []}" in source
    assert "to={{ pathname, search: tabSearch(project.key) }}" in source
    assert "to={{ pathname: lane.href, search }}" in source
    assert 'href="/workers" rel="noopener noreferrer" target="_blank"' in source
    assert "Kanban <ExternalLink" in source
    assert "opens in a new tab" in source.split("function ProjectTabs", 1)[1].split("function ActionButton", 1)[0]
    assert render_source.index("<ProjectTabs") < render_source.index("<WorkStatePanel")
    for forbidden in ("Operator Surface", "KPI", "status distribution"):
        assert forbidden not in source
    assert "Sligo operator navigation" not in source


def test_command_center_uses_cohesive_list_and_audit_layout_without_kpi_chrome():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "web/src/index.css").read_text(encoding="utf-8")
    page_source = source.split("export default function CommandCenterPage", 1)[1]

    assert "command-center-shell" in page_source
    assert "command-center-content-grid" in page_source
    assert "command-center-list-pane" in page_source
    assert "function AuditPane" in source
    assert 'aria-label="Command Center detail and audit"' in source
    assert "Detail / Audit" in source
    assert "selectedItems[0] || visibleWorkItemsForAudit[0] || null" in source
    assert ".command-center-content-grid" in styles
    assert ".command-center-audit-sticky" in styles
    assert "@media (min-width: 1180px)" in styles
    assert "grid-template-columns: minmax(0, 1fr) minmax(18rem, 22rem);" in styles
    for forbidden in ("Operator Surface", "KPI", "status distribution", "Work states by status"):
        assert forbidden not in source


def test_command_center_worker_and_ticket_links_open_new_tabs():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert 'window.open(workerUrl, "_blank", "noopener,noreferrer")' in card_source
    assert 'role={workerUrl ? "link" : undefined}' in card_source
    assert 'tabIndex={workerUrl ? 0 : undefined}' in card_source
    assert 'href={workerUrl} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank"' in card_source
    assert "Worker board <ExternalLink" in card_source
    assert 'href={item.execution.task_url} onClick={(event) => event.stopPropagation()} rel="noopener noreferrer" target="_blank"' in card_source


def test_command_center_rows_use_visual_status_indicators_without_visible_status_labels():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    styles = (ROOT / "web/src/index.css").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert "function StatusPill" not in source
    assert "function RunningIndicator" not in source
    assert "function StatusGlyph" in source
    assert "function StatusRail" in source
    assert "function runningDescriptor" in source
    assert "const running = isRunningWorkItem(item);" in card_source
    assert "command-center-card-running" in card_source
    assert "<StatusRail status={visualStatus} />" in card_source
    assert "command-center-status-rail" in source
    assert "{running && <RunningMeter />}" in card_source
    assert "command-center-live-meter" in source
    assert "<StatusPill" not in source
    assert "<span>Running</span>" not in source
    assert "{value || \"unknown\"}" not in source
    assert "aria-label={`Status:" in source
    assert "command-center-status-breathe" in styles
    assert "command-center-running-chip" not in styles
    assert ".sligo-light .command-center-card-running" in styles
    assert ".sligo-light .command-center-status-running.command-center-status-indicator" in styles


def test_command_center_archive_action_is_one_click_without_removing_other_prompts():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    archive_branch = source.split('if (kind === "archive") {', 1)[1].split('} else if (proposalId && kind === "approve")', 1)[0]

    assert "window.confirm" not in archive_branch
    assert "archiveKanbanBoard" in archive_branch
    assert "archiveSelfImprovementProposal" in archive_branch
    assert "haltSelfImprovementProposal" in archive_branch
    assert 'window.prompt("Reject reason for future prong feedback?"' not in source
    assert 'window.prompt("Reason for revert follow-up?"' in source


def test_command_center_proposal_archive_action_uses_halt_flow():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    handle_source = source.split("const handleAction = useCallback", 1)[1].split("return (", 1)[0]
    run_action_source = source.split("const runActionForItem = useCallback", 1)[1].split("const handleAction = useCallback", 1)[0]

    assert 'type ActionKind = "approve" | "pause" | "replay" | "repair" | "undo" | "archive";' in source
    assert 'if (canApproveReject) actions.push("approve", "archive");' in source
    assert 'window.prompt("Reject reason for future prong feedback?"' not in source
    assert 'rejectReason' not in handle_source
    assert 'await api.rejectSelfImprovementProposal' not in run_action_source
    assert 'else if (proposalId && item.status === "proposed") await api.archiveSelfImprovementProposal(proposalId);' in run_action_source
    assert 'else if (proposalId) await api.haltSelfImprovementProposal(proposalId);' in run_action_source
    assert "targetItems" in handle_source


def test_command_center_completed_lane_is_separate_and_paginated():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    completed_branch = source.split('activeView === "completed"', 1)[1].split('activeView === "archive"', 1)[0]

    assert 'type ViewKey = "overview" | "inbox" | "work" | "completed" | "archive"' in source
    assert 'type PaginatedViewKey = "overview" | "inbox" | "work" | "completed" | "archive";' in source
    assert 'function isCompletedItem(item: CommandCenterWorkItem): boolean' in source
    assert 'return item.status === "archived";' in source
    assert 'return ["shipped", "done"].includes(item.status);' in source
    assert 'if (normalized.includes("/completed") || normalized.includes("/rejected")) return "completed";' in source
    assert 'shipped: { label: "Shipped", icon: CheckCircle2' in source
    assert 'completed: completedItems.length' in source
    assert 'const pagedCompletedItems = useMemo(() => pageSlice(completedItems, pages.completed)' in source
    assert 'activeView === "completed"' in source
    assert 'setPage("completed", page)' in source
    assert '<PaginationControls label="completed"' in source
    assert 'showActions={false}' not in completed_branch
    assert 'items={pagedCompletedItems}' in completed_branch


def test_command_center_completed_lane_preserves_revert_and_archive_buttons():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    action_source = source.split("function availableActionKinds", 1)[1].split("function actionSet", 1)[0]
    completed_branch = source.split('activeView === "completed"', 1)[1].split('activeView === "archive"', 1)[0]

    assert 'const canUndo = Boolean(isCompletedItem(item) && ((proposalId && item.decision?.undo_followup_action) || (board && item.execution?.undo_followup_action)));' in action_source
    assert 'actions.push("undo")' in action_source
    assert 'actions.push("archive")' in action_source
    assert action_source.index('actions.push("undo")') < action_source.index('actions.push("archive")')
    assert 'await api.requestKanbanBoardUndoFollowup(board, reason);' in source
    assert 'if (proposalId && item.decision?.undo_followup_action) await api.requestSelfImprovementUndoFollowup(proposalId, reason);' in source
    assert 'else if (board && item.execution?.undo_followup_action) await api.requestKanbanBoardUndoFollowup(board, reason);' in source
    assert 'showActions={false}' not in completed_branch
    assert 'showActions={false}' in source.split('activeView === "archive"', 1)[1]


def test_command_center_archive_action_renders_last_in_row_rail():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    action_source = source.split("function availableActionKinds", 1)[1].split("function actionSet", 1)[0]
    row_footer = source.split('className="command-center-card-footer mt-4 border-t border-white/[0.08] pt-3"', 1)[1].split("</article>", 1)[0]

    assert action_source.index('actions.push("approve", "archive")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("replay")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("pause")') < action_source.index('actions.push("archive")')
    assert action_source.index('actions.push("undo")') < action_source.index('actions.push("archive")')
    assert "command-center-card-link-group" in row_footer
    assert "command-center-card-action-group" in row_footer
    assert row_footer.index("command-center-card-link-group") < row_footer.index("command-center-card-action-group")
    assert "actions.map((kind)" in row_footer
    assert "kind={kind}" in row_footer
    assert "command-center-card-timestamp" in row_footer


def test_command_center_repair_action_wiring_and_footer_removed():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "web/src/lib/api.ts").read_text(encoding="utf-8")
    action_source = source.split("function availableActionKinds", 1)[1].split("function actionSet", 1)[0]

    assert "Wrench" in source
    assert 'repair: "Repairing"' in source
    assert 'repair: { label: "Repair", icon: Wrench' in source
    assert 'actions.push("repair")' in action_source
    assert 'api.repairKanbanBoard(board' in source
    assert 'repair_action?: string | null;' in api_source
    assert '/boards/${encodeURIComponent(slug)}/repair' in api_source
    assert "Worker-board work rolls up" not in source
    assert "snapshot?.summary" not in source
    assert "Send" not in source


def test_command_center_row_actions_lock_during_refresh_settle():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]
    handle_source = source.split("const handleAction = useCallback", 1)[1].split("return (", 1)[0]

    assert "const ACTION_SETTLE_MS = 600" in source
    assert "const actionDisabled = (kind: ActionKind) => (Boolean(activeAction) && !actionBusy(kind)) || (selectionActive && (!selected || !multiSelectActionCommon.has(kind)));" in card_source
    assert "aria-busy={rowBusy || undefined}" in card_source
    assert "<div aria-busy={rowBusy}" in card_source
    assert "command-center-card-footer" in card_source
    assert "aria-live=\"polite\"" in card_source
    assert "const disabled = actionDisabled(kind);" in card_source
    assert "disabled={disabled}" in card_source
    assert "if (activeAction) return;" in handle_source
    assert "delayBeforeApplyMs: Math.max(0, ACTION_SETTLE_MS - (Date.now() - startedAt))" in handle_source
    assert "settleAfterApplyMs: ACTION_SETTLE_MS" in handle_source
    assert "preserveMissingWorkItemId" not in source


def test_command_center_worker_url_has_real_row_action_link():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert "const workerUrl = item.execution?.worker_url || null" in card_source
    assert "Open worker board for" in card_source
    assert "Worker board <ExternalLink" in card_source
    assert 'href={workerUrl}' in card_source
    assert 'onClick={(event) => event.stopPropagation()}' in card_source
    assert "function DetailPanel" not in source
    assert 'href="/workers"' not in card_source
    assert 'to="/workers"' not in card_source


def test_command_center_annotation_controls_are_accessible_and_do_not_open_cards():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "web/src/lib/api.ts").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]
    button_source = source.split("function AnnotationButton", 1)[1].split("function AnnotationSummary", 1)[0]

    assert "createCommandCenterAnnotation" in api_source
    assert 'CommandCenterAnnotationMode = "note" | "correction"' in api_source
    assert "annotations?: CommandCenterAnnotation[];" in api_source
    assert "operator_note_count?: number;" in api_source
    assert "latest_operator_note?: CommandCenterAnnotation | null;" in api_source
    assert "latest_correction?: CommandCenterAnnotation | null;" in api_source
    assert card_source.count("<AnnotationButton item={item} onOpen={onAnnotate} />") == 1
    assert "mode=\"correction\"" not in card_source
    assert "Add operator note" in button_source
    assert "Correct or redirect work" not in button_source
    assert 'onOpen(item, "note");' in button_source
    assert "event.stopPropagation();" in button_source
    assert "onAnnotate={openAnnotationDraft}" in source


def test_command_center_annotation_modal_accessibility_and_pause_condition():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    modal_source = source.split("function AnnotationModal", 1)[1].split("function SourceBadge", 1)[0]

    assert "useModalBehavior({ open: true, onClose })" in modal_source
    assert 'role="dialog"' in modal_source
    assert 'aria-modal="true"' in modal_source
    assert "aria-labelledby={titleId}" in modal_source
    assert "aria-describedby={descriptionId}" in modal_source
    assert "data-autofocus" in modal_source
    assert "Note / clarification" in modal_source
    assert "Correction / redirect" in modal_source
    assert "Annotation text" in modal_source
    assert "required" in modal_source
    assert "Optional correction title" in modal_source
    assert 'selectedMode === "correction" ?' in modal_source
    assert "const canPauseCurrent = isRunningWorkItem(item);" in modal_source
    assert 'selectedMode === "correction" && canPauseCurrent ?' in modal_source
    assert "Pause current work while applying this correction" in modal_source
    assert "pause_current: selectedMode === \"correction\" && canPauseCurrent && pauseCurrent" in modal_source


def test_command_center_annotations_render_as_safe_plain_text():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    summary_source = source.split("function AnnotationSummary", 1)[1].split("function AnnotationModal", 1)[0]

    assert "<AnnotationSummary item={item} />" in source
    assert "Latest correction" in summary_source
    assert "{latestCorrection.title}" in summary_source
    assert "{latestCorrection.text}" in summary_source
    assert "operator {noteCount === 1 ? \"note\" : \"notes\"}" in summary_source
    assert "Latest note: {latestNote.text}" in summary_source
    assert "dangerouslySetInnerHTML" not in source
    assert "marked(" not in source
    assert "markdown" not in summary_source.lower()


def test_command_center_work_item_full_description_toggle_is_accessible_plain_text():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    api_source = (ROOT / "web/src/lib/api.ts").read_text(encoding="utf-8")
    card_source = source.split("function WorkItemCard", 1)[1].split("function SourceCard", 1)[0]

    assert "full_description?: string | null;" in api_source
    assert "const compactDescription = item.summary || item.body_preview || \"No summary yet.\";" in card_source
    assert "const fullDescription = item.full_description?.trim();" in card_source
    assert "canShowFullDescription" in card_source
    assert "ChevronDown" in source
    assert "command-center-description-disclosure" in card_source
    assert "Full context" in card_source
    assert "Expand full context for" in card_source
    assert "Collapse full context for" in card_source
    assert "Show full description" not in card_source
    assert "Hide full description" not in card_source
    assert "aria-expanded={fullDescriptionOpen}" in card_source
    assert "aria-controls={descriptionId}" in card_source
    assert "rotate-180" in card_source
    assert "whitespace-pre-wrap" in card_source
    assert "{fullDescription}" in card_source
    assert "event.stopPropagation();" in card_source
    assert "item.raw" not in card_source
    assert "dangerouslySetInnerHTML" not in card_source


def test_command_center_annotation_submit_surfaces_partial_failures_and_refreshes():
    source = (ROOT / "web/src/pages/CommandCenterPage.tsx").read_text(encoding="utf-8")
    submit_source = source.split("const submitAnnotation = useCallback", 1)[1].split("const runActionForItem = useCallback", 1)[0]

    assert "await api.createCommandCenterAnnotation(annotationDraft.item.id, payload);" in submit_source
    assert "await refresh();" in submit_source
    assert "result.errors" in submit_source
    assert "followup_task" in submit_source
    assert "pause_current" in source
    assert "setAnnotationError(err instanceof Error ? err.message : String(err));" in submit_source
    assert "setError(warnings);" in submit_source
    assert "role=\"status\"" in source


def test_sligo_shell_has_no_duplicate_top_tab_navigation():
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    shell_source = source.split("function SligoSurfaceShell", 1)[1].split("export default function App", 1)[0]

    assert "Sligo operator navigation" not in shell_source
    assert "<SligoNavLink" not in shell_source
    assert "Refresh Command Center" in shell_source


def test_sligo_completed_route_is_registered_with_rejected_redirect():
    source = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")

    assert '"/sligo/completed": CommandCenterPage' in source
    assert '"/sligo/rejected": () => <RootRedirect to="/sligo/completed" />' in source
    assert '"/sligo/rejected": HermesToSligoRedirect' in source
    assert '{ path: "/sligo/completed", label: "Completed" }' in source
    assert '{ path: "/sligo/rejected", label: "Rejected" }' not in source


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
