---
name: test-driven-development
description: "Use red-green-refactor for meaningful testable behavior."
version: 1.2.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    visibility:
      prompt_index: false
    tags: [testing, tdd, development, quality]
    related_skills: [systematic-debugging, plan]
---

# Test-Driven Development

Use this skill when the user requests TDD or when a meaningful behavior change
benefits from defining its contract before implementation. It is especially
useful for reusable logic, regressions, parsers, state transitions, and public
interfaces.

Do not force TDD onto documentation, generated files, exploratory prototypes,
one-off migrations, configuration-only changes, or work where a different
verification method proves the result more directly.

## Red, Green, Refactor

1. Write the smallest test that expresses the intended observable behavior.
2. Run it and confirm it fails for the expected reason.
3. Implement the smallest change that makes it pass.
4. Run the focused test and adjacent affected checks.
5. Refactor only when it improves the current code without widening scope.

Prefer behavior assertions over implementation snapshots. Use real boundaries
when mocks would hide the integration being tested. If production code already
exists, do not delete working code merely to recreate an idealized sequence;
add the regression test, verify it distinguishes the bug, then fix the code.

## Completion

Report the contract tested, the red failure observed when applicable, and the
focused checks that now pass.
