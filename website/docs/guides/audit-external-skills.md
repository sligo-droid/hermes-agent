---
title: Audit External Skills
---

# Audit External Skills

Use `hermes skills vet` before importing a third-party skill pack, copied skill directory, or direct `SKILL.md` URL. The command runs the same static Skills Guard scanner used by `hermes skills install`, but it does not install files, update skill state, or change config.

```bash
hermes skills vet ./candidate-skill
hermes skills vet ./candidate-pack --recursive
hermes skills vet https://example.com/SKILL.md
hermes skills vet owner/repo/path/to/skill
```

When a local directory contains multiple nested `SKILL.md` files, `skills vet` treats it as a skill pack and scans each skill independently. Use `--recursive` when the root is itself a skill but you also want to audit nested candidate skills.

For local paths, Hermes treats the source as `community` unless you pass a different source label:

```bash
hermes skills vet ./candidate-skill --source official
```

For automation or review pipelines, emit JSON:

```bash
hermes skills vet ./candidate-skill --json
```

Recommended workflow for external packs:

1. Clone or download the pack into a temporary review directory.
2. Run `hermes skills vet` on each candidate skill directory.
3. For packs, run `hermes skills vet ./pack --recursive --json` and review the summary.
4. Review `critical` and `high` findings before installing anything.
5. Install only the specific skills you want with `hermes skills install`.
6. Prefer a small first import over a full pack import.

The scanner checks for risky patterns such as prompt injection, hidden Unicode, secret access, destructive commands, persistence hooks, unpinned install commands, and suspicious binary files. A safe scan is not a guarantee that a skill is trustworthy; it is a first-pass supply-chain check before human review.
