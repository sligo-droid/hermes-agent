---
title: "Dcf Model — Build institutional DCF valuation models in Excel"
sidebar_label: "Dcf Model"
description: "Build institutional DCF valuation models in Excel"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Dcf Model

Build institutional DCF valuation models in Excel.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/finance/dcf-model` |
| Path | `optional-skills/finance/dcf-model` |
| Version | `1.0.0` |
| Author | Anthropic (adapted by Nous Research) |
| License | Apache-2.0 |
| Platforms | linux, macos, windows |
| Tags | `finance`, `valuation`, `dcf`, `excel`, `openpyxl`, `modeling`, `investment-banking` |
| Related skills | [`excel-author`](/docs/user-guide/skills/optional/finance/finance-excel-author), [`pptx-author`](/docs/user-guide/skills/optional/finance/finance-pptx-author), [`comps-analysis`](/docs/user-guide/skills/optional/finance/finance-comps-analysis), [`lbo-model`](/docs/user-guide/skills/optional/finance/finance-lbo-model), [`3-statement-model`](/docs/user-guide/skills/optional/finance/finance-3-statement-model) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# DCF Model Builder

Use this skill to build institutional-quality discounted cash flow valuation models in Excel: operating forecast, unlevered FCF, WACC, terminal value, scenario cases, and sensitivity tables.

This is a high-stakes financial-analysis task. Use current filings/market data where applicable, make assumptions explicit, and do not present outputs as investment advice.

## Prerequisites

- Pair with `excel-author` for workbook generation and recalc.
- Prefer user-provided data, institutional data sources, SEC/company filings, and auditable market data.
- Keep historical actuals, assumptions, projections, and valuation outputs separated.
- Projection cells should be Excel formulas; hardcode only historical actuals and assumption inputs.

## Workflow

1. Define valuation date, currency, forecast horizon, fiscal year-end, share count, and source hierarchy.
2. Build operating forecast: revenue, growth, margins, taxes, D&A, capex, NWC, and unlevered FCF.
3. Build discount rate: capital structure, beta, risk-free rate, ERP, cost of debt, tax rate, and WACC.
4. Build terminal value: Gordon Growth and/or exit multiple, with clear rationale.
5. Discount explicit FCF and terminal value to enterprise value, then bridge to equity value and per-share value.
6. Add Bear/Base/Bull scenarios and 5x5 sensitivities for WACC/terminal growth or WACC/exit multiple.
7. Format, source, validate, and summarize key drivers and valuation range.

## Reference map

- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/dcf-model/references/full-guide.md) — archived full DCF guide with expanded workbook design, assumptions, formula patterns, sensitivity tables, and presentation notes.
- [TROUBLESHOOTING.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/dcf-model/TROUBLESHOOTING.md) — common DCF model failures.
- `scripts/validate_dcf.py` — validation helper for generated DCF workbooks.
- `requirements.txt` — optional validation dependencies.
- Related skills: `3-statement-model`, `comps-analysis`, `excel-author`, `pptx-author`.

Load the full guide when implementing formulas, sector-specific forecasts, sensitivity layouts, or validation repairs.

## Pitfalls

- Do not mix levered and unlevered cash flows/discount rates.
- Do not use stale risk-free rates, market prices, or share counts without source dates.
- Do not hide circularity or balance-sheet plugs.
- Do not imply precision beyond the sensitivity range.

## Verification

- Workbook recalculates without formula errors.
- FCF bridge ties to projected operating drivers.
- WACC inputs are sourced and dated.
- Enterprise-to-equity bridge ties.
- Sensitivities flex when assumptions change.
- Run `scripts/validate_dcf.py` when applicable.
