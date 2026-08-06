# Implementation Plans

Updated on 2026-08-05 from repository, runtime, Gmail, Notion, GBrain, Honcho, and historical-intake evidence. Plan 018 is now an implementation-ready sequence focused on restoring PID Gmail intake first. A Sol xhigh review returned `REVISE`; all material findings were incorporated. Operator decisions now require present-forward intake only, reuse PID's existing Notion `Client Feedback` data source, resolve model calls through the named `advanced` tier, and route high-impact approvals through the top-level PID Discord channel with `@Devs`. A future executor must read the full plan, honor its STOP conditions, and update this row as work progresses.

## Execution Order And Status

| Plan | Title | Priority | Effort | Depends on | Status | Verification |
| --- | --- | --- | --- | --- | --- | --- |
| 018 | Ship Gmail client-knowledge intake | P1 | L | #834, #836, #852 | IN PROGRESS (PR 1 durable core) | PID email -> Notion source -> GBrain synthesis -> Honcho projection; Discord deferred |

Status values: `PLANNING`, `TODO`, `IN PROGRESS`, `DONE`, `BLOCKED (<reason>)`, or `REJECTED (<reason>)`.

## Dependency Notes

- Plan 018 supersedes the earlier email-only/Command Center design. The GBrain proof and Obsidian retirement prerequisites are complete. Execute its ordered PRs; restore PID Gmail first and add Discord attachment ingestion only after Gmail is stable.
- The external Cloudflare route setup occurs only after the local alias registry and dry-run verification exist. This prevents live mail from arriving before Hermes can store it safely.

## Findings Considered And Rejected

- **Move `sligolabs.com` mail hosting to Proton before building intake:** rejected for the first release. The domain already uses Cloudflare Email Routing, while Proton custom-domain hosting would replace the domain MX records and Proton Bridge would add a local authenticated IMAP process. Neither change improves project mapping. Proton remains a valid future archive destination after the intake contract is provider-neutral.
- **Create one Proton account or mailbox per project:** rejected. It adds account and credential sprawl when the recipient alias already provides an exact project boundary.
- **Keep using the current CC/BCC/forward-only Gmail gate:** rejected for project aliases. A message addressed directly to a registered `project@sligolabs.com` alias is already explicit project intake and should not need forwarding heuristics.
- **Keep the retired Markdown-vault integration as a canonical or fallback store:** rejected. It was routinely skipped by agents and is being removed from Hermes entirely; GBrain's Git-backed brain repository is the selected synthesis store because retrieval and brain-first orchestration are explicit parts of the contract.
- **Write every incoming email directly into Honcho memory:** rejected. Honcho is the active always-loaded memory provider, but project mail is too large, mutable, and provenance-sensitive for always-on memory. Only separately curated stable cross-session facts should reach Honcho.
- **Require Gmail Pub/Sub before launch:** rejected. Push notifications are an optional latency optimization with expiry and delivery gaps. V1 uses durable history polling plus bounded reconciliation and may add Pub/Sub later as a wake-up hint.
- **Ship Gmail and Discord intake in one release:** rejected. The artifact contract remains provider-neutral, but Gmail is the broken production workflow. Finish and observe it before adding a gateway hook and Discord descriptor surface.
- **Create a separate PID source database:** rejected. PID's existing `Client Feedback` data source is accessible and already represents client feedback. Add only the machine identity/provenance fields needed for durable email archival and preserve existing rows/views/options.
- **Reconcile or import historical Gmail/Notion intake:** rejected by operator decision. Capture a fresh immutable Gmail history baseline and process only message additions strictly after it.
