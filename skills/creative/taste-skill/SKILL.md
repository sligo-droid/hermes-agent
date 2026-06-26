---
name: taste-skill
description: Apply Taste Skill anti-slop frontend checks.
version: 1.0.0
author: Leon Lin + blueemi + Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [design, frontend, ui, ux, anti-slop, visual-quality]
    related_skills: [claude-design, popular-web-designs, design-md]
---

# Taste Skill For Hermes UI Specialists

Use this skill as the Hermes adaptation of Taste Skill for frontend design quality. It is grounded in the public Taste Skill project at `https://www.tasteskill.dev/`, which describes open-source `SKILL.md` files for preventing generic AI frontends in agents including Codex and OpenCode.

This bundled Hermes skill is not a local installer for the upstream package and does not claim any unavailable Taste Skill tooling. It gives UI specialists a compact Taste Skill review protocol that composes with Hermes' existing `claude-design` and `popular-web-designs` skills.

## When to Use

Use this skill for UI-specialist frontend work where visual quality matters:

- landing pages, marketing pages, portfolios, and redesigns
- product surfaces that need visual polish, spacing, typography, responsive behavior, or theme work
- existing frontend repos where the task asks to make the UI feel less generic
- route-selected `ui_visual_specialist` worker tasks

Prefer `claude-design` with this skill when the worker needs the broader design workflow: scoping, variants, artifact format, and verification.

Prefer `popular-web-designs` with this skill when the user names a known product or brand-style reference. Let `popular-web-designs` supply the concrete visual vocabulary and use this skill as the anti-slop quality gate.

Do not use this skill as the main guide for backend-only tasks, terminal UI rendering, data plumbing, migrations, pure test fixes, or formal DESIGN.md token authoring.

## Prerequisites

- Inspect the repo's actual UI source before changing design.
- Read existing theme, token, global CSS, layout, component, and route files.
- Check `package.json` before importing any third-party library.
- Respect the repo's existing design system when one exists.
- Treat `https://www.tasteskill.dev/` and the upstream `Leonxlnx/taste-skill` repository as the primary external source if deeper Taste Skill detail is needed.

## How to Run

Load this skill when a task is routed to `ui_visual_specialist` or when the user explicitly asks for Taste Skill, anti-slop frontend review, visual polish, a redesign, or high-quality frontend implementation.

Then work in this order:

1. Declare a short design read: page kind, audience, vibe, and likely design-system or aesthetic family.
2. Inspect the existing UI source and constraints.
3. Choose the foundation: official design system, existing component system, Tailwind/native CSS, or repo-local patterns.
4. Implement the smallest complete change that raises quality.
5. Run the hard pre-flight checks below before finalizing.

## Quick Reference

- Read the brief before generating UI. Do not default to AI-purple gradients, centered dark mesh heroes, glass cards, or three equal feature cards.
- Pick one accent color and one shape system. Keep them consistent across the surface.
- Prefer real screenshots, supplied imagery, or honest placeholders over div-based fake product UI.
- Avoid fake metrics, fake social proof, fake precision, and filler sections.
- Keep heroes inside the initial viewport: concise headline, short subtext, visible CTA, single-line desktop nav.
- Use motion only when it clarifies hierarchy, story, feedback, or state. Honor reduced motion.
- Test light and dark mode when both are in scope.
- Do not silently change URL structure, nav labels, form field names, or user flows during redesigns.

## Procedure

1. **Read the room.** Infer whether this is marketing, product UI, portfolio, redesign, dashboard, or support UI. Identify the audience and trust level.

2. **Audit existing context.** For existing projects, inspect source and note what should be preserved: brand colors, typography, spacing, routes, copy labels, accessibility behavior, and component APIs.

3. **Select the foundation honestly.** Use the official package when the brief truly maps to systems such as Material, Fluent, Carbon, Polaris, Atlassian, Primer, GOV.UK, USWDS, Bootstrap, Radix, shadcn, or Tailwind. Do not recreate an official system by hand unless the repo already does that.

4. **Set design dials.** Choose practical levels for variance, motion, and density. Trust-first and accessibility-critical surfaces should be calmer. Agency, portfolio, and premium consumer work can use more variance and motion.

5. **Apply consistency locks.** Use one page theme, one primary accent, and one corner-radius rule unless the existing system has documented exceptions.

6. **Build complete UI states.** Include loading, empty, error, hover, focus, active, disabled, and responsive states when the affected component needs them.

7. **Remove common AI tells.** Sweep for generic gradient blobs, decorative status dots, section-number eyebrows everywhere, fake dashboards, repeated split sections, over-carded layouts, and text that sounds plausible but says nothing.

8. **Verify in the real surface.** Run focused tests or type checks. When practical, inspect the rendered UI at desktop and mobile widths and check interaction states.

## Pitfalls

- Do not install upstream Taste Skill into a user's profile or mutate global agent state from repo work.
- Do not claim Taste Skill CLI or website tooling ran unless it actually did.
- Do not import Motion, GSAP, Radix, shadcn, or icon packages unless the repo already has them or the user approves adding them.
- Do not replace a production app's established design system with a new aesthetic because this skill is loaded.
- Do not create visual novelty that harms scanning, accessibility, or task completion.
- Do not invent data, testimonials, customer logos, release labels, or metrics for visual effect.

## Verification

Before final response, perform a Taste Skill pre-flight check:

- The design read matches the user brief and repo context.
- Existing design-system contracts were preserved or intentionally changed.
- No obvious AI-default patterns remain without a brief-driven reason.
- Color, shape, typography, density, and theme are internally consistent.
- CTA, form, and key text contrast are readable.
- Desktop and mobile layouts are accounted for.
- Motion respects reduced-motion preferences when motion is present.
- Visible copy is grammatical, specific, and free of fake claims.
- Focused repo checks ran, or the exact blocker is reported.
