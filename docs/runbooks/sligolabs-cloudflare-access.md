# Sligo Labs Cloudflare Access Rollout

This runbook applies the approved edge-authentication contract for first-level `*.sligolabs.com` hosts. Cloudflare Access is the public human-authentication boundary; each origin remains loopback-only and keeps its application-specific authentication and authorization.

The repository owns the desired semantics and offline validator. Cloudflare account state, tunnel state, service-token material, DNS, and credentials remain operator-owned and out of this public repository.

## Scope and invariants

- Create one human Access application for `*.sligolabs.com` with the same IdP, allow/group semantics, and session behavior as the existing exact `claw.sligolabs.com` application.
- Preserve the exact Claw application. Claw consumes `CF-Access-Authenticated-User-Email` for local authorization; a Bypass would remove that identity boundary.
- Do not add `*.dev.sligolabs.com`. Access `*.sligolabs.com` covers one hostname level and does not cover deeper development hosts.
- Keep Hermes dashboard, gateway webhook receiver, trace resolver, and Claw origins bound to loopback.
- Keep origin authenticators and authorization in place: webhook HMAC, NAS cron-fire JWT, Hermes session/application auth, Claw role checks, and the trace resolver's required Cloudflare identity headers.
- Never publish raw Cloudflare exports, IDs, policy identities/rules, service-token metadata, tunnel credentials, environment dumps, or secret values in git, CI artifacts, issues, or pull requests.

## Current origin and tunnel boundary

The operator-managed local tunnel configuration is `/home/droid/.cloudflared/sligo-dashboard.yml`. Its expected ordered ingress is:

1. `sligo.sligolabs.com/webhooks/*` to loopback port `8644`.
2. `sligo.sligolabs.com` trace paths to loopback port `8788`.
3. The remaining `sligo.sligolabs.com` traffic to loopback port `9119`.
4. `claw.sligolabs.com` to loopback port `8720`.
5. A terminal `http_status:404` rule.

PID is deployed through Vercel feature branches and is outside this Cloudflare tunnel contract. Any retired PID Access application or tunnel ingress should be removed by the Cloudflare operator.

`hermes.sligolabs.com` reaches the dashboard through a separately managed remote tunnel. Do not pretend that route is present in the local YAML. Verify it manually or provide the validator a sanitized route-only export.

Cloudflare Access and origin authentication serve different purposes:

- Access authenticates a public human request before it reaches the tunnel.
- Origin checks authenticate or authorize the application request itself.
- A Bypass removes only the Access challenge. It does not replace HMAC, bearer-token, Basic, session-token, or application-login checks.

## Desired Access topology

The canonical, secret-free topology is in `sligolabs-cloudflare-access.expected.yaml`.

### Human applications

| Match | Action | Contract |
| --- | --- | --- |
| `*.sligolabs.com` | Allow | Human policy semantics and session behavior match exact Claw. First-level hosts only. |
| `claw.sligolabs.com` | Allow | Preserve the existing exact application and policy. It remains the policy reference. |
| `*.dev.sligolabs.com` | None | Explicitly excluded from this rollout. |

Cloudflare resolves more-specific exact host/path applications before the wildcard. Create all exceptions before enabling the wildcard.

### Machine and separate-origin paths

| Match | Access action | Origin boundary |
| --- | --- | --- |
| `sligo.sligolabs.com/webhooks/*` | Bypass | GitHub HMAC, body limits, rate limiting, and idempotency remain required. |
| Observed host(s) `/api/status` | Bypass | Deploy public-edge status redaction first. Keep entries host-specific in the manifest. |
| Observed NAS/Chronos host(s) `/api/cron/fire` | Bypass initially | The short-lived NAS JWT remains in `Authorization`; its audience and signature are verified against an HTTPS NAS JWKS endpoint. |
| `sligo.sligolabs.com/traces*` | Human Allow matching Claw | The loopback resolver requires Cloudflare's JWT assertion and authenticated-user email headers, and never emits a Basic challenge. |

The manifest currently records the observed status and cron hosts. If monitoring or Chronos changes host, update the host-specific entries before cutover; never replace them with a hostname-wide Bypass. Each Bypass policy must include Everyone without Require or Exclude selectors so non-browser callers are not challenged.

A future migration from cron Bypass to Service Auth is allowed only after every caller sends `CF-Access-Client-Id` and `CF-Access-Client-Secret`. Do not repurpose the `Authorization` header because the origin uses it for the NAS JWT.

## Pre-change inventory

1. Confirm the merged Hermes deployment includes forwarded/Cloudflare `/api/status` redaction and shared public-path handling.
2. Export Access applications and embedded policies with a read-only Cloudflare credential to an operator-owned secure location.
3. Confirm the exact Claw application, its human allow policy semantics, IdP configuration, and session duration.
4. Confirm there is no conflicting wildcard, retired PID application, duplicate path application, or `*.dev.sligolabs.com` application.
5. Confirm which public host monitoring uses for `/api/status` and which callback host Chronos uses for `/api/cron/fire`; update the expected manifest if the observed hosts differ.
6. Inspect the local tunnel ingress ordering and separately verify the remote Hermes route.
7. Save an operator-owned rollback export. Do not attach it to the public PR or copy it into this repository.

For validator input, a raw read-only Access response is accepted locally, but it must remain outside the repository. The tool compares sensitive policy semantics in memory and emits only bounded findings.

## Offline validation

The validator performs no network calls and no mutations:

```bash
python scripts/check_sligolabs_access.py \
  --expected docs/runbooks/sligolabs-cloudflare-access.expected.yaml \
  --tunnel-config /home/droid/.cloudflared/sligo-dashboard.yml \
  --access-export /secure/operator-owned/access-apps.json
```

The Access input may be either a bare application array or Cloudflare's `{"result": [...]}` wrapper. Relevant applications need embedded policies, or an operator-generated `policy_semantics` value, so the wildcard and trace human policy can be compared to Claw.

To validate the separately managed Hermes route, provide a sanitized YAML or JSON document containing only an `ingress`/`result` route list with hostname and service:

```bash
python scripts/check_sligolabs_access.py \
  --expected docs/runbooks/sligolabs-cloudflare-access.expected.yaml \
  --tunnel-config /home/droid/.cloudflared/sligo-dashboard.yml \
  --access-export /secure/operator-owned/access-apps.json \
  --hermes-tunnel-export /secure/operator-owned/hermes-routes-sanitized.yaml
```

Without that optional input, local and Access conformance can still exit successfully, but the Hermes tunnel is reported separately as unverified.

Exit codes:

- `0`: supplied state conforms; optional remote Hermes state may be reported unverified.
- `1`: semantic or route drift.
- `2`: unreadable or structurally invalid input.

Before sharing output, confirm it contains no IDs, identities, raw rules, credential locations, or secret values. The validator intentionally never renders those fields.

## Deployment order

1. Deploy the merged Hermes hardening to loopback port `9119`.
2. Verify direct-loopback `/api/status` still has operator detail and a simulated forwarded/Cloudflare request has only the safe liveness shape.
3. Verify dashboard roots do not emit an origin Basic challenge, while sensitive APIs still require Hermes session/application authentication and `/api/status` and `/api/cron/fire` retain their narrow public-path behavior.
4. Verify invalid webhook HMAC and invalid cron JWT requests are rejected at the origin.
5. Remove any retired PID Access application and tunnel ingress through the operator-owned Cloudflare configuration.
6. Create the Sligo webhook Bypass, every configured status Bypass, every configured cron Bypass, and the Sligo trace human application.
7. Confirm trace Access policy matches Claw and the trace resolver rejects requests missing Cloudflare identity headers without emitting `WWW-Authenticate: Basic`.
8. Confirm the exact Claw application is unchanged.
9. Create or enable `*.sligolabs.com` with Claw-equivalent human policy semantics.
10. Run the offline validator against a fresh read-only export and the out-of-tree tunnel configuration.
11. Only after all checks pass, stop forcing Basic Auth in every live browser surface. Internal worker QA credentials may remain available, but no `*.sligolabs.com` route may depend on or advertise them.

Do not remove origin authentication during this sequence.

## End-to-end verification

### Human surfaces

- An unauthenticated request to Sligo and Hermes roots receives a Cloudflare Access login, not an origin Basic popup.
- An authorized user can load both dashboards, redirects, APIs, WebSocket/PTY flows, and sign out/sign back in.
- Claw still receives an Access-authenticated email and local role restrictions still apply.
- A user not allowed by the Claw-equivalent human policy cannot enter through the wildcard.

### Machine paths

- An unsigned or invalidly signed webhook bypasses Access but is rejected by the origin; a valid redelivery succeeds once.
- Status bypasses Access only on configured hosts and exposes no absolute host paths, process ID, or internal health URL through the public edge.
- Cron bypasses Access only on configured hosts. An invalid NAS JWT is rejected; a valid fire request succeeds with `Authorization` unchanged.
- Trace paths require human Access. The origin accepts the forwarded Cloudflare identity headers and never returns a Basic challenge.

### PID and development boundary

- `pid.sligo-labs.vercel.app` login and bearer-authenticated APIs behave exactly as before.
- PID feature work uses its branch-specific `pid-git-*-sligo-labs.vercel.app` URL.
- No PID hostname or origin is served by the Sligo Labs Cloudflare tunnel.
- No `*.dev.sligolabs.com` application, DNS, tunnel, or TLS change is made.

## Rollback

1. Keep all origin authenticators and path exceptions in place.
2. Disable only the wildcard human application first.
3. Restore the previously exported Claw/policy state if any unintended change occurred.
4. Do not expose a browser surface without Access. If Access cannot be restored, stop the affected tunnel route instead of restoring Basic Auth.
5. Recheck webhook HMAC, status redaction, cron JWT rejection, trace Cloudflare-header rejection, Claw role checks, and the Vercel-hosted PID login.
6. Retain narrow machine-path exceptions until the public edge is stable; removing exceptions before the parent can invert precedence during rollback.
7. Record the incident using sanitized facts only. Keep account exports and secrets in operator-owned storage.

## Ownership boundary

Do not introduce partial Terraform for this rollout. There is no imported Cloudflare state or approved secure state backend in this repository. A future IaC migration belongs in a private infrastructure repository and must begin with complete imports and a reviewed no-op plan.
