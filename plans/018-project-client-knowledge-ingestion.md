# Plan 018: Ship Gmail Client-Knowledge Intake

> **Status:** IN PROGRESS. Reviewed implementation plan; PR 1 durable core is
> under implementation. Initial xhigh verdict: `REVISE`.
> Material findings were incorporated on 2026-08-05; execute only the revised
> ordered pull requests below.
> Operator decisions were recorded on 2026-08-05: Gmail-first PID scope and the
> trusted same-UID worker model are approved; model calls follow the named
> `advanced` tier; historical email processing is forbidden; PID reuses its
> existing Notion `Client Feedback` data source; and high-impact review requests
> go to the top-level PID Discord channel with an `@Devs` role mention.
> Do not route live mail to the replacement until the controlled rollout gate
> passes.
>
> **Drift check (run first):**
> `git diff --stat 3466fed8b..HEAD -- plugins/client_knowledge_gbrain gateway/run.py gateway/platforms/base.py plugins/platforms/discord/adapter.py plugins/memory/honcho hermes_cli/config.py agent/plugin_llm.py agent/auxiliary_client.py pyproject.toml tests/plugins tests/gateway`
> Re-read any changed in-scope seam before implementing. Stop if the trusted
> project boundary, GBrain pin, worker isolation, plugin LLM API, or gateway
> authorization order changed materially.

## Outcome

Deliver one reliable production path for explicit project email aliases:

```text
pid@sligolabs.com
  -> archival Gmail mailbox (read-only API)
  -> durable local intake ledger + immutable raw MIME
  -> one Notion source record with raw .eml and original attachments
  -> cited GBrain project synthesis
  -> compact Honcho projection for selected stable current claims
```

The first production milestone is Gmail for PID. Discord attachment ingestion
reuses the same artifact contract later, but is not on the critical path to
turning email intake back on.

## Status And Boundaries

- **Priority:** P1
- **Effort:** L, split into bounded PRs below
- **Risk:** HIGH because this writes client source material and synthesized knowledge
- **Planned at:** `3466fed8b`, 2026-08-05
- **Depends on:** merged GBrain proof PRs #834/#836 and merged Obsidian retirement PR #852

### Plan publication gate

This checkout currently excludes `/plans/` through local `.git/info/exclude`, so
the plan is not reproducible from commit `3466fed8b`. Before implementation
begins, create the implementation branch/worktree and deliberately add
`plans/018-project-client-knowledge-ingestion.md` plus `plans/README.md` to the
first PR (for example with `git add -f` if the local exclude still applies).
Record the resulting plan commit as the execution baseline. `Planned at` above
identifies the source-code baseline inspected during planning, not a claim that
the plan file already exists in that commit.

### Knowledge roles

| Store | Role |
| --- | --- |
| Notion | Canonical long-term client source archive: raw email, attachments, provenance, readable rendering, and processing receipt. |
| GBrain Git source | Canonical synthesized project knowledge with citations, status, supersession, contradictions, and Git history. |
| Honcho | Rebuildable compact recall of selected stable current project claims. Never raw source and never canonical. |
| Local SQLite + raw spool | Operational recovery only: cursors, claims, retries, hashes, external IDs, and immutable pending source bytes. Not a knowledge system. |

Command Center, task creation, outbound email, Gmail mutation, GBrain autopilot,
ambient capture, dream cycles, and coding-worker knowledge access are out of scope.

## What Already Exists

### Continue directly

1. **Project-scoped GBrain retrieval and validation**
   - `plugins/client_knowledge_gbrain/client.py` pins GBrain `v0.42.73.1` at
     `aecb33e795cc4806f760446c55ab1c350194ddc8` and Bun `1.3.14`.
   - `plugins/client_knowledge_gbrain/scope.py` validates lowercase project keys,
     `projects/<project-key>/` slugs, frontmatter, same-project supersession, and
     `notion:page:<id>` citations.
   - `plugins/client_knowledge_gbrain/tools.py` exposes only bounded read-only
     search and exact page reads tied to the trusted gateway project key.
   - `docs/runbooks/gbrain-client-knowledge-proof.md` and
     `scripts/prove_gbrain_client_knowledge.py` prove project isolation, cited
     retrieval, Git backup/restore, and the pinned runtime.

2. **Orchestrator retrieval policy and worker denial**
   - `agent/prompt_builder.py::CLIENT_KNOWLEDGE_GUIDANCE` already requires
     explicit brain-first retrieval for mapped project history and decisions.
   - `hermes_cli/config.py` denies the `client_knowledge` toolset to delegated
     children.
   - `agent/opencode_worker.py` requires isolated OpenCode config and strips
     `GBRAIN_HOME` plus `HERMES_CLIENT_KNOWLEDGE_*` from coding workers.

3. **Trusted project identity**
   - `gateway/discord_project_mapping.py` and `gateway/session_context.py`
     already establish trusted project keys for mapped Discord sessions.
   - Reuse the same lowercase project-key validator and the same project
     registry for email aliases. Do not invent a second project namespace.

4. **Supported model and durability primitives**
   - `agent/plugin_llm.py::complete_structured()` provides host-owned,
     schema-validated plugin inference.
   - `hermes_cli/plugins.py::register_auxiliary_task()` provides operator-
     configurable routing for plugin side jobs.
   - `gateway/delivery_ledger.py` and `hermes_cli/kanban_db.py` provide patterns
     for WAL, atomic claims, owner identity, retries, and stale-claim recovery.
   - `tools/read_extract.py` provides dependency-free bounded extraction for
     DOCX, XLSX, and notebooks; reuse it behind stricter intake limits.

5. **Historical evidence worth preserving**
   - The archived collector is at
     `/home/droid/archives/hermes-retired-obsidian/20260805T225049Z/config/gmail-intake/gmail_intake_collector.py`.
   - `$HERMES_HOME/state/gmail-intake/state.json` and `events.jsonl` prove the
     old history cursor, exclusion decisions, one unresolved human message, and
     four PID source pages already created in Notion.
   - Those four pages still exist in PID's `Client Feedback` data source. Leave
     them untouched as historical records. The replacement must not import,
     reprocess, reinterpret, or use them to seed GBrain/Honcho.

### Reuse only as reference

1. `plugins/teams_pipeline/pipeline.py::NotionWriter` proves authenticated page
   create/update via `httpx`, but it is meeting-summary-specific, uses an older
   API version, and does not upload source files. Copy the transport/testing
   style into a dedicated client-knowledge Notion adapter; do not subclass it.
2. The archived Gmail collector's `history.list`, MIME metadata, and
   `Delivered-To`/`X-Original-To` inspection are useful fixture material. Port
   behavior behind tests; do not copy the script wholesale.
3. Existing Discord attachment cache code proves authenticated download and
   size gates, but its `MessageEvent.media_urls` arrays are lossy and temporary.
   A later Discord PR must add immutable descriptors rather than infer durable
   identity from cache filenames.
4. Honcho `create_conclusion`, `list_conclusions`, and `delete_conclusion`
   demonstrate supported APIs. Add projection IDs and project-aware projection
   semantics in the intake subsystem; do not turn ordinary conversation memory
   into the queue.

### Discard

1. The archived JSON state/event files as a live queue. They have no atomic
   claims, stage transactions, or external-object recovery.
2. Pub/Sub/watch as a release dependency. The former watch expired, required a
   separate service-account path, and had no reconciliation fallback. Gmail's
   own documentation requires periodic reconciliation because notifications can
   be delayed or dropped. Start with bounded history polling; push may later be
   an optional wake-up optimization using the same poller.
3. CC/BCC/forward heuristics for registered aliases. The exact delivered alias
   is authoritative. Heuristics may only flag ambiguity on legacy mailbox mail;
   they may never select or change a project.
4. Content-based project guessing such as subject/snippet keyword matching.
   Unknown, missing, or conflicting recipients go to `needs_mapping` without
   model inference or downstream writes.
5. The old `invoke_agent()` subprocess and skill bundle. Deterministic code owns
   routing, state transitions, citations, paths, and writes. Models only return
   bounded structured interpretation/assimilation proposals.
6. Writing synthesis to Notion, repo docs, Discord topics, or the retired vault.
7. Feeding raw emails, source digests, tentative claims, disputes, deadlines,
   action items, credentials, or every project fact into Honcho.
8. Broadening model-visible GBrain tools into writes. Publishing remains an
   internal deterministic service.
9. Any historical Gmail replay, backfill, migration, or reconciliation. The new
   ledger starts at a fresh cutover history baseline and processes only message
   additions strictly after that baseline.

## Decisions Locked For V1

1. **One bundled standalone plugin:** extend `client-knowledge-gbrain` into a
   coherent `client-knowledge` plugin package, or rename it once in the first PR
   if repository plugin migration tests prove a clean rename. Do not create a
   web of sink plugins.
2. **One plugin-owned SQLite database:**
   `$HERMES_HOME/client-knowledge/intake.db`, WAL mode, restrictive permissions.
3. **One raw spool:** `$HERMES_HOME/client-knowledge/raw/<artifact-id>/`, atomic
   writes, restrictive permissions, SHA-256 receipts. Keep raw files permanently
   through PID rollout; retention comes only after a tested restore exercise.
4. **Reuse PID `Client Feedback`:** PID source records go into the existing
   `Client Feedback` database under the PID Notion page
   `https://app.notion.com/p/PID-4b02af635e494840b7620ef94009b1f2`.
   Its database ID is `b189603e-e75f-435c-b8b3-0babcf6be30d` and data-source ID
   is `c8c5d6df-c08f-458a-a640-5745986a11e7`. Keep IDs configurable rather than
   code constants. Reuse its current `Feedback`, `Status`, `Date Received`,
   `Client / Source`, `Category`, and `Priority` properties, and add only the
   minimal machine identity/provenance properties required for idempotent source
   archival, such as `Source ID`, `Source Type`, `Source Hash`, and `Source URL`.
   Future projects may configure their own feedback/source data source; V1 does
   not create a global replacement database.
5. **Notion source representation:** raw `.eml`, each original attachment,
   selected headers, faithful readable body, provenance, hash, processing status,
   and links. No synthesized decisions or project brief.
6. **GBrain topology:** one Git-backed `client-knowledge` source with hard paths
   under `projects/<project-key>/` and the existing read-side validation.
7. **Gmail trigger and present-only boundary:** a systemd timer invokes a one-shot
   poller every two minutes. At cutover, persist Gmail's current mailbox
   `historyId` as an immutable `cutover_history_id` before enabling processing.
   Admit only message-addition records whose Gmail message `historyId` is
   strictly greater than that baseline. Each run uses the durable cursor and a
   bounded reconciliation query, but reconciliation applies the same baseline
   filter. A 404/expired cursor performs a bounded alias resync and still rejects
   every message at or below the cutover baseline. There is no historical
   backfill command or rollout mode in V1.
8. **Model routing:** register two auxiliary tasks, `client_knowledge_interpret`
   and `client_knowledge_assimilate`, that resolve the named Hermes `advanced`
   tier at call time. Do not duplicate or pin the tier's current model,
   reasoning, provider, or fast-mode values in this plugin; if `advanced` moves,
   this pipeline follows it. Add the smallest generic task/tier-aware parameter
   to `PluginLlm`. The host must return the concrete provider/model actually
   used. Do not use a generic fallback chain for client content: if the resolved
   `advanced` route is unavailable or changes during a call, retain the job and
   retry rather than silently using another route.
9. **Human review in Discord:** high-impact contractual, financial, security,
   credential, or major-scope changes and every unresolved contradiction remain
   `needs_review`; they do not become current or reach Honcho automatically.
   Sligo Labs posts the review request in the exact mapped top-level project
   channel, not an intake/feature thread, and mentions the configured project
   reviewer role. For PID, the channel is `1505275259006484570` and the `Devs`
   role is `1534760260273967289`; send `<@&1534760260273967289>` with Discord
   metadata `allowed_role_mentions: ["1534760260273967289"]` so the role is
   actually notified without enabling arbitrary role mentions. An authorized
   Dev resolves it with `/client-knowledge approve <review-id>` or
   `/client-knowledge reject <review-id> [reason]` in that same top-level
   channel. The command is deterministic and records Discord user ID, decision,
   reason, message ID, and timestamp. It rejects DMs, threads, other channels,
   other projects, stale/already-resolved IDs, and users who lack the configured
   review role or explicit operator allowlist. Conversational prose, reactions,
   and agent interpretation never approve a claim.
10. **Same-UID limitation:** coding workers are trusted processes for V1. Tool
    and environment isolation remain mandatory, but they are not a filesystem
    security boundary: a same-UID process may be able to read client files or
    credentials. V1 must not claim otherwise. Launch requires the operator to
    explicitly accept this trust model. If coding workers are not fully trusted,
    stop rollout and isolate the corpus/credentials under a separate UID,
    container, or mount namespace first.
11. **Structured validation:** schema-validated client inference is fail-closed.
    Add direct exact-pinned `jsonschema==4.26.0` dependency coverage (matching
    the current lock) and reject structured calls when validation is unavailable.

## Target Contracts

### Artifact

```text
InboundArtifact
  artifact_id                 stable provider identity, not a hash alone
  provider                    gmail | discord
  source_type                 email | attachment
  project_key
  provider_message_id
  provider_attachment_id
  parent_artifact_id
  occurred_at
  actor_display
  actor_id
  delivered_alias
  original_filename
  mime_type
  size_bytes
  sha256
  raw_path
  source_url
  text_context
  provenance_json
```

For Gmail, the message is the parent artifact and the raw RFC 822 message is
stored once. Original attachments are child artifacts. Provider identity is
`gmail:<mailbox>:message:<message-id>` and, for children,
`...:attachment:<attachment-id-or-part-id>`.

### Operational stages

```text
discovered
raw_preserved
notion_archived
extracted
interpreted
assimilated
honcho_projected
complete
needs_mapping
needs_review
quarantined
```

Each successful stage records its output receipt in the same transaction that
advances the stage. Each external write stores an idempotency identity before
the next stage runs. Reconciliation must recover existing Notion pages/uploads,
GBrain source commits, and Honcho conclusions after a crash.

### Interpretation

The source-interpretation auxiliary task receives only bounded, deterministically
redacted extracted text plus source coordinates and returns schema-validated
JSON:

```json
{
  "summary": "...",
  "candidate_learnings": [],
  "decisions": [],
  "requirements": [],
  "preferences": [],
  "risks": [],
  "stakeholders": [],
  "deadlines": [],
  "open_questions": [],
  "suggested_actions": [],
  "evidence": []
}
```

Source text is quoted data, never instructions. Persist source hash, schema and
prompt versions, actual provider/model attribution, usage, and evidence offsets.

### Assimilation

For each candidate learning, retrieve only current pages within the mapped
project and propose one operation:

```text
add | confirm | refine | supersede | contradict | mark_tentative |
ignore_transient | needs_review
```

Deterministic code validates the operation, slug, status transition, citation,
project boundary, and final Markdown before writing. Use topic/claim pages, not
one canonical summary page per email. Every current claim cites at least one
Notion page. Commit Git and then run pinned GBrain sync with `--no-pull`; recover
by artifact ID + assimilation version before writing again.

### Honcho projection

Promote only claims that are current, stable, broadly useful, compact, and not
confidential/restricted. Prefix with project and GBrain reference:

```text
[PID] Weekly project status is currently due Monday. Source: gbrain:projects/pid/requirements/reporting-cadence
```

The operational ledger stores the Honcho conclusion ID and exact projection
content. A changed/superseded/disputed/archived page creates a corrected
projection and retires the obsolete projection using its recorded ID according
to Honcho's supported semantics. A reconciliation command can rebuild all
promotable projections from GBrain.

The existing Honcho boolean create API is insufficient. Add a dedicated
projection API that returns created IDs and carries a deterministic projection
marker. Recovery must be able to query/adopt an uncertain remote creation after
a crash instead of creating a duplicate or losing the ability to retract it.

## Delivery Plan

### PR 1: Durable Core And Operator Surface

**Implement**

- Extend the bundled `client-knowledge-gbrain` plugin in place with:
  - immutable artifact dataclasses and validators;
  - plugin-owned SQLite store and schema migrations;
  - atomic job claim/lease/heartbeat/retry/quarantine;
  - cursor and external receipt tables;
  - atomic raw-spool writes and hashes;
  - `hermes client-knowledge status|list|show|retry|quarantine|reconcile|run-once`;
  - non-secret `client_knowledge` config defaults and secret metadata only for
    required credentials.
- Reuse project-key validation from `scope.py`.
- Keep the existing plugin manifest identity through PR 1 so enabled-plugin
  configuration, imports, proof scripts, and read-tool registrations remain
  compatible. Reconsider a rename only in a separately reviewed migration.
- Add a small task-aware plugin LLM bridge if needed, register the two auxiliary
  task slots, but do not call models yet.
- Propagate concrete provider/model attribution from the dynamically resolved
  `advanced` route and fail closed if that route is unavailable or changes
  during the call.
- Make JSON Schema validation a direct, exact-pinned, fail-closed dependency.

**Core tests**

- Duplicate provider identity inserts once.
- Same content from two provider identities remains two source artifacts.
- Two workers cannot claim the same job.
- Expired/dead owner claims are recovered without stealing live claims.
- Stage + receipt transitions are atomic.
- Raw writes are atomic, hashed, mode-restricted, and never target real HOME in tests.
- CLI output never prints source bodies, secrets, or sensitive filenames.
- The resolved `advanced` route persists actual attribution; route drift or any
  attempted fallback is rejected before inference.
- Missing schema-validation support rejects structured output.

**Verify**

```bash
scripts/run_tests.sh tests/plugins/test_client_knowledge_intake_store.py \
  tests/hermes_cli/test_client_knowledge_cli.py \
  tests/agent/test_plugin_llm.py \
  tests/hermes_cli/test_plugin_auxiliary_tasks.py
```

### PR 2: Notion Source Archive

**Implement**

- Add a dedicated Notion client using current API version `2026-03-11`.
- Configure PID's existing `Client Feedback` data-source ID and explicit
  property-name mapping. Do not create a new PID source database.
- Preflight the data-source schema and integration access.
- Add the minimal `Source ID`, `Source Type`, `Source Hash`, and `Source URL`
  properties if absent, preserving every existing property, row, view, option,
  and current value. Use the existing fields as follows:
  - `Feedback`: email subject/title;
  - `Status`: `New` at archive, `In Progress` while processing/reviewing,
    `Resolved` only after completion;
  - `Date Received`: Gmail receipt time;
  - `Client / Source`: sender/source label;
  - `Category`: deterministic `Other` at archive; no model classification is
    required for source preservation;
  - `Priority`: leave unset unless an operator or later approved workflow sets it.
- Create or recover one page per parent source artifact.
- Upload raw `.eml` and original files through Notion File Upload APIs:
  single-part up to 20 MB, multipart above that, bounded by an operator-configured
  admission cap and verified workspace limits.
- Add faithful body/header blocks and processing metadata. Chunk rich text to
  Notion limits.
- Store page/file-upload IDs before moving forward. Query by configured stable
  Source ID property when a local receipt is missing after a crash.
- Model file upload as explicit durable substages: upload-created, bytes-sent,
  multipart-completed, page-attached, receipt-verified. Recover or quarantine an
  uncertain upload at each boundary using artifact identity, filename, size,
  SHA-256, upload ID, and page/block identity. Never append duplicate attachment
  blocks merely because the local receipt was lost.
- Explicitly exclude the four historical PID rows from operational receipt
  import. The new pipeline recognizes only fresh source IDs after cutover.

**Core tests**

- Page and file creation happy paths using `httpx` mock transport.
- Existing page/upload recovery after simulated crash.
- Crash recovery at upload creation, byte transfer, multipart completion, page
  attachment, and local receipt persistence.
- 429/5xx retry classification and non-retryable schema/permission failure.
- Small and multipart file flows.
- No synthesized knowledge fields are written to Notion.
- Existing historical rows and schema options remain byte/field-value unchanged
  except for additive empty machine-properties introduced at the data-source level.

**Verify**

```bash
scripts/run_tests.sh tests/plugins/test_client_knowledge_notion.py \
  tests/plugins/test_teams_pipeline_plugin.py
```

Then run an operator preflight against a sandbox Notion data source and attach
only synthetic fixture files.

### PR 3: Gmail Poller And Raw Preservation

**Implement**

- Use a dedicated OAuth token with exactly `gmail.readonly`; do not reuse the
  disabled Gmail MCP scope configuration because it includes compose.
- Add explicit mailbox and alias registry configuration such as:

```yaml
client_knowledge:
  projects:
    pid:
      aliases: [pid@sligolabs.com]
      notion_source_id: <configured-id>
  gmail:
    enabled: false
    mailbox: sligolabs@gmail.com
    token_path: <profile-scoped-secret-path>
    poll_interval_seconds: 120
    reconcile_lookback_days: 14
    max_reconcile_messages: 500
    historical_processing: disabled
```

- On first start, initialize `cutover_history_id` and the normal cursor from the
  current mailbox history ID without replay. The baseline is immutable after
  processing begins. Do not expose a backfill command in V1.
- On every run:
  1. request all `history.list` pages from the committed cursor;
  2. retrieve candidates as raw RFC 822 plus minimal metadata;
  3. resolve the exact alias in order `Delivered-To`, `X-Original-To`, `To`, `Cc`;
  4. reject conflicts and unknown aliases to `needs_mapping`;
  5. atomically preserve raw MIME and enqueue artifacts;
  6. advance the cursor only after every candidate from the page range is durable.
- Reconcile recent explicit-alias mail every run, bounded by date and max count,
  and discard candidates whose message `historyId` is not strictly above the
  immutable cutover baseline.
- On Gmail history 404, perform bounded alias sync and record why; apply the
  immutable cutover baseline before admitting anything, then establish the next
  incremental cursor without processing historical mail.
- Parse MIME only after raw preservation. Enforce message bytes, MIME parts,
  nesting, attachment count, and attachment bytes limits.
- Do not include sender/system heuristics in admission for explicit aliases.
  Optional noise classification may mark a source as `ignored` after archival,
  but must remain inspectable and reversible.
- Ship a one-shot command and a systemd timer/service; do not launch a separate
  forever loop.

**Core tests**

- Alias precedence, case normalization, plus-address handling policy, conflicts,
  and unknown recipient behavior.
- Multi-page history, duplicate notifications, cursor crash points, 404 recovery,
  and reconciliation after a simulated dropped notification.
- Messages at or below `cutover_history_id` are rejected on normal history,
  reconciliation, restart, and 404 recovery paths. The baseline cannot be moved
  backward through config or CLI.
- Raw MIME/attachment identity and duplicate handling.
- OAuth/rate-limit failures never advance the cursor.
- Gmail write methods/scopes are absent.

**Verify**

```bash
scripts/run_tests.sh tests/plugins/test_client_knowledge_gmail.py \
  tests/skills/test_google_workspace_api.py
```

Do not send a live email yet. Validate with recorded/synthetic Gmail API fixtures.

### PR 4: Extraction And Structured Interpretation

**Implement**

- Deterministically parse selected headers, `text/plain`, sanitized HTML text
  with no remote fetches, and original attachment metadata.
- Reuse `tools/read_extract.py` for DOCX/XLSX/IPYNB behind intake-specific zip
  member, expanded-size, row, and output-character limits.
- Support UTF-8/plain structured text first. Add PDF extraction only through a
  separately pinned, sandboxed dependency or subprocess with explicit resource
  limits; otherwise archive PDF and mark extraction unsupported for V1.
- Run deterministic secret-pattern redaction before model input. Record redaction
  categories/counts, never values.
- Invoke `client_knowledge_interpret` through the task-aware structured LLM API.
- Validate the exact schema, evidence coordinates, output sizes, and project key.
- Prompt injection in client content remains quoted source data. No tools are
  available to the interpretation call.

**Core tests**

- MIME alternatives, malformed MIME, HTML sanitization, nested archives/OOXML
  limits, unsupported binary files, oversized source handling, secret redaction,
  prompt-injection fixtures, schema failures, timeout/retry, and provenance.
- Assert actual model/provider and schema version are persisted from the host response.

**Verify**

```bash
scripts/run_tests.sh tests/plugins/test_client_knowledge_extraction.py \
  tests/plugins/test_client_knowledge_interpretation.py \
  tests/tools/test_read_extract.py \
  tests/agent/test_plugin_llm.py
```

### PR 5: GBrain Assimilation And Honcho Projection

**Implement**

- Add an internal publisher beside the existing read-only GBrain client. It may
  write only inside the configured Git source checkout under
  `projects/<project-key>/`.
- Retrieve relevant current pages through the existing bounded client.
- Invoke `client_knowledge_assimilate` for a structured operation proposal.
- Deterministically validate operation, page kind/status/confidence/sensitivity,
  slug, citations, supersession links, and final Markdown.
- Commit one assimilation transaction with artifact/interpretation version in
  commit metadata, then run pinned GBrain sync and verify the exact pages through
  the read-side validator.
- If the proposal is high-impact or disputed, store `needs_review` and expose an
  operator `approve`/`reject` command; do not write current truth automatically.
- Add a durable Discord review notifier that resolves the mapped top-level
  project channel and configured reviewer role, posts one idempotent review
  message with the Notion source citation and proposed operation, and records
  the Discord message ID.
- Extend plugin slash-command dispatch minimally so a handler may receive a
  trusted command context containing the authorized `MessageEvent`/source while
  preserving existing one-argument handlers. Use that context to enforce exact
  project channel, reviewer role/user authorization, and review identity before
  applying the durable decision.
- Add Honcho projection after successful verified assimilation. Store external
  conclusion IDs and exact content, support correction/retraction, and add a
  rebuild/reconcile command.
- Enable the existing read-only GBrain plugin/toolset only after the live PID
  source is configured and proof checks pass.

**Core tests**

- Add/confirm/refine/supersede/contradict/tentative/transient/review operations.
- Cross-project slugs/citations and arbitrary paths fail closed.
- Crash after Git commit recovers without a duplicate commit.
- Failed sync does not project to Honcho.
- Only promotable current claims reach Honcho; disputes, tentative claims,
  sensitive claims, source digests, actions, and deadlines do not.
- Reconciliation repairs missing/stale Honcho projections from current pages.
- Review notifications deduplicate after restart; unauthorized users, threads,
  other channels, other projects, and duplicate/stale decisions fail closed.
- Approve records reviewer provenance and resumes assimilation exactly once;
  reject records the reason and never writes/promotes the claim.
- PID review delivery tags only the `Devs` role through the scoped
  `allowed_role_mentions` metadata path.
- Existing GBrain read and coding-worker isolation tests remain green.

**Verify**

```bash
scripts/run_tests.sh tests/plugins/test_client_knowledge_gbrain.py \
  tests/plugins/test_client_knowledge_assimilation.py \
  tests/plugins/test_client_knowledge_honcho.py \
  tests/agent/test_opencode_worker.py \
  tests/tools/test_delegate.py

python scripts/prove_gbrain_client_knowledge.py \
  --lane all \
  --gbrain-ref aecb33e795cc4806f760446c55ab1c350194ddc8 \
  --network-isolation required \
  --network-audit required \
  --output /tmp/opencode/hermes-gbrain-proof-plan018
```

### PR 6: End-To-End PID Rollout

**Implement and operate**

1. Preflight and add the minimal machine properties to the existing PID
   `Client Feedback` data source. Verify existing rows/views/options are
   unchanged, and verify actual file-size limits and restore/export behavior.
2. Create the production Git-backed `client-knowledge` source, seed project
   root pages, prove Git bundle backup/restore, and run the existing GBrain proof.
3. Create a fresh operational ledger and capture the current Gmail
   `cutover_history_id`. Do not read the retired cursor into the new ledger, do
   not disposition old messages, and do not import historical Notion receipts.
   Prove with fixture and API metadata that all messages at or below the baseline
   are excluded.
4. Run fixture E2E cases through the full worker:
   new requirement, confirmation, refinement, contradiction, duplicate,
   unsupported attachment, Notion failure, model failure, GBrain failure,
   Honcho failure, and crash recovery at every external boundary.
5. Install the one-shot Gmail poll service and timer disabled. Run `status` and
   `run-once --dry-run` with no downstream writes.
6. Enable processing only after the fresh baseline receipt is durable.
7. Enable processing with only `pid@sligolabs.com` registered. Confirm the
   Cloudflare route preserves the alias before sending any fixture.
8. Send controlled emails covering plain text, attachment, duplicate delivery,
   contradiction, and an intentional downstream failure. Verify Notion source,
   GBrain citations/history, Honcho projection policy, and recovery.
9. Trigger one synthetic `needs_review` case. Verify Sligo Labs posts it in the
   top-level PID channel, tags `@Devs`, rejects approval from the source thread or
   an unauthorized user, and accepts one authorized `/client-knowledge approve`
   or `/client-knowledge reject` command with durable provenance.
10. Run a fresh top-level mapped PID conversation: confirm GBrain search/get is
    used and cited. Test every configured coding-worker backend for tool and
    environment scrubbing, then record the operator's acceptance that same-UID
    workers remain trusted and may not be filesystem-isolated.
11. Observe at least 24 hours of healthy two-minute polls and reconciliation
    before adding another project alias.

**Production proof receipt**

Write a redacted receipt under `$HERMES_HOME/client-knowledge/receipts/` with:

- source commit and config hash;
- Gmail token scope names only;
- mailbox and registered alias names;
- Notion data-source ID and schema hash;
- GBrain source ID, Git HEAD, proof receipt hashes, and restore result;
- artifact IDs/hashes and external IDs for controlled fixtures;
- stage timing, retry counts, and final health;
- worker-isolation test result;
- explicit same-UID trust-model acceptance or separate-UID isolation proof;
- immutable fresh cutover history baseline and proof that no historical source
  IDs or Notion receipts entered the new ledger;
- Discord review message/channel/role IDs and reviewer-decision provenance for
  the controlled review fixture;
- no source body, secret, or sensitive filename.

**Final verification**

```bash
scripts/run_tests.sh
scripts/run_tests.sh --full
python -m hermes_cli.pr_workflow_preflight --base origin/main --head HEAD
```

If `--full` is blocked by the repository's known `/tmp` resource failure, report
that exact blocker and retain focused + smoke + proof evidence; do not claim a
full-suite pass.

## Deferred Follow-Up: Discord Attachments

After Gmail PID is stable, add Discord as a small adapter PR:

1. Add `InboundAttachmentDescriptor` to `MessageEvent.metadata` or a typed event
   field with attachment ID, message/channel/thread IDs, original name, MIME,
   size, source URL, cached path, SHA-256, and mapping provenance.
2. Populate it in `plugins/platforms/discord/adapter.py` while bytes are already
   available. Never reconstruct identity from `media_urls`.
3. Add one gateway hook after authorization and final session/project resolution,
   immediately after the final `_set_session_env`/project binding and before
   prompt construction. The hook performs only durable enqueue and returns a
   compact receipt; parsing/model/sink work remains in the worker.
4. Fail open for the Discord turn but retain a retryable artifact. Do not use
   `pre_gateway_dispatch`, which intentionally runs before authorization.
5. Add bounded cleanup for temporary audio/document caches after durable archive.

This follow-up is deliberately not required to restore Gmail intake.

## Acceptance Criteria

- Gmail authorization has exactly `gmail.readonly`; no Gmail write operation is
  present in code, config, or runtime credentials.
- A message addressed to a registered alias is mapped deterministically from the
  preserved recipient header. Unknown/conflicting aliases never guess.
- The replacement processes no Gmail message whose `historyId` is at or below
  the immutable cutover baseline, including reconciliation and 404 recovery.
- Cursor advancement occurs only after candidate identities and raw MIME are
  durable. Restart, retry, dropped notification, and history expiry do not skip
  or duplicate work.
- Every admitted PID email has one recoverable row/page in the existing PID
  `Client Feedback` data source with raw `.eml`, original attachments,
  provenance, and processing receipt. Historical rows remain untouched.
- Notion contains source material only, not synthesized project truth.
- GBrain pages model current/tentative/disputed/superseded knowledge, cite Notion,
  stay inside the project prefix, and retain Git history.
- A new source can confirm, refine, supersede, or dispute a claim without erasing
  its source history.
- Honcho contains only compact promotable current claims and can be rebuilt from
  GBrain plus the projection ledger.
- Top-level agents retrieve and cite GBrain on mapped project-history/decision
  turns. Coding workers receive no client-knowledge tools or environment bridge;
  the production receipt truthfully states whether filesystem isolation exists
  or the same-UID workers are trusted.
- Logs and status output contain IDs, counts, hashes, stages, and error classes,
  not source bodies, secrets, or sensitive filenames.
- No Command Center, Obsidian, Gmail MCP, Pub/Sub, outbound email, or task-tracker
  dependency exists in the live V1 path.
- Every `needs_review` claim posts once to the exact top-level project channel,
  tags the configured reviewer role, and can be resolved only by an authorized
  deterministic command in that channel.

## Stop Conditions

Stop and report rather than improvise if:

- Cloudflare/Gmail does not preserve one authoritative delivered alias.
- The dedicated Gmail OAuth credential cannot be restricted to
  `gmail.readonly`.
- Notion cannot preserve original files within approved limits and no separate
  approved source archive exists.
- GBrain pin/source layout must change or retrieval cannot return validated cited
  PID pages reliably.
- The implementation would expose raw source content or credentials to coding
  workers through configured tools/environment, Honcho, logs, or an unapproved
  model/provider. If same-UID workers are not trusted, filesystem access itself
  is also a stop condition.
- Crash recovery cannot prove idempotency after each Notion, Git/GBrain, and
  Honcho boundary.
- A live replacement would overlap an active legacy Gmail worker.
- Any implementation path can process a message at or below the immutable
  cutover history baseline or import a historical Gmail/Notion receipt.
- Discord review delivery cannot safely mention only the configured reviewer
  role, or approval cannot prove reviewer role/user and exact project channel.
- Required restore tests for SQLite/raw spool and the GBrain Git source fail.

## Maintenance Notes

- Push notification support may be added later only as a wake-up hint feeding
  the same idempotent poller. It must never own the canonical cursor.
- PDF/OCR support is a separate dependency/security decision. Archival must not
  wait for extraction support.
- Project aliases and Discord mappings must converge on one lowercase project
  registry. Reviewers should reject adapters that invent independent routing.
- If GBrain is upgraded, rerun and update the pinned proof before changing the
  production source.
- If hostile same-UID workers become possible, isolate the client corpus under a
  separate UID/container before continuing rollout.
