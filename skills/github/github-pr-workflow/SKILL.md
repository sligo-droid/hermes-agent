---
name: github-pr-workflow
description: "Run a repository-governed GitHub pull request lifecycle."
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [GitHub, Pull-Requests, CI/CD, Git]
    related_skills: [github-auth, github-code-review]
---

# GitHub Pull Request Workflow

Use this skill when repository work must be delivered through a GitHub pull
request. Repository and client instructions own draft state, required review,
visual QA, approval, merge method, deployment, and canonical-checkout policy.
Never replace those rules with a generic automatic-merge default.

## Lifecycle

1. Inspect the repository state, remotes, base branch, and existing changes.
2. Work on a task branch in an isolated worktree when repository policy
   requires it. Preserve unrelated work.
3. Run the smallest checks that prove the requested change.
4. Commit only the intended files and push the task branch.
5. Open or update the PR using the repository's required draft state and body.
6. Report the PR and any preview or QA target required by local policy.
7. Wait for required checks, review, and explicit approval without blocking a
   worker or inventing approval from silence.
8. Merge only when local policy authorizes it. Verify the resulting PR state
   and merge commit before reporting completion.

## Practical commands

Prefer `gh` when available:

```bash
git status --short --branch
git diff --check
git push -u origin HEAD
gh pr create --draft --fill
gh pr view --json number,state,isDraft,url,headRefName,baseRefName,statusCheckRollup
gh pr checks --watch
```

Promote a draft only when the applicable policy's readiness conditions are
met:

```bash
gh pr ready <number>
```

Before merging, verify the required human approval and current head state.
Use the repository's merge method rather than assuming merge, squash, or
rebase. A green check is evidence, not permission.

## Failure handling

- Update against the current base when required; do not hide conflicts.
- Distinguish failed checks, pending checks, requested changes, missing
  permission, and an explicit hold.
- Do not mutate a protected canonical checkout from a worker context.
- Do not enable auto-merge unless the user or repository policy specifically
  requests it.
- Do not expose tokens or embed credentials in Git remotes or command output.

## Completion evidence

Report the PR URL, draft/ready state, checks run, approval state, merge state,
and any required preview or deployment result. Keep implementation, merge,
deployment, and verification as distinct claims.
