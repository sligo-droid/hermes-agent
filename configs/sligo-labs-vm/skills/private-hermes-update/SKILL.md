---
name: private-hermes-update
description: Use when updating private Hermes from upstream.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [hermes-agent, git, private-fork, update]
    related_skills: [hermes-agent, github-repo-management]
---

# Private Hermes Update

## Overview

Use this skill when the user asks to update the Hermes Agent checkout on this machine. This checkout is a private patch stack over `NousResearch/hermes-agent`, not an upstream contribution branch.

The goal is to regularly absorb upstream changes while preserving local commits and local-only behavior. Do not treat local changes as disposable just because they are not upstreamable.

## When to Use

- The user asks to update Hermes, pull latest Hermes, sync with upstream, refresh the private fork, or rebase local Hermes changes.
- The working directory is `/home/droid/hermes` or the repo remote points at `NousResearch/hermes-agent`.
- The user asks what to commit or discard in the Hermes checkout before an update.

Do not use this for unrelated repositories. For normal one-off GitHub fork work, use the general GitHub repo workflow instead.

## Repository Model

Expected setup:

- `/home/droid/hermes` is the local checkout.
- `upstream` is `https://github.com/NousResearch/hermes-agent.git`.
- `origin` is the user's private fork.
- `main` is a clean mirror of `upstream/main`.
- `sligo/main` is the long-lived private branch that carries local commits.

Never push to `upstream`. Push only to `origin`.

Use `sligo/main` for private changes. Keep `main` boring and easy to reset to upstream.

## First Checks

Start every update request with read-only inspection:

```bash
cd /home/droid/hermes
git status --short --branch
git remote -v
git branch -vv
git log --oneline --decorate upstream/main..HEAD
```

If there are dirty changes, classify them before any rebase, reset, stash, discard, or commit:

- private-local behavior to keep
- general bug fix that can live in the private branch
- generated/cache noise to discard
- secrets or machine-private config to move out of the repo
- conflicted or unclear changes that require user approval

Do not silently discard dirty work. Do not run destructive commands until the user has agreed to the classification.

## Regular Update Procedure

Use this sequence for the normal private patch-stack update:

```bash
cd /home/droid/hermes
git fetch upstream
git switch sligo/main
git rebase upstream/main
```

If the rebase conflicts:

1. Inspect each conflict.
2. Preserve local intent unless upstream clearly supersedes it.
3. Prefer the newer upstream implementation for generic Hermes behavior.
4. Reapply private-only policy in the smallest local patch.
5. Continue with `git rebase --continue` after conflicts are resolved and tests pass for the touched area.

After a clean rebase:

```bash
git status --short --branch
git push --force-with-lease origin sligo/main
```

Use `--force-with-lease`, not plain `--force`.

## Refreshing Main

Only refresh `main` after private work is committed or otherwise preserved on `sligo/main`.

```bash
cd /home/droid/hermes
git fetch upstream
git switch main
git reset --hard upstream/main
```

Push `origin main` only if the private fork intentionally uses `main` as a mirror of upstream:

```bash
git push origin main
```

Never reset `main` while it contains unpreserved private commits. If `main` is ahead of `upstream/main`, first move those commits onto `sligo/main` or confirm they are intentionally disposable.

## Built-In `hermes update`

Do not run `hermes update` blindly for this private checkout. The built-in updater is designed around updating `main`; it can stash local work, switch branches, and reset divergent history.

It is acceptable to use `hermes update --check` for status. For actual updates, prefer the explicit Git procedure above so the private patch stack stays understandable.

If the user specifically asks to run `hermes update`, first explain the risk in this checkout and confirm whether they want the built-in updater or the private branch update flow.

## Testing

Run tests based on changed areas after the rebase or conflict resolution:

- Python changes: `scripts/run_tests.sh <targeted tests> -q`
- TUI changes: `pnpm --dir ui-tui exec vitest run <targeted test file>`
- Docs-only changes: validate formatting or links only if the touched docs have a known local check

Always report skipped tests explicitly. Do not say tests pass if only a subset ran.

## Common Pitfalls

1. Running `git pull` on whichever branch happens to be checked out. Always identify the branch and remotes first.
2. Treating private changes as upstream PR candidates. This checkout intentionally carries local-only behavior.
3. Letting `main` accumulate private commits. Put private work on `sligo/main`.
4. Using plain `--force`. Use `--force-with-lease`.
5. Forgetting dirty untracked files. Include them in the classification before updating.
6. Updating docs or config to remove upstream-supported features just because this machine does not use them. That may be acceptable locally, but label it as private policy.

## Verification Checklist

- [ ] `upstream` points at `NousResearch/hermes-agent`.
- [ ] `origin` points at the private fork before pushing.
- [ ] Dirty changes were classified before update operations.
- [ ] Private work is on `sligo/main`.
- [ ] `main` is clean or intentionally reset to `upstream/main`.
- [ ] Rebase conflicts, if any, are resolved intentionally.
- [ ] Targeted tests ran, or skipped tests are reported.
- [ ] Private branch was pushed with `--force-with-lease` when rewritten.
