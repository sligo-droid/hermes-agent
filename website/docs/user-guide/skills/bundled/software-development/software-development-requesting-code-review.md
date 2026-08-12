---
title: "Requesting Code Review — Request an independent review when policy or risk warrants it"
sidebar_label: "Requesting Code Review"
description: "Request an independent review when policy or risk warrants it"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Requesting Code Review

Request an independent review when policy or risk warrants it.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/software-development/requesting-code-review` |
| Version | `2.1.0` |
| Author | Hermes Agent (adapted from obra/superpowers) |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `code-review`, `security`, `verification`, `quality` |
| Related skills | [`github-code-review`](/docs/user-guide/skills/bundled/github/github-github-code-review) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Requesting Code Review

Use this workflow when the user asks for review, repository policy requires an
independent reviewer, or the change has material security, financial, data,
permission, production, or irreversible risk. Do not trigger it merely because
a change touched several files or the user said commit, push, ship, or verify.

## Review brief

Give the reviewer:

- the requested behavior and important constraints;
- the exact diff, branch, or commit range;
- relevant tests and observed results;
- known uncertainty and areas where a wrong decision would matter.

Ask for prioritized, evidence-backed findings. Treat feedback as blocking only
when it identifies a material correctness, security, requested-behavior,
maintainability, or verification problem. Resolve blockers and report optional
improvements separately.

The implementation owner remains responsible for reconciling the review,
rerunning affected checks, and explaining any material disagreement.
