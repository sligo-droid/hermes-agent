---
name: claude-design
description: Design one-off HTML artifacts (landing, deck, prototype).
version: 1.1.0
author: BadTechBandit
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, html, prototype, ux, ui, creative, artifact, deck, motion, design-system]
    related_skills: [design-md, popular-web-designs, taste-skill, excalidraw, architecture-diagram]
---

# Claude Design for CLI/API Agents

Use this skill when the user wants a designed artifact in a normal agent environment: landing page, product mock, pitch deck, prototype, component lab, motion study, or static HTML deliverable.

This skill provides design process and quality gates. It does not require the hosted Claude Design UI.

## Prerequisites

- Identify the deliverable: static HTML, React component, deck-like page, prototype, image spec, or design critique.
- Ask for brand/assets only when missing inputs materially affect the result. Otherwise state assumptions and proceed.
- Use `popular-web-designs` if the user asks for a known brand aesthetic.
- Use `design-md` if the deliverable is a persistent token/spec file instead of a rendered artifact.
- Use `taste-skill` when polishing or auditing frontend implementation quality.

## Workflow

1. Convert the prompt into a concrete brief: audience, job-to-be-done, emotional tone, constraints, and success criteria.
2. Choose a strong visual direction before coding: layout rhythm, typography, color system, spacing, motion, and interaction.
3. Build one excellent artifact rather than many generic variants unless the user explicitly asks for options.
4. Make the artifact self-contained when practical: local HTML/CSS/JS, stable assets, and no unnecessary build step.
5. Check the result for hierarchy, contrast, responsive behavior, empty/error states when relevant, and "AI-design slop" tells.
6. Deliver with path, how to open/run, and any known limitations.

## Quick quality bar

- The first screen has a clear focal point and reason to keep reading.
- Typography has intentional scale, line-height, and measure.
- Color is restrained; accent color means something.
- Spacing and alignment are consistent across sections.
- Motion, if present, supports comprehension rather than distracting.
- The artifact handles at least desktop and mobile widths unless scoped otherwise.

## Reference map

- [references/full-guide.md](references/full-guide.md) — archived full guide with detailed decision table, design heuristics, implementation patterns, and critique checklists.
- Related skills:
  - `popular-web-designs` for brand/style-system recipes.
  - `design-md` for durable design token/spec files.
  - `taste-skill` for frontend anti-slop review.
  - `excalidraw` and `architecture-diagram` for diagrammatic artifacts.

Load the full guide when a task needs deeper design heuristics, multi-variant exploration, critique language, or a detailed artifact checklist.

## Pitfalls

- Do not over-ask for inputs; make useful assumptions for low-risk aesthetic choices.
- Do not ship generic gradients, floating cards, or stock SaaS sections without a concept.
- Do not depend on external CDNs when the user needs an offline or repo-native artifact.
- Do not treat screenshots as verification if the generated file itself is broken.

## Verification

- Open or render the artifact when practical.
- Check responsive widths and obvious accessibility issues.
- Confirm all referenced assets exist and links resolve.
