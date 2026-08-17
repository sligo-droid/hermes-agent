---
title: "Comfyui — Generate images/video/audio with ComfyUI"
sidebar_label: "Comfyui"
description: "Generate images/video/audio with ComfyUI"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Comfyui

Generate images/video/audio with ComfyUI.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/creative/comfyui` |
| Version | `5.1.0` |
| Author | ['kshitijk4poor', 'alt-glitch', 'purzbeats'] |
| License | MIT |
| Platforms | macos, linux, windows |
| Tags | `comfyui`, `image-generation`, `stable-diffusion`, `flux`, `sd3`, `wan-video`, `hunyuan-video`, `creative`, `generative-ai`, `video-generation` |
| Related skills | `stable-diffusion-image-generation`, `image_gen` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

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

- [references/official-cli.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/comfyui/references/official-cli.md) — `comfy ...` lifecycle commands.
- [references/rest-api.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/comfyui/references/rest-api.md) — local/cloud REST and WebSocket APIs.
- [references/workflow-format.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/comfyui/references/workflow-format.md) — API-format JSON and common node patterns.
- [references/template-integrity.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/comfyui/references/template-integrity.md) — editor-to-API conversion, Reroute bypass, dynamic inputs, Cloud quirks, Discord-compatible ffmpeg stitching.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/creative/comfyui/references/full-guide.md) — archived full root guide with expanded setup and troubleshooting notes.
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
