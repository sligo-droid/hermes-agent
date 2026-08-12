---
name: systematic-debugging
description: "Investigate ambiguous, recurrent, or high-impact failures."
version: 1.2.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [debugging, troubleshooting, root-cause, investigation]
    related_skills: [test-driven-development, plan]
---

# Systematic Debugging

Use this workflow when the cause is unclear, the failure recurs, multiple
layers interact, production behavior differs from local behavior, or a wrong
fix would be costly. Do not load it for a narrow failure whose cause and fix
are already supported by direct evidence.

## Procedure

1. Capture the exact symptom, environment, and last known good behavior.
2. Build the smallest useful feedback loop. Prefer an existing focused test or
   command; add instrumentation when reproduction is intermittent.
3. Trace the failing data or control flow across each relevant boundary.
4. Form the fewest hypotheses that explain the evidence. Test the most
   discriminating one first.
5. Fix the cause at the narrowest responsible layer.
6. Rerun the focused loop and any adjacent checks needed to rule out regression.

When deterministic reproduction is not practical, use logs, traces, state
comparisons, or guarded instrumentation. State the remaining uncertainty; do
not block a safe bounded fix merely because perfect reproduction is impossible.

## Pitfalls

- Changing several variables before learning which one mattered.
- Treating a downstream symptom as the cause.
- Expanding a small repair into a general framework.
- Demanding exhaustive proof when a reversible, well-instrumented change is
  the fastest discriminating test.

## Verification

Report the observed cause, evidence connecting it to the symptom, focused
checks run, and any material uncertainty that remains.
