---
sidebar_position: 15
title: "Subscription Proxy"
description: "Expose Hermes-managed built-in or configured providers through a local inference proxy"
---

# Subscription Proxy

The subscription proxy is a small HTTP server that lets another application use
an inference provider already configured in Hermes. It supports:

- **Built-in specialized adapters** for Nous Portal, xAI Grok OAuth, and OpenAI
  Codex OAuth.
- A **named configured-provider adapter** for entries under `providers:` in
  `config.yaml`.

The proxy resolves the upstream `base_url` and credential through Hermes, removes
caller-supplied provider credentials, and sends the request using the configured
upstream credential.

This is different from the [API server](./api-server.md):

| | API server | Subscription proxy |
|---|---|---|
| What it serves | The Hermes agent, including tools, memory, and skills | Model inference routes |
| Use case | Use Hermes as a chat backend | Let another client use a Hermes-managed provider |
| Authentication | `API_SERVER_KEY` | No inbound authentication; Hermes attaches the upstream credential |
| Payload handling | Runs the agent loop | Configured providers preserve payload bytes; specialized adapters may translate protocols |

:::warning Trusted boundary only
The proxy does not authenticate callers. A client token such as `sk-unused` is
only a placeholder and is not an access-control mechanism. Keep the listener on
localhost, or put it behind authentication and network controls you trust.
:::

## Quick start with a built-in adapter

Log into a supported provider, then start the proxy. For Nous Portal:

```bash
hermes auth add nous
hermes proxy start --provider nous
```

The default listener is `http://127.0.0.1:8645/v1`. An OpenAI-compatible client
can use:

```text
Base URL: http://127.0.0.1:8645/v1
API key:  sk-unused
Model:    Hermes-4-70B
```

The client key is discarded. Hermes resolves and attaches the real upstream
credential for each request.

## Proxy a named configured provider

Named providers use the same `providers:` entries as normal Hermes model
routing. Define the upstream URL and the environment variable that holds its
credential:

```yaml
# config.yaml
providers:
  cli-proxy-api:
    name: CLIProxyAPI
    base_url: "http://127.0.0.1:8317/v1"
    key_env: "CLI_PROXY_API_KEY"
    api_mode: "codex_responses"
    default_model: "gpt-5.6-luna"
```

Set `CLI_PROXY_API_KEY` in the active Hermes profile's environment or `.env`,
then confirm discovery and start it by name:

```bash
hermes proxy providers
hermes proxy status
hermes proxy start --provider cli-proxy-api
```

`base_url` and `key_env` are the load-bearing fields for an authenticated
configured provider. If `key_env` is declared but no provider-associated key can
be resolved, startup fails closed; Hermes does not silently borrow
`OPENAI_API_KEY` or `OPENROUTER_API_KEY`.

The configured-provider proxy is byte-preserving. It does not inspect or convert
Chat Completions, Responses, or Anthropic Messages bodies. The provider entry's
`api_mode` is used by Hermes when that provider is selected for Hermes inference;
it does not make the proxy transform payloads.

## Providers and adapter behavior

List the currently available names:

```bash
hermes proxy providers
```

The built-ins are:

| Provider | Adapter behavior |
|---|---|
| `nous` | Attaches refreshed Nous Portal credentials and forwards supported Nous inference routes. |
| `xai` | Attaches xAI OAuth pool credentials and can rotate them after supported auth/rate-limit failures. |
| `openai-codex` | Specialized adapter: exposes OpenAI-style `/v1/chat/completions`, translates it to the native Codex Responses API, then normalizes the result back to Chat Completions. |

Configured entries are discovered dynamically from `providers:` (and compatible
legacy custom-provider configuration). Built-in names take precedence if a
configured entry uses the same name.

The proxy is not uniformly passthrough: the configured-provider adapter is
byte-preserving, while `openai-codex` is intentionally a protocol-translating
specialized adapter.

## Supported paths

### Configured providers

A named configured-provider adapter accepts exactly these paths:

| Local path | Upstream path |
|---|---|
| `/v1/chat/completions` | `<base_url>/chat/completions` |
| `/v1/responses` | `<base_url>/responses` |
| `/v1/messages` | `<base_url>/messages` |
| `/v1/models` | `<base_url>/models` |

Request body bytes, raw query encoding, response body bytes, content encoding,
and streaming data are preserved. Unsupported paths return a local 404 rather
than being sent upstream.

### Built-in providers

Built-in adapters expose only the paths their implementation supports:

| Provider | Paths |
|---|---|
| `nous` | `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `xai` | `/v1/chat/completions`, `/v1/responses`, `/v1/completions`, `/v1/embeddings`, `/v1/models` |
| `openai-codex` | `/v1/chat/completions`, `/v1/models` |

## Header handling

Before forwarding a generic request, the proxy strips hop-by-hop headers and all
caller-supplied provider credentials, including:

- `Authorization`
- `Proxy-Authorization`
- `x-api-key`

If the selected upstream has a credential, the proxy adds its own
`Authorization` header. This prevents a key supplied by the calling application
from leaking to the configured upstream.

## Liveness and readiness

`GET /health` is a **local liveness** endpoint. It confirms that the proxy process
is serving requests and reports the adapter's local authentication view, but it
does not call `/v1/models` or perform inference. A 200 from `/health` is not
upstream readiness proof.

Use both of these for readiness:

```bash
# Credential plus upstream route check
curl http://127.0.0.1:8645/v1/models \
  -H 'Authorization: Bearer sk-unused'

# Representative inference check; choose a path and body the selected upstream supports
curl http://127.0.0.1:8645/v1/chat/completions \
  -H 'Authorization: Bearer sk-unused' \
  -H 'Content-Type: application/json' \
  -d '{"model":"your-model","messages":[{"role":"user","content":"Reply with ready."}]}'
```

For a Responses-only or Messages-only configured route, make the representative
call to `/v1/responses` or `/v1/messages` instead. Readiness means the model list
and the actual inference shape your application uses both succeed.

## Local, Docker, and LAN trust boundaries

### Local host

The default `--host 127.0.0.1` limits access to processes on the same host. That
is the recommended default, but every local process that can reach the port can
use the upstream credential indirectly.

### Docker

Inside a container, bind the proxy to `0.0.0.0` only when container networking
requires it. Keep host publication loopback-only when the client is also local:

```bash
# Example publication policy: host-local only
# docker run ... -p 127.0.0.1:8645:8645 ...
hermes proxy start --provider cli-proxy-api --host 0.0.0.0 --port 8645
```

Publishing with `-p 8645:8645` commonly exposes the port on every host
interface, subject to Docker and firewall rules. Treat that as LAN exposure, not
as a harmless container detail.

### LAN or remote access

```bash
hermes proxy start --provider cli-proxy-api --host 0.0.0.0 --port 8645
```

Anyone who can reach that socket can consume the selected upstream account. Use
a firewall, private VPN, or authenticated reverse proxy, and do not expose the
listener directly to the public internet.

## Status and troubleshooting

```bash
hermes proxy status
```

- `not ready` means Hermes cannot currently resolve usable credentials for that
  adapter.
- If a configured provider declares `key_env`, set that variable in the active
  profile and restart the proxy.
- If `/health` works but `/v1/models` fails, investigate upstream URL,
  credentials, network access, and path support.
- If `/v1/models` works but inference fails, test the exact protocol, model, and
  request shape your application sends.

The proxy does not log request bodies. Normal upstream rate limits and quotas
still apply.
