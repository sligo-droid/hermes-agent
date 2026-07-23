# Safe Discord Intake With Transactional Action Escalation

## Context

Discord intake previously used narrow heuristics followed by an auxiliary LLM
classifier to decide whether a message was a question or an action request
before the main agent ran. The verdict selected more than response style:
action routing creates lifecycle summaries, selects the action model tier,
provisions a mutable worktree for protected repositories, and enables action
verification behavior.

That classifier added latency and made an ambiguous false-negative effectively
final. The ordinary agent had better conversational context but could not move
the request into the isolated action runtime without asking the user to repeat
it or performing work in the wrong runtime.

## Decision

Discord intake uses asymmetric routing:

1. Structural routes and narrow high-confidence heuristics continue to send
   explicit work requests directly to action mode.
2. Every ambiguous message defaults to the safe question/intake runtime; no
   auxiliary LLM classifier runs.
3. Question/intake turns expose `escalate_to_action`. A successful call is a
   control-plane signal only and immediately ends the intake agent loop.
   Tool execution is fail-closed to explicit observation tools until that
   handoff; file writes, shell execution, coding delegation, and unknown tools
   cannot run in the intake runtime.
4. Intake persistence is deferred until the turn outcome is known. Ordinary
   answers are persisted normally; an escalation's provisional tool turn is
   discarded.
5. The Discord adapter initializes or reuses the correct action thread and
   summary. The gateway then replays the original user request, never a
   model-rewritten substitute, as a fresh action turn.
6. Same-thread escalation is placed ahead of racing user follow-ups. Cross-
   thread escalation starts the new thread session directly.
7. If escalation is emitted in a mixed tool batch, every sibling call is
   skipped. The intake agent cannot perform preliminary reads or mutations
   before the action runtime owns the request.

## Consequences

- Ambiguous questions stay cheap and safe while the full agent can recover an
  action request without another user message.
- Clear imperatives retain the one-pass fast path.
- The handoff pays for a second model turn, but only after the first model has
  established that ambiguous language actually requests work.
- The action turn receives the normal model tier, worktree, summary, ledger,
  and verification contracts; no special reduced action path is introduced.
- The deprecated auxiliary triage configuration remains readable for backward
  compatibility but no longer participates in Discord intake routing.
