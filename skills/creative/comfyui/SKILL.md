---
name: comfyui
description: "Generate images/video/audio with ComfyUI."
version: 5.1.0
author: [kshitijk4poor, alt-glitch, purzbeats]
license: MIT
platforms: [macos, linux, windows]
compatibility: "Requires ComfyUI (local, Comfy Desktop, or Comfy Cloud) and comfy-cli (auto-installed via pipx/uvx by the setup script)."
prerequisites:
  commands: ["python3"]
setup:
  help: "Run scripts/hardware_check.py FIRST to decide local vs Comfy Cloud; then scripts/comfyui_setup.sh auto-installs locally (or use Cloud API key for platform.comfy.org)."
metadata:
  hermes:
    tags:
      - comfyui
      - image-generation
      - stable-diffusion
      - flux
      - sd3
      - wan-video
      - hunyuan-video
      - creative
      - generative-ai
      - video-generation
    related_skills: [stable-diffusion-image-generation, image_gen]
    category: creative
---

# ComfyUI

Use this skill to generate or automate images, video, audio, and 3D content through ComfyUI local, Comfy Desktop, or Comfy Cloud.

## Prerequisites

- Run `scripts/hardware_check.py` before choosing local vs cloud.
- Use `scripts/comfyui_setup.sh` for local setup when ComfyUI is not installed.
- Use `scripts/health_check.py` before a generation run.
- Keep prompts, workflows, model choices, seeds, dimensions, and output paths in the final report.

## Workflow

1. Determine output type: txt2img, img2img, inpaint, upscale, video, audio, or custom workflow.
2. Choose runtime: local ComfyUI, Comfy Desktop, or Comfy Cloud based on hardware, model size, and queue constraints.
3. Select a workflow from `workflows/` or adapt an API-format JSON workflow.
4. Validate node inputs and model dependencies with the scripts before submitting long jobs.
5. Run through `scripts/run_workflow.py` or `scripts/run_batch.py`.
6. Monitor with `scripts/ws_monitor.py`, inspect logs with `scripts/fetch_logs.py`, and fix missing nodes/models with `scripts/check_deps.py` / `scripts/auto_fix_deps.py`.
7. Return the output files plus seed, workflow, model names, and any manual post-processing.

## Reference map

- [references/official-cli.md](references/official-cli.md) — `comfy ...` lifecycle commands.
- [references/rest-api.md](references/rest-api.md) — local/cloud REST and WebSocket APIs.
- [references/workflow-format.md](references/workflow-format.md) — API-format JSON and common node patterns.
- [references/template-integrity.md](references/template-integrity.md) — editor-to-API conversion, Reroute bypass, dynamic inputs, Cloud quirks, Discord-compatible ffmpeg stitching.
- [references/full-guide.md](references/full-guide.md) — archived full root guide with expanded setup and troubleshooting notes.
- `scripts/` — deterministic setup, health, dependency, schema, run, batch, monitor, and log helpers.
- `workflows/` — starter workflows for SDXL, Flux, AnimateDiff, WAN video, inpaint, upscale, and img2img.

Load only the reference that matches the task. Load the full guide when a failure is not covered by the targeted reference/script.

## Pitfalls

- Do not assume editor-format workflow JSON can be posted directly; convert/validate API format.
- Do not run long local jobs before checking VRAM and model availability.
- Do not ignore Comfy Cloud concurrency, redirect, or size limits.
- Do not lose reproducibility details: seed, workflow, model, dimensions, sampler, and runtime.

## Verification

- `python3 scripts/health_check.py`
- targeted workflow dry-run or schema validation when possible
- confirm output files exist and are viewable/playable
