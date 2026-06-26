# Project State

Last updated: 2026-06-26
State owner: Sligo Labs agent

This file is a tiny routing and pickup cursor for the Sligo Labs `hermes-agent` fork. It is not authoritative history, a completed-work ledger, a product spec, or runtime evidence. CI enforces a 3,000-byte ceiling; replace stale bullets instead of appending.

## How To Use

- Read this after `AGENTS.md` only to choose the next source of truth.
- Keep at most the active pickup facts that prevent wrong routing.
- Do not record completed PR logs, broad history, runtime dumps, or duplicated code facts here.
- For authoritative state, inspect the code, tests, config, git/PRs, runtime logs/state DB, and the focused docs below.

## Current Cursor

- Hermes is Sligo Labs command infrastructure: CLI/TUI agent, gateway, Discord dev-shop, Kanban workers, cron/self-improvement, and Command Center. Reliability for Discord, Kanban, gateway, worker routing, and Command Center wins over novelty.
- Command Center should remain the operator ledger over the Work Item / Source / Worker Run model. Worker boards, Kanban tasks, task runs, PRs, logs, and deploys are execution artifacts, not peer ledgers.
- Keep installed skills procedural. Move current target/status prose out of skills and into the narrowest repo source: this cursor for routing, `docs/context.md` for durable context, `docs/decisions/` for rationale, or focused runbooks/plans.
- Dirty secondary main worktree quarantine remains active for `/home/droid/workspaces/hermes-self-improvement-live-test`; keep it excluded from automatic worker routing until human-approved reconciliation follows `docs/runbooks/worktree-quarantine-2026-06-17.md`.

## Route To Truth

- Code/tests/config: authoritative behavior and contracts. Start with files named in `AGENTS.md`, then immediate callers and focused tests.
- Git/PRs/issues: shipped status and review history.
- Runtime evidence: `$HERMES_HOME/logs/`, `$HERMES_HOME/state.db`, `$HERMES_HOME/kanban/`, systemd user services, and current dashboard/gateway checks.
- Durable product context: `docs/context.md`.
- Command Center product model: `docs/sligo-command-center.md`.
- Reversal-prone rationale: `docs/decisions/`.
- Operational procedures: `docs/runbooks/`.
- Historical lookup only: `docs/archive/project-state-archive-20260626.md`.

## Update Rule

Update this file only when a routing/pickup fact changes and the fact does not belong in a narrower authoritative source. For normal PRs, prefer `Project-state: not needed - <reason>` unless the change alters one of the bullets above.

Allowed status words when a temporary pickup fact needs one: `planned`, `ready`, `in_progress`, `blocked`, `implemented`, `merged`, `deployed`, `verified`, `superseded`.
