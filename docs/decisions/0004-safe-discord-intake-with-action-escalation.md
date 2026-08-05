# Read-Only Discord Intake With Transactional Action Escalation

## Context

Discord previously split ordinary answers from "observation tasks." Questions
ran in a narrow safe-intake runtime, while audits, research, verification,
planning, and recommendations were routed to the full action runtime because
safe intake blocked the tools needed to inspect anything. This created a
binary-mode mismatch: a request could load a large question history, discover
that it needed browser/file/test observation, and then pay for a second action
turn even though no durable state change was requested.

Action routing also carries important mutable-work semantics: isolated project
worktrees, action model routing, feature/project lifecycle summaries, durable
closeout, verification, PR/merge handling, and protected-checkout safeguards.
Those semantics must remain intact and must never be partially enabled inside a
running lower-authority agent.

## Decision

Discord has one explicit per-turn capability: `read_only` or `action`.

1. Ordinary Discord messages default to `read_only`. This runtime can answer
   from context or actively observe through proven read-only file/search,
   history/log, browser navigation/snapshot/vision, API, process-inspection,
   disposable verification, and read-only delegation capabilities.
2. Deterministic high-confidence mutation and operational requests enter the
   existing `action` runtime directly. Ambiguous mutation language starts
   read-only and may call `escalate_to_action`.
3. Audits, research, verification, plans, and recommendations remain read-only
   even when they require tools. Explicit constraints such as "do not
   implement," "plan only," "recommend only," and "tell me what you would do"
   override action-channel and established-action-thread context.
4. Authority is enforced structurally. Registry entries declare a tool effect
   (`read_only`, `conditional`, `mutating`, or `unknown`). Read-only schema
   assembly exposes only proven read/conditional tools; sequential and
   concurrent execution recheck calls; middleware-rewritten arguments are
   checked again immediately before dispatch; registry dispatch provides a
   final fail-closed boundary. Unknown plugin/MCP tools are unavailable unless
   explicitly classified. Arbitrary MCP server annotations are advisory; an
   operator-owned exact `read_only_tools` allowlist is required.
5. Broad tools receive bounded policies. Generic terminal access accepts only
   parsed shell-free process/system inspection. Verification uses recognized
   test/check entrypoints inside a temporary Git snapshot with the host
   filesystem mounted read-only, network disabled, credentials removed, and
   cleanup guaranteed. Browser interaction primitives that can submit or
   mutate state are blocked. Read-only delegation propagates the same runtime
   capability to every child.
6. Escalation is transactional and gateway-owned. `escalate_to_action` is a
   control-plane signal that ends the read-only turn. The running agent never
   unlocks tools in place. After the generation fence confirms the result is
   current, the adapter initializes/reuses action state and the gateway replays
   the original text, media, reply data, and captured context into a fresh
   action turn. Same-session replay is prepended ahead of racing follow-ups;
   cross-session replay enters the new thread directly. Provisional escalation
   messages are not persisted.
7. Read-only persistence remains deferred until the outcome is known, but
   persistence is not an authority signal. A normal read-only answer is appended
   after completion; an escalation tool turn is discarded. Action and read-only
   agents use distinct cache signatures.
8. Read-only Discord API history is a bounded recent view, grouped so
   assistant/tool-call sequences remain valid. Oversized tool output may be
   truncated only in the API copy. Durable transcript rows are never mutated,
   rewritten, or deleted by this bounding step, read-only turns bypass durable
   gateway hygiene compression, and the returned history offset continues to
   append only the new turn. Agent-side session compression also fails closed
   in read-only mode rather than locking, rotating, or compacting the session.
9. Action worktree behavior is unchanged for mutable work. A read-only turn may
   inspect an already-existing deterministic action worktree for its thread but
   never provisions a directory, branch, worktree, dependency link, warmup, or
   closeout record. Otherwise it observes the mapped canonical/session cwd under
   read-only tool enforcement.
10. Redacted delegation live transcripts/manifests and restart-safe background
    completion records live only under Hermes-owned runtime/cache state. They are
    operational observability and delivery metadata, not user or project
    mutation. Read-only children still cannot write project files, persist
    conversation memory, or request service changes through their transport.
11. Discord read-only turns use the configured `discord_action` model tier and
    reasoning profile for consistent observation quality and latency. This is a
    model-route choice only: `discord_action_runtime` remains the separate
    authority for mutable worktrees, action prompts, zero tool delay,
    verification-on-stop, and action lifecycle behavior. Read-only orchestration
    should usually handle small, tightly coupled observations directly to retain
    context and reduce latency, but may delegate whenever parallelism, independent
    verification, context isolation, or deeper reasoning adds value. Any configured
    delegation tier remains available; omitted tiers inherit the route-tier parent
    (Sol/medium by default) while preserving read-only enforcement.

## Consequences

- Answers and active observation share one default runtime, eliminating the
  question-to-action double hop for non-mutating work.
- Clear mutations retain their direct fast path and all existing action
  isolation, verification, lifecycle, and closeout semantics.
- Explicit non-implementation constraints remain safe in long-running action
  threads.
- New or overly broad tools fail closed until their effect is deliberately
  classified or a bounded safe operation is exposed.
- Gateway proxy mode is blocked for read-only Discord turns until the remote
  protocol can attest and enforce the same capability; silently trusting a
  remote full-authority agent would break the boundary.
- Read-only history bounding improves latency and context cost without making
  transcript compaction or retention destructive.
