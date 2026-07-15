---
title: "Comps Analysis — Build comparable-company valuation analysis in Excel"
sidebar_label: "Comps Analysis"
description: "Build comparable-company valuation analysis in Excel"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Comps Analysis

Build comparable-company valuation analysis in Excel.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/finance/comps-analysis` |
| Path | `optional-skills/finance/comps-analysis` |
| Version | `1.0.0` |
| Author | Anthropic (adapted by Nous Research) |
| License | Apache-2.0 |
| Platforms | linux, macos, windows |
| Tags | `finance`, `valuation`, `comps`, `excel`, `openpyxl`, `modeling`, `investment-banking` |
| Related skills | [`excel-author`](/docs/user-guide/skills/optional/finance/finance-excel-author), [`pptx-author`](/docs/user-guide/skills/optional/finance/finance-pptx-author), [`dcf-model`](/docs/user-guide/skills/optional/finance/finance-dcf-model), [`lbo-model`](/docs/user-guide/skills/optional/finance/finance-lbo-model) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# Comparable Company Analysis

Use this skill for public-company valuation, IPO pricing support, sector benchmarking, outlier detection, and peer multiple analysis in Excel.

This is a financial-analysis task. Use authoritative, current market and filing data, and cite source dates.

## Data source priority

1. Use available institutional MCP/data sources first, such as S&P Kensho, FactSet, or Daloopa.
2. If unavailable, use Bloomberg, SEC EDGAR, company filings, exchange data, or other auditable sources.
3. Use general web search only as a last resort for discovery, then verify against primary or institutional sources.

## Workflow

1. Define the target company, valuation date, currency, fiscal calendar, and peer-screen logic.
2. Build the peer universe and document inclusions/exclusions.
3. Collect market data and operating metrics: share price, diluted shares, net debt, minority interest, preferred stock, revenue, EBITDA, EBIT, net income, growth, margins, and sector-specific KPIs.
4. Normalize one-time items, lease/accounting differences, fiscal-year mismatches, and stale estimates.
5. Calculate enterprise value, equity value, and relevant multiples.
6. Add statistical summary: mean, median, quartiles, high/low, outlier flags, and implied valuation range.
7. Produce a clean Excel output with sources, assumptions, peer notes, and sensitivity/football-field support when requested.

## Reference map

- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/finance/comps-analysis/references/full-guide.md) — archived full comps guide with expanded data hierarchy, workbook structure, formula examples, benchmarking patterns, and presentation guidance.
- Related skills:
  - `excel-author` for workbook construction and styling.
  - `dcf-model` for intrinsic valuation.
  - `pptx-author` for investment-banking presentation output.

Load the full guide when implementing workbook formulas, peer-screen tables, outlier logic, sector-specific metrics, or presentation-ready layouts.

## Pitfalls

- Do not mix market values from one date with estimates from another without disclosure.
- Do not use unverified web snippets as a primary financial source.
- Do not average clear outliers without showing both included and excluded views.
- Do not compare companies with incompatible fiscal calendars or accounting bases without normalization.

## Verification

- Sources and source dates are visible.
- EV bridge ties: equity value plus net debt and other claims.
- Multiples use consistent numerator/denominator timing.
- Peer exclusions and outliers are documented.
- Workbook recalculates cleanly.
