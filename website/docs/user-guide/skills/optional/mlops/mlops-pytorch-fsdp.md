---
title: "Pytorch Fsdp — Use PyTorch FSDP for distributed model training"
sidebar_label: "Pytorch Fsdp"
description: "Use PyTorch FSDP for distributed model training"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Pytorch Fsdp

Use PyTorch FSDP for distributed model training.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/mlops/pytorch-fsdp` |
| Path | `optional-skills/mlops/pytorch-fsdp` |
| Version | `1.0.0` |
| Author | Orchestra Research |
| License | MIT |
| Dependencies | `torch>=2.0`, `transformers` |
| Platforms | linux, macos |
| Tags | `Distributed Training`, `PyTorch`, `FSDP`, `Data Parallel`, `Sharding`, `Mixed Precision`, `CPU Offloading`, `FSDP2`, `Large-Scale Training` |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# PyTorch FSDP

Use this skill for PyTorch Fully Sharded Data Parallel training: parameter sharding, wrapping policy, mixed precision, CPU offload, activation checkpointing, state dicts, checkpointing, FSDP2, and distributed-training debugging.

Prefer current local package docs/source for exact API behavior; PyTorch FSDP APIs evolve.

## Prerequisites

- Confirm PyTorch version, CUDA/NCCL availability, GPU count, model architecture, optimizer, precision target, and checkpoint format.
- Know whether the project uses FSDP1 (`torch.distributed.fsdp.FullyShardedDataParallel`) or FSDP2 (`fully_shard` style).
- Reproduce distributed bugs with the smallest world size and batch that triggers the issue.

## Workflow

1. Inspect the existing training stack: launch command, process group setup, model construction, optimizer creation, precision/autocast, checkpointing, and data loader.
2. Choose sharding and wrapping policy based on model structure and memory bottleneck.
3. Configure precision and memory techniques deliberately: mixed precision, activation checkpointing, CPU offload, gradient accumulation, and ignored modules.
4. Ensure optimizer and state-dict code match the FSDP API/version.
5. Validate with a tiny multi-process run before scaling.
6. Profile memory, throughput, and communication if performance is the issue.
7. Document exact environment and launch command in the handoff.

## Reference map

- [references/index.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-fsdp/references/index.md) — curated FSDP reference index.
- [references/other.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-fsdp/references/other.md) — additional generated PyTorch distributed/FSDP notes.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/mlops/pytorch-fsdp/references/full-guide.md) — archived full generated guide from official documentation; load for API lookup or rare edge cases.

Load the full guide only for detailed API signatures, state-dict variants, join/uneven-input behavior, or specific generated documentation passages. For current API disputes, inspect installed PyTorch docs/source or official docs.

## Pitfalls

- Do not wrap modules after optimizer construction unless the stack explicitly supports it.
- Do not mix FSDP1 and FSDP2 checkpoint/state-dict assumptions.
- Do not enable CPU offload or activation checkpointing blindly; they trade memory for speed/complexity.
- Do not debug distributed hangs without checking rank-specific logs and collective ordering.
- Do not scale before a minimal multi-rank smoke test passes.

## Verification

- Minimal distributed launch completes forward, backward, optimizer step, and checkpoint save/load.
- Loss changes as expected on a tiny batch.
- No rank diverges, hangs, or OOMs.
- Memory/throughput claims are backed by measured logs.
