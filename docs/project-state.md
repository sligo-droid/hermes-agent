# Project State

Last updated: 2026-06-06 18:08 UTC
State owner: Sligo Labs agent

This is the canonical repo-backed state file for Sligo Labs' `hermes-agent` fork. It exists to stop Hermes project continuity from drifting into skills, memories, Discord threads, or stale worktrees.

## Current focus

- Move Hermes project continuity and intention-setting to repo-tracked docs, matching the PID pattern: `AGENTS.md`, this file, `docs/context.md`, and focused decisions/runbooks.
- Keep the main target stable: Hermes is Sligo Labs' command infrastructure and dev-shop operator. Stability, reliable Discord/Kanban/gateway behavior, and a clean Command Center work ledger beat novelty.
- Treat skills as procedural memory only. If a note says what Hermes is currently trying to accomplish, what is shipped, what is blocked, or what the next session should do, it belongs here or in a linked repo doc.

## Snapshot

| Area | State | Evidence |
| --- | --- | --- |
| Main branch | verified | `/home/droid/hermes` was fast-forwarded to `origin/main` after PR #320 merged (`aa988915a docs: add Hermes repo project state (#320)`). |
| Open PRs | verified | `gh pr list --state open --limit 10` returned no rows after PR #320 merged. |
| Repo-state transition | merged | PR #320 added `docs/project-state.md`, `docs/context.md`, and `docs/decisions/0001-repo-backed-project-state.md`; `AGENTS.md` now routes workers here. |
| Command Center model | merged | `docs/sligo-command-center.md` records the Work Item / Source / Worker Run model, Hermes/PID project scoping, and current UX contract. Live runtime must still be smoked separately when code changes. |
| Skills boundary | in_progress | Installed default-profile pointers were patched for `hermes-agent`, `hermes-operations`, and `general-coding/references/operator-command-center-ui.md`; intention-setting material should keep moving into this repo state/doc set. |

Allowed states: `planned`, `ready`, `in_progress`, `blocked`, `implemented`, `merged`, `deployed`, `verified`, `superseded`.

## Done

- [x] Added a repo-native Hermes state/context split so future sessions have a single pickup point instead of reconstructing target from skills and memory.
- [x] Recorded the knowledge-store boundary: repo for Hermes project state, skills for reusable procedures, memory for compact durable facts, Obsidian for synthesis, Notion for client-facing docs/feedback.
- [x] Preserved the existing Command Center architecture doc as the focused product-model reference instead of duplicating it here.
- [x] Patched the installed default-profile `hermes-agent`, `hermes-operations`, and Command Center UI skill/reference pointers so they route current-state material back to repo docs.
- [x] Tightened completed Discord worker-board terminal summaries: merged PRs are inferred from PR state/merge evidence even when GitHub reports post-merge `mergeStateStatus=UNKNOWN`, deployment status falls back to done for merged+green PRs when no separate deployment check exists, recovered reviewer verdict metadata is surfaced, stale summary sidecars are rebuilt on terminal-board views, and concise outcomes are included in the terminal block.

## In Progress

- **Skill-to-repo transition:** remove current-state and target-setting prose from installed Hermes skills over time. Leave short pointers from `hermes-agent`, `hermes-operations`, and related references to this file and `docs/context.md`.
- **Command Center as operator ledger:** keep Hermes (`#dev`) and PID as project tabs over one Work Item / Source / Worker Run model. Do not reintroduce competing self-improvement/worker-board ledgers.

## Current Command Center UX Intent

This is product intention, so it belongs in repo state/docs, not in a skill. If the Command Center direction changes, update this section or `docs/sligo-command-center.md` in the same PR.

- **One work-status model:** the in-body `Work State` lane is the only primary work-status navigator. Keep project scoping for Hermes/PID, but do not add duplicate top tabs, KPI rows, status-distribution bars, or stale `Operator Surface` chrome.
- **Rows are Work Items:** show proposed recommendations, decision/intake items, and board-level worker rollups. Do not show individual Kanban task/ticket rows or accepted downstream proposal rows without a canonical Work Item/board rollup.
- **Board execution stays child-level:** worker boards, Kanban tasks, task runs, PRs, logs, and deploys are execution artifacts attached to Work Items, not a competing ledger.
- **Layout target:** Sligo shell header with one refresh/control area; left work list plus right detail/audit pane; source/status chips before title; created-date footer; active/running work visibly above quieter proposed/non-running work.
- **Action target:** compact icon row actions with real affordances. Archive is one-click for archiveable non-default boards; pause/replay/cancel/revert/approve/reject are shown only when source and lifecycle state make them real.
- **Worker/source links:** show a Worker pill only after execution starts, point it directly to the worker board URL, and never use bare `/workers` as a per-item destination. Show direct Discord source/thread links when metadata exists.
- **Archive target:** Archive is a historical ledger, including boards moved under `boards/_archived/`; archived rows are not actionable as live worker boards.

## Parked / Not Current Focus

These are known ideas or cleanup candidates, not the active target unless the user promotes them:

- Normal Discord feature-request global concurrency limiter: designed previously, not implemented.
- Browser/typed-input secret redaction hardening: useful core safety work, not this transition.
- Orphan-DAG tests and PID-specific Operations checks: belong in PID repo state, not Hermes state, unless the Hermes orchestration layer changes.
- Broad stale-worktree cleanup: useful hygiene, but should not distract from the repo-state boundary unless it blocks current work.

## Blocked

- No hard blocker for the initial repo-state transition.
- The transition remains incomplete until future Hermes PRs routinely update this file when durable state changes, and skills stop accumulating current-state sediment.

## External Configuration

Do not store secret values here. Record names and locations only.

- Hermes runtime config: `$HERMES_HOME/config.yaml`.
- Hermes secrets: `$HERMES_HOME/.env`.
- Gateway/Dashboard/Command Center runtime evidence: `$HERMES_HOME/logs/`, `$HERMES_HOME/state.db`, `$HERMES_HOME/kanban/`, and systemd user services.
- Public/internal routes and deployment handles should be recorded as evidence when they matter, not copied as long logs.

## Next Actions

1. On every meaningful Hermes repo PR, update this file if current focus, blockers, live runtime state, Command Center behavior, or worker/gateway operational truth changed.
2. Keep moving current-state material out of skills into this repo doc set. Skills should link here and keep only reusable procedure, pitfalls, and verification checklists.
3. Add a Hermes repo QMD index only if retrieval friction remains after this file and `docs/context.md` exist. Do not use `qmd-skills` as the long-term substitute for repo state.
4. When a broad request arrives from `#dev`, read this file before choosing work so Hermes does not chase whichever skill/reference happened to be loaded most recently.

## Verification Checklist

- [x] Main checkout status inspected before creating this file.
- [x] Existing repo docs inspected; no pre-existing `docs/project-state.md`, `docs/context.md`, or `STATUS.md` was present.
- [x] This file distinguishes implemented, verified, in-progress, blocked, and parked work.
- [x] No secret values are stored here.
- [ ] Future code/runtime changes that affect operator behavior update this file in the same PR.

## Future Worker Notes

- Start with `AGENTS.md`, then this file, then `docs/context.md`.
- Use `docs/sligo-command-center.md` for Command Center product-model work.
- Use `docs/decisions/` for durable rationale. If the rationale is surprising or reversal-prone, do not bury it in a skill.
- If you are about to patch `hermes-agent` or `hermes-operations` with current state, stop and update this repo state/context split instead.
