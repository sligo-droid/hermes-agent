# Command Center Initial Load

Date: 2026-06-19

## Observed Bottlenecks

- Initial React load calls `/api/plugins/kanban/command-center/snapshot?include_archived=true&recent_run_limit_per_board=25&project=hermes`.
- Backend snapshot construction eagerly includes per-item `full_description`, `raw`, `source_excerpts`, and duplicate per-item `runs` even though the first screen needs only row summaries, actions, links, counts, and active status.
- A synthetic profile with 30 worker boards and 8 completed tasks per board produced a filtered Hermes snapshot with 15 work items, 120 run rows, and a 701,242 byte JSON payload. Of that, `full_description` accounted for 84,265 bytes and `raw` accounted for 92,330 bytes before gzip.

## Selected Fixes

- Add a summary snapshot mode that preserves row behavior while omitting heavy detail fields from initial payloads.
- Keep full Work Item detail available through a row detail endpoint.
- Make the Command Center frontend request summary snapshots on initial load.
- Keep recent run rows lazy: request `recent_run_limit_per_board=0` except on the Runs view, where the existing 25-row-per-board behavior remains.

## Rejected Or Riskier Fixes

- Broadly changing board discovery or reconciliation was rejected because Command Center reads intentionally reconcile proposal state with Kanban source-of-truth data.
- Parallelizing board SQLite reads was deferred; it risks increasing file descriptor pressure and lock contention without first reducing avoidable payload work.
- Removing archived data from initial loads was rejected because archive lane counts and project tabs currently come from the same snapshot contract.

## Validation Plan

- Backend tests assert the cache key distinguishes detail level and summary snapshots omit heavy fields while keeping `has_full_description`.
- Plugin route tests assert `include_details` passes through and the detail endpoint returns full Work Item data.
- Frontend static tests assert summary initial fetches, lazy detail fetches, and lazy recent-run loading.
- Focused checks: `scripts/run_tests.sh` for Command Center/plugin/web static tests, `npm run build` in `web`, and local payload measurement before and after.
