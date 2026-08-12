---
name: requesting-code-review
description: "Request an independent review when policy or risk warrants it."
version: 2.1.0
author: Hermes Agent (adapted from obra/superpowers)
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    visibility:
      prompt_index: false
    tags: [code-review, security, verification, quality]
    related_skills: [github-code-review]
---

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
