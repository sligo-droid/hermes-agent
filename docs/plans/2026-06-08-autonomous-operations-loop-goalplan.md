# Autonomous Operations Loop Goalplan

## Purpose

Define the complete target state for the next-level autonomous Hermes operations loop. The loop turns evidence into governed action without creating another ledger, another unattended cron pile, or an agent that can freely mutate itself.

The operating loop is:

```text
Observe -> Diagnose -> Decide -> Execute -> Verify -> Learn -> Repeat
```

Command Center remains the canonical control plane and ledger. Sources create evidence and candidate work. Work Items own operator status and decisions. Worker Runs record execution. Accepted findings and actions land as Command Center Work Items, not as side ledgers.

## End State

Hermes is complete for this goal when it can continuously watch its owned operational surface, identify actionable conditions, route them through policy, prepare bounded work, execute safe actions when allowed, verify outcomes with deterministic evidence, and feed lessons back into future policy or implementation work.

At completeness:

- Always-on observers collect runtime, repo, Command Center, cron, worker, Discord, and deployment signals into source-scoped findings.
- A triage/planner layer dedupes findings, classifies severity and actionability, and either attaches them to existing Work Items or creates new candidate Work Items.
- Command Center is the single operator ledger for intake, decisions, execution, verification, exceptions, and history.
- The Work Item API is the central primitive used by observers, planners, foremen, workers, approvals, verification, and retrospectives.
- A constrained autonomous foreman can plan, prepare, and execute within explicit autonomy levels and policy scopes.
- Runtime actions require deterministic preflight and deterministic post-verification. LLM reasoning can recommend actions, but cannot be the only restart, deploy, permission, or security decision.
- Humans stay minimally in the loop at policy gates, exception queues, public-posting gates, credential/security gates, and irreversible or high-blast-radius execution gates.
- Learning produces governed system changes: policy edits, observer adjustments, tests, runbooks, or product backlog Work Items. Learning does not directly self-modify runtime code.

## Non-Goals

- Build a free-roaming self-improvement agent.
- Add more crons that bypass the Command Center ledger.
- Create a second recommendations, incidents, worker, or runtime-action ledger.
- Let LLM-only judgments restart services, change credentials, change permissions, publish externally, or spawn unbounded workers.
- Replace human ownership of policy, trust boundaries, credential handling, or public communications.
- Auto-fix every issue. The loop should reliably decide when to stop, ask, park, or escalate.
- Redesign Kanban or worker boards into the primary work-status surface. They remain execution children attached to Work Items.

## System Invariants

- Command Center is canonical: `Sources -> Work Items -> Worker Runs` is the only operator-status model.
- Every observation, decision, action, verification result, approval, exception, and lesson is attached to a Source, Work Item, Worker Run, or policy artifact.
- No accepted finding becomes executable work without a Work Item identity.
- No duplicate ledgers: cron output, self-improvement proposals, Discord threads, worker boards, and observer findings are sources or artifacts.
- Worker boards are execution children. They do not own product/work status independently of Work Items.
- Every automated action declares an autonomy level, policy decision, actor, scope, preflight result, and rollback/stop condition.
- Runtime-changing actions require deterministic preflight and deterministic verification before they can close as successful.
- Learning changes are proposed as ordinary Work Items unless they are read-only notes attached to the originating item.

## System Model

The loop has five durable layers:

| Layer | Responsibility | Canonical output |
| --- | --- | --- |
| Observers | Collect evidence from runtime, repo, scheduled jobs, Discord, workers, dashboards, and deployment surfaces. | `ObserverFinding` source artifacts |
| Triage/planner | Normalize, dedupe, classify, score, and map findings to existing or candidate Work Items. | Work Item create/update proposals |
| Policy registry | Decide allowed autonomy level, required approvals, safety gates, and execution ceilings. | `PolicyDecision` artifacts |
| Constrained foreman | Prepare plans, dispatch workers, manage retries, enforce concurrency and stop rules. | Worker Runs and execution artifacts |
| Verification/learning | Prove outcomes, close or escalate work, and convert lessons into policy/system/backlog changes. | `VerificationResult` and learned Work Items |

The Command Center API is the integration surface between these layers. Observers and planners do not talk directly to worker boards except to read execution evidence or attach execution artifacts.

## Autonomy Levels

Autonomy is a per-policy, per-scope decision. Higher levels inherit lower-level capabilities but must still satisfy the policy gates for the specific Work Item.

| Level | Name | Allowed behavior | Human role |
| --- | --- | --- | --- |
| 0 | Observe only | Collect evidence, attach source artifacts, update freshness/heartbeat fields. | None unless observer health fails. |
| 1 | Propose | Create candidate Work Items, dedupe, classify, suggest actions, request approval. | Approve, reject, archive, or ask for more evidence. |
| 2 | Prepare fix | Create a plan, branch/worktree, draft patch, dry-run command, or rollback plan without applying runtime side effects. | Approve execution or request changes. |
| 3 | Safe auto-fix | Execute low-risk, reversible, pre-approved fixes with deterministic preflight and bounded concurrency. | Review exceptions and sampled completions. |
| 4 | Governed execution | Execute broader repo/runtime actions after explicit policy approval for the class and scope. | Approve policy scope and handle exception gates. |
| 5 | Runtime operator | Perform tightly bounded operational actions such as restart/replay/requeue only when deterministic preflight, rate limits, and rollback paths pass. | Own high-blast-radius gates and post-incident review. |

## Policy And Safety Boundaries

Hard boundaries:

- No free-roaming self-modification. System changes must be represented as Work Items and go through normal repo/worktree/review flow.
- No cron bypass. Scheduled jobs can observe, propose, or enqueue, but accepted work must attach to Command Center Work Items.
- No automatic credential, secret, security, permission, auth, or trust-boundary changes.
- No public posting, publishing, client-facing messages, release announcements, or external callbacks without explicit approval.
- No LLM-only restarts, deploys, rollbacks, database repairs, queue drains, or service lifecycle actions.
- No auto-spawn storms. Foreman and worker creation are bounded by per-project, per-policy, per-source, and global concurrency limits.
- No runtime action without deterministic preflight. Preflight must inspect real state and produce a machine-checkable allow/deny result.
- No success claim without deterministic verification. The verifier must cite logs, state, command output, tests, CI, live probes, or API responses.

Policy decisions must be explainable in Command Center and include the matched policy rule, allowed autonomy level, required approvals, execution ceiling, retry budget, and stop conditions.

## Core Components

- **Always-on observers:** bounded readers that emit normalized findings from logs, service state, cron outputs, worker boards, Command Center snapshots, repo/PR state, CI, deploys, and Discord delivery state.
- **Triage/planner layer:** converts findings into candidate Work Items or updates existing Work Items; performs dedupe, severity classification, and next-action planning.
- **Command Center Work Item API:** the central primitive for creating, updating, approving, executing, verifying, archiving, and auditing autonomous work.
- **Constrained autonomous foreman:** schedules and supervises prepared work, dispatches workers, enforces concurrency/lease/retry budgets, and escalates exceptions.
- **Retrospective-to-system-change loop:** converts repeated failures, noisy observers, bad plans, or successful patterns into policy, docs, tests, observer, or implementation Work Items.
- **Evidence broker:** stores and resolves evidence artifacts so every decision and verification can be traced without copying long logs into UI rows.
- **Policy registry:** defines autonomy levels, safety gates, approval requirements, source trust, action classes, concurrency ceilings, and exception handling rules.

## Data Contracts

These schemas are conceptual contracts for implementation. Field names should be adapted to the existing Command Center snapshot/API conventions when code is inspected.

### Work Item

```text
WorkItem {
  id,
  project,
  title,
  status,              // inbox | active | blocked | review | completed | archived | exception
  decision_state,      // proposed | accepted | rejected | parked | approved_for_execution
  source_refs[],
  finding_refs[],
  policy_decision_refs[],
  worker_run_refs[],
  verification_refs[],
  autonomy_level,
  risk_class,
  owner,               // human | foreman | worker profile | policy actor
  created_at,
  updated_at,
  artifacts[],
  audit_events[]
}
```

### Worker Run

```text
WorkerRun {
  id,
  work_item_id,
  board_slug,
  worker_profile,
  action_class,
  status,              // planned | queued | running | paused | failed | verified | exception
  autonomy_level,
  policy_decision_ref,
  preflight_ref,
  command_or_task_ref,
  output_artifact_refs[],
  retry_count,
  lease,
  started_at,
  ended_at
}
```

### Policy Decision

```text
PolicyDecision {
  id,
  work_item_id,
  policy_id,
  matched_rule,
  source_trust,
  requested_action_class,
  allowed_autonomy_level,
  required_approvals[],
  required_preflight_checks[],
  required_verification_checks[],
  concurrency_scope,
  retry_budget,
  stop_conditions[],
  decision,            // allow | deny | require_approval | exception
  decided_at
}
```

### Observer Finding

```text
ObserverFinding {
  id,
  source_id,
  observer_id,
  project,
  title,
  evidence_refs[],
  fingerprint,
  severity,
  confidence,
  freshness,
  proposed_action_class,
  related_work_item_ids[],
  observed_at
}
```

### Verification Result

```text
VerificationResult {
  id,
  work_item_id,
  worker_run_id,
  verifier_id,
  check_refs[],
  status,              // passed | failed | inconclusive | skipped_with_reason
  evidence_refs[],
  residual_risk,
  next_action,          // close | retry | escalate | learn | park
  verified_at
}
```

## Lifecycle State Machine

Primary lifecycle:

```text
source event
  -> observer finding
  -> candidate Work Item
  -> triaged Work Item
  -> policy decision
  -> planned Worker Run
  -> prepared change or action
  -> approval gate when required
  -> execution
  -> verification
  -> completed | exception | parked | learned follow-up
```

Important transitions:

- A finding with a matching active Work Item attaches to that Work Item instead of creating a duplicate.
- A candidate Work Item enters `Inbox` until triage accepts, rejects, parks, archives, or requests evidence.
- Accepted work enters `Active` only after policy decides what autonomy level and approvals apply.
- Prepared fixes stay non-runtime-impacting until execution approval or safe-auto policy is satisfied.
- Failed preflight sends the item to `Exceptions` or `Active/blocked` with the failed check attached.
- Failed verification never closes the item as successful; it triggers retry, rollback, escalation, or a learned follow-up.
- Completed work can create learned Work Items, but those are separate candidate items linked back to the original evidence.

## Observer Contracts

Observers are bounded readers. They may collect, normalize, and attach evidence, but they may not mutate runtime state, spawn workers, or publish messages unless a separate policy-approved action executes through the Work Item flow.

Each observer must declare:

- Source kind and trust level.
- Polling or subscription cadence.
- Evidence retention and artifact references.
- Finding fingerprint strategy.
- Freshness and heartbeat expectations.
- Maximum findings per interval.
- What counts as stale, noisy, duplicated, or exception-worthy.

Observer examples:

- Gateway/runtime observer: service status, recent fatal logs, restart history, adapter health.
- Worker observer: blocked boards, stale leases, failed role workers, missing summaries, finalizer drift.
- Cron observer: missed jobs, repeated failures, proposal output quality, skipped catchup windows.
- Command Center observer: snapshot freshness, Work Item/action inconsistencies, orphaned runs.
- Repo/PR observer: open PR age, CI failures, merge drift, dirty canonical checkout warnings.
- Discord observer: source-thread delivery failures, stale reactions, approval/embed drift.

## Triage And Dedupe Rules

Triage must prefer updating existing Work Items over creating new rows.

Rules:

- Fingerprint findings by source kind, project, durable target, failure class, and normalized evidence signature.
- If an active Work Item already owns the fingerprint, append evidence and update severity/freshness.
- If a completed Work Item receives the same fingerprint after a cooling window, create a recurrence candidate linked to the completed item.
- If multiple observers report the same condition, merge findings under one Work Item and preserve each observer's evidence.
- Low-confidence findings can stay as source artifacts until corroborated or promoted by a policy rule.
- Triage output must identify one recommended next action: observe more, ask human, prepare fix, execute safe action, park, or archive.
- Dedupe must never hide an exception gate. If policy says a human must decide, the Work Item remains visible in `Inbox` or `Exceptions`.

## Execution Rules

Execution is allowed only through policy-scoped Work Items and Worker Runs.

Rules:

- Every execution run has a Work Item, policy decision, autonomy level, actor, scope, preflight result, retry budget, and stop condition.
- Repo changes happen in git worktrees/branches and follow the normal code-review lifecycle. Autonomous preparation may draft patches, but merge/deploy authority follows policy.
- Runtime actions use an action registry with deterministic preflight functions, not free-form shell commands selected only by an LLM.
- Worker spawning is rate-limited by global, project, source, action-class, and Work Item ceilings.
- A foreman may pause, retry, or escalate within policy, but may not silently broaden scope.
- Credential/security/publication actions always require explicit human approval and cannot be auto-executed at any autonomy level.
- If execution observes unexpected state outside the approved scope, it stops and creates or updates an exception.

## Verification Rules

Verification proves the outcome of an action against the Work Item's acceptance criteria and policy requirements.

Rules:

- Verification is separate from execution. The executor may report results, but a verifier or deterministic check owns closure evidence.
- Runtime actions need post-action probes such as service status, health endpoints, logs, queue depth, board state, or delivery confirmation.
- Repo changes need focused local checks and, when required by policy, CI/PR/deploy evidence.
- If verification is inconclusive, the Work Item remains active or moves to `Exceptions`; it does not close as successful.
- Rollback or retry verification must attach both the failed evidence and the remediation evidence.
- Verification output should be compact in Command Center and link to artifacts through the evidence broker.

## Human Approval Surfaces

Humans interact through three queues over the same Work Item ledger:

- **Inbox:** new candidate Work Items, proposals, low-confidence findings, policy decisions needing approval, and items needing more context.
- **Active:** accepted work, prepared fixes, running Worker Runs, review-ready work, and safe auto-fixes sampled for operator awareness.
- **Exceptions:** failed preflight, failed verification, policy denial, repeated retries, suspected duplicate storms, credential/security/publication gates, and unexpected scope expansion.

Human actions:

- Approve, reject, park, archive, or request evidence on candidate Work Items.
- Approve execution for policy-gated prepared fixes.
- Pause, cancel, retry, revert, or escalate active execution when those actions are real for the item state.
- Promote a learned recommendation into a new Work Item.
- Edit policy registry entries through normal reviewed configuration or code changes, not ad hoc runtime mutation.

## Learning Loop

Learning is the controlled path from operational outcomes to system improvement.

Learning inputs:

- Repeated observer findings.
- Failed or noisy triage decisions.
- Failed preflight or verification checks.
- Human approvals, rejections, and exception resolutions.
- Successful safe auto-fixes and their verification evidence.
- Retrospectives from completed incidents or work batches.

Learning outputs:

- Policy adjustments proposed as Work Items.
- New or refined observer contracts.
- New deterministic preflight or verification checks.
- Focused tests or runbooks.
- Product backlog items for Command Center, worker, cron, or gateway improvements.

Learning may automatically attach notes and metrics to the originating Work Item. It may not directly rewrite code, policies, credentials, schedules, or approvals without creating governed work.

## Rollout Phases

### Phase 1: Unify Intake

- Normalize observer, cron, Discord, self-improvement, and manual findings into Command Center Sources and candidate Work Items.
- Add dedupe fingerprints and source artifact links.
- Acceptance: repeated source findings update one Work Item; no new ledger appears.

### Phase 2: Policy-Gated Autonomy

- Add policy registry entries for source trust, action classes, autonomy levels, approval requirements, and safety gates.
- Surface policy decisions on Work Items.
- Acceptance: every proposed execution shows why it is allowed, denied, or waiting for approval.

### Phase 3: Foreman Loop

- Add constrained foreman behavior for planning, preparation, worker dispatch, lease/retry management, and exception escalation.
- Enforce concurrency ceilings and stop conditions.
- Acceptance: safe prepared work can move through planning and bounded execution without duplicate worker storms.

### Phase 4: Verification Broker

- Add evidence broker and deterministic verification results for repo, runtime, worker, cron, and Discord action classes.
- Separate executor reports from verifier closure.
- Acceptance: Work Items cannot close successful without required verification evidence.

### Phase 5: Learning Loop

- Convert retrospectives and repeated patterns into governed policy/system/backlog Work Items.
- Add metrics for noise, recurrence, auto-fix success, exception rates, and human approval burden.
- Acceptance: the loop improves through reviewed Work Items rather than direct self-modification.

## Acceptance Criteria

The goalplan is complete when implementation can satisfy these end-to-end criteria:

- A runtime failure observed from logs creates or updates exactly one Command Center Work Item with source evidence.
- A duplicate finding from another observer attaches to the existing Work Item instead of creating a duplicate row.
- A policy decision records the allowed autonomy level and required approvals before any execution run starts.
- A safe low-risk action can be prepared, preflighted, executed, verified, and closed without human approval when policy allows it.
- A credential/security/publication action always lands in an approval or exception queue and cannot auto-execute.
- A runtime restart or replay cannot proceed from LLM text alone; deterministic preflight must pass first.
- Worker spawning respects global and scoped concurrency limits and cannot create an auto-spawn storm.
- Failed preflight or verification moves the item to `Exceptions` or blocked active state with evidence.
- Completed work and learned follow-ups remain linked through artifacts and Work Item references.
- Command Center shows the operator one ledger across Inbox, Active, Exceptions, Completed, and Archive without resurrecting separate self-improvement or worker-board status models.

## Verification Strategy

Implementation should be verified with layered tests and live-safe smoke checks:

- Unit tests for observer fingerprinting, triage dedupe, policy decisions, autonomy-level ceilings, and action gating.
- Contract tests for Command Center Work Item, Source, Worker Run, policy decision, and verification-result snapshot/API shapes.
- Integration tests for finding-to-Work-Item lifecycle, prepared-fix approval gates, safe auto-fix execution, failed preflight, and failed verification.
- Concurrency tests for foreman spawn ceilings, retry budgets, and stop conditions.
- Regression tests proving credential/security/publication classes require human approval.
- Runtime-safe smoke tests using read-only observers and no-op/preflight-only action classes before enabling auto-fix policies.
- UI smoke for Inbox, Active, Exceptions, Work Item detail/audit, evidence links, policy decisions, worker links, and verification results.

Docs-only planning changes do not require runtime tests. Runtime implementation must add focused tests at the layer being changed.

## Open Questions

- Which action classes should be eligible for Level 3 safe auto-fix first?
- What is the minimum Work Item write API needed before observers can stop emitting source-only artifacts?
- Should `Exceptions` be a separate primary Work State lane or a filtered subset of blocked/active rows with stronger visual treatment?
- What evidence retention policy keeps artifacts traceable without bloating Command Center snapshots?
- Which policy registry entries are configuration-only, and which require reviewed code changes?
