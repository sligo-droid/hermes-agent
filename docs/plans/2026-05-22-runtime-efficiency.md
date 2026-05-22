# Runtime Efficiency Plan

## Skill Loading

Goal: keep `AGENTS.md` and repo-native instructions fully loaded, while cutting
the amount of optional skill prose injected into ordinary coding turns.

Planned approach:

1. Treat `AGENTS.md`, repo `AGENTS.md`, and directly discovered project context as the non-negotiable full baseline.
2. Add a compact skill manifest cache containing each skill's name, trigger, short description, prerequisites, and section headings.
3. During turn setup, inject only compact manifests for candidate skills instead of full `SKILL.md` bodies.
4. Keep full skill bodies available through the existing skill tools, and load them on demand when the turn reaches that phase.
5. Add phase-specific escalation hints: for example, load `github-pr-workflow` only when the agent is about to create or merge a PR, not during initial repo inspection.
6. Preserve explicit user skill loads and slash-command skill loads exactly; compaction should only affect automatic/topic-triggered skill injection.
7. Track prompt savings with the new `turn_runtime_summary` fields, especially `max_prompt_tokens`, `last_prompt_tokens`, and `api_ms`.

Initial implementation in this branch changes the automatic skills prompt from
mandatory eager loading to a summary-first index: routine coding work can use
repo instructions and local evidence without pulling full skill bodies, while
phase-specific work such as PR creation/merge still loads the relevant full
skill at that boundary.

Implementation sketch:

- Extend skill discovery to build a `SkillSummary` object from frontmatter and headings.
- Store summary text alongside the existing skill cache, invalidated by file mtime/hash.
- Add a config flag such as `skills.auto_load_mode: summary_first | full` with `summary_first` as the eventual default after validation.
- Add a tool/system hint that says: if a summary-matched skill becomes necessary, call the skill-loading tool for the full body before acting on detailed procedure.
- Regression tests should verify that full explicit loads still include the complete `SKILL.md`, while automatic loads include summaries only.

## Worker Dependency Reuse

Fresh git worktrees do not include ignored `node_modules`, so pnpm projects often
pay an install cost before the worker can run even basic checks. Hermes now does

1. Scan the worker cwd for pnpm package roots with both `package.json` and `pnpm-lock.yaml`.
2. List other git worktrees for the same repo.
3. If another worktree has the same relative package root, matching lockfile hash, and an existing `node_modules`, symlink the missing `node_modules` into the new worktree.
4. If no exact lockfile match exists, do nothing and let the worker install normally.

This avoids repeated installs on unchanged dependency graphs while preventing
accidental dependency reuse across incompatible branches.
