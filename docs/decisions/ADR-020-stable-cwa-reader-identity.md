# ADR-020: Stable CWA reader identity

- Status: Accepted
- Date: 2026-10-02
- Amends: [ADR-004](ADR-004-authentication-boundaries.md), [ADR-013](ADR-013-stock-reader-connectors.md)

## Context

The managed CWA connector derived its tenant from a hash of the forwarded
`session`/`remember_token` cookies. ADR-004 accepted that logging out or
rotating a CWA session would create a cold cache tenant.

CWA 4.0.6 re-signs its permanent Flask session on every response, so the
`session` value changes every second. Each session exchange therefore created
a new tenant. Server cache entries were reused for at most one five-minute
plugin session, and per-book glossaries, which are stored by tenant,
disappeared after the next exchange. Kavita was unaffected because its tenant
derives from the account id returned by `/api/Account`.

## Decision

During `POST /session`, the CWA connector reads the user id claimed by each
forwarded credential: `_user_id` in the Flask `session` payload, or `user` in
the `remember_token` payload. It then probes `/ajax/emailstat` with only
that one cookie. If CWA accepts the probe, CWA has verified that cookie's
signature and authenticated the user it names, and the tenant becomes an HMAC
of `cwa-user:<id>` under the connector key.

- The session cookie is tried first; a rejected session falls back to a
  `remember_token`-only probe. Each probe still uses the browser's address and
  User-Agent binding.
- If every forwarded cookie claims a user and all probes are rejected, the
  exchange is rejected without a wider probe.
- Cookies whose format is not recognized keep the previous combined probe and
  per-credential tenant, so unknown CWA versions are never worse off.
- Payloads are decoded with inflation and size bounded to 64 KiB before any
  probe and are never persisted, logged or forwarded to the browser.
- For both connectors the subject HMAC uses a private `tenant_key` beside the
  session key instead of the session key itself. `btctl uninstall` removes the
  session key to revoke browser sessions, which previously also changed every
  tenant on reinstall or upgrade. The tenant key is kept with the translation
  data. A missing tenant key is created from the session key only when that
  key already existed, so an install restarted in place keeps its tenants and
  a new install gets an independent key.

## Consequences

- One CWA user keeps one cache and glossary tenant across cookie rotation,
  logout and devices, matching Kavita; in the hub both also keep them across
  reinstall and upgrade.
- An install upgraded from a pre-2.4.1 runtime via uninstall/install loses its
  tenants once, because that runtime never created a tenant key before
  uninstall deleted the session key.
- Split and Unraid installs regenerate `BT_READER_CONNECTOR_ID` per install,
  so their tenants still change on reinstall; only the hub, whose connector id
  is fixed in the operator environment, keeps them.
- An exchange can make two authority probes instead of one.
- The connector now depends on CWA keeping Flask/itsdangerous cookie payloads.
  A format change degrades to the previous per-credential tenant rather than
  failing closed or guessing.
- Existing per-session CWA cache entries are orphaned and age out through the
  normal TTL and cap.
- CWA and Kavita user ids are autoincrement database keys. If a reader's
  database is recreated while `BT_READER_CONNECTOR_ID` and `tenant_key` are
  kept, a new account can reuse an old id and inherit that tenant; rotate the
  connector id or delete `tenant_key` with the reader.
- `tenant_key` outlives session revocation by design, so retained cache rows
  stay linkable to reader accounts by anyone who can read the data directory.
- The legacy `cwa_session` authentication mode still hashes cookies per
  request and is not changed by this decision.
