# Sligo Operator V1

Sligo Operator V1 is a Hermes-native dashboard surface for reviewing self-improvement proposals and routing accepted work into Hermes Workers. It is intentionally not a separate Sligo app or repository.

## Architecture

The v1 flow is:

1. Cron prongs emit the `self_improvement.proposal_run.v1` proposal contract.
2. Hermes ingests proposal runs and cards into profile-scoped storage.
3. The dashboard Self-Improvement Board groups cards by project and prong for operator review.
4. Approval creates or reuses a Kanban task with idempotency key `self-improvement:<proposal_id>` and links the card to `/workers`.
5. Rejection archives the card from the default board view and persists feedback.
6. Future prong prompts receive bounded accepted/rejected feedback summaries.

The Sligo shell in `web/src/pages/SligoOperatorPage.tsx` is a narrow operator landing page. It links to `/self-improvement` for upstream proposal review and `/workers` for downstream execution without replacing the real Hermes worker board or the dashboard `/chat` PTY.

## Storage

Proposal data is stored under the active Hermes profile:

```text
$HERMES_HOME/self_improvement/proposals.db
```

Core tables are managed by `self_improvement/proposal_storage.py`:

1. `proposal_runs` records cron outputs and parse status.
2. `proposal_cards` records discrete proposal cards and Kanban linkage.
3. `proposal_feedback` records approval, rejection, and future-prong feedback context.

## Config Keys

Self-improvement defaults live in `DEFAULT_CONFIG` under `self_improvement`:

```yaml
self_improvement:
  proposal_contract_version: self_improvement.proposal_run.v1
  default_max_cards_per_run: 5
  feedback_context:
    enabled: true
    max_items_per_kind: 3
    max_text_chars: 180
  projects:
    pid:
      label: PID
      description: Sligo PID operator self-improvement proposals.
```

The first configured project is PID, with prongs for the Airflow scraper doctor, admin dogfood UX bugfixes, invisible technical recommendations, and visible UI/UX recommendations.

Worker board public links continue to use the existing Kanban Discord worker config:

```yaml
kanban:
  discord_worker:
    public_base_url: ""
```

Dashboard URL hosting remains controlled by existing dashboard keys such as `dashboard.public_url`. Clean public routing such as `sligo.sligolabs.com` is a deployment/proxy concern and is not changed by v1.

## Operational Limitations

1. The Sligo shell is a dashboard narrowing layer, not a tenancy or auth boundary by itself.
2. The Workers board remains the source of truth for execution state after approval.
3. Rejected cards are hidden from the default board view but retained for audit and feedback context.
4. Public DNS, deployment routing, and reverse-proxy configuration are out of scope for this repo change.
5. The board depends on cron prongs emitting the structured proposal contract; malformed outputs appear as parse failures for operator inspection.

## Verification

Focused verification for this slice should cover:

1. Contract and storage tests for proposal ingestion and feedback summaries.
2. Dashboard plugin API tests for read-only board data, authenticated approve/reject, and worker links.
3. Web build/type-check to validate the Sligo shell and Self-Improvement Board routes.
