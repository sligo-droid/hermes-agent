# Manual OAuth Bootstrap for a Remote Hermes Gateway

Use this reference when the compact `SKILL.md` routes to dashboard discovery,
the native TTY escape hatches, or full manual OAuth token bootstrap.

## Contents

- [Why remote loopback callbacks fail](#why-remote-loopback-callbacks-fail)
- [Dashboard first](#dashboard-first)
- [Native TTY fallbacks](#native-tty-fallbacks)
- [Manual bootstrap procedure](#manual-bootstrap-procedure)
- [Completion criteria](#completion-criteria)

## Why Remote Loopback Callbacks Fail

Hermes' native MCP OAuth client in `tools/mcp_oauth.py`:

1. Picks a free local port `P`.
2. Registers a dynamic client with
   `redirect_uri=http://127.0.0.1:P/callback`.
3. Starts a one-shot HTTP listener on that address inside the Hermes process.
4. Opens or prints the authorization URL and waits for the callback.

On a remote gateway, the user's browser resolves `127.0.0.1` to the user's
machine, not the gateway. The browser fails to connect, the code never reaches
Hermes, and the flow times out. Common symptoms are an `[xdg-open] <defunct>`
process, an empty `$HERMES_HOME/mcp-tokens/`, and `/reload-mcp` returning "No MCP
tools available" without an Added/Reconnected detail.

## Dashboard First

A remote gateway may run the dashboard as a separate process, for example:

```bash
ps aux | grep 'hermes dashboard'
```

The dashboard exposes authenticated connector/MCP surfaces such as
`/api/mcp/servers`, `/api/mcp/status`, and `/connectors`. A cookieless request
returning 401 or 302 is enough to confirm the login-gated surface exists.

Find the public URL without leaking adjacent credentials:

```bash
env | grep -iE "HERMES_DASHBOARD_PUBLIC_URL|RAILWAY_PUBLIC_DOMAIN|RAILWAY_STATIC_URL|RAILWAY_SERVICE_.*_URL|PUBLIC_URL|BASE_URL|DOMAIN" \
  | sed -E 's/(TOKEN|SECRET|KEY|PASSWORD)=.*/\1=***REDACTED***/I'
```

`HERMES_DASHBOARD_PUBLIC_URL` is authoritative when present. On Railway, also
check `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_STATIC_URL`, and
`RAILWAY_SERVICE_*_URL`. Give the user the full `https://` URL and direct them to
Connectors/MCP.

The dashboard does not configure host-side login state for stdio MCP servers and
does not replace credentials read from `$HERMES_HOME/.env`.

## Native TTY Fallbacks

When an operator has an interactive terminal on the gateway, Hermes' native
OAuth flow offers two cheaper alternatives to manual token files:

1. **Paste-back.** Let the browser fail at the loopback callback, copy the full
   address-bar URL containing `code` and `state`, and paste it into the waiting
   Hermes prompt. A stdin reader races the local HTTP listener.
2. **SSH port-forward.** Forward the exact callback port selected by Hermes:

   ```bash
   ssh -N -L <port>:127.0.0.1:<port> <user>@<host>
   ```

   The user's browser can then reach the remote listener through the tunnel.

Use the full manual procedure only when there is no dashboard browser session
and no interactive TTY.

## Manual Bootstrap Procedure

Perform host commands through the `terminal` tool. Use `execute_code` or a
single host-side Python invocation for PKCE, exchange, and file writes. The token
exchange and final file write must occur in the same execution block so secret
redaction cannot consume the one-time code while hiding the returned token.

### 1. Confirm the Remote Topology

```bash
env | grep -iE "HERMES|RAILWAY|CONTAINER"
echo "$DISPLAY $WAYLAND_DISPLAY $SSH_CLIENT"
```

No display plus a remote/container indicator confirms the problematic topology.
`tools/mcp_oauth.py::_can_open_browser()` uses the same signals.

### 2. Resolve the Active Hermes Profile

```bash
HERMES_HOME=$(python3 -c 'from hermes_constants import get_hermes_home; print(get_hermes_home())')
echo "config: $HERMES_HOME/config.yaml"
echo "tokens: $HERMES_HOME/mcp-tokens/"
```

Use this path for every subsequent config and token operation.

### 3. Discover OAuth Metadata

MCP servers advertise OAuth through RFC 9728 Protected Resource Metadata. Start
with the `WWW-Authenticate` header on a 401:

```bash
curl -sI https://mcp.example.com | grep -i www-authenticate
# Bearer realm="mcp", resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"
```

Some servers return a bare unauthorized body without the header. Probe the
well-known endpoints directly:

```bash
for p in \
  /.well-known/oauth-protected-resource \
  /.well-known/oauth-authorization-server \
  /.well-known/openid-configuration ; do
  echo "=== $p ==="
  curl -s -A "python-httpx/0.27" "https://mcp.example.com$p" | head -c 400; echo
done
```

Fetch resource metadata to obtain `authorization_servers` and the exact
resource/audience value. Fetch the authorization server's metadata to obtain
`authorization_endpoint`, `token_endpoint`, and `registration_endpoint`.

Use `User-Agent: python-httpx/0.27` or another browser-like value for every
request in this flow. Some Cloudflare-fronted providers reject default urllib
user agents even though Hermes' real httpx connection works.

### 4. Register a Dynamic Client

POST this RFC 7591 shape to the `registration_endpoint`:

```json
{
  "client_name": "Hermes Agent (manual OAuth)",
  "redirect_uris": ["http://127.0.0.1:8765/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none",
  "scope": "<scopes_from_resource_metadata>"
}
```

Use port `8765` or another stable unused port; nothing will listen there. The
failed redirect is intentional. `token_endpoint_auth_method: none` declares a
public PKCE client. Save the returned `client_id`.

Omit `scope` when the authorization server advertises an empty
`scopes_supported` array and the resource metadata declares no explicit scopes.
Invented scope strings can produce `invalid_scope`.

Providers without DCR require a pre-registered client. GitHub's confidential
client variant is documented in
[Troubleshooting, security, and provider variants](troubleshooting-and-security.md#github-copilot-mcp-variant).

### 5. Generate PKCE and the Authorization URL

Generate:

- `code_verifier = secrets.token_urlsafe(64)[:128]`
- `code_challenge = base64url(sha256(code_verifier))` without padding
- `state = secrets.token_urlsafe(24)`

Build the authorization URL with `urllib.parse.urlencode()` and these query
parameters:

- `response_type=code`
- `client_id=<registered client>`
- `redirect_uri=http://127.0.0.1:8765/callback`
- `code_challenge=<derived challenge>`
- `code_challenge_method=S256`
- `state=<random state>`
- `resource=<exact MCP resource URL>` when required by RFC 8707/provider metadata
- `scope=<space-separated scopes>` only when metadata supports or declares them

Copy the `resource` value verbatim, including a trailing slash, and use the same
value again at token exchange.

Persist `code_verifier`, `state`, redirect URI, endpoints, resource, client ID,
and server name to a temporary file such as
`/tmp/.mcp-oauth-work/<server>.json`. Create it with `0600` permissions because
the verifier is proof-of-identity material needed across turns.

### 6. Ask the User to Authorize

Give the user the generated URL and these expectations:

```text
Open this URL in your browser:
<authorize_url>

After approval, the browser will try to load
http://127.0.0.1:8765/callback and fail to connect. That is expected. Copy the
entire URL from the address bar, including ?code=...&state=..., and paste it back.
```

Never ask the user to extract only the code; the returned state must also be
validated.

### 7. Exchange the Authorization Code

Parse `code` and `state` from the callback URL. Compare the returned `state` to
the stashed value exactly and stop on any mismatch.

POST `application/x-www-form-urlencoded` to the `token_endpoint`:

- `grant_type=authorization_code`
- `code=<callback code>`
- `redirect_uri=<same registered redirect>`
- `client_id=<registered client>`
- `code_verifier=<stashed verifier>`
- `resource=<same exact MCP resource>` when the provider required it

The response may contain `access_token`, `refresh_token`, `token_type`,
`expires_in`, and `scope`. Treat the returned scope and rotated refresh token as
authoritative.

### 8. Write Hermes Token Storage Exactly

Create `$HERMES_HOME/mcp-tokens/` with `0700` permissions. Sanitize the server
filename using the same rule as Hermes:

```python
re.sub(r'[^\w\-]', '_', server_name)[:128]
```

Write both files with `json.dumps(..., indent=2)`, atomic replacement where
possible, and `0600` permissions.

`<server>.json` uses the OAuth token model:

```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "expires_in": 7200,
  "refresh_token": "...",
  "scope": "read write"
}
```

`<server>.client.json` uses the full client-information model:

```json
{
  "client_id": "...",
  "redirect_uris": ["http://127.0.0.1:8765/callback"],
  "grant_types": ["authorization_code", "refresh_token"],
  "response_types": ["code"],
  "token_endpoint_auth_method": "none",
  "scope": "read write",
  "client_name": "Hermes Agent (manual OAuth)"
}
```

The client file is required for later refresh grants. Write what the server
actually returned; do not echo secrets to tool output. Delete the temporary
verifier/state file immediately after the exchange and durable write succeed.

### 9. Configure the MCP Server

Add or update the active profile's `config.yaml`:

```yaml
mcp_servers:
  <name>:
    url: "https://mcp.example.com"
    auth: oauth
    timeout: 180
    connect_timeout: 60
```

Use existing config helpers when changing Hermes config programmatically.

### 10. Smoke-Test Before Reload

POST an MCP `initialize` request directly with the stored access token:

```python
body = json.dumps({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "hermes-debug", "version": "1.0"},
    },
}).encode()
```

Send it to the MCP URL with:

- `Authorization: Bearer <access_token>`
- `Accept: application/json, text/event-stream`
- `Content-Type: application/json`
- `MCP-Protocol-Version: 2025-06-18`
- `User-Agent: python-httpx/0.27`

Expect HTTP 200 and a JSON-RPC result containing `serverInfo` and
`capabilities`; many servers use `text/event-stream`. The bundled
`scripts/diagnose-oauth-mcp.py` performs this probe without printing secrets.

### 11. Reload and Exercise a Tool

Ask the user or gateway operator to run `/reload-mcp`. Hermes sees `auth: oauth`,
loads the cached token and client files through `HermesTokenStorage`, skips the
browser flow, and registers `mcp_<name>_*` tools. Refresh should occur
automatically before expiration.

If reload is silent or a live call still fails, move to
[Troubleshooting, security, and provider variants](troubleshooting-and-security.md)
instead of immediately repeating authorization.

## Completion Criteria

- The active profile paths and exact server resource were used.
- Callback state was validated and the PKCE stash was deleted.
- Both token files have the expected schema and `0600` permissions; the token
  directory is `0700`.
- A direct `initialize` request succeeds with the stored token.
- `/reload-mcp` reports the server added or reconnected.
- A real registered MCP tool call succeeds.
