# ADR 0002: Bound visual QA and make trusted closeout durable

Status: accepted
Date: 2026-07-18

## Context

Rendered UI checks and pull-request closeout both benefit from fast follow-up, but foreground agents are the wrong long-lived owner. Unbounded visual inspection can consume tool/model budgets or retain sensitive browser material. Foreground CI polling can pin a chat turn, race a changing PR head, replay model work after restart, or merge using evidence collected for an older commit.

Hermes therefore needs acceleration without weakening evidence: visual checks must be small and deterministic, while PR/merge/post-merge reconciliation must survive process boundaries and remain bound to the exact commit it observed.

## Decision

Adopt two related, bounded mechanisms.

### Visual QA

- Classify only explicit rendered UI or artifact implementation requests. Routing metadata and changed paths may support a classification but cannot create a visual requirement by themselves.
- Intake classification owns only that applicability gate. During the existing action implementation turn, the action orchestrator uses the accepted request/thread and its code understanding to formulate the transient semantic execution contract: the smallest relevant target/region, intended or already-prepared page state, viewport/state assumptions, and concrete assertion intent. This does not add a classifier/model call or cycle stage.
- Run visual QA through the dedicated declarative assertion surface against the task's existing browser session. The surface does not accept JavaScript, CDP or shell commands, URLs, screenshots, cookies, headers, or credentials.
- The host binds the orchestrator-supplied contract to an opaque trusted requirement, assigns opaque assertion identifiers, requires at least one bounded screenshot-appearance judgement for new contracts, and accepts only the executor-produced receipt. The orchestrator cannot self-declare success.
- Enforce hard runtime ceilings: at most six assertions, two attempts, one vision call, one receipt, one follow-up turn, 30 seconds per attempt, 60 seconds total, and 6,000 output characters.
- Persist only a sanitized, prose-free receipt containing opaque requirement/contract/assertion identifiers, status, bounded counters, order, and allowlisted diagnostic codes.
- An enforced gate passes only on the latest fresh `passed` receipt matching the trusted requirement. Missing, failed, blocked, uncertain, malformed, stale, or timed-out evidence never satisfies the gate.

### Trusted closeout

- Persist closeout state on the exact gateway work item. In `enforce` mode that state is authoritative for lifecycle completion; the model or legacy finalizer must not independently claim completion.
- Reconcile with a storage-neutral, synchronous one-shot engine. One pass has a bounded command budget, never sleeps, never calls a model, and returns a normalized state plus `next_due_at`.
- Let the gateway watcher own durable scheduling. It wakes from same-process signals, an identifier-only cross-process dirty marker, or a bounded periodic fallback; claims work with revision-checked leases; runs a bounded concurrent batch off the event loop; and persists the next state with compare-and-swap semantics.
- Do not replay a model turn merely to continue closeout. Do not synthesize terminal delivery over a live model/worker turn; persist terminal state and let the existing owner finish delivery first.

## Exact-head and exact-SHA invariants

- A PR head is accepted only as a full valid SHA. Local verification, review, visual-QA, and CI receipts are bound to that head.
- When GitHub reports a different head, all head-bound evidence is invalidated and required checks are recomputed only from the current head.
- Immediately before merge, refresh the authoritative PR snapshot and re-evaluate draft, review, CI, visual, local-verification, and mergeability gates even if the SHA did not change.
- Merge with GitHub's exact-head guard (`--match-head-commit <head-sha>`). A concurrent head change must fail the merge attempt rather than merge unverified code.
- After merge, persist GitHub's independently reported merge SHA before starting collectors. Canonical sync, post-merge CI, deployment, production QA, and restart receipts target that exact SHA.
- Every required post-merge receipt must be `passed` and report `observed_sha` equal to the persisted target SHA before closeout can complete.

## Rollout and rollback

Both mechanisms are non-enforcing by default.

- Visual QA modes are `off`, `shadow`, and `enforce_explicit`; the default is `shadow`.
- Trusted closeout modes are `off`, `shadow`, and `enforce`; the default is `shadow`.
- Shadow mode records what enforcement would observe but does not authorize completion or perform closeout mutations. The previously authorized legacy finalizer remains responsible where applicable.
- Roll out enforcement per surface/repository only after shadow evidence is stable and required post-merge adapters are configured.
- Roll back new work by returning the relevant mode to `shadow` or `off`. Already-authoritative in-flight closeouts remain durable and must be explicitly completed, repaired, or transitioned; rollback must not silently discard ownership or reinterpret stale evidence as success.

## Sensitive-data boundaries

- Visual requirements and durable receipts contain opaque identifiers, bounded counters, statuses, and allowlisted codes, not raw screenshots, selectors, page text, URLs, cookies, headers, tokens, or model output.
- Closeout state may retain operational identifiers needed for reconciliation, including repository, branch, workspace path, PR reference, and exact SHAs. It must not retain credentials or raw command output.
- Closeout diagnostics are bounded and redact URLs, authorization/cookie/token/password/secret/API-key values, bearer credentials, and GitHub token forms. Runtime spans record allowlisted operation names rather than command arguments.
- Cross-process wakeup files contain only bounded work-item identifiers and timing metadata.

## Consequences

Positive:

- Visual evidence is faster and cheaper without turning screenshots or browser state into an unbounded durable record.
- CI, merge, canonical sync, and post-merge ownership survive gateway restarts without foreground polling or model replay.
- A newer PR head or mismatched post-merge SHA fails closed instead of inheriting stale success.

Costs:

- Enforcement depends on durable ledger integrity, watcher availability, and correctly configured required adapters.
- Conservative classification can leave ambiguous visual work in shadow or require an explicit follow-up.
- Blocked exact-head or exact-SHA gates require repair rather than best-effort completion.
