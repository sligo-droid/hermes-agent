---
name: 3-statement-model
description: Build integrated 3-statement Excel financial models.
version: 1.0.0
author: Anthropic (adapted by Nous Research)
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [finance, three-statement, income-statement, balance-sheet, cash-flow, excel, openpyxl, modeling]
    related_skills: [excel-author, pptx-author, dcf-model, lbo-model]
---

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

- [references/formulas.md](references/formulas.md) — canonical statement and schedule formula patterns.
- [references/formatting.md](references/formatting.md) — Excel styling conventions.
- [references/sec-filings.md](references/sec-filings.md) — SEC filing source workflow.
- [references/full-guide.md](references/full-guide.md) — archived full template-completion guide with expanded modeling rules and examples.

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
