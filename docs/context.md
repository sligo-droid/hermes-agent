# Project Context

This document is durable context for Sligo Labs' `hermes-agent` fork. It should change when the product model, architecture boundaries, or knowledge-store split changes. Keep only narrow routing/pickup facts in `docs/project-state.md`.

## Product Goal

Hermes Agent is an autonomous AI agent framework. In Sligo Labs' fork, Hermes is also command infrastructure for building and operating high-quality end-client software: CLI/TUI agent, messaging gateway, Discord dev-shop, Kanban worker orchestration, cron/self-improvement loops, and the Sligo Command Center.

The target is not “more autonomous churn.” The target is reliable, evidence-backed execution that preserves attention, ships the right work, and keeps operator state visible.

## Users and Workflows

Primary users:

- Sligo Labs operator/developer using CLI, TUI, Discord, and dashboard surfaces.
- Trusted Discord project channels, especially Hermes `#dev` and PID.
- Future agents/workers that need to resume repo work without reconstructing state from conversations.

Core workflows:

- **Hermes repo development:** code/doc changes in worktrees under `/home/droid/workspaces/`, reviewed and merged back to `main` after focused tests/CI.
- **Discord dev-shop intake:** action requests and project work enter through Discord, with long-running work moving through worker boards/Kanban where appropriate. “Action request” means any ask to do work, not only product-feature work.
- **Discord plan privacy:** Discord responses may say that a plan was saved, but must never include the plan artifact's filename, filesystem path, or link. This includes relative paths under `.hermes/plans/` and absolute workspace paths.
- **Command Center operation:** Sligo Command Center shows Sources, Work Items, Worker Runs, and project tabs. It should be the operator ledger, not another competing board.
- **Cron/self-improvement:** scheduled audits and proposals may create work, but they are intake/provenance. Accepted work should attach to the same work-item model.
- **Runtime troubleshooting:** gateway, dashboard, Kanban, model routing, and cron issues require evidence from logs/state DB/systemd/runtime checks before claims.

## Knowledge Store Split

Use the smallest stable home for each kind of knowledge:

| Store | Canonical role |
| --- | --- |
| Repo docs | Durable product context, architecture decisions, runbooks, and narrow routing/pickup pointers tied to this codebase. |
| Skills | Reusable procedures, pitfall checklists, command recipes, and class-level workflows. Skills should not be the current-state ledger. |
| Honcho / user memory | Compact stable recall such as user preferences, environment facts, and hard safety constraints. It is non-canonical and should be rebuildable from authoritative sources. |
| Session search | Recall prior conversations and exact past claims when needed; do not encode session progress as memory. |
| Notion | Client-facing documents, feedback, shared project folders, and source artifacts. |
| GBrain | Canonical, reviewed, project-scoped client learnings used by agent retrieval. Client-knowledge publication does not project to Honcho. |
| Discord | Live conversation, approvals, operator pings, and feature threads. Not canonical state by itself. |

Boundary rule: if a note is an authoritative fact, put it in the source that owns it: code, tests, config, git/PRs, runtime evidence, a focused doc, or a runbook. Use `docs/project-state.md` only for tiny routing/pickup facts that tell future workers which source to inspect next. If a note says “why this product or architecture works this way,” put it here or in `docs/decisions/`. If it says “how to debug this class of incident,” keep it in a skill/reference and link to repo docs only when needed.

## Architecture Overview

Stable repo map lives in `AGENTS.md`; this section is the product-level map.

- **Agent runtime:** `run_agent.py`, `agent/`, `model_tools.py`, `toolsets.py`, `tools/`.
- **CLI/TUI:** classic CLI in `cli.py`/`hermes_cli/`; modern Ink TUI in `ui-tui/` with Python bridge under `tui_gateway/`.
- **Gateway:** `gateway/` plus platform plugins. Discord adapter behavior is central to Sligo dev-shop operations.
- **Kanban/workers:** durable SQLite boards and dispatcher/worker code under `hermes_cli/kanban*` plus worker-specific tools.
- **Cron:** `cron/` scheduler and job state, used for unattended audits and delivery.
- **Command Center/dashboard:** dashboard server and web UI expose Sligo operator surfaces; `docs/sligo-command-center.md` is the current product-model reference.
- **Skills:** bundled skills in `skills/`, optional skills in `optional-skills/`, installed/local Sligo skills under `$HERMES_HOME/skills/`.

## Current Product Boundaries

- Command Center should use one Work Item / Source / Worker Run model. Self-improvement, Discord, and cron are sources/intake channels, not separate peer ledgers.
- Worker boards are execution children. They should not become the source of truth for product/work status.
- Concurrent async coding children launched directly by a Discord parent are durable Worker Runs on that parent's Work Item; their restart recovery does not route through Kanban or require a worker board.
- Hermes `#dev` and PID are current Command Center project tabs. Adding projects should extend the same project metadata model, not hard-code a new screen.
- `AGENTS.md` is worker instruction; it is not a status ledger.
- `docs/project-state.md` is a tiny routing cursor, not authoritative current state.
- Skills are procedural. A skill may say how to inspect gateway health; it should not say what the current Hermes target is.

## External References

- Sligo Command Center model: `docs/sligo-command-center.md`.
- Network egress/security design: `docs/security/network-egress-isolation.md`.
- Implementation plans: `docs/plans/`.
- Routing and pickup cursor: `docs/project-state.md`.
- Durable decisions: `docs/decisions/`.

## Boundaries

- Do not store secrets, tokens, private keys, client private content, or `.env` values here.
- Do not use this file for PR-by-PR logs or temporary task progress.
- Do not duplicate long skill runbooks here. Link them only when the repo needs stable product context.
- Do not let this become a second `AGENTS.md`. Worker instructions stay in `AGENTS.md`; product/project context stays here.
