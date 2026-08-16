---
name: mcp-oauth-remote-gateway
description: Manual OAuth for remote MCP servers on headless gateways.
version: 1.0.0
author: Ben Barclay (benbarclay), Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [MCP, OAuth, PKCE, Remote-Deployment]
    related_skills: [hermes-agent, mcporter, fastmcp]
---

# MCP OAuth on a Remote Hermes Gateway

## Overview

Use this skill to connect or recover an OAuth-protected remote HTTP MCP server
when Hermes runs on a different machine from the user's browser. Hermes' native
flow registers a loopback callback inside the gateway process; on a remote host,
the browser's `127.0.0.1` is the user's machine, so the authorization code cannot
reach Hermes directly.

Prefer the least invasive path that fits the deployment. Use manual token
bootstrap only when the dashboard and native interactive fallbacks are
unavailable. Manual bootstrap performs OAuth 2.1 PKCE, writes the exact token and
client files Hermes expects, smoke-tests them, then reloads MCP.

## When to Use

Use when all of these are true:

- The target is a remote HTTP MCP server that requires OAuth rather than a
  static Bearer token.
- Hermes runs as a remote gateway, container, VPS, managed service, or
  messaging bot rather than on the user's local desktop.
- The provider supports OAuth 2.1 with PKCE and, for the generic manual flow,
  RFC 7591 Dynamic Client Registration (DCR).

Do not use for:

- Local CLI Hermes: configure `auth: oauth` and run `/reload-mcp`; the native
  loopback callback works there.
- A provider and user willing to use a static API key: configure a Bearer header
  instead, especially for unattended gateways that suffer recurring revocation.
- GitHub Copilot MCP through the generic DCR path: GitHub uses a pre-registered
  confidential OAuth App. Read the GitHub variant in
  [Troubleshooting, security, and provider variants](references/troubleshooting-and-security.md).

## Prerequisites

- Have authorized access to the gateway host and its active Hermes profile.
- Resolve paths with `hermes_constants.get_hermes_home()`; never assume
  `~/.hermes` when profiles may be active.
- Keep a user available to approve consent and return the failed loopback
  callback URL if manual authorization is required.
- Read [Manual OAuth bootstrap](references/manual-oauth-flow.md) completely
  before changing config or token files.
- For a broken existing connection, run the bundled diagnostic before starting
  a new authorization flow.

## Escalation Order

### 1. Dashboard first

If the gateway exposes the Hermes dashboard, give the user its public HTTPS URL
and direct them to Connectors/MCP. OAuth initiated in the user's authenticated
browser avoids the remote loopback mismatch and requires no hand-written token
files.

Use the dashboard public-URL discovery and redaction procedure in
[Manual OAuth bootstrap](references/manual-oauth-flow.md#dashboard-first).
The dashboard does not replace host-side authentication for stdio servers or
credentials stored in `$HERMES_HOME/.env`.

Completion criterion: the dashboard reports the connector active and a live MCP
tool call succeeds.

### 2. Native interactive fallbacks

If an operator has a TTY on the gateway, keep Hermes' native OAuth flow and use
one of its built-in escape hatches:

- Paste the browser's failed full callback URL back into the waiting prompt.
- Forward the selected callback port over SSH so the browser redirect reaches
  the gateway listener.

Read [Manual OAuth bootstrap](references/manual-oauth-flow.md#native-tty-fallbacks)
for the exact behavior and command shape.

Completion criterion: native OAuth stores tokens and `/reload-mcp` registers the
server's tools.

### 3. Manual OAuth bootstrap

Use this only for a pure-chat or otherwise headless gateway with no usable
dashboard session and no interactive TTY.

Follow [Manual OAuth bootstrap](references/manual-oauth-flow.md#manual-bootstrap-procedure)
in order:

1. Confirm the remote/headless topology and resolve the active `HERMES_HOME`.
2. Discover protected-resource and authorization-server metadata.
3. Register a public PKCE client, or follow the documented provider-specific
   path when DCR is unavailable.
4. Generate PKCE values and an authorization URL programmatically.
5. Have the user approve access and return the complete failed callback URL.
6. Validate `state`, exchange the single-use code, and write both Hermes token
   files in the same execution block.
7. Add `auth: oauth` to the MCP server config, smoke-test `initialize`, and run
   `/reload-mcp`.

Completion criterion: the direct `initialize` probe succeeds, reload reports the
server added or reconnected, and a real MCP tool call works.

## Diagnose an Existing Connection First

Run the read-only diagnostic whenever an OAuth MCP server reports "not
connected":

```bash
python3 optional-skills/mcp/mcp-oauth-remote-gateway/scripts/diagnose-oauth-mcp.py <server>
```

It smoke-tests the stored access token, attempts one refresh when needed, tests
the new token, and prints one recovery branch without revealing secrets. Use
`--write` only after it reports that the refreshed token works; the write is
atomic and permissioned `0600`.

Read [Troubleshooting, security, and provider variants](references/troubleshooting-and-security.md)
before acting on `TOKEN_OK`, `REFRESH_FIXED`, `SESSION_REVOKED`, or
`REFRESH_DEAD`. Do not refresh-loop an `invalid_grant` or a server-side revoked
session.

## Core Security Rules

- Verify the callback `state` exactly before exchanging the code.
- Copy the advertised OAuth `resource` value verbatim, including a trailing
  slash, and send it in both authorization and token requests when required.
- Generate URLs with `urllib.parse.urlencode()`; never concatenate query strings
  containing scopes, state, or PKCE values by hand.
- Use an httpx-like or browser-like `User-Agent` on discovery and diagnostic
  requests because some Cloudflare-fronted providers reject default urllib UAs.
- Create `$HERMES_HOME/mcp-tokens/` with `0700`; write both `<server>.json` and
  `<server>.client.json` with `0600`. The client file is required for refresh.
- Exchange the code and write the returned tokens in the same execution block.
  Secret redaction can hide printed tokens after consuming the single-use code.
- Persist the scope and rotated refresh token actually returned by the token
  endpoint, not assumptions from the request.
- Delete the temporary PKCE verifier/state stash immediately after a successful
  exchange. Never print or paste access tokens, refresh tokens, client secrets,
  `.env` contents, or unredacted environment dumps.

## Quick Reference

| Resource | Read or run when |
|---|---|
| [Manual OAuth bootstrap](references/manual-oauth-flow.md) | Starting dashboard discovery, native TTY recovery, or a full manual PKCE flow. Contains commands, metadata rules, request schemas, token schemas, config, and smoke-test details. |
| [Troubleshooting, security, and provider variants](references/troubleshooting-and-security.md) | A server is disconnected, refresh fails, reload is silent, the breaker may be tripped, GitHub differs, or a static token may be more durable. |
| [Stripe revocation example](references/stripe-mcp-oauth-revocation.md) | Stripe or another unattended provider repeatedly revokes OAuth sessions. |
| `scripts/diagnose-oauth-mcp.py` | Diagnose stored and refreshed tokens safely; add `--write` only to persist a verified refresh. |

## Common Pitfalls

1. Do not treat "headless" as "OAuth impossible." Dashboard, paste-back, and
   SSH forwarding may still make the native flow usable.
2. Do not use `mcp-remote` to solve this callback problem; its listener is also
   on the remote host's loopback.
3. Do not omit `resource`, normalize away its trailing slash, invent scopes when
   metadata says none are supported, or skip the client-info file.
4. Try `/reload-mcp` before a full restart. Restart only when a verified token
   still hits a live circuit-breaker short-circuit or after manual token repair.
5. Do not assume a successful refresh proves the provider session is valid.
   Smoke-test the new token; repeated `-32002`/"Session expired" requires full
   re-authorization.
6. Respect the requested auth model. Offer a static-token alternative with its
   durability tradeoff, but do not silently replace an explicit OAuth request.
7. Check `tools/mcp_oauth.py`, `tools/mcp_tool.py`, and current MCP config docs
   before claiming Hermes lacks a capability.

## Verification Checklist

- [ ] The deployment was classified correctly: dashboard, native TTY fallback,
      or manual bootstrap.
- [ ] The active profile's `HERMES_HOME`, config path, token directory, and MCP
      URL were verified without exposing secrets.
- [ ] OAuth metadata, exact `resource`, scopes, redirect URI, PKCE verifier, and
      callback `state` were handled according to the provider metadata.
- [ ] Both token files exist with the correct schema and `0600` permissions;
      the token directory is `0700`.
- [ ] Temporary verifier/state material was deleted after exchange.
- [ ] The stored token passed a direct MCP `initialize` smoke test with an
      httpx-like `User-Agent`.
- [ ] `/reload-mcp` reported the server added or reconnected; if not, the
      diagnostic branch and restart rule were followed.
- [ ] A real `mcp_<name>_*` tool call succeeded without duplicate token delivery
      or secret exposure.

## Related

- `native-mcp` — general Hermes MCP configuration and authoritative config
  reference.
- `mcporter` — ad-hoc MCP calls outside Hermes' configured server registry.
- `fastmcp` — building and operating FastMCP servers.
