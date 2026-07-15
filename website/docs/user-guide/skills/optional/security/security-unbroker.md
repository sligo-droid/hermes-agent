---
title: "Unbroker — Autonomously remove your info from data-broker sites"
sidebar_label: "Unbroker"
description: "Autonomously remove your info from data-broker sites"
---

{/* This page is auto-generated from the skill's SKILL.md by website/scripts/generate-skill-docs.py. Edit the source SKILL.md, not this page. */}

# Unbroker

Autonomously remove your info from data-broker sites.

## Skill metadata

| | |
|---|---|
| Source | Optional — install with `hermes skills install official/security/unbroker` |
| Path | `optional-skills/security/unbroker` |
| Version | `1.0.0` |
| Author | SHL0MS (github.com/SHL0MS) |
| License | MIT |
| Platforms | linux, macos, windows |
| Tags | `privacy`, `data-broker`, `opt-out`, `ccpa`, `gdpr`, `security`, `doxxing` |
| Related skills | [`google-workspace`](/docs/user-guide/skills/bundled/productivity/productivity-google-workspace), [`agentmail`](/docs/user-guide/skills/optional/email/email-agentmail), [`himalaya`](/docs/user-guide/skills/bundled/email/email-himalaya), [`scrapling`](/docs/user-guide/skills/optional/research/research-scrapling), [`osint-investigation`](/docs/user-guide/skills/optional/research/research-osint-investigation) |

## Reference: full SKILL.md

:::info
The following is the complete skill definition that Hermes loads when this skill is triggered. This is what the agent sees as instructions when the skill is active.
:::

# unbroker

Use this skill to find and remove a consenting person's personal information from people-search and data-broker sites.

This skill does not defeat anti-bot systems, impersonate without authorization, remove public records directly, or act on anyone without recorded consent.

## Prerequisites

- Record consent before scanning or submitting opt-outs.
- Keep each person's dossier, consent, broker plan, ledger, drafts, and reports separate.
- Use the Python CLI in `scripts/pdd.py` for deterministic state and queue management.
- Escalate to the user for CAPTCHA, government ID, phone calls, fax, payment, or ambiguous authorization.

## Workflow

1. Intake the person and consent using the templates and CLI.
2. Build or update the dossier: names, aliases, addresses, phones, emails, relatives, and jurisdictions the user authorizes.
3. Plan broker tiers from `references/brokers/` and legal basis from `references/legal/`.
4. Scan with native web/browser tools and record evidence in the ledger.
5. Submit automated or guided opt-outs only within consent and site terms.
6. Poll email verification links when configured; queue manual steps when required.
7. Generate a report and schedule recurring re-scan if requested.

## Reference map

- [references/state-machine.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/state-machine.md) — lifecycle states and queue semantics.
- [references/methods.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/methods.md) — scan and opt-out methods.
- [references/site-playbooks.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/site-playbooks.md) — site-specific handling notes.
- [references/legal/ccpa.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/legal/ccpa.md), [references/legal/gdpr.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/legal/gdpr.md), [references/legal/drop.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/legal/drop.md) — legal request framing.
- `references/brokers/*.json` — broker metadata and opt-out vectors.
- `templates/consent/authorization.md` — consent record template.
- `templates/emails/*.txt` — request email templates.
- `scripts/pdd.py` — main CLI for config, dossier, consent, planning, queue, ledger, drafts, email, verification, and reports.
- [references/full-guide.md](https://github.com/NousResearch/hermes-agent/blob/main/optional-skills/security/unbroker/references/full-guide.md) — archived full guide with expanded autonomy contract and command examples.

Load the full guide when running the complete autonomous workflow, debugging queue behavior, or handling a broker-specific edge case not obvious from the targeted reference.

## Safety boundaries

- Do not act for a non-consenting person.
- Do not bypass CAPTCHAs, rate limits, paywalls, ID checks, or anti-bot controls.
- Do not submit false information.
- Do not expose dossier data in logs or final reports beyond what the user needs.
- Do not promise total removal; brokers can relist data.

## Verification

- Consent exists and matches the requested scope.
- Ledger records every broker action and result.
- Manual steps are clearly separated from automated completions.
- Final report includes completed, pending, blocked, and rescan recommendations.
