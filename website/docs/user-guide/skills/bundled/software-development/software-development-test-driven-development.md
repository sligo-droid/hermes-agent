---
title: "Test Driven Development — Use red-green-refactor for meaningful testable behavior"
sidebar_label: "Test Driven Development"
description: "Use red-green-refactor for meaningful testable behavior"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Test Driven Development

Use red-green-refactor for meaningful testable behavior.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/test-driven-development` |
| Version | `1.2.0` |
| Author | Hermes Agent (adapted from obra/superpowers) |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `testing`, `tdd`, `development`, `quality` |
| Related skills | [`systematic-debugging`](/docs/user-guide/skills/bundled/software-development/software-development-systematic-debugging), [`plan`](/docs/user-guide/skills/bundled/software-development/software-development-plan) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

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
