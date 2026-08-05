# Hermes Agent - Development Guide

Instructions for AI coding assistants and developers working on the `hermes-agent` codebase. Keep this file compact: it is auto-loaded as project context, so prefer pointers to source files and docs over long copied examples.

## Repo State Entry Points

Before broad Hermes work, read the repo-backed routing/context split:

- `docs/project-state.md` — tiny non-authoritative routing/pickup cursor; not a history ledger or source of truth.
- `docs/context.md` — durable Sligo/Hermes product context and knowledge-store boundaries.
- `docs/sligo-command-center.md` — Command Center product model when that surface is in scope.
- `docs/decisions/` — durable rationale for reversal-prone project choices.

Keep durable project state in the narrowest repo-owned source: code/tests/config, git/PRs, runtime evidence, focused docs, or runbooks. Skills are reusable procedures, pitfalls, and verification checklists; they are not the Hermes target/status ledger.

## Working Rules

- Prefer `.venv`, then `venv`, then `$HOME/.hermes/hermes-agent/venv` for shared-worktree installs.
  In a linked worktree, run `hermes worktrees prepare .` before installing dependencies; exact-lock `.venv` and pnpm trees are reused from the durable primary checkout.
  ```bash
  source .venv/bin/activate   # or: source venv/bin/activate
  ```
- When developing Hermes itself, work in a git worktree based on `main`; keep the main checkout clean. Merge changes back to `main` when done unless the user asks to pause.
- A normal Hermes PR lifecycle ends after the PR is merged and the clean canonical checkout is fast-forwarded to the verified merge SHA. Do not restart the gateway merely because Hermes code was merged. Restart only for an explicit user request, runtime-critical configuration that cannot reload safely, or a demonstrated operational necessity; active development workers make noncritical restarts wait/defer, and Discord commentary must continue to state what is happening while work is in flight or recovering.
- For routine PR/worktree decisions, run `python -m hermes_cli.pr_workflow_preflight --base <base> --head <head>` and use its bounded summary. Do not put raw `git worktree list --porcelain` or an unbounded `git worktree list` inventory into conversational terminal results; reserve raw inventories for explicit repair/audit work outside the active model context.
- Prefer config changes over code changes when the desired behavior is already configurable.
- Inspect structure/status first, preserve unrelated local edits, and verify with the project test wrapper before reporting completion.
- When locating Hermes source, search the checked-out repo first; do not use web/community forums for file locations unless local context is missing or stale.

## Load-Bearing Repo Map

File counts shift constantly; use the filesystem as source of truth. Common entry points:

- `run_agent.py` — `AIAgent`, conversation loop, memory/compression/tool-call orchestration.
- `model_tools.py` — tool discovery and `handle_function_call()` dispatch.
- `toolsets.py` — toolset definitions and `_HERMES_CORE_TOOLS`.
- `cli.py` + `hermes_cli/` — classic CLI, config, slash commands, plugins, skins, subcommands.
- `hermes_cli/commands.py` — central slash-command registry consumed by CLI, gateway, help, Telegram, Slack, autocomplete.
- `hermes_constants.py` — profile-aware `get_hermes_home()` and `display_hermes_home()`.
- `hermes_logging.py` — profile-aware logs in `$HERMES_HOME/logs/`.
- `tools/registry.py` + `tools/*.py` — native tool registration.
- `tools/environments/` — terminal backends.
- `gateway/` — messaging gateway/session orchestration and core platform helpers; platform adapters may live in plugins (Discord: `plugins/platforms/discord/adapter.py`, `DiscordAdapter`; do not infer missing Hermes source from `discord.py` package bytecode).
- `plugins/` — plugin systems: platform adapters, memory, context engine, model providers, kanban, observability, image generation, etc.
- `skills/` — bundled default skills; `optional-skills/` — heavier/niche skills installed explicitly.
- `ui-tui/` + `tui_gateway/` — Ink TUI and Python JSON-RPC gateway.
- `hermes_cli/pty_bridge.py` + `hermes_cli/web_server.py` — dashboard `/chat` embeds the real TUI through a PTY.
- `cron/` — scheduled jobs.
- `tests/` — pytest suite; always run through `scripts/run_tests.sh`.

User config: `$HERMES_HOME/config.yaml`; secrets only: `$HERMES_HOME/.env`. Browse logs with `hermes logs [--follow] [--level ...] [--session ...]`.

## Core Runtime Notes

### Agent loop

`AIAgent.run_conversation()` is synchronous: build OpenAI-format messages, call the provider with tool schemas, dispatch tool calls through `handle_function_call()`, append tool results, repeat until final content or iteration budget. Reasoning content is stored on assistant messages as `reasoning`.

`AIAgent.__init__` has many parameters; inspect `run_agent.py` before changing construction. Commonly touched fields include provider/model/API mode, toolsets, platform/session IDs, quiet mode, context/memory skips, credential pool, budget/fallback/checkpoint settings.

### Prompt caching

Do not break prompt caching mid-conversation:

- Do not mutate past context.
- Do not change toolsets mid-turn.
- Do not reload memories or rebuild the system prompt except through context compression.
- Slash commands that mutate system-prompt state must default to deferred invalidation and expose an explicit `--now` path for immediate invalidation.

## CLI and Slash Commands

`COMMAND_REGISTRY` in `hermes_cli/commands.py` is the single source of truth for slash commands. Add aliases there only; CLI dispatch, gateway known-command routing, help text, Telegram commands, Slack subcommands, and autocomplete all derive from it.

When adding a command:

1. Add `CommandDef(...)` in `hermes_cli/commands.py`.
2. Add handler dispatch in `HermesCLI.process_command()` in `cli.py`.
3. If gateway-accessible, add handler in `gateway/run.py`.
4. Persist settings via the existing config helpers, not ad hoc YAML writes.

The CLI skin engine lives in `hermes_cli/skin_engine.py`. Skins are data-only; built-ins live in `_BUILTIN_SKINS`, user skins live under `$HERMES_HOME/skins/*.yaml`, and runtime activation uses `/skin <name>` or `display.skin`.

## TUI and Dashboard

The TUI is the primary terminal UI: Node/Ink owns rendering and Python `tui_gateway` owns sessions/tools/model calls over newline-delimited JSON-RPC.

Useful commands:

```bash
cd ui-tui
npm install        # first time
npm run dev        # watch mode
npm start          # production start
npm run build      # build hermes-ink + tsc
npm run type-check # tsc --noEmit
npm run lint
npm run fmt
npm test           # vitest
```

Dashboard `/chat` embeds the real `hermes --tui` through `hermes_cli/pty_bridge.py`; do **not** rebuild the main chat transcript/composer in React. Support UI around the PTY pane is fine (sidebars, inspectors, status panels) as long as it does not replace the terminal chat surface or couple state destructively to the PTY child.

## Tools and Toolsets

For custom/local-only tools, prefer plugins under `$HERMES_HOME/plugins/<name>/` with `plugin.yaml`, `__init__.py`, and `ctx.register_tool(...)`. Edit core only for tools intended to ship in the base system.

For a Discord URL or message/thread investigation, first use Hermes' native Discord path: `tools/discord_tool.py` (`discord`/`discord_admin`) when it is enabled. If those model tools are not injected into the current agent, use the read-only CLI fallback first: `hermes discord trace '<url>'` for the surrounding message/thread context or `hermes discord get-message '<url>'` for one message. The CLI uses the active Hermes profile's bot credentials and remains available through the terminal even when `discord` is absent from the callable tool schema. Only fall back to correlating Discord snowflakes with `$HERMES_HOME/logs/` and `$HERMES_HOME/sessions/` if both native paths are unavailable; do not assume an external connector is required.

Core tool rules:

- Tool modules live in `tools/*.py` and call `registry.register()` at import time.
- Add exposed tools to `toolsets.py`; auto-discovery registers schemas, but a tool is not available to an agent until included in a toolset.
- Handlers return JSON strings and use the registry for schema, dispatch, availability, and error wrapping.
- Persistent tool state must use `get_hermes_home()`; user-facing path text should use `display_hermes_home()`.
- Tool schema descriptions must not hard-reference tools from other toolsets. If a cross-reference is needed, add it dynamically in `model_tools.py` while building definitions.
- Agent-level tools such as todo/memory are intercepted in `run_agent.py` before normal dispatch; inspect existing patterns first.

Toolsets are defined in `toolsets.py` as `TOOLSETS`. Enable/disable per platform via `hermes tools` or `tools.<platform>.enabled` / `tools.<platform>.disabled` in config.

## Configuration and Dependencies

### Config

- Add defaults to `DEFAULT_CONFIG` in `hermes_cli/config.py`.
- Bump `_config_version` only for migrations that actively transform existing user config; simple new keys are covered by deep merge.
- Non-secret settings belong in `config.yaml`. `.env` is for API keys, tokens, and passwords only.
- Add secret metadata to `OPTIONAL_ENV_VARS` in `hermes_cli/config.py`.
- Loader split matters: `cli.py::load_cli_config()` for interactive CLI, `hermes_cli/config.py::load_config()` for most subcommands, and raw gateway YAML loading in `gateway/run.py`/`gateway/config.py`.
- Working directory: CLI uses process cwd; messaging uses `terminal.cwd` from config, bridged to `TERMINAL_CWD`. `MESSAGING_CWD` and `.env` `TERMINAL_CWD` are deprecated.

### Dependency pinning

All dependencies need upper bounds or exact immutable pins:

- PyPI: `>=floor,<next_major`; pre-1.0 usually `<0.(minor+2)`.
- Git URLs: pin commit SHA.
- GitHub Actions: pin SHA with a version comment.
- CI-only pip installs: exact `==`.
- Run `uv lock` after dependency changes.

This policy exists because of real supply-chain incidents; do not add bare `>=` dependencies.

## Plugins

General plugins are discovered by `hermes_cli/plugins.py` from `$HERMES_HOME/plugins/`, `./.hermes/plugins/`, and pip entry points. They can register lifecycle hooks, tools, and CLI subcommands. Discovery is triggered as a side effect of importing `model_tools.py`; code that reads plugin state earlier must call `discover_plugins()` explicitly.

Memory-provider plugins live under `plugins/memory/<name>/` and implement `agent/memory_provider.py::MemoryProvider`, orchestrated by `agent/memory_manager.py`. CLI commands in `plugins/memory/<name>/cli.py` are exposed only for the active memory provider.

Policy: do not add new in-tree memory providers. New memory backends should ship as standalone plugin repos installable into `$HERMES_HOME/plugins/` or via pip entry points. Existing providers may receive bug fixes.

Model-provider plugins live under `plugins/model-providers/<name>/`, register `ProviderProfile(...)`, and use lazy discovery through `providers/__init__.py`. User plugins of the same name override bundled ones.

Plugin rule: plugins must not patch core files for plugin-specific behavior. If a plugin needs a missing capability, expand the generic plugin surface instead.

## Skills

- Bundled default skills: `skills/<category>/<skill>/`.
- Optional/heavy/niche skills: `optional-skills/<category>/<skill>/`, installed via `hermes skills install official/<category>/<skill>`.
- Heavy-dependency or niche contributions belong in `optional-skills/`, not the default bundle.

Skill standards:

- `description` ≤ 60 characters, one sentence, ends with a period; avoid marketing fluff.
- Prose should name native Hermes tools or explicit MCP servers, not shell utilities already wrapped by tools (`search_files` not grep/find/ls, `read_file` not cat/head/tail, `patch` not sed/awk).
- Audit `platforms:` against actual script imports and OS primitives.
- Credit human contributors first in `author`.
- Modern body order: title, intro, `When to Use`, `Prerequisites`, `How to Run`, `Quick Reference`, `Procedure`, `Pitfalls`, `Verification`.
- Use `scripts/`, `references/`, and `templates/` for support files instead of forcing large logic into prose.
- Tests live under `tests/skills/test_<skill>_skill.py` and use stdlib + pytest + mocks only.
- `.env.example` edits should be isolated to a clearly delimited block.

Load the dedicated skill-authoring or salvage skill before polishing large contributed skill PRs.

## Profiles and Paths

Hermes profiles are isolated instances with separate `HERMES_HOME` trees. `_apply_profile_override()` in `hermes_cli/main.py` sets `HERMES_HOME` before module imports.

Rules:

- Use `get_hermes_home()` for state/config/cache paths.
- Use `display_hermes_home()` for user-facing path text.
- Do not hardcode `~/.hermes` or `Path.home() / ".hermes"` for profile-scoped files.
- Module-level constants are acceptable if they call `get_hermes_home()` after profile override.
- Tests that mock `Path.home()` should also set `HERMES_HOME`.
- Gateway platform adapters that use unique credentials should acquire/release scoped locks in `connect()`/`start()` and `disconnect()`/`stop()`.
- Profile listing is intentionally HOME-anchored (`Path.home() / ".hermes" / "profiles"`) so any active profile can see all profiles.

## Delegation, Cron, and Kanban

### Delegation

`tools/delegate_tool.py` spawns synchronous subagents with isolated context and terminal sessions. Batch mode runs concurrent children up to `delegation.max_concurrent_children`. Leaf children cannot delegate, clarify, memory, send messages, or execute code. Orchestrator children are gated by config and spawn-depth limits.

`delegate_task` is not durable. For work that must outlive the current turn, use `cronjob` or `terminal(background=True, notify_on_complete=True)`.

### Cron

Cron jobs are managed by `cron/jobs.py` and `cron/scheduler.py`. Users can use `hermes cron <verb>` or `/cron`; agents use the `cronjob` tool. Schedules support durations, “every …” phrases, 5-field cron, and ISO one-shots. Per-job fields include skills, model/provider overrides, scripts, `context_from`, `workdir`, profile, and delivery targets.

Hardening: cron sessions have a 3-minute interrupt, bounded catchup/grace windows, a tick file lock, and `skip_memory=True` by default. Deliveries land in their own cron session, not into the target gateway session history.

### Kanban

Kanban is a durable SQLite work queue exposed via `hermes kanban <verb>` and worker-specific `kanban_*` tools. The dispatcher reclaims stale claims, promotes ready tasks, atomically claims, and spawns assigned profiles; by default it runs inside the gateway via `kanban.dispatch_in_gateway: true`.

Isolation: board is the hard boundary (`HERMES_KANBAN_BOARD` pinned); tenant is a soft namespace within a board. After `kanban.failure_limit` consecutive failures, the dispatcher auto-blocks a task to avoid spin loops.

## Gateway and Messaging Pitfalls

- The gateway has two message guards: the base adapter queues messages while a session is active, and `gateway/run.py` intercepts control commands. Any command that must reach a blocked/running agent must bypass both guards and dispatch inline.
- Background process completion notifications are controlled by `display.background_process_notifications` (`all`, `result`, `error`, `off`) or `HERMES_BACKGROUND_NOTIFICATIONS`.
- When using `terminal(background=True, notify_on_complete=True)`, the gateway watcher triggers a follow-up agent turn on process completion.

## Known Pitfalls

- Do not introduce new `simple_term_menu` usage. Use `hermes_cli/curses_ui.py` for new interactive menus.
- Do not use ANSI erase-to-EOL (`\033[K`) in spinner/display code under prompt_toolkit; use carriage return plus space padding.
- `_last_resolved_tool_names` in `model_tools.py` is process-global; delegate subagents save/restore it, so reads during child runs may be stale.
- Squash merges from stale branches can silently revert fixes. Update branch against `main`, reapply commits, and inspect `git diff HEAD~1..HEAD` after merging.
- Do not wire unused/dead modules into live paths without E2E-validating the real import and resolution chain against a temp `HERMES_HOME`.
- Tests must not write to real `$HOME/.hermes`; rely on the autouse isolation fixture and mock `Path.home()` for profile tests.

## Testing

Always run tests through the wrapper, not direct `pytest`:

```bash
scripts/run_tests.sh                                  # fork smoke suite, CI-parity
scripts/run_tests.sh --full                           # inherited upstream suite
scripts/run_tests.sh tests/gateway/                   # directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # wrapper + pytest flags
```

The wrapper enforces CI parity: credential env vars unset, temporary HOME/HERMES_HOME, UTC timezone, C.UTF-8 locale, and 4 xdist workers. Direct pytest on large machines or with API keys set has repeatedly diverged from CI.

Run smoke before pushing. Use `--full` when changing inherited upstream surfaces outside the fork-owned smoke suite or when broader regression confidence is needed. Docker image/build checks are not a routine gate for this fork unless Docker support is explicitly in scope.

### Change-detector tests

Do not write tests that fail merely because expected data changes (model catalogs, config version literals, enum counts, hardcoded provider model names). Prefer relationship/invariant tests: catalog plumbing works, migrations end at current config version, no plan-only model leaks into legacy lists, every catalog model has a context-length entry, etc.
