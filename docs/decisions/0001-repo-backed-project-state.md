# ADR 0001: Adopt repo-backed Hermes project state

Status: accepted
Date: 2026-06-06

## Context

PID already uses a markdown-first repo-state system: `AGENTS.md`, `docs/project-state.md`, `docs/context.md`, and focused runbooks/decisions. That gives future sessions a single place to recover current truth and stay on target.

Hermes did not have the same split. Operational and product continuity drifted into installed skills such as `hermes-agent`, `hermes-operations`, and related skill references. That created a bad feedback loop:

- current target/state changed outside the repo and PR lifecycle;
- future workers loaded different skills and pursued different priorities;
- skills accumulated project-state sediment instead of reusable procedures;
- agents had to reconstruct the target from memory, Discord, session history, and skill snippets.

## Decision

Adopt a repo-backed state/context split for Sligo Labs' `hermes-agent` fork:

- `AGENTS.md` — repo worker instructions and entrypoint routing.
- `docs/project-state.md` — current operational truth, focus, blockers, and next actions.
- `docs/context.md` — durable Hermes/Sligo product context and knowledge-store boundaries.
- `docs/decisions/` — durable tradeoff decisions and rationale.
- `docs/runbooks/` — optional repo-owned operational procedures when tightly coupled to this codebase.

Skills remain procedural memory. They may keep reusable debugging recipes, command checklists, pitfalls, and verification patterns, but they should not be the canonical home for current Hermes project state, target, active blockers, shipped status, or next-session pickup notes.

## Consequences

Positive:

- Future sessions have a repo-local target instead of whichever skill was loaded most recently.
- Hermes state changes travel with code changes and PR review.
- Skills become narrower and more reusable.
- The Hermes repo now matches the PID project-state pattern.

Costs:

- Meaningful Hermes PRs must update `docs/project-state.md` when durable state changes.
- Existing installed skills need gradual trimming and pointers back to repo docs.
- Agents must resist writing current-state notes into skills just because the skill tool is convenient.

## Operating Rule

If the note answers “what is true right now for this Hermes repo?” update `docs/project-state.md`.

If the note answers “why is the Hermes/Sligo product or architecture this way?” update `docs/context.md` or `docs/decisions/`.

If the note answers “how do I repeatedly debug or operate this class of system?” update the narrowest skill/reference.
