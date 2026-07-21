# Upstream v0.19 Integration Review

Status: accepted for merge-commit and normal PR/CI review. Focused owner reconciliation, the fork smoke gate and fresh-process residual verification are complete. The monolithic inherited suite still exhibits order-dependent module/global-state contamination, but an xhigh failure-gate audit found no remaining independently reproducible product regression and classified that contamination as a non-blocking test-isolation follow-up.

## Integration identity

| Item | Value |
|---|---|
| Upstream source | `https://github.com/NousResearch/hermes-agent.git` |
| Upstream release | `v2026.7.20` — Hermes Agent v0.19.0, “The Quicksilver Release” |
| Upstream commit | `3ef6bbd201263d354fd83ec55b3c306ded2eb72a` |
| Annotated tag object | `c7d08de287556b3d339df336b180a39d4980ebd7` |
| Integration branch | `merge/upstream-v0.19.0-2026-07-20` |
| Integration worktree | `/home/droid/workspaces/hermes-upstream-v019-20260720` |
| Fork HEAD at merge start | `c093bcb99` (`origin/main`) |
| Upstream release notes | `https://github.com/NousResearch/hermes-agent/releases/tag/v2026.7.20` |

## Adopted capability map

“Adopted” means the capability is present in the candidate tree or its source/tests, not that every platform has completed end-to-end verification.

| Area | Adopted 0.19 capability | Evidence / integration note |
|---|---|---|
| Providers and models | Fireworks, DeepInfra, Upstage Solar, GPT-5.6 families, newer Grok/Kimi/Claude catalog entries, provider exclusion controls, LM Studio and Bedrock updates | `plugins/model-providers/{fireworks,deepinfra,upstage}/`, provider catalogs, model metadata and transport tests. CLI/runtime-provider reconciliation is staged and focused browser/reasoning coverage passed. |
| Reasoning and MoA | Explicit `max` and `ultra`, per-model overrides, auxiliary-task effort, per-slot MoA effort, provider clamping | `hermes_constants.py`, `agent/transports/`, `agent/moa_loop.py`, `hermes_cli/moa_config.py`, provider and reasoning tests. |
| Approvals | Smart LLM classification, one-command verdicts, unconditional user deny rules, denial reasons, plugin approval escalation | `tools/approval.py` and approval tests. The capability landed, but the fork default remains `manual`. |
| Secrets and security | Pluggable `SecretSource`, Bitwarden and 1Password, deterministic precedence/conflict/provenance reporting, credential-read guards and redaction hardening | `agent/secret_sources/`, `tests/secret_sources/`, security and secrets documentation. |
| Delegation | Live child transcripts, unified concurrency controls, explicit durable background completion delivery | `tools/delegation_live_log.py`, `tools/delegate_tool.py`, `tools/async_delegation.py`, delegation tests. Background execution remains opt-in rather than automatic. |
| Gateway delivery | Ordinary final-response delivery-obligation ledger with restart redelivery | `gateway/delivery_ledger.py`, `gateway/run.py`, `gateway/platforms/base.py`. Fork work-ledger events stay on their own recovery path. |
| Gateway profiles | Multiplex profile routing by platform/guild/channel/thread and profile-local secrets/config/session state | `gateway/profile_routing.py`, `gateway/config.py`, `docs/profile-routing.md`, multi-profile gateway tests. |
| Discord and messaging | Reconnect recovery, selective action/direct-question thread routing, view/approval authorization hardening, richer platform controls | `plugins/platforms/discord/adapter.py` and Discord tests. The fork retains open no-policy admission and selective rather than universal parent-channel threading. |
| Sessions and compression | Expanded session export formats and filters, lineage-aware exports, state DB consolidation, compression integrity work | `hermes_cli/session_export*.py`, `gateway/session.py`, `agent/context_compressor.py`, session/compression tests. The fork additionally retains JSONL compatibility and later/larger compression defaults. |
| Cron and Kanban | Cron run claims/audit hardening and Kanban attachments/project-directory/result improvements | `cron/`, `tools/kanban_tools.py`, `hermes_cli/kanban*.py`, focused cron/Kanban tests. |
| Dashboard, TUI and desktop | Upstream OAuth, reconnect, image, billing, settings, worktree, layout and performance work is integrated while the fork's real-PTY dashboard and Command Center/compute-host split remain intact | `apps/desktop/`, `ui-tui/`, `web/`, `tui_gateway/`. Focused web, TUI, desktop, Python, build and packaging checks passed; see the completed UI owner update below. |
| Skills and MCP | MCP OAuth/provider updates and new optional skills, with generated docs/catalog updates | `optional-skills/mcp/mcp-oauth-remote-gateway/`, `optional-skills/security/unbroker/`, MCP code/tests and generated website pages. The OAuth gateway skill now uses progressive disclosure below the fork context budget; compact default Hermes/humanizer prompts remain. |
| Performance | Upstream initialization, caching, streaming, mixed-tool and UI performance work is represented in the merged source | Agent initialization/cache code plus desktop/TUI changes. The release-note claim of about 80% TTFT improvement was not independently benchmarked in this integration worktree. |

## Preserved fork invariants

- Profile-scoped files continue to use `HERMES_HOME`; local gateway, work-ledger, Command Center, cron and Kanban contracts remain fork-owned where they are stronger or more specific than upstream.
- Smart approvals and `approvals.deny` are available, but `hermes_cli/fork_defaults.py` keeps `approvals.mode: manual` as the fresh-install default.
- `max` and `ultra` remain valid explicit reasoning efforts. Automatic fork tier/classifier spillover remains capped at `xhigh`; this is a routing policy, not removal of the explicit tiers.
- Discord keeps the existing open development default when no user, role or channel admission policy is configured. Any configured allowlist still fails closed for non-matches. This is deliberately less restrictive than upstream's empty-allowlist default and must not be presented as equivalent security.
- `delegate_task` remains synchronous by default. `background=true` is explicit, read-only for general delegation, and requires a session capable of later delivery; no blanket top-level auto-backgrounding was adopted.
- Discord does not automatically thread every ordinary parent-channel message. The fork selectively threads classified action requests, message-link/threaded work and addressed direct questions while leaving ordinary free-response chatter inline.
- Events carrying `work_item_id` do not create generic delivery obligations. `GatewayWorkLedger` remains authoritative for their exact-attempt and uncertain-send recovery, avoiding duplicate delivery across two ledgers.
- Gateway transcript loading retains the longer of SQLite and legacy JSONL histories and falls back when either source is missing or unreadable. This is a stronger migration/recovery behavior than strict `state.db`-only loading, not feature loss.
- Compression keeps a configurable `max_summary_tokens` default of 32,000 and a 70% threshold for 512K-and-larger contexts. Small-context safeguards and provider-specific raises still apply.
- Compact fork `hermes-agent` and `humanizer` skill bodies remain the default prompt surface. Expanded upstream material is retained in `references/full-guide.md` rather than injected on every trigger.
- The repo's bounded smoke/full test wrapper, source/trusted CI separation and fork operational checks remain the release gate rather than upstream's complete workflow topology.

## Differences, omissions and replacements

| Upstream behavior | Candidate integration | Classification and impact |
|---|---|---|
| Smart approvals default | Smart capability retained; default is manual | Deliberate default difference. No capability loss; more human confirmation and more approval friction. |
| Automatic use of the highest reasoning tiers | Explicit `max`/`ultra` retained; automatic tier routing stops at `xhigh` | Deliberate routing difference. Operators and per-model overrides can still request higher effort. |
| Live reasoning enabled by default | Live reasoning remains available, but defaults off | True upstream default difference. Users can enable it explicitly; capability is retained without imposing live reasoning output on existing fork sessions. |
| Empty Discord allowlist fails closed | Empty admission policy remains open for fork development compatibility | Deliberate weakened security default, not a stronger replacement. Shared/external bots must configure user/role/channel policy. |
| Every eligible top-level delegation is detached | Detachment requires explicit `background=true`; unsupported/stateless contexts run synchronously | Replaced by stricter ownership and delivery semantics. Durable background completion still exists when requested. |
| Broad parent-channel auto-threading | Selective classifier/action routing; ordinary chatter stays inline | Replaced by fork-specific Discord workflow behavior. Threaded implementation work is retained without forcing all messages into threads. |
| One generic delivery-obligation recovery path | Ordinary finals use `delivery_obligations`; fork `work_item_id` events use `GatewayWorkLedger` only | Stronger local replacement. Double enrollment would create duplicate cross-ledger delivery risk. |
| State DB as the sole modern transcript source | SQLite plus legacy JSONL, choosing the longer valid transcript | Stronger migration/recovery replacement. Costs extra storage and two-source complexity. |
| Upstream compression defaults | 32K configurable summary cap; 70% large-context threshold | Deliberate fidelity/cost tradeoff. More context is retained before compression and summaries may be larger. |
| Unified middleware around tool execution | Core concurrent/sequential executors have middleware support, but legacy inline/intercept branches are not proven to cross every upstream request/execution boundary | Partial omission / extension risk. Plugins depending on exhaustive middleware coverage require targeted parity tests before sign-off. |
| Expanded Hermes and humanizer prompt prose | Compact default skills plus archived full guides in `references/` | Stronger prompt-cache/token replacement for the fork's operating model; detailed information remains loadable on demand. |
| Automatic xAI retirement startup warning/check | Automatic startup scan intentionally absent; manual `hermes migrate xai` remains | True convenience omission, not runtime inference loss. Documentation must not promise `hermes chat`/`doctor` startup warnings. |
| Upstream generated CI/release workflow suite and duration-sliced Python runner | Fork keeps its own `.github/workflows/tests.yml`, trusted validation and `scripts/run_tests.sh`; upstream per-file duration slicer is absent | Tooling replacement/omission, not runtime feature loss. See exact inventory below. |
| Read-only-source setuptools output redirection | `setup.py` remains identical to the fork parent | True packaging convenience omission. The fork's trusted-base supply-chain gate categorically rejects install-hook changes, matching the precedent from the previous upstream sync. Builds from a read-only source tree must provide writable build/egg-info output locations explicitly. |

## Tooling inventory deliberately not adopted

The following upstream 0.19 files are absent in the candidate tree and should be described as CI/release tooling omissions, not Hermes runtime feature loss:

- `.github/workflows/ci.yml`
- `.github/workflows/deploy-site.yml`
- `.github/workflows/docs-site-checks.yml`
- `.github/workflows/docker.yml`
- `.github/workflows/docker-lint.yml`
- `.github/workflows/lint.yml`
- `.github/workflows/history-check.yml`
- `.github/workflows/skills-index.yml`
- `.github/workflows/skills-index-freshness.yml`
- `.github/workflows/supply-chain-audit.yml`
- `.github/workflows/upload_to_pypi.yml`
- `.github/workflows/uv-lockfile-check.yml`
- `.github/actions/detect-changes/action.yml`
- `.github/actions/retry/action.yml`
- `scripts/run_tests_parallel.py`

The fork still has its own test/site lanes, OSV scanning, JS workflows, lockfile checks and wrapper-enforced hermetic test behavior. Absence of the upstream topology means parity must be evaluated by outcome, not filename count.

## Resolved integration losses

### Verification candidates remain private until verified

The upstream-dominated merge initially reversed the fork's verification-final invariant by persisting and emitting a candidate answer before verify-on-stop/pre-verify completed. The staged restoration now:

- Marks both verification-stop and pre-verify candidates and nudges as ephemeral scaffolding.
- Keeps the candidate/nudge pair only in model history for role alternation and prompt-cache continuity.
- Withholds private candidates from interim UI callbacks, SQLite and JSON logs.
- Lets a later verified answer supersede all private candidates.
- Reuses a pending candidate exactly once when the continuation budget is exhausted, preserving `response_previewed=True` only when provider deltas had already exposed that same fallback.
- Restores explicit intent-ack continuation across configured provider/runtime paths while preserving the Codex-only default, available-tool requirement, no-prior-tool safeguards and two-nudge cap.

Verification: the candidate/finalizer focused set passed 33 tests. The later full acknowledgement/helper and verification-continuation set passed 26 tests, including both formerly failing iteration-budget acknowledgement nodes and an unmocked AIAgent forwarder check.

This was a true unintended behavior loss and is now restored, not accepted as an upstream replacement.

### Optional MCP skill fits the fork context budget

`optional-skills/mcp/mcp-oauth-remote-gateway/SKILL.md` was reduced from 23,843 to 9,710 characters. Full dashboard/TTY/manual OAuth capability and safety guidance remains available through directly linked progressive-disclosure references for the manual flow, troubleshooting/provider variants and Stripe revocation behavior; the diagnostic script remains intact.

Verification: 12 focused skill/source-hygiene tests and 9 website generator tests passed, and `website/scripts/generate-skill-docs.py --check` reported the generated English docs current. This was prompt-source compaction only; no OAuth behavior was omitted.

## Completed owner updates

### CLI, config, MCP, plugins and runtime-provider

The CLI/config/MCP/plugins/runtime-provider reconciliation is staged.

- Focused results: 362 config/MCP/tools tests, 118 plugin tests, 44 `/fast` and single-query tests, and 70 browser/reasoning tests passed.
- `/fast` remains a session-scoped default rather than a process-global mutation.
- Explicit `max` and `ultra` reasoning efforts remain supported.
- Live reasoning remains available but defaults off, which is a true upstream default difference rather than capability loss.
- A configured custom `api_mode` remains authoritative.
- Fork-local initialization, resume and compression behavior remains retained.

### UI, TUI, dashboard and desktop

The UI reconciliation is staged.

- Focused results: web 104 passed; TUI 1,233 passed; desktop 2,159 passed with 2 skipped; Python UI/runtime groups passed 849 plus 195 tests.
- Web/TUI/desktop builds and desktop packaging checks passed.
- Dashboard chat continues to embed the real PTY rather than replacing the terminal transcript/composer in React.
- Command Center, compute-host separation and worker behavior remain preserved.
- Upstream OAuth, reconnect, image, billing, settings, worktree, layout and performance work is integrated.
- UI approval defaults were corrected to manual while smart approval remains selectable.

These focused results establish the owner handoffs, not release sign-off.

## Final verification checkpoint — 2026-07-21

The staged candidate is clean of unresolved, unstaged and untracked paths. Ruff, Python compilation, whitespace checks and the deterministic local autoreview helper passed.

- Fork smoke gate: 1,175 passed under the standard four-worker wrapper.
- Instrumented inherited aggregate: 45,598 passed, 918 failed and 120 skipped in 51 minutes, with no inode, logging-handler or fixture cascade. The failures were captured in a JUnit report and an immutable `lastfailed` snapshot.
- Fresh-process classification: all 155 unique aggregate-failing files pass when each file runs in its own wrapper process with one worker, pytest caching disabled and a unique disk-backed temp root:
  - Core: 21/21 files.
  - Gateway and TUI gateway: 26/26 files, 489 tests.
  - CLI and miscellaneous: 63/63 files, 2,646 tests with one expected Windows-only skip.
  - Tools and plugins: 45/45 files.

The isolation pass found and repaired genuine merge losses before reaching green:

- Race-safe/deepest-message resume selection, owned-session closeout and prompt/tool cache parity.
- MoA request-client routing and primary-facade behavior, plus bounded steer-marker persistence.
- Plain successful STT payloads while retaining Discord sender/intermediary provenance.
- Provider-isolated doctor checks, local authenticated dashboard status metadata and deterministic dashboard MCP discovery tests.
- TUI-safe Honcho subprocess stdin, blocking Ruff enforcement in the fork CI topology, a 1,000-row recent-session scan and profile-aware Chromium path assertions.

The xhigh failure-gate audit classified all 918 aggregate failures as follows:

| Classification | Nodes | Files | Disposition |
|---|---:|---:|---|
| Independently reproducible product regressions | 18 | 7 | Fixed; final fresh-process runs pass. |
| Stale assertions or deliberate fork-policy differences | 14 | 7 | Tests corrected to the integrated contracts. |
| Infrastructure-only port collision | 1 | 1 | Test made deterministic. |
| Aggregate-only cross-file contamination | 885 | 142 | Accepted as a non-blocking test-isolation follow-up. |

File counts overlap because two dashboard test files contained both independently reproducible and aggregate-only nodes. The immutable failing-node snapshot used for classification has SHA-256 `7cdafce9d1e2441648aeb2dbf4edfba591b35128c8ebc4438ea1aa502c2c1362`.

The 885 failures that disappear in fresh processes show repeated module/class identity, registry, `HERMES_HOME`, CWD and callback state leakage between files. The strongest identified cause is test fixtures that delete and reimport broad core package graphs without restoring the original module objects; already-collected tests then retain stale classes, registries and `ContextVar` instances. Production does not perform that package-wide eviction pattern. The accepted release gate is therefore the fork-owned smoke suite plus fresh-process verification of every inherited aggregate-failing file, followed by required PR CI. Any independently reproducible failure remains blocking.

## Residual risks and required verification

1. Track inherited-suite cross-file isolation as follow-up work, starting with the non-restoring Kanban module-eviction fixtures and a guard that identifies the first test replacing core module identities. Until repaired, fresh-process per-file reruns are the authoritative classifier for aggregate-only failures.
2. Add targeted tests for middleware coverage on any legacy inline/intercept tool paths relied on by plugins.
3. Review Discord deployment configuration: the preserved open default is suitable only for trusted development environments.
4. Confirm release/CI outcome parity for omitted upstream workflow topology, especially docs generation, dependency/lockfile drift, supply-chain checks, Docker validation and package publishing.
5. Live Discord/Telegram network E2E, Nix evaluation and PowerShell syntax checks remain unrun in this environment.
6. The two-parent merge commit has been created and inspected, release-tag ancestry is verified, and the first-parent preservation scan is complete. The PR must be merged without squashing.

No additional confirmed runtime capability absence was found beyond the explicit inventory above. The candidate-final visibility regression, optional-skill context-budget violation and every independently reproducible residual are resolved in the staged tree. The aggregate isolation disposition is accepted for this integration; required PR CI and live messaging checks remain before release sign-off.
