---
name: claude-code
description: "Delegate coding to Claude Code CLI (features, PRs)."
version: 2.2.0
author: Hermes Agent + Teknium
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Claude, Anthropic, Code-Review, Refactoring, PTY, Automation]
    related_skills: [codex, hermes-agent, opencode]
---

# Claude Code Orchestration

Use this skill when Hermes should hand a scoped coding task to Anthropic's Claude Code CLI, supervise the run, or review/repair Claude Code output.

Claude Code is another autonomous coding agent. Treat it as a capable collaborator with its own tool loop, not as a shell command that returns instantly.

## Prerequisites

- Install Claude Code: `npm install -g @anthropic-ai/claude-code`.
- Authenticate once with `claude`, `claude auth login --console`, or `claude auth login --sso`.
- Confirm health with `claude --version`, `claude auth status`, and `claude doctor`.
- Use a clean branch/worktree for non-trivial implementation. Never ask Claude Code to overwrite unrelated local edits.

## Choose the operating mode

1. **Interactive PTY mode** — use when you need to supervise, approve, interrupt, or feed context incrementally. Start `claude`, watch its plan, answer prompts, and stop it if it drifts.
2. **Non-interactive task mode** — use when the task is already bounded, reversible, and testable. Pass a concise task, relevant files, acceptance criteria, and verification command.
3. **Review mode** — use when Claude Code already produced a diff. Inspect the diff yourself, run focused tests, and ask Claude Code only for targeted repair if needed.

## Workflow

1. Inspect repo state first: current branch, dirty files, task-relevant instructions, and nearby tests.
2. Scope the handoff. Include goal, files/areas in scope, files/areas out of scope, verification command, and what to report back.
3. Run Claude Code in the target worktree. Keep terminal output bounded; do not paste secrets or huge logs into the prompt.
4. Monitor for scope drift, destructive commands, dependency churn, or formatting-only rewrites.
5. After Claude Code stops, verify locally. Read the diff, inspect generated files, and run the smallest checks that prove the claim.
6. Summarize what Claude Code changed, what you verified, what failed, and what still needs human or Hermes follow-up.

## Handoff prompt skeleton

```text
You are working in <repo path> on branch <branch>.
Goal: <one-sentence outcome>.
Scope: edit only <files/modules>. Do not touch <out-of-scope>.
Constraints: preserve unrelated local edits; do not commit; avoid new deps unless necessary.
Acceptance criteria:
- <criterion 1>
- <criterion 2>
Verification: run <command> and report exact result.
If blocked, stop and explain the blocker instead of broadening scope.
```

## Reference map

- [references/full-guide.md](references/full-guide.md) — archived full v2 orchestration guide, including advanced CLI flags, JSON output patterns, hooks, MCP/server usage, slash commands, and long troubleshooting notes.
- Related Hermes skills:
  - `codex` for OpenAI Codex CLI orchestration.
  - `opencode` for OpenCode CLI orchestration.
  - `hermes-agent` for configuring or extending Hermes itself.

Load the full guide only when the concise workflow is not enough: complex permissions, advanced Claude Code CLI options, MCP integration, custom slash commands, hooks, or persistent Claude Code configuration.

## Pitfalls

- Do not let Claude Code operate in an ambiguous dirty tree. Preserve user and other-agent changes.
- Do not delegate broad repo ownership without acceptance criteria and a stop condition.
- Do not trust a claimed pass. Verify the actual diff and test output yourself.
- Do not paste credentials, `.env` values, private logs, or personal data into Claude Code prompts.

## Verification

- `git status --short`
- focused test or lint command named in the handoff
- manual diff review for scope, secrets, generated artifacts, and unrelated rewrites
