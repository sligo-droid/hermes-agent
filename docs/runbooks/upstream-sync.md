# Upstream Sync Runbook

Use this when pulling `NousResearch/hermes-agent` `upstream/main` into the Sligo Labs fork.

## Goals

- Keep `/home/droid/hermes` clean and on `main` until the PR is merged.
- Preserve fork-owned Discord, Kanban, gateway, Command Center, worker, and profile-isolation behavior.
- Pull in upstream changes that merge cleanly, then fix concrete integration regressions found by tests.
- Record conflict policy so the next upstream sync can use rerere/history instead of rediscovering choices.

## Procedure

1. Inspect state from the canonical checkout: `git status --short --branch`, `git remote -v`, `git worktree list`, and `docs/project-state.md`.
2. Fetch both remotes: `git fetch origin --prune` and `git fetch upstream --prune`.
3. Measure divergence: `git rev-list --left-right --count origin/main...upstream/main` and inspect `git log --oneline origin/main..upstream/main`.
4. Create a worktree under `/home/droid/workspaces/` from `origin/main`, for example `git worktree add -b merge/upstream-main-YYYY-MM-DD /home/droid/workspaces/hermes-upstream-main-YYYYMMDD origin/main`.
5. Start the merge without auto-commit: `git merge --no-ff --no-commit upstream/main`.
6. Resolve conflicts conservatively unless there is clear local evidence to do otherwise.
7. Run `scripts/run_tests.sh`; fix concrete failures from the merged tree.
8. Add this runbook or `docs/project-state.md` updates when the sync reveals a reusable policy or durable operational state.
9. Commit, push, open a PR, wait for CI, merge, then fast-forward the canonical checkout when clean.

## Conflict Policy

- For fork-deleted CI workflows or upstream surfaces, keep the fork deletion unless the current fork explicitly needs the upstream workflow again.
- For files upstream deleted but the fork still modifies, keep the fork file when it backs active fork functionality.
- For content conflicts in fork-owned runtime surfaces, prefer fork-side behavior first, then reintroduce upstream code only when tests or local evidence show it composes.
- For dependency manifests, do not convert package managers during the sync. Keep root/workspace manifests and the lockfile as one contract, then verify the install command used by CI.
- For broad upstream docs/skill catalog reshuffles, accept clean upstream changes but preserve local current-state docs and Sligo-specific operational guidance.

## Verification

- Minimum local gate: `scripts/run_tests.sh` from the merge worktree.
- If npm workspaces or lockfiles changed, run `npm ci` and the Typecheck workflow's package commands locally before pushing.
- If smoke fails, run focused tests for the failing areas, fix the smallest integration break, then rerun smoke.
- Before committing, check for unmerged paths and conflict markers: `git diff --name-only --diff-filter=U` and a search for `<<<<<<<` / `>>>>>>>`.
- In the PR body, include upstream head SHA, divergence counts, conflict policy, and test results.

## 2026-06-23 Sync Notes

- Synced `upstream/main` at `351afd353` into `origin/main` at `374a19327`.
- Initial divergence was `10126 2538` from `git rev-list --left-right --count origin/main...upstream/main`.
- First-pass conflicts used the conservative fork-side policy above; upstream non-conflicting changes were retained.
- Smoke initially exposed regressions in tool middleware/hooks, file write safety guards, skills-hub recursive vetting, browse progress callbacks, and platform-bundle disabling. Those were fixed before commit.
- CI exposed npm workspace manifest/lockfile drift and TUI prop-contract drift; root/workspace manifests were realigned with the lockfile, `npm ci` and the Typecheck package commands passed locally, and the workflow now installs the declared npm version before `npm ci`.
- Verification: `scripts/run_tests.sh` passed with `856 passed, 16 warnings`.
