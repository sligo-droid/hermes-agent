---
title: "Plan — Write a plan-only deliverable when the user asks for one"
sidebar_label: "Plan"
description: "Write a plan-only deliverable when the user asks for one"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Plan

Write a plan-only deliverable when the user asks for one.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/plan` |
| Version | `2.1.0` |
| Author | Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `planning`, `plan-mode`, `implementation`, `documentation` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Plan Mode

Use this skill only when the user requests a plan instead of execution or
explicitly invokes plan mode. A request to plan and then implement authorizes
both phases; do not manufacture a second approval turn unless the plan reveals
a material decision that requires the user.

For a plan-only turn:

- inspect the relevant context with read-only tools;
- do not implement, commit, push, or perform external mutations;
- save the plan under `.hermes/plans/` when the runtime or user expects a file;
- otherwise return the plan directly.

Keep the plan proportional. Include the goal, material assumptions, approach,
ordered work, verification, and genuine risks. Add exact files or commands only
when they make execution more reliable. Do not require TDD, micro-tasks,
frequent commits, or exhaustive code sketches by default.

If the request is clear, produce the plan without another clarification turn.
After saving it, state the result briefly. On Discord, do not expose local
filesystem paths unless the user asks for them.
