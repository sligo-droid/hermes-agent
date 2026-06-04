# Sligo Command Center

The Sligo Command Center is the canonical operator surface for Sligo Labs work. It replaces the old split mental model of a self-improvement board plus a workers board.

## Operating Model

The Command Center uses one read-only ledger model:

1. **Sources** create or attach work: self-improvement proposal runs, Discord worker threads, cron outputs, and manual Kanban entries.
2. **Work Items** own the canonical operator status, decision state, provenance, execution links, and artifacts.
3. **Worker Runs** are execution detail: Kanban task attempts, worker units, logs, summaries, errors, and active runtime state.

This keeps self-improvement and Discord as intake/provenance, not competing boards. Worker boards remain the execution monitor and ticket detail surface.

## Routes

Canonical Sligo-host routes:

- `/sligo` — overview
- `/sligo/inbox` — proposals, parse failures, and blocked items needing operator attention
- `/sligo/work` — accepted/queued/running/review/blocked work items
- `/sligo/runs` — worker execution attempts across boards
- `/sligo/recommendations` — self-improvement recommendations as a source-filtered work view
- `/sligo/sources` — source audit/provenance view
- `/workers` and `/workers/<board>` — existing worker board execution pages

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

## Verification

Focused verification for this surface should cover:

1. Proposal ingestion, flat card listing, hidden rejected/archived defaults, and feedback summaries.
2. Command Center snapshot contract, including source IDs, active-run inclusion, ISO timestamp ordering, artifact fallback, and include-archived behavior.
3. Dashboard plugin endpoint shape for `/command-center/snapshot`.
4. Web build/type-check for routing, API types, and Command Center page integration.
5. Host split behavior: Sligo routes on `hermes.sligolabs.com` redirect to `sligo.sligolabs.com`, while `sligo.sligolabs.com` owns `/sligo/*` and `/workers/*`.
