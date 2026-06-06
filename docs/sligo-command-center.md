# Sligo Command Center

The Sligo Command Center is the canonical operator surface for Sligo Labs work. It replaces the old split mental model of a self-improvement board plus a workers board.

## Operating Model

The Command Center uses one read-only ledger model:

1. **Sources** create or attach work: self-improvement proposal runs, Discord worker threads, cron outputs, and manual Kanban entries.
2. **Work Items** own the canonical operator status, decision state, provenance, execution links, and artifacts.
3. **Worker Runs** are execution detail: Kanban task attempts, worker units, logs, summaries, errors, and active runtime state.

This keeps self-improvement and Discord as intake/provenance, not competing boards. Worker boards remain the execution monitor and ticket detail surface.

Project scoping controls select this same ledger by project. The controls are populated from project metadata in the snapshot, not hard-coded per view. Current projects are Hermes (`#dev`) and PID. Filtering by project keeps Sources as intake, Work Items as the status ledger, and worker boards/Kanban task runs as child execution artifacts.

This code change only adds project/read-model support. Live Hermes recommendation cron jobs still need their `self_improvement_proposal.project` and `self_improvement_proposal.prong` fields updated by the operator/parent agent; the Command Center code does not mutate live cron jobs.

## Routes

Canonical Sligo-host routes:

- `/sligo` — overview
- `/sligo/inbox` — proposals, parse failures, and blocked items needing operator attention
- `/sligo/work` — accepted/queued/running/review/blocked work items
- `/sligo/completed` — completed/shipped work items
- `/sligo/archive` — archived/historical work items and board rollups
- `/workers` and `/workers/<board>` — existing worker board execution pages

Compatibility/diagnostic routes may still render through the same page, but they should not become a second primary navigation model:

- `/sligo/runs` — worker execution attempts across boards
- `/sligo/recommendations` — self-improvement recommendations as a source-filtered work view
- `/sligo/sources` — source audit/provenance view

Compatibility aliases:

- `/command-center` renders the Command Center overview.
- `/self-improvement` renders the recommendations view.
- Hermes-host requests for Sligo-specific routes redirect to `sligo.sligolabs.com` so `hermes.sligolabs.com` can stay focused on the classic native dashboard.

## Backend Read Model

`hermes_cli/command_center.py` builds the snapshot returned by:

```text
GET /api/plugins/kanban/command-center/snapshot
```

The read model aggregates existing durable stores without replacing them:

- `self_improvement/proposal_storage.py` remains the source of truth for proposal runs, cards, audit events, approvals, rejections, and feedback context.
- `hermes_cli/kanban_db.py` remains the source of truth for boards, tasks, task runs, and worker state.
- Discord worker board metadata remains attached to board metadata under the existing Discord worker metadata key.

## Data Contract

Snapshot shape:

```text
{
  schema_version,
  generated_at,
  summary,
  projects[],
  current_project,
  work_items[],
  sources[],
  runs[],
  boards[],
  metrics
}
```

Important invariants:

1. Every `work_items[*].source` has a stable `id`, `kind`, `label`, and `ref`.
2. Self-improvement rejected/archived cards are hidden by default and included only with `include_archived=true`.
3. Active worker runs are always included in `runs`, even when they are older than the recent-run limit.
4. Approval artifacts such as Discord thread URLs and worker-board URLs survive later halt/undo audit events.
5. Stored legacy `proposal_cards.worker_url` values remain visible as execution/artifact fallbacks.

## Frontend

`web/src/pages/CommandCenterPage.tsx` renders all Command Center views. The old page modules are compatibility wrappers that export this page so stale imports cannot resurrect the previous UI.

The Sligo shell in `web/src/App.tsx` provides the focused internal navigation for `sligo.sligolabs.com`. The combined/local dashboard can still surface Command Center links, but Hermes-host Sligo routes redirect to the Sligo host.

## UX Contract

Current UX intention is tracked in `docs/project-state.md`; update that file when the target changes. The durable contract here is:

1. **Work State is the primary work navigation.** Overview, Inbox, Active/Work, Completed, and Archive are states of the same Work Item ledger. Do not reintroduce separate primary tabs for recommendations, sources, runs, metrics, or status distributions.
2. **Project scoping is not a competing status model.** Hermes/PID project controls may filter the same ledger, but they should not create duplicate screens with different semantics.
3. **Rows are Work Items or board-level rollups.** Proposed recommendations, intake/decision items, and named worker boards can render as rows. Individual Kanban tasks/tickets and accepted downstream proposal cards should stay inside board/detail surfaces unless they are promoted to canonical Work Items.
4. **Worker links are execution artifacts.** Show a Worker link only after execution starts; point it directly to the board URL; never use bare `/workers` as a per-item destination.
5. **Archive is historical, not a live-board filter.** Archived board rows can be listed from `boards/_archived/`, but they should not expose live-board actions or look like active execution.
6. **UI chrome stays quiet.** Use one shell header/refresh area, one Work State lane, a left work list, and a right detail/audit pane. Avoid duplicate refresh rows, KPI cards, status bars, and stale `Operator Surface` copy.

## Feature Requests Without Worker Boards

Feature requests should enter the Command Center as Work Items even before a dedicated Kanban worker board exists. The request row should use a stable `source_ref` that points at the intake object, such as a Discord message, form submission, or manual operator record, and should remain separate from child worker tickets.

If the request later receives a worker board, attach the board slug to the Work Item execution metadata and let the board become the active execution rollup. The Work Item remains the canonical request identity; the board contributes task counts, run history, artifacts, and worker-board links.

Status ownership belongs to the Work Item until a worker board is attached. After attachment, board/task rollup can drive execution statuses such as queued, running, review, blocked, and shipped, while operator decisions such as accepted, rejected, parked, or archived stay on the Work Item.

Artifacts should be collected on the Work Item regardless of whether they originate from intake or execution. Examples include source links, Discord thread URLs, worker-board URLs, run summaries, pull requests, deployment notes, and follow-up decisions.

The UI should group individual feature request cards apart from worker-board cards. Request cards should be labeled by source kind and request title, while worker-board cards should use `kanban-board:<slug>` identities and board/source badges. This prevents standalone requests from looking like individual worker tickets and keeps ticket-level detail inside the board rollup or detail panel.

## Verification

Focused verification for this surface should cover:

1. Proposal ingestion, flat card listing, hidden rejected/archived defaults, and feedback summaries.
2. Command Center snapshot contract, including source IDs, active-run inclusion, ISO timestamp ordering, artifact fallback, and include-archived behavior.
3. Dashboard plugin endpoint shape for `/command-center/snapshot`.
4. Web build/type-check for routing, API types, and Command Center page integration.
5. Host split behavior: Sligo routes on `hermes.sligolabs.com` redirect to `sligo.sligolabs.com`, while `sligo.sligolabs.com` owns `/sligo/*` and `/workers/*`.
