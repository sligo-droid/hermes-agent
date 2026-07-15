---
title: "Hermes Agent — Configure, extend, or contribute to Hermes Agent"
sidebar_label: "Hermes Agent"
description: "Configure, extend, or contribute to Hermes Agent"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Hermes Agent

Configure, extend, or contribute to Hermes Agent.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/autonomous-ai-agents/hermes-agent` |
| Version | `2.3.0` |
| Author | Hermes Agent + Teknium |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `hermes`, `setup`, `configuration`, `multi-agent`, `spawning`, `cli`, `gateway`, `development` |
| Related skills | [`claude-code`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-claude-code), [`codex`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-codex), [`opencode`](/docs/user-guide/skills/bundled/autonomous-ai-agents/autonomous-ai-agents-opencode) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Hermes Agent

Use this skill when the user asks how to install, configure, operate, troubleshoot, extend, or contribute to Hermes Agent.

Hermes is a profile-aware AI agent framework with CLI/TUI, gateway, dashboard, plugins, skills, cron, memory providers, MCP, and model-provider surfaces. Prefer local repo/docs evidence over memory.

## Prerequisites

- Know which surface is in scope: CLI, TUI, gateway, dashboard, plugin, skill, cron, memory, MCP, or provider.
- For repo work, read `AGENTS.md`, relevant docs, immediate callers, and focused tests before editing.
- For profile-scoped files, use `HERMES_HOME` and repo helpers such as `get_hermes_home()` / `display_hermes_home()`.
- Never print secrets from `$HERMES_HOME/.env`, config, logs, tokens, or platform credentials.

## Workflow

1. Clarify the desired outcome only if it materially changes scope or risk.
2. Inspect local truth first: repo files, docs, config, status, logs, tests, and installed plugin/skill directories.
3. Choose config over code when the behavior is already configurable.
4. Make surgical changes that preserve profiles, prompt caching, plugin boundaries, and gateway/session contracts.
5. Verify with the smallest relevant command. For this repo, prefer `scripts/run_tests.sh` over direct `pytest`.
6. Report exact files changed, commands run, skipped checks, and remaining risk.

## Common entry points

- Agent loop: `run_agent.py`, `agent/`, `model_tools.py`, `toolsets.py`, `tools/`
- CLI/config/slash commands: `cli.py`, `hermes_cli/`, `hermes_cli/commands.py`
- Gateway/platforms: `gateway/`, `plugins/platforms/`
- Skills: `skills/`, `optional-skills/`, `tools/skill_manager_tool.py`, `tools/skills_tool.py`
- TUI/dashboard: `ui-tui/`, `tui_gateway/`, `hermes_cli/web_server.py`
- Cron/kanban: `cron/`, `hermes_cli/kanban*`

## Reference map

- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/full-guide.md) — archived full Hermes guide with detailed setup, configuration, feature, platform, and troubleshooting notes.
- [references/native-mcp.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/native-mcp.md) — native MCP integration details.
- [references/webhooks.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/autonomous-ai-agents/hermes-agent/references/webhooks.md) — webhook operation details.
- Repo docs to read when relevant:
  - `AGENTS.md` for contributor workflow and load-bearing map.
  - `docs/context.md` for durable Sligo/Hermes product context.
  - `docs/project-state.md` for the narrow routing cursor.
  - `website/docs/` for user-facing documentation.

Load the full guide only when the user needs detailed commands or broad feature comparison. For implementation, prefer the live repo files and focused tests.

## Pitfalls

- Do not hardcode `~/.hermes`; profiles are isolated by `HERMES_HOME`.
- Do not mutate prompt history/toolsets mid-turn unless the runtime path already supports that invalidation.
- Do not patch core for plugin-specific behavior; expand generic plugin surfaces instead.
- Do not add dependencies without following repo pinning policy.
- Do not treat skills, memory, or docs as a current-state ledger when code/tests/runtime evidence own the fact.

## Verification

- Config/docs-only change: run relevant source/doc tests.
- Python behavior change: run the focused `scripts/run_tests.sh ...` target.
- UI/TUI change: run the matching npm type-check/test/build command plus any Python endpoint tests.
- Runtime/gateway issue: include log/state evidence, not just code inspection.
