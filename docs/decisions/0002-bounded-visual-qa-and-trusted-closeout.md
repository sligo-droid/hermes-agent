# ADR 0002: Bound visual QA and publish preview pull requests

Status: accepted
Date: 2026-08-09

## Context

Rendered UI checks and pull-request publication both benefit from fast follow-up, but foreground agents are the wrong long-lived owner. Unbounded visual inspection can consume tool/model budgets or retain sensitive browser material. Foreground deployment and CI polling can pin a chat turn, race a changing PR head, or replay model work after restart.

Hermes therefore needs acceleration without weakening evidence: visual checks must be small and deterministic, while PR and preview reconciliation must survive process boundaries and remain bound to the exact commit it observed. Discord feature work must leave `main` untouched for human review.

## Decision

Adopt two related, bounded mechanisms.

### Visual QA

- Classify only explicit rendered UI or artifact implementation requests. Routing metadata and changed paths may support a classification but cannot create a visual requirement by themselves. After an action turn has actually mutated an obvious rendered component or stylesheet, an otherwise-unclassified request is promoted to a host-owned surface requirement so vocabulary misses fail closed; read-only turns and non-rendered paths cannot trigger this fallback.
- Intake classification owns only that applicability gate. During the existing action implementation turn, the action orchestrator uses the accepted request/thread and its code understanding to formulate the transient semantic execution contract: the smallest relevant target/region, intended or already-prepared page state, viewport/state assumptions, and concrete assertion intent. This does not add a classifier/model call or cycle stage.
- Run visual QA through the dedicated declarative assertion surface against the task's existing browser session. The surface does not accept JavaScript, CDP or shell commands, URLs, screenshots, cookies, headers, or credentials.
- Every visual-QA run holds a per-supervisor viewport lease across deterministic checks, screenshots, retry fingerprints, and restoration so ambient and concrete contracts cannot race responsive captures. Concrete dimensions additionally apply and verify the requested viewport. Hermes restores and verifies the exact supervisor-owned prior viewport state; inability to acquire, apply, verify, reapply, or restore that state is non-passing evidence.
- `viewport_contained` is strict four-edge containment and targets controls or regions expected to fit entirely in the viewport. It must not be used for full pages, document roots, long columns, or intentionally vertical scroll surfaces. For those surfaces, `no_horizontal_overflow` targets the selected root or container and checks horizontal content fit while allowing vertical scrolling.
- Protected task-browser sessions may be prepared through an operator-configured opaque authentication profile. Hermes reads a locked credential file under `$HERMES_HOME/secrets`, verifies the exact current origin, and injects the login form values through its private CDP supervisor without returning or logging credentials, cookies, tokens, or storage state.
- For explicit visual implementation work, the transient contract normally requests human-facing screenshot artifacts that meaningfully cover the changed region and surrounding context, plus distinct responsive viewports when relevant. The executor captures at most four deduplicated artifacts, reuses those same images in its existing bounded vision calls, and exposes them through the established media-delivery path without adding a model pass or receipt field.
- The host binds the orchestrator-supplied contract to an opaque trusted requirement, assigns opaque assertion identifiers, requires at least one bounded screenshot-appearance judgement for new contracts, and accepts only the executor-produced receipt. The orchestrator cannot self-declare success.
- Enforce hard runtime ceilings: at most six assertions, four screenshot artifacts sharing an 8 MiB evidence budget, two attempts, two vision calls in the existing sweep-plus-inspector execution, one receipt, one follow-up turn, 30 seconds per attempt, 60 seconds total, and 6,000 output characters.
- Persist only a sanitized, prose-free receipt containing opaque requirement/contract/assertion identifiers, status, bounded counters, order, and allowlisted diagnostic codes.
- An enforced gate passes only on the latest fresh `passed` receipt matching the trusted requirement. Missing, failed, blocked, uncertain, malformed, stale, or timed-out evidence never satisfies the gate.

### Visual design advisor

- The same conservative explicit-visual classifier may select a bounded hybrid implementation route. Before the normal Codex/OpenCode coding worker starts, Hermes runs one read-only Claude Opus 5 design consultation and injects its bounded guidance into the worker brief.
- Opus is the design director and rendered-result reviewer, not the mutation engine. The ordinary coding worker retains repository edits, focused verification, worktree isolation, recovery, and git/PR lifecycle boundaries.
- Rendered QA keeps the cheap Luna evidence sweep, then uses one Opus appearance-judgement call. Failed or uncertain appearance assertions may return one bounded implementation-oriented correction to the active orchestrator; this transient correction is not copied into the durable receipt.
- Advisor or Opus-review unavailability is conspicuous and fail-open for implementation, but it never becomes passing QA evidence. When the Anthropic pool already reports Opus extra-usage exhaustion, the rendered review selects the configured OpenRouter Sonnet fallback before the provider call rather than retrying the exhausted route. Enforced visual closeout still requires the existing host-produced receipt.
- Operators can disable the pre-implementation consultation with `ui_work.route_delegate_task: false`, and an explicit `default_coding_worker` route remains a per-task opt-out.

### Trusted closeout

- Persist closeout state on the exact gateway work item. In `enforce` mode that state is authoritative for lifecycle completion; the model or legacy finalizer must not independently claim completion.
- Reconcile with a storage-neutral, synchronous one-shot engine. One pass has a bounded command budget, never sleeps, never calls a model, and returns a normalized state plus `next_due_at`.
- Let the gateway watcher own durable scheduling. It wakes from same-process signals, an identifier-only cross-process dirty marker, or a bounded periodic fallback; claims work with revision-checked leases; runs a bounded concurrent batch off the event loop; and persists the next state with compare-and-swap semantics.
- Do not replay a model turn merely to continue closeout. Do not synthesize terminal delivery over a live model/worker turn; persist terminal state and let the existing owner finish delivery first.
- Push the exact feature-branch head and create or refresh a draft pull request. Hermes never marks the pull request ready and never merges it.
- Prove readiness through GitHub Deployments for the exact PR head, `Preview` environment, feature branch, and a Vercel creator identity. Then publish the HTTPS `*-git-*.vercel.app` branch alias from the Vercel bot's PR comment, not the immutable deployment hostname.
- As soon as the exact-head preview is ready, persist one durable Discord delivery obligation and post the preview URL with the draft PR URL in the originating thread. State that `main` is untouched and visual QA continues in the background.
- Continue current-head CI and visual QA asynchronously. Post a separate terminal result in the same thread after those gates pass or require repair.
- Treat legacy persisted `auto` and `manual` merge policy values as `never`. An upgrade cannot merge an in-flight PR.

## Exact-head invariants

- A PR head is accepted only as a full valid SHA. Local verification, review, visual-QA, and CI receipts are bound to that head.
- When GitHub reports a different head, all head-bound evidence is invalidated and required checks are recomputed only from the current head.
- A preview is publishable only when its deployment SHA equals the current PR head, its deployment ref matches the feature branch or exact head SHA, and the Vercel bot comment supplies the branch alias for that PR.
- A preview delivery is keyed by preview URL, draft PR URL, and exact head SHA. A completed delivery is not sent again; an uncertain send requires operator repair instead of risking a duplicate.
- A changed PR head invalidates the prior preview and creates a new delivery obligation only after Vercel reports the new exact-head URL.
- PR publication never depends on mergeability, canonical checkout sync, post-merge CI, production deployment, restart, or live-runtime pickup.

## Rollout and rollback

Both mechanisms are non-enforcing by default.

- Visual QA modes are `off`, `shadow`, and `enforce_explicit`; the default is `shadow`.
- Trusted closeout modes are `off`, `shadow`, and `enforce`; the default is `shadow`.
- Shadow mode records what enforcement would observe but does not authorize completion or perform closeout mutations.
- Roll out enforcement per surface/repository only after shadow evidence is stable and Vercel GitHub Deployments are available.
- Roll back new work by returning the relevant mode to `shadow` or `off`. Already-authoritative in-flight closeouts remain durable and must be explicitly completed, repaired, or transitioned; rollback must not silently discard ownership or reinterpret stale evidence as success.

## Sensitive-data boundaries

- Visual requirements and durable receipts contain opaque identifiers, bounded counters, statuses, and allowlisted codes, not raw screenshots, screenshot paths, selectors, page text, URLs, cookies, headers, tokens, or model output. Human-facing screenshot artifacts are transient tool output outside the durable receipt/ledger boundary and never satisfy or bypass receipt enforcement.
- Closeout state may retain operational identifiers needed for reconciliation, including repository, branch, workspace path, PR reference, and exact SHAs. It must not retain credentials or raw command output.
- Closeout diagnostics are bounded and redact URLs, authorization/cookie/token/password/secret/API-key values, bearer credentials, and GitHub token forms. Runtime spans record allowlisted operation names rather than command arguments.
- Cross-process wakeup files contain only bounded work-item identifiers and timing metadata.

## Consequences

Positive:

- Visual evidence is faster and cheaper without turning screenshots or browser state into an unbounded durable record.
- Premium visual-model spend is concentrated on design judgement and one rendered review while mechanical implementation remains on the normal coding worker.
- Draft PR publication, preview delivery, CI, and visual-QA ownership survive gateway restarts without foreground polling or model replay.
- A newer PR head or mismatched preview deployment fails closed instead of inheriting stale success.
- Every Discord feature thread can receive its own feature-branch preview before visual QA completes.

Costs:

- Enforcement depends on durable ledger integrity, watcher availability, and Vercel publishing GitHub Deployment status for preview branches.
- Conservative classification can leave ambiguous visual work in shadow or require an explicit follow-up.
- Blocked exact-head preview or QA gates require repair rather than best-effort completion.
