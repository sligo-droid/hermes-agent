---
name: autoreview
description: Run deterministic local closeout review checks.
---

# Autoreview

Use this repo-local skill after non-trivial code edits and focused checks.

## How to Run

Run:

```bash
.agents/skills/autoreview/scripts/autoreview --mode local
```

The helper is deterministic and advisory. It reports local git evidence and
closeout instructions; it does not claim that a model or human reviewer ran.

## Procedure

- Run focused checks for the files you changed before invoking the helper.
- Treat output and later review findings as advisory.
- Verify any actionable finding in the real code path before fixing it.
- Fix only concrete in-scope issues.
- Rerun affected checks and this helper after review-triggered edits.
