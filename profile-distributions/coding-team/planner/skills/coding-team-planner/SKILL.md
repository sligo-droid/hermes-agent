---
name: coding-team-planner
description: Plan coding work for specialist profiles.
version: 0.1.0
author: Hermes Agent
license: MIT
metadata:
  hermes:
    tags: [planning, coding, profile-distribution]
---

# Coding Team Planner Skill

Use this skill when this profile is asked to scope or decompose coding work.

## Procedure

1. Inspect repository entry points, tests, and current git state.
2. Identify the smallest safe implementation path.
3. Split work into tasks with clear ownership and verification.
4. Include expected changed files and commands to run.
5. Mark unresolved product decisions as blockers instead of guessing.

## Output

Return a concise task plan that implementer, reviewer, and tester profiles can follow.
