# Project State

Last updated: 2026-06-26
State owner: Sligo Labs agent

This is the active pickup-state file for Sligo Labs' `hermes-agent` fork. Keep it short and current. Move completed ledger material to `docs/archive/project-state-archive-20260626.md` or a newer dated archive instead of letting this file become historical context bloat.

## Current Focus

- Keep Hermes stable as Sligo Labs' command infrastructure and dev-shop operator. Reliability for Discord, Kanban, gateway, worker routing, and Command Center matters more than novelty.
- Keep project continuity in repo docs: `AGENTS.md`, this file, `docs/context.md`, focused runbooks, and `docs/decisions/`. Skills are reusable procedures only, not the target/status ledger.
- Use Command Center as the canonical operator ledger over the Work Item / Source / Worker Run model for Hermes and PID project scopes. Do not reintroduce competing self-improvement or worker-board ledgers.
- Continue the skill-to-repo transition by removing current-state and target-setting prose from installed Hermes skills and replacing it with pointers to this file and `docs/context.md`.
- Preserve the dirty secondary main worktree quarantine for `/home/droid/workspaces/hermes-self-improvement-live-test` until a human-approved reconciliation follows `docs/runbooks/worktree-quarantine-2026-06-17.md`.

## Active Snapshot

| Area | State | Active pickup note |
| --- | --- | --- |
| Repo-state boundary | in_progress | PR #320 merged the repo-native state/context split. Future durable state changes should update this file, `docs/context.md`, a runbook, or a decision doc, not a skill. |
| Command Center model | merged | `docs/sligo-command-center.md` is the durable product-model reference for Work Items, Sources, Worker Runs, lanes, actions, and detail/audit behavior. Live runtime still needs focused smoke tests when code changes. |
| Autonomous operations loop | planned | `docs/plans/2026-06-08-autonomous-operations-loop-goalplan.md` remains the durable target for policy-gated Observe -> Diagnose -> Decide -> Execute -> Verify -> Learn autonomy. |
| Skills boundary | in_progress | Installed default-profile pointers were patched for `hermes-agent`, `hermes-operations`, and `general-coding/references/operator-command-center-ui.md`; continue moving intention-setting material into repo docs. |
| Worker autoreview helper | implemented | Hermes/PID-style worker bootstraps materialize `.agents/skills/autoreview/scripts/autoreview`; workers should run it after non-trivial edits and treat findings as deterministic advisory checks. |
| Upstream sync workflow | implemented | `docs/runbooks/upstream-sync.md` records the worktree-based `upstream/main` merge procedure, fork-preserving conflict policy, and smoke gate. |
| Dirty secondary main worktree quarantine | in_progress | `/home/droid/workspaces/hermes-self-improvement-live-test` remains excluded from Discord role-worker selection via `kanban.discord_worker.excluded_workspaces`; keep it out of worker routing until reconciled. |

Allowed states: `planned`, `ready`, `in_progress`, `blocked`, `implemented`, `merged`, `deployed`, `verified`, `superseded`.

## Current Command Center UX Intent

This product intention belongs in repo docs, not skills. If the direction changes, update this section or `docs/sligo-command-center.md` in the same PR.

- The in-body `Work State` lane is the only primary work-status navigator. Keep Hermes/PID project scoping, but do not add duplicate top tabs, KPI rows, status-distribution bars, or stale `Operator Surface` chrome.
- Rows are Work Items: show proposed recommendations, decision/intake items, and board-level worker rollups. Do not show individual Kanban task/ticket rows or accepted downstream proposal rows without a canonical Work Item/board rollup.
- Source-only cron runs, including self-improvement parse-failure provenance, stay in Sources/metrics unless converted into a canonical Work Item needing operator action.
- Worker boards, Kanban tasks, task runs, PRs, logs, and deploys are execution artifacts attached to Work Items, not a competing ledger.
- Layout target: Sligo shell header with one refresh/control area; left work list plus right detail/audit pane; source/status chips before title; created-date footer; active/running work visibly above quieter proposed/non-running work.
- Action target: compact icon row actions with real affordances. Proposed self-improvement items expose Approve native, Worker board, Reject, and Archive actions. Completed jobs show Revert plus Archive only when those actions are real.
- Worker/source links: show a Worker pill only after execution starts, point it directly to the worker board URL, and never use bare `/workers` as a per-item destination. Show direct Discord source/thread links when metadata exists.
- Descriptions: rows show compact summaries by default, with a quiet `Full context` disclosure when richer request/proposal context exists.
- Terminal board status: stale blocked/paused Discord thread embed state should not override stronger terminal board evidence such as all tasks done, approved reviewer verdict, merged/green PR metadata, or synced canonical checkout metadata. Real running work still wins over stale terminal metadata.
- Archive is a historical ledger, including boards moved under `boards/_archived/`; archived rows are not actionable as live worker boards.

## Active Risks And Blockers

- No hard blocker for the repo-state transition, but it remains incomplete until future Hermes PRs routinely update repo state docs and skills stop accumulating current-state sediment.
- Dirty secondary main worktree cleanup is deliberately parked behind human approval; do not let automatic worker routing select `/home/droid/workspaces/hermes-self-improvement-live-test`.
- Any code/runtime change affecting operator behavior should update this file or the focused doc/runbook in the same PR.

## Parked / Not Current Focus

- Normal Discord action-request global concurrency limiter: designed previously, not implemented.
- Browser/typed-input secret redaction hardening: useful core safety work, but not part of the repo-state transition.
- Orphan-DAG tests and PID-specific Operations checks: belong in PID repo state unless Hermes orchestration changes.
- Broad stale-worktree cleanup: useful hygiene, but should not distract from the repo-state boundary unless it blocks current work.
- Broad automatic worktree janitor/preflight: out of scope for the current dirty-main quarantine; use the runbook inventory for human-approved cleanup.

## External Configuration

Do not store secret values here. Record names and locations only.

- Hermes runtime config: `$HERMES_HOME/config.yaml`.
- Hermes secrets: `$HERMES_HOME/.env`.
- Gateway/Dashboard/Command Center runtime evidence: `$HERMES_HOME/logs/`, `$HERMES_HOME/state.db`, `$HERMES_HOME/kanban/`, and systemd user services.
- Public/internal routes and deployment handles should be recorded as evidence when they matter, not copied as long logs.

## Historical Archive

- Completed ledger entries and the pre-trim snapshot are preserved in `docs/archive/project-state-archive-20260626.md`.
- Use the archive for historical lookup only. If an archived item becomes active again, copy the minimal active pickup fact back into this file instead of expanding the archive inline.

## Next Actions

1. On every meaningful Hermes repo PR, update this file if current focus, blockers, live runtime state, Command Center behavior, or worker/gateway operational truth changed.
2. Keep moving current-state material out of skills into this repo doc set. Skills should link here and keep only reusable procedure, pitfalls, and verification checklists.
3. Add a Hermes repo QMD index only if retrieval friction remains after this file and `docs/context.md` exist. Do not use `qmd-skills` as the long-term substitute for repo state.
4. When a broad request arrives from `#dev`, read this file before choosing work so Hermes does not chase whichever skill/reference happened to be loaded most recently.

## Future Worker Notes

- Start with `AGENTS.md`, then this file, then `docs/context.md`.
- Use `docs/sligo-command-center.md` for Command Center product-model work.
- Use `docs/decisions/` for durable rationale. If the rationale is surprising or reversal-prone, do not bury it in a skill.
- If you are about to patch `hermes-agent` or `hermes-operations` with current state, stop and update this repo state/context split instead.
