# ADR-004: Authenticate before deriving cache tenants

- Status: Accepted
- Date: 2026-07-12
- Amended by: [ADR-020](ADR-020-stable-cwa-reader-identity.md)

## Context

The API previously authenticated only when an optional shared secret happened
to be configured. The proxy loader recovered that JavaScript-readable secret
from `localStorage`, and anonymous callers shared one cache tenant. CWA v4.0.6
has a login-protected JSON status route but no supported stable current-user
JSON identity suitable for cache ownership.

## Decision

Authentication is explicit and fail-closed before cache or provider work:

- `cwa_session` forwards only configured cookie names to CWA's
  exact `/ajax/emailstat` endpoint, refuses redirects or responses outside the
  bounded JSON task-list contract, and derives a one-way per-session tenant.
  CWA strong protection also binds a session to its login-time remote address
  and `User-Agent`, so the API replays those exact values from the validated
  managed-proxy path. Positive/negative decisions and singleflight coalescing
  use a separate domain-separated digest of session plus context; the tenant
  remains stable for the same valid session. This is the reference same-origin
  proxy mode.
- `forwarded` accepts a stable subject and roles only from configured peer
  CIDRs. Directly supplied identity headers are rejected. This mode supports
  identity-aware proxies and tenant continuity across login sessions.
- `token` is compatibility mode. A non-empty secret is mandatory and all
  callers intentionally share one opaque tenant.
- `disabled` requires both an explicit mode and
  `BT_ALLOW_INSECURE_AUTH=true`; it is development/test-only.

Only `/ping`, `/health`, `/ready`, and CORS preflight bypass authentication.
Authentication attempts have a separate per-client budget. Raw cookies,
tokens, session addresses, User-Agents, subjects, and provider error details
never enter public errors or authentication-layer logs. The managed proxy uses
a privacy-safe access format that omits client context and request URLs.
Credentialed CORS uses exact origins; subnet-wide origin matching is disabled
for CWA-session mode. The browser sends cookies only in that mode; token and
forwarded requests set Fetch credentials to `omit`.

## Consequences

- Logging out or rotating a CWA session creates a cold cache tenant. Stable
  reuse requires the `forwarded` contract because guessing a CWA user from
  page content or internal database state would create a brittle security
  dependency.
- The API role joins the CWA network for its auth probe but is not published in
  the reference Compose topology. The browser reaches it only through the
  injection proxy. Only that exact proxy peer may supply one clean
  `X-Forwarded-For` value; the legacy broad trust switch is not authentication
  authority. A direct low-level topology falls back to its socket peer only
  when no explicit proxy authority is configured.
- The bundled injection proxy strips forwarded-identity headers. A `forwarded`
  deployment routes the API path directly through its allowlisted identity
  proxy, which must be the immediate peer and must have no public bypass.
- Token deployments must provision the secret out of band. The proxy loader no
  longer reads credentials from browser storage. The legacy Unraid overlay
  helper supports only CWA-session mode rather than copying a secret-bearing
  JavaScript bootstrap. Because that helper publishes the API on plain HTTP
  `:8390`, it rejects HTTPS reader origins; HTTPS deployments use the
  same-origin injection proxy or a separately reviewed TLS API route.

## Source contract

CWA v4.0.6 defines `/ajax/emailstat` with `user_login_required`, serializes
`render_task_status(...)` as a JSON list, selects strong protection when
`config_session=1`, and hashes `request.remote_addr` plus `User-Agent` into the
session identifier. The integration relies only on those pinned contracts:
[route](https://github.com/crocodilestick/Calibre-Web-Automated/blob/v4.0.6/cps/web.py#L172-L176),
[mode](https://github.com/crocodilestick/Calibre-Web-Automated/blob/v4.0.6/cps/__init__.py#L164-L166), and
[identifier](https://github.com/crocodilestick/Calibre-Web-Automated/blob/v4.0.6/cps/cw_login/utils.py#L414-L433).
