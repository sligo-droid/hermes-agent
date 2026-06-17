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

## Reference Check Results

- Repo/runtime references to the quarantined path were found in historical Hermes session summaries and worktree inventory output only; no checked repo source in this implementation branch hard-coded it before this quarantine.
- Active user systemd units included Hermes gateway/dashboard/proxy services and the current worker units, but no unit description or command in the inspected output referenced `/home/droid/workspaces/hermes-self-improvement-live-test`.
- User crontab returned `no crontab for droid`.
- Service/cron inspection was read-only. If operators need process-level environment confirmation, inspect live process environments before cleanup approval.

## Risky Worktree Inventory

| Path | Branch | HEAD | Tracking | Dirty count | Status | Proposed disposition |
| --- | --- | --- | --- | ---: | --- | --- |
| `/home/droid/workspaces/hermes-self-improvement-live-test` | `main` | `30818b22616805e61233e5097d0ebc7821cfbbd6` | `origin/main` | 247 | dirty secondary main; quarantined | Preserve patch/file summary, then human chooses branch/archive/reset. |
| `/home/droid/.hermes/worktrees/hermes-silent-compaction` | not present in current `git worktree list --porcelain` output | unknown | previously reported gone | unknown | stale/gone example from context, not found in current registered worktrees | Operator should inspect filesystem path before deleting; preserve diff first if it exists. |
| `/home/droid/workspaces/.hermes-discord-1507071138612642073` | not present in current `git worktree list --porcelain` output | unknown | previously reported `origin/discord/1507071138612642073: gone` | unknown | stale/dirty gone example from context, not found in current registered worktrees | Operator should inspect filesystem path before cleanup; preserve diff first if it exists. |
| `/home/droid/workspaces/codex-worker-hermes-routing` | not present in current `git worktree list --porcelain` output | unknown | previously reported `[ahead 8833, behind 8831]` | unknown | divergent example from context, not found in current registered worktrees | Operator should locate path/branch before cleanup; do not force reset without approval. |

The current porcelain worktree list is large and remains the authoritative live registry. This ticket did not delete or prune stale records.

## Cleanup Plan Requiring Human Approval

1. Preserve: export a full patch from `/home/droid/workspaces/hermes-self-improvement-live-test` and store it in an operator-approved artifact location outside any worktree scheduled for deletion.
2. Classify: decide whether the 247-file staged diff is an abandoned rollback, a useful branch, or obsolete test debris.
3. Branch/archive/reset: only after preservation and approval, either create an archival branch, archive the directory, or reset/remove the worktree.
4. Reconcile stale examples: inspect `/home/droid/.hermes/worktrees/hermes-silent-compaction`, `/home/droid/workspaces/.hermes-discord-1507071138612642073`, and `/home/droid/workspaces/codex-worker-hermes-routing` directly; preserve dirty diffs before any cleanup.
5. Remove quarantine only after the path is clean or gone and no worker/config reference can select it.
