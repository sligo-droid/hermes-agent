---
name: unbroker
description: Autonomously remove your info from data-broker sites.
version: 1.0.0
author: SHL0MS (github.com/SHL0MS)
license: MIT
platforms: [linux, macos, windows]
prerequisites:
  commands: [python3]
metadata:
  hermes:
    tags: [privacy, data-broker, opt-out, ccpa, gdpr, security, doxxing]
    category: security
    related_skills: [google-workspace, agentmail, himalaya, scrapling, osint-investigation]
    homepage: https://github.com/NousResearch/hermes-agent
---

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

- [references/state-machine.md](references/state-machine.md) — lifecycle states and queue semantics.
- [references/methods.md](references/methods.md) — scan and opt-out methods.
- [references/site-playbooks.md](references/site-playbooks.md) — site-specific handling notes.
- [references/legal/ccpa.md](references/legal/ccpa.md), [references/legal/gdpr.md](references/legal/gdpr.md), [references/legal/drop.md](references/legal/drop.md) — legal request framing.
- `references/brokers/*.json` — broker metadata and opt-out vectors.
- `templates/consent/authorization.md` — consent record template.
- `templates/emails/*.txt` — request email templates.
- `scripts/pdd.py` — main CLI for config, dossier, consent, planning, queue, ledger, drafts, email, verification, and reports.
- [references/full-guide.md](references/full-guide.md) — archived full guide with expanded autonomy contract and command examples.

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
