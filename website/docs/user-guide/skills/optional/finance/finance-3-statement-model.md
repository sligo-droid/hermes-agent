---
title: "3 Statement Model — Build integrated 3-statement Excel financial models"
sidebar_label: "3 Statement Model"
description: "Build integrated 3-statement Excel financial models"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# 3 Statement Model

Build integrated 3-statement Excel financial models.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/finance/3-statement-model` |
| Path | `optional-skills/finance/3-statement-model` |
| Version | `1.0.0` |
| Author | Anthropic (adapted by Nous Research) |
| License | Apache-2.0 |
| Platforms | linux, macos, windows |
| Tags | `finance`, `three-statement`, `income-statement`, `balance-sheet`, `cash-flow`, `excel`, `openpyxl`, `modeling` |
| Related skills | [`excel-author`](/docs/user-guide/skills/optional/finance/finance-excel-author), [`pptx-author`](/docs/user-guide/skills/optional/finance/finance-pptx-author), [`dcf-model`](/docs/user-guide/skills/optional/finance/finance-dcf-model), [`lbo-model`](/docs/user-guide/skills/optional/finance/finance-lbo-model) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# 3-Statement Financial Model

Use this skill to build or audit integrated Income Statement, Balance Sheet, and Cash Flow models in Excel.

This skill assumes headless `openpyxl` and pairs with `excel-author` for workbook formatting, formulas, named ranges, scenario controls, and recalculation.

## Prerequisites

- Use current company filings or user-provided financials; do not rely on stale web snippets.
- Separate historical actuals from assumptions and projections.
- Write Excel formulas into projection cells. Do not compute projections in Python and paste hardcoded outputs.
- Recalculate before delivery: `python /path/to/excel-author/scripts/recalc.py ./out/model.xlsx`.

## Workflow

1. Build the workbook structure: Assumptions, Income Statement, Balance Sheet, Cash Flow, schedules, checks, and outputs.
2. Load historical actuals and source notes.
3. Add operating assumptions: revenue, margins, working capital, capex, D&A, taxes, debt, and share count as needed.
4. Link the statements:
   - Net income flows to retained earnings and cash flow.
   - D&A, capex, and working capital bridge cash flow to balance sheet changes.
   - Debt schedule feeds interest expense and financing cash flow.
   - Cash is the balance sheet plug; retained earnings ties through net income/dividends.
5. Add integrity checks for balance sheet balance, cash roll-forward, retained earnings roll-forward, and circularity.
6. Format clearly and document sources/assumptions.

## Reference map

- [references/formulas.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/3-statement-model/references/formulas.md) — canonical statement and schedule formula patterns.
- [references/formatting.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/3-statement-model/references/formatting.md) — Excel styling conventions.
- [references/sec-filings.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/3-statement-model/references/sec-filings.md) — SEC filing source workflow.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/3-statement-model/references/full-guide.md) — archived full template-completion guide with expanded modeling rules and examples.

Load formulas/formatting first for implementation. Load the full guide for unusual schedules, deeper checks, or template-specific completion detail.

## Pitfalls

- Hardcoding projection outputs silently breaks scenarios.
- Missing working-capital sign conventions can make cash flow look plausible but wrong.
- Balance-sheet balance alone is not enough; retained earnings and cash roll-forward must tie too.
- Circular debt-interest models need explicit iteration support or a simplified assumption.

## Verification

- Workbook recalculates without formula errors.
- Balance sheet check equals zero for every period.
- Cash flow statement reconciles beginning to ending cash.
- Retained earnings roll-forward ties.
- Historical values trace to sources and assumptions are visibly separated.
