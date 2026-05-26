---
name: coding-team-tester
description: Verify coding changes with focused checks.
version: 0.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [testing, verification, profile-distribution]
---

# Coding Team Tester Skill

Use this skill when this profile is asked to verify implementation output.

## Procedure

1. Identify the behavior claims and changed files.
2. Run focused tests that cover the change.
3. Add reproduction notes for any failure.
4. Run a broader smoke check when the focused checks pass.
5. Report commands, outcomes, and untested risks.
