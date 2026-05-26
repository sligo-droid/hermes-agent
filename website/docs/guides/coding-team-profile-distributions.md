---
title: Coding Team Profile Distributions
---

# Coding Team Profile Distributions

Hermes ships example profile distributions for a small coding team: planner, implementer, reviewer, and tester. They are examples, not defaults. Install only the roles you want, then tune their `SOUL.md` and `config.yaml` for your environment.

The distributions live in the repository under:

```text
profile-distributions/coding-team/
```

Install a role from a checkout:

```bash
hermes profile install profile-distributions/coding-team/planner --name coding-planner --alias
hermes profile install profile-distributions/coding-team/implementer --name coding-implementer --alias
hermes profile install profile-distributions/coding-team/reviewer --name coding-reviewer --alias
hermes profile install profile-distributions/coding-team/tester --name coding-tester --alias
```

Each role includes:

| Role | Purpose |
|------|---------|
| `coding-planner` | Decompose ambiguous coding work into small verified tasks. |
| `coding-implementer` | Make focused code changes from a scoped task. |
| `coding-reviewer` | Find concrete defects, regressions, and missing tests. |
| `coding-tester` | Verify behavior with focused tests and smoke checks. |

Recommended workflow:

1. Ask `coding-planner` to inspect the repo and produce task handoffs.
2. Give one task at a time to `coding-implementer`.
3. Ask `coding-reviewer` to review the resulting diff against the original task.
4. Ask `coding-tester` to run focused verification before merging.

These distributions intentionally do not set API keys, credentials, memories, sessions, or hooks. Profile distribution installs preserve user-owned data and only copy distribution-owned files such as `SOUL.md`, `config.yaml`, and bundled role skills.

To publish your own version, copy one of these directories into a new git repo, edit the manifest and role prompt, then share it with `hermes profile install <repo-url>`.
