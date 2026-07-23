# Durable Discord Coding Worker Recovery

## Context

Discord parent orchestrators can launch several concurrent `delegate_coding_task(background=true)` children outside the Kanban dispatcher. The Work Item required-async ledger already fenced each child result to the exact parent attempt and suppressed unsafe parent transcript replay, but a gateway restart converted every non-terminal child directly to `outcome_unknown`. That discarded recoverable Codex thread/worktree identity and made restart itself a lifecycle hazard.

Hermes code delivery also accumulated an unsafe operational assumption: merge and canonical synchronization were sometimes treated as reasons to restart the live gateway. A normal code merge does not by itself require a runtime replacement, especially while development workers are active.

## Decision

Command Center's Work Item / Worker Run model remains authoritative. Each required async coding dispatch is one durable Worker Run keyed by its stable `delegation_id`; Kanban is not involved.

The dispatch record persists:

- exact parent Work Item, session, run generation, attempt id/order, task, role, scope, worktree/repository identity, Git baseline, and model/backend settings;
- producer and backend process identity when available, Codex thread/turn identity, heartbeat, phase/plan checkpoint, and parallel-worktree merge metadata;
- recovery policy, side-effect class, launch generation/id, recovery status/error, and terminal result evidence.

State invariants:

1. Registration is durable before executor submission; coding starts only after the same record is marked running.
2. A restart enumerates every child for a Work Item before ordinary parent replay. While any exact attempt owns recovery, the parent transcript is not replayed.
3. A live previous owner/backend defers recovery. Hermes never launches a competing side-effecting attempt merely because the new gateway cannot attach to its stdio stream.
4. A dead child is atomically claimed with a new launch generation. Hermes validates the exact Git worktree, common repository, and unchanged baseline immediately before release, then resumes the recorded Codex thread when supported or starts a fresh Codex thread in that same checkout.
5. External git/PR lifecycle authority, missing mutation scope/task/backend identity, changed repository identity/baseline, unavailable worktrees, non-revalidatable analysis handoffs, and OpenCode runs without a pre-execution process barrier fail closed to a conspicuous structured manual fallback. They are never silently relaunched.
6. Terminal results remain idempotent on the stable delegation id. Conflicting terminal replay is a failure; it cannot overwrite earlier evidence.
7. When all registered children are terminal, the recovered parent attempt is sealed and deterministic reconciliation produces its single terminal response without a model replay.

Exact in-flight token streaming is not recovered. The guarantee is reattach/wait when an owner is alive, resume an existing Codex thread when supported, or relaunch from durable worktree/checkpoint state when that is safe.

## Restart and delivery policy

A routine Hermes PR ends after verified merge plus clean canonical fast-forward. Gateway restart is exceptional: an explicit user request, runtime-critical non-reloadable configuration, or demonstrated operational necessity. Detached coding workers count as active drain work, so noncritical restart paths defer while they finish. If an exceptional restart interrupts work, Discord receives interim commentary that automatic recovery will run, with a user message described only as the fallback when recovery does not proceed.

## Consequences

- Multiple concurrent Discord coding children survive gateway replacement under one Work Item without reviving Kanban or creating a second ledger.
- Recovery data is intentionally bounded and recursively secret-redacted at dispatch and every runtime checkpoint.
- Codex `thread/resume` is best effort; unsupported versions start a fresh thread in the existing worktree only for workspace-only tasks.
- Parallel recovery reuses the isolated worktree while holding the original base-workspace mutation reservation through merge-back.
- Orphaned advisory children become terminal advisory failures without creating a required-work failure or leaving parent replay suppressed forever.
- Workers authorized for external lifecycle side effects require explicit operator recovery after uncertainty, favoring duplicate-side-effect prevention over automatic progress.
