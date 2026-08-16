---
title: "P5Js — p5.js sketches: gen art, shaders, interactive, 3D"
sidebar_label: "P5Js"
description: "p5.js sketches: gen art, shaders, interactive, 3D"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# P5Js

p5.js sketches: gen art, shaders, interactive, 3D.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/creative/p5js` |
| Version | `1.0.0` |
| Author | SHL0MS, Hermes Agent |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `creative-coding`, `generative-art`, `p5js`, `canvas`, `interactive`, `visualization`, `webgl`, `shaders`, `animation` |
| Related skills | [`ascii-video`](/docs/user-guide/skills/bundled/creative/creative-ascii-video), [`manim-video`](/docs/user-guide/skills/bundled/creative/creative-manim-video), [`excalidraw`](/docs/user-guide/skills/bundled/creative/creative-excalidraw) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# p5.js Production Pipeline

Use this skill for p5.js sketches, creative coding, generative art, interactive visualizations, canvas animation, WebGL scenes, shaders, and browser-based visual experiments.

## Prerequisites

- Determine deliverable: source sketch, standalone HTML, rendered PNG/GIF/MP4/SVG, or interactive page.
- Use the bundled template/scripts when producing files rather than only explaining code.
- State any browser, resolution, duration, or export assumptions.

## Workflow

1. Define the creative concept before writing code. Name the visual idea, mood, motion, and interaction.
2. Choose renderer and references:
   - 2D/canvas: core API, shapes, color, typography, animation.
   - WebGL/3D: WebGL reference, camera/materials, shaders.
   - Export: export pipeline and scripts.
3. Scaffold from `templates/viewer.html` or an existing project structure.
4. Implement with deterministic parameters where useful: seed, canvas size, palette, frame count, and export path.
5. Render locally, inspect the first frame and motion, then iterate until it is visually intentional.
6. Deliver source plus generated output if requested.

## Reference map

- [references/core-api.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/core-api.md) — p5 setup/draw, canvas, drawing, transforms, utilities.
- [references/animation.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/animation.md) — timing, easing, loops, frame capture.
- [references/color-systems.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/color-systems.md) — palettes, gradients, color spaces.
- [references/shapes-and-geometry.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/shapes-and-geometry.md) — primitives, curves, grids, geometry.
- [references/visual-effects.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/visual-effects.md) — particles, noise, flow fields, post effects.
- [references/webgl-and-3d.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/webgl-and-3d.md) — WEBGL, shaders, cameras, lights.
- [references/interaction.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/interaction.md) — mouse, keyboard, touch, UI controls.
- [references/typography.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/typography.md) — kinetic type and text layout.
- [references/export-pipeline.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/export-pipeline.md) — high-res image/video/GIF/SVG export.
- [references/troubleshooting.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/troubleshooting.md) — blank canvas, shader, performance, and browser issues.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/p5js/references/full-guide.md) — archived full root guide with expanded production standards.
- `scripts/setup.sh`, `scripts/serve.sh`, `scripts/render.sh`, `scripts/export-frames.js` — setup, preview, and render helpers.
- `templates/viewer.html` — standalone sketch wrapper.

Load only the reference needed for the requested medium. Load the full guide when the artistic/production standard needs more detail.

## Creative standard

- Avoid tutorial-looking defaults.
- Make composition, palette, and motion deliberate.
- Prefer a memorable visual rule over random decoration.
- Keep performance stable at the target resolution.

## Verification

- Serve/open the sketch.
- Check browser console for errors.
- Confirm exports are created and playable/viewable when requested.
