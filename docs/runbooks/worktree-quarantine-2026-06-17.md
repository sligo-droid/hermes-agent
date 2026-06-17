# Worktree Quarantine Inventory - 2026-06-17

This report quarantines the secondary Hermes `main` worktree at `/home/droid/workspaces/hermes-self-improvement-live-test`. It is not worker-selectable through the Discord role-worker launcher.

## Active Disposition

- Quarantined path: `/home/droid/workspaces/hermes-self-improvement-live-test`.
- Source of truth: `kanban.discord_worker.excluded_workspaces` default in `hermes_cli/config.py` includes the quarantined absolute path.
- Active runtime path checked: Discord role workers enter through `hermes_cli/kanban_codex_workers.py::spawn_codex_worker()`, which rejects excluded workspaces before host or Docker worker launch.
- No destructive cleanup was performed. Do not reset, delete, rebase, force-push, or commit this worktree without explicit operator approval.

## Provenance

- Canonical checkout check: `git -C /home/droid/hermes status --short --branch` returned `## main...origin/main`.
- Quarantined worktree check: `git -C /home/droid/workspaces/hermes-self-improvement-live-test status --short --branch` returned `## main...origin/main` plus 247 dirty entries.
- Dirty count: `git -C /home/droid/workspaces/hermes-self-improvement-live-test status --short | wc -l` returned `247` before quarantine documentation and was unchanged after implementation.
- Patch summary: `git -C /home/droid/workspaces/hermes-self-improvement-live-test diff --cached --stat` reported `247 files changed, 3878 insertions(+), 44947 deletions(-)`.
- File summary: `git -C /home/droid/workspaces/hermes-self-improvement-live-test diff --cached --name-status` produced the dirty file list. Representative entries include `.github/PULL_REQUEST_TEMPLATE.md`, `AGENTS.md`, `agent/conversation_loop.py`, deleted `docs/project-state.md`, deleted `hermes_cli/command_center.py`, and modified `web/src/App.tsx`.
- Worktree inventory source: `git worktree list --porcelain` from `/home/droid/hermes`.
- Reference checks: `rg -n "hermes-self-improvement-live-test" /home/droid/hermes /home/droid/.hermes /home/droid/workspaces --glob '!**/.git/**' --glob '!**/node_modules/**' --glob '!**/.venv/**'`; `systemctl --user list-units --all --no-pager`; `crontab -l`.
- Complete raw evidence bundle: `/home/droid/.local/share/opencode/tool-output/tool_ed5e97969001DOCKUW5tneZpTX`. It contains the canonical checkout status, the complete 247-entry dirty status, the cached diff stat, the complete 247-entry `diff --cached --name-status`, the full `git worktree list --porcelain` output, and a 355-row inventory table for registered Hermes worktrees under `/home/droid/workspaces/` and `/home/droid/.hermes/worktrees/`.

## Reference Check Results

- Repo/runtime references to the quarantined path were found in historical Hermes session summaries and worktree inventory output only; no checked repo source in this implementation branch hard-coded it before this quarantine.
- Active user systemd units included Hermes gateway/dashboard/proxy services and the current worker units, but no unit description or command in the inspected output referenced `/home/droid/workspaces/hermes-self-improvement-live-test`.
- User crontab returned `no crontab for droid`.
- Service/cron inspection was read-only. If operators need process-level environment confirmation, inspect live process environments before cleanup approval.

## Risky Worktree Inventory

| Path | Branch | HEAD | Tracking | Dirty count | Status | Proposed disposition |
| --- | --- | --- | --- | ---: | --- | --- |
| `/home/droid/workspaces/hermes-self-improvement-live-test` | `main` | `30818b22616805e61233e5097d0ebc7821cfbbd6` | `origin/main` | 247 | dirty secondary main; quarantined | Preserve patch/file summary, then human chooses branch/archive/reset. |
| `/home/droid/.hermes/worktrees/hermes-silent-compaction` | `fix/silent-compression-start-status` | `522e2efe359b872d97eed27109efbd6a2bdab644` | `origin/fix/silent-compression-start-status [gone]` | 0 | stale/gone registered worktree | Operator may remove only after confirming no out-of-band artifacts are needed. |
| `/home/droid/workspaces/.hermes-discord-1507071138612642073` | `discord/1507071138612642073` | `f6cb0f96f07dff9aad834662232f868f3cbf4af7` | `origin/discord/1507071138612642073 [gone]` | 1 | dirty gone registered worktree | Preserve dirty diff before any cleanup. |
| `/home/droid/workspaces/codex-worker-hermes-routing` | `codex-worker-hermes-routing` | `e78851827eaf353d446f9ece2d701601dbdebd0c` | `origin/codex-worker-hermes-routing [ahead 8833, behind 8831]` | 0 | divergent registered worktree | Do not force-reset; operator should decide whether to archive, rebase, or remove. |
| `/home/droid/workspaces/hermes-codex-proxy-pool` | `fix-codex-proxy-pool` | `96111882cd8d1b56b05273b2fc7849a9de41566f` | `origin/main [ahead 1, behind 10748]` | 0 | highly stale/divergent example | Preserve or retire only after human review. |
| `/home/droid/workspaces/hermes-discord-worker-concurrency` | `discord-worker-cross-board-concurrency` | `5b838c25e44ea71ccac270fd0aff70b8083e7b8c` | `origin/discord-worker-cross-board-concurrency [gone]` | 7 | dirty gone example | Preserve dirty diff before any cleanup. |
| `/home/droid/workspaces/merge-open-prs` | `merge-open-prs` | `05dbaf88cc3b1e954c666ade9bcacefeed876b37` | `origin/main [ahead 197, behind 11054]` | 0 | highly divergent example | Do not force-reset; operator should classify before cleanup. |

The current porcelain worktree list is large and remains the authoritative live registry. The complete review inventory is in the raw evidence bundle above and covers 355 registered Hermes worktrees under `/home/droid/workspaces/` and `/home/droid/.hermes/worktrees/`. This ticket did not delete, reset, prune, commit, rebase, or force-push any dirty or stale worktree.

## Cleanup Plan Requiring Human Approval

1. Preserve: export a full patch from `/home/droid/workspaces/hermes-self-improvement-live-test` and store it in an operator-approved artifact location outside any worktree scheduled for deletion.
2. Classify: decide whether the 247-file staged diff is an abandoned rollback, a useful branch, or obsolete test debris.
3. Branch/archive/reset: only after preservation and approval, either create an archival branch, archive the directory, or reset/remove the worktree.
4. Reconcile stale examples: inspect `/home/droid/.hermes/worktrees/hermes-silent-compaction`, `/home/droid/workspaces/.hermes-discord-1507071138612642073`, and `/home/droid/workspaces/codex-worker-hermes-routing` directly; preserve dirty diffs before any cleanup.
5. Remove quarantine only after the path is clean or gone and no worker/config reference can select it.
