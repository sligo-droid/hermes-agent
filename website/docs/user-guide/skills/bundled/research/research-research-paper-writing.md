---
title: "Research Paper Writing — Write ML papers for NeurIPS/ICML/ICLR: design→submit"
sidebar_label: "Research Paper Writing"
description: "Write ML papers for NeurIPS/ICML/ICLR: design→submit"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Research Paper Writing

Write ML papers for NeurIPS/ICML/ICLR: design→submit.

## Skill metadata

| | |
|---|---|
| Source | Bundled (installed by default) |
| Path | `skills/research/research-paper-writing` |
| Version | `1.1.0` |
| Author | Orchestra Research |
| License | MIT |
| Dependencies | `semanticscholar`, `arxiv`, `habanero`, `requests`, `scipy`, `numpy`, `matplotlib`, `SciencePlots` |
| Platforms | linux, macos |
| Tags | `Research`, `Paper Writing`, `Experiments`, `ML`, `AI`, `NeurIPS`, `ICML`, `ICLR`, `ACL`, `AAAI`, `COLM`, `LaTeX`, `Citations`, `Statistical Analysis` |
| Related skills | [`arxiv`](/docs/user-guide/skills/bundled/research/research-arxiv), `ml-paper-writing`, [`subagent-driven-development`](/docs/user-guide/skills/optional/software-development/software-development-subagent-driven-development), [`plan`](/docs/user-guide/skills/bundled/software-development/software-development-plan) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Research Paper Writing Pipeline

Use this skill to plan, execute, write, revise, and package ML/AI research papers for venues such as NeurIPS, ICML, ICLR, ACL, AAAI, and COLM.

This is an iterative research loop, not a linear writing template. Results can change experiments; reviews can change analysis; submission rules can change formatting.

## Prerequisites

- Confirm venue, deadline, anonymity requirements, page limits, artifact/supplement rules, and template.
- Establish what evidence already exists: code, logs, metrics, tables, figures, ablations, notes, and citations.
- Do not fabricate experiments, numbers, citations, reviewer claims, or limitations.
- Use current authoritative venue/source information when rules or deadlines matter.

## Workflow

1. Define the paper claim: problem, contribution, baseline, evaluation target, and falsifiable success criteria.
2. Build or audit the literature map before writing strong novelty claims.
3. Plan experiments and ablations that directly support the claim.
4. Track every reported number to a run, script, seed, dataset split, and commit/artifact when possible.
5. Draft the paper around evidence: abstract, intro, method, experiments, related work, limitations, ethics, conclusion.
6. Run self-review and targeted revision: missing baselines, weak claims, unclear figures, statistical support, reproducibility, and venue compliance.
7. Package final LaTeX, bibliography, figures, appendix/supplement, checklist, and artifact notes.

## Reference map

- [references/sources.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/sources.md) — literature/source discovery and citation sources.
- [references/citation-workflow.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/citation-workflow.md) — BibTeX, citation validation, and bibliography hygiene.
- [references/experiment-patterns.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/experiment-patterns.md) — experiment design, ablations, reporting, and statistical analysis.
- [references/writing-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/writing-guide.md) — section-by-section writing guidance.
- [references/reviewer-guidelines.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/reviewer-guidelines.md) — self-review and rebuttal thinking.
- [references/checklists.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/checklists.md) — submission, reproducibility, and final checks.
- [references/paper-types.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/paper-types.md) — empirical, systems, benchmark, theory, and position paper variants.
- [references/human-evaluation.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/human-evaluation.md) — annotation and user-study guidance.
- [references/autoreason-methodology.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/autoreason-methodology.md) — AutoReason-specific methodology notes.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/skills/research/research-paper-writing/references/full-guide.md) — archived full pipeline guide with expanded phase details.
- `templates/` — venue LaTeX templates for AAAI, ACL, COLM, ICLR, ICML, and NeurIPS.

Load only the references that match the current phase. Load the full guide for end-to-end orchestration or when the phase boundaries are unclear.

## Pitfalls

- Do not cite papers you have not verified.
- Do not overstate novelty beyond the evidence.
- Do not tune on the test set or hide negative results that matter.
- Do not leave venue formatting/checklist tasks to the last pass.
- Do not present generated plots/tables without traceable source data.

## Verification

- Every claim has evidence or a citation.
- Every table/figure has source data and a generation path.
- LaTeX compiles with the selected venue template.
- Page limits, anonymity, checklist, ethics, and supplement rules are checked against current venue instructions.
