---
sidebar_position: 8
title: "MCP Config Reference"
description: "Reference for Hermes Agent MCP configuration keys, filtering semantics, and utility-tool policy"
---

# MCP Config Reference

This page is the compact reference companion to the main MCP docs.

For conceptual guidance, see:
- [MCP (Model Context Protocol)](/user-guide/features/mcp)
- [Use MCP with Hermes](/guides/use-mcp-with-hermes)

## Root config shape

```yaml
mcp_servers:
  <server_name>:
    command: "..."      # stdio servers
    args: []
    env: {}

    # OR
    url: "..."          # HTTP servers
    headers: {}

    # Optional HTTP/SSE TLS settings:
    ssl_verify: true                # bool or path to a CA bundle (PEM)
    client_cert: "/path/to/cert.pem"  # mTLS client certificate (see below)
    # client_key: "/path/to/key.pem"  # optional, when key lives in a separate file

    enabled: true
    timeout: 120
    connect_timeout: 60
    supports_parallel_tool_calls: false
    runtime_modes: [action, read_only]
    read_only_tools: []
    tools:
      include: []
      exclude: []
      resources: true
      prompts: true
```

## Server keys

| Key | Type | Applies to | Meaning |
|---|---|---|---|
| `command` | string | stdio | Executable to launch |
| `args` | list | stdio | Arguments for the subprocess |
| `env` | mapping | stdio | Environment passed to the subprocess |
| `url` | string | HTTP | Remote MCP endpoint |
| `headers` | mapping | HTTP | Headers for remote server requests |
| `ssl_verify` | bool or string | HTTP | TLS verification. `true` (default) uses system CAs, `false` disables verification (insecure), or a string path to a custom CA bundle (PEM) |
| `client_cert` | string or list | HTTP | mTLS client certificate. String = path to a PEM file containing cert + key. List `[cert, key]` = separate files. List `[cert, key, password]` = encrypted key |
| `client_key` | string | HTTP | Path to the client private key, when `client_cert` is a string and the key is in a separate file |
| `enabled` | bool | both | Skip the server entirely when false |
| `timeout` | number | both | Tool call timeout in seconds (default: `300`) |
| `connect_timeout` | number | both | Initial connection timeout in seconds (default: `60`) |
| `supports_parallel_tool_calls` | bool | both | Allow tools from this server to run concurrently |
| `runtime_modes` | string or list | both | Optional exact agent-runtime exposure allowlist: `action` and/or `read_only`. Missing preserves existing availability; invalid configured values fail closed. This does not attest that a tool is safe. |
| `read_only_tools` | string or list | both | Operator-owned exact list of server-native tool names that Hermes may execute in `read_only` mode. Server annotations are not trusted. |
| `skip_preflight` | bool | HTTP | Bypass the fail-fast content-type probe for valid Streamable HTTP endpoints whose HEAD/GET answers a non-MCP content type (default: `false`) |
| `tools` | mapping | both | Filtering and utility-tool policy |
| `auth` | string | HTTP | Authentication method. Set to `oauth` to enable OAuth 2.1 with PKCE |
| `sampling` | mapping | both | Server-initiated LLM request policy (see MCP guide) |

## `tools` policy keys

| Key | Type | Meaning |
|---|---|---|
| `include` | string or list | Whitelist server-native MCP tools |
| `exclude` | string or list | Blacklist server-native MCP tools |
| `resources` | bool-like | Enable/disable `list_resources` + `read_resource` |
| `prompts` | bool-like | Enable/disable `list_prompts` + `get_prompt` |

## Runtime and effect policy

`runtime_modes` and `read_only_tools` are independent controls:

- `runtime_modes` decides which agent runtimes receive a server's tool schemas.
- `read_only_tools` attests that exact server-native operations are safe to execute in Hermes' read-only runtime.

For example, a provider-enforced read-only database endpoint can be exposed only
to read-only agents while a separate full-authority endpoint remains action-only:

```yaml
mcp_servers:
  database_full:
    url: "https://example.test/mcp"
    runtime_modes: [action]

  database_observer:
    url: "https://example.test/mcp?read_only=true"
    runtime_modes: [read_only]
    tools:
      include: [list_tables, execute_sql]
    read_only_tools: [list_tables, execute_sql]
```

Hermes does not infer safety from URLs, server names, or MCP `readOnlyHint`
annotations. The endpoint must enforce its own read-only boundary; otherwise an
operator must not classify a broad operation such as arbitrary SQL as read-only.

## Filtering semantics

### `include`

If `include` is set, only those server-native MCP tools are registered.

```yaml
tools:
  include: [create_issue, list_issues]
```

### `exclude`

If `exclude` is set and `include` is not, every server-native MCP tool except those names is registered.

```yaml
tools:
  exclude: [delete_customer]
```

### Precedence

If both are set, `include` wins.

```yaml
tools:
  include: [create_issue]
  exclude: [create_issue, delete_issue]
```

Result:
- `create_issue` is still allowed
- `delete_issue` is ignored because `include` takes precedence

## Utility-tool policy

Hermes may register these utility wrappers per MCP server:

Resources:
- `list_resources`
- `read_resource`

Prompts:
- `list_prompts`
- `get_prompt`

### Disable resources

```yaml
tools:
  resources: false
```

### Disable prompts

```yaml
tools:
  prompts: false
```

### Capability-aware registration

Even when `resources: true` or `prompts: true`, Hermes only registers those utility tools if the MCP session actually exposes the corresponding capability.

So this is normal:
- you enable prompts
- but no prompt utilities appear
- because the server does not support prompts

## `enabled: false`

```yaml
mcp_servers:
  legacy:
    url: "https://mcp.legacy.internal"
    enabled: false
```

Behavior:
- no connection attempt
- no discovery
- no tool registration
- config remains in place for later reuse

## Empty result behavior

If filtering removes all server-native tools and no utility tools are registered, Hermes does not create an empty MCP runtime toolset for that server.

## Example configs

### Safe GitHub allowlist

```yaml
mcp_servers:
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: "***"
    tools:
      include: [list_issues, create_issue, update_issue, search_code]
      resources: false
      prompts: false
```

### Stripe blacklist

```yaml
mcp_servers:
  stripe:
    url: "https://mcp.stripe.com"
    headers:
      Authorization: "Bearer ***"
    tools:
      exclude: [delete_customer, refund_payment]
```

### Resource-only docs server

```yaml
mcp_servers:
  docs:
    url: "https://mcp.docs.example.com"
    tools:
      include: []
      resources: true
      prompts: false
```

### TLS client certificate (mTLS)

For HTTP/SSE servers that require a client certificate, set `client_cert` (and optionally `client_key`):

```yaml
mcp_servers:
  # Combined cert + key in a single PEM file
  internal_api:
    url: "https://mcp.internal.example.com/mcp"
    client_cert: "~/secrets/mcp-client.pem"

  # Separate cert and key files
  partner_api:
    url: "https://mcp.partner.example.com/mcp"
    client_cert: "~/secrets/client.crt"
    client_key: "~/secrets/client.key"

  # Encrypted key with a passphrase (3-element list form)
  bank_api:
    url: "https://mcp.bank.example.com/mcp"
    client_cert: ["~/secrets/client.crt", "~/secrets/client.key", "my-passphrase"]

  # Custom CA bundle (private CA / self-signed server)
  lab_api:
    url: "https://mcp.lab.local/mcp"
    ssl_verify: "~/secrets/lab-ca.pem"
    client_cert: "~/secrets/lab-client.pem"
```

Notes:
- Paths support `~` expansion. Missing files fail fast at connect time with a server-scoped error message.
- `ssl_verify: false` disables server certificate verification entirely. Don't use this with real services.
- Works on both Streamable HTTP and SSE transports.

## Reloading config

After changing MCP config, reload servers with:

```text
/reload-mcp
```

## Tool naming

Server-native MCP tools become:

```text
mcp_<server>_<tool>
```

Examples:
- `mcp_github_create_issue`
- `mcp_filesystem_read_file`
- `mcp_my_api_query_data`

Utility tools follow the same prefixing pattern:
- `mcp_<server>_list_resources`
- `mcp_<server>_read_resource`
- `mcp_<server>_list_prompts`
- `mcp_<server>_get_prompt`

### Name sanitization

Hyphens (`-`) and dots (`.`) in both server names and tool names are replaced with underscores before registration. This ensures tool names are valid identifiers for LLM function-calling APIs.

For example, a server named `my-api` exposing a tool called `list-items.v2` becomes:

```text
mcp_my_api_list_items_v2
```

Keep this in mind when writing `include` / `exclude` filters — use the **original** MCP tool name (with hyphens/dots), not the sanitized version.

## OAuth 2.1 authentication

For HTTP servers that require OAuth, set `auth: oauth` on the server entry:

```yaml
mcp_servers:
  protected_api:
    url: "https://mcp.example.com/mcp"
    auth: oauth
```

Behavior:
- Hermes uses the MCP SDK's OAuth 2.1 PKCE flow (metadata discovery, dynamic client registration, token exchange, and refresh)
- On first connect, a browser window opens for authorization
- Tokens are persisted to `~/.hermes/mcp-tokens/<server>.json` and reused across sessions
- Token refresh is automatic; re-authorization only happens when refresh fails
- Only applies to HTTP/StreamableHTTP transport (`url`-based servers)
