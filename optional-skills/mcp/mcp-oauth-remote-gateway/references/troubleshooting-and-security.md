# OAuth MCP Troubleshooting, Security, and Provider Variants

Use this reference after manual bootstrap or whenever an existing OAuth MCP
server reports "not connected," refresh failures, silent reloads, or recurring
session expiry.

## Contents

- [Run the diagnostic first](#run-the-diagnostic-first)
- [Recovery decision tree](#recovery-decision-tree)
- [Transport and metadata pitfalls](#transport-and-metadata-pitfalls)
- [Refresh, revocation, and breaker pitfalls](#refresh-revocation-and-breaker-pitfalls)
- [Token storage and secret handling](#token-storage-and-secret-handling)
- [Scopes, REST reuse, and static-token alternatives](#scopes-rest-reuse-and-static-token-alternatives)
- [GitHub Copilot MCP variant](#github-copilot-mcp-variant)
- [Actions to avoid](#actions-to-avoid)

## Run the Diagnostic First

Run the bundled script before re-authorizing:

```bash
python3 optional-skills/mcp/mcp-oauth-remote-gateway/scripts/diagnose-oauth-mcp.py <server>
```

The script is read-only by default. It:

1. Resolves the active `HERMES_HOME` and reads the stored token/client files.
2. Smoke-tests the stored access token with MCP `initialize`.
3. If rejected, attempts one refresh and tests the newly minted token.
4. Prints a recovery branch without printing access tokens, refresh tokens, or
   client secrets.

Pass `--mcp-url` or `--token-endpoint` if metadata discovery is unavailable.
Pass `--write` only to atomically persist a refreshed token that the script has
already verified; it writes with `0600` permissions.

## Recovery Decision Tree

### `TOKEN_OK`

The stored token works. "Not connected" is likely stale runtime state or a
tripped module-level circuit breaker in `tools/mcp_tool.py`.

1. Run `/reload-mcp` first; current builds may clear the counter cheaply.
2. Make a live MCP tool call.
3. Restart the gateway only if the verified token still short-circuits after
   reload.

### `REFRESH_FIXED`

The stored access token failed, but one refresh minted a working token.

1. Re-run with `--write` to persist the verified token atomically.
2. Restart the gateway when needed to clear a breaker that prevented the normal
   in-call refresh path.
3. Reload and exercise a real tool.

### `REFRESH_DEAD`

There is no refresh token or the refresh grant returned `invalid_grant` (common
messages include "Grant not found," "Token expired," or "refresh token is
invalid"). The refresh token is dead; do not loop.

Offer the user either:

- A fresh authorization-code flow from
  [Manual OAuth bootstrap](manual-oauth-flow.md), or
- A provider-supported static personal/restricted API key for a more durable
  unattended gateway.

Before a create/update operation, compare `expires_at` with `time.time()` and
surface this state before failing mid-task.

### `SESSION_REVOKED`

The refresh endpoint succeeded, but the new access token still receives
`401 invalid_token`, JSON-RPC `-32002`, or "Session expired. Please
re-authenticate." The provider revoked the underlying MCP session; refresh can
re-mint credentials but cannot re-establish the session.

Stop refreshing. Run a fresh authorization-code flow. If the provider repeatedly
revokes unattended sessions, prefer a static key when available. See the
[Stripe revocation example](stripe-mcp-oauth-revocation.md).

### `UNKNOWN`

The new token failed for a non-session reason. Inspect the returned status/body,
resource audience, scopes, exact URL/trailing slash, protocol headers, provider
status, and current Hermes logs. Do not overwrite known-good token files until a
new token passes `initialize`.

## Transport and Metadata Pitfalls

### Remote versus local

Do not assume "headless" means OAuth is impossible. The problem is specifically
that the browser and Hermes process are on different machines. A local CLI can
use native OAuth; a remote gateway may still use the dashboard, paste-back, or
SSH port forwarding.

### Source of truth

Read `tools/mcp_oauth.py`, `tools/mcp_tool.py`, and the current MCP config
reference before claiming a feature does not exist. Skills explain the workflow;
source and shipped config docs define the current contract.

### Cloudflare user-agent filtering

Public OAuth/MCP metadata endpoints can reject `python-urllib/*` while allowing
httpx and browsers. Set `User-Agent: python-httpx/0.27` or a browser-like value
on every discovery, token-diagnostic, and manual smoke-test request. Hermes'
actual MCP path uses httpx, so a default-urllib 403 does not prove the server is
unreachable.

### RFC 8707 `resource`

Include the provider-advertised `resource` in both authorization and token
requests when required. Omitting it can produce a token with the wrong audience
or scope.

Copy the value verbatim. Some servers advertise
`https://mcp.example.com/` and reject the no-slash variant.

### Scope discovery

When `scopes_supported` is empty and resource metadata declares no specific
scopes, omit `scope`; fabricated values may cause `invalid_scope`. When scopes
are supported, request least privilege. Always persist the token endpoint's
returned `scope`, which is authoritative even when it differs from the request.

### Silent `/reload-mcp`

If reload says "No MCP tools available" without Added/Reconnected detail, a
configured server failed without a surfaced connection error. Inspect the error
log and direct `initialize` result. If the token works, try reload then restart
according to the breaker decision tree rather than immediately re-authorizing.

## Refresh, Revocation, and Breaker Pitfalls

### Breaker state can outlive reload

`tools/mcp_tool.py` keeps a module-level error-count dictionary with a small
threshold. After repeated failures, calls may short-circuit before the MCP
client reaches auto-refresh, leaving no successful call to reset the counter.
Symptom: reload reports Reconnected, but calls in the same process still report
the server unreachable.

Recovery order is `/reload-mcp`, live tool call, then full gateway restart only
if the call remains short-circuited.

### Expired token plus tripped breaker

Auto-refresh happens inside the MCP call path. If the breaker stops that path,
refresh cannot run. A manual refresh on disk is insufficient by itself; persist
the verified token and restart the process to clear the breaker.

### Dead refresh token

An `invalid_grant` response means gateway-side retry cannot recover the grant.
Do not keep calling the token endpoint. Re-authorize or switch to a static key.

### Revoked provider session

A refresh can return HTTP 200 and a new access token even when the provider has
revoked the MCP session represented by the grant. Always smoke-test the new
token. If it gets the same `-32002`/"Session expired" result, only a fresh
authorization-code flow can recover.

## Token Storage and Secret Handling

### Both files are mandatory

Hermes needs both `$HERMES_HOME/mcp-tokens/<server>.json` and
`<server>.client.json`. Without client information, the first refresh cannot
construct the grant and the user must re-authorize.

Create the token directory with `0700` and files with `0600`. Sanitize server
filenames with `re.sub(r'[^\w\-]', '_', server_name)[:128]` to match
`HermesTokenStorage`.

### Generate URLs programmatically

Use `urllib.parse.urlencode()` for the authorization URL. Hand-concatenated
query strings commonly corrupt scope spaces, state, or PKCE values.

### Protect the PKCE stash

The temporary stash contains the `code_verifier` and `state`. Write it with
`0600`, validate state exactly, and delete it immediately after successful
exchange and durable token writes.

### Write in the exchange block

Secret redaction may render opaque tokens as `***` in tool output. An
authorization code is single-use: printing the exchange response can consume
the code while hiding the token, forcing the whole user consent flow to restart.

Exchange the code and write the access token directly to its final file in the
same execution block. For diagnostics, print only non-secret metadata such as
token length, token type, scope, and expiry. Never print the token itself.

### Persist returned values

Write the token type, scope, expiry, and rotated refresh token actually returned
by the token endpoint. Do not preserve a stale refresh token when the provider
rotated it, and do not substitute the requested scope for the granted scope.

## Scopes, REST Reuse, and Static-Token Alternatives

OAuth access tokens may also authorize the provider's documented REST API when
the matching resource scope was granted. If the MCP server is read-only but the
task needs a write operation, check the provider's OAuth REST documentation
before asking for another API key. This is ordinary OAuth behavior, not a
provider-specific loophole.

For unattended gateways, a provider-supported personal or restricted API key
can be more durable than browser OAuth because it has no refresh/session cycle.
Offer it when appropriate, especially after recurring revocation, but explain
the tradeoff and respect an explicit OAuth preference.

When using a static key, configure the server's `Authorization: Bearer <token>`
header through Hermes config/secret handling. Do not place secrets in tracked
files or conversational output.

## GitHub Copilot MCP Variant

`https://api.githubcopilot.com/mcp/` does not expose Dynamic Client
Registration. It uses a pre-registered confidential GitHub OAuth App:

- Client information includes a real `client_secret`.
- `token_endpoint_auth_method` is `client_secret_post`.
- The token exchange to `https://github.com/login/oauth/access_token` includes
  `client_secret` with `client_id`, `code`, `code_verifier`, and `redirect_uri`.
- PKCE still applies in addition to the client secret.
- The redirect URI is fixed in the OAuth App configuration and cannot be
  replaced with an arbitrary listener port.

The user may still approve in their browser, let the fixed loopback redirect
fail, and paste the entire address-bar URL back. Store the confidential client
secret only in an approved secret store or `$HERMES_HOME/.env`; never print it or
commit it. If a PAT is acceptable, that is the simpler non-DCR path.

## Actions to Avoid

- Do not use `mcp-remote` as a callback workaround. Its OAuth listener also runs
  on the remote container's localhost; Hermes already supports remote HTTP MCP
  natively.
- Do not push a static token as though it were the only option when the user
  explicitly requested OAuth. Explain dashboard/TTY/manual choices first.
- Do not refresh-loop `invalid_grant` or a newly minted token that still reports
  a revoked session.
- Do not write only the access-token file, normalize the resource URL, invent
  scopes, or omit state validation.
- Do not lead with a gateway restart. Try reload first and restart only when the
  verified diagnosis calls for it.
- Do not claim Hermes lacks MCP OAuth behavior without checking current source.
