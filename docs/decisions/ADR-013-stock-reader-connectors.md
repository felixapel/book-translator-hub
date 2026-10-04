# ADR-013: Extend the overlay through stock reader connectors

- Status: Accepted
- Date: 2026-08-08
- Amends: [ADR-002](ADR-002-split-non-root-runtime-roles.md)
- Amends: [ADR-004](ADR-004-authentication-boundaries.md)
- Amended by: [ADR-020](ADR-020-stable-cwa-reader-identity.md)

## Context

The proxy-injected overlay was coupled to CWA route, iframe and authentication
details even though translation, caching and provider work are reader-neutral.
Adding Kavita by forking its frontend or writing translated books would create
a second application to maintain, broaden the data-loss boundary and make
upstream upgrades difficult to certify.

Kavita's SPA also differs materially from CWA: its EPUB page uses a top-level
`.book-content` container and native clients may hold a bearer token, while an
OIDC deployment may authenticate with ASP.NET cookie chunks. Passing either
credential to every translation request would unnecessarily expose reader
authority to the translation API.

## Decision

- Keep each upstream reader stock. One reader-neutral proxy injects the loader;
  explicit adapters own route recognition, DOM discovery, cache scope and SPA
  activation/deactivation.
- Schema-2 lifecycle state records `reader_type`, exact upstream/container/
  network/version and one fixed connector contract. Schema-1 CWA state remains
  readable, but CWA and Kavita installations never share ownership or data.
- The first Kavita contract is exactly v0.9.0.2 at commit
  `6bcd5689385d0e96824982d843c54f15ce784ddc`, EPUB route
  `/library/:libraryId/series/:seriesId/book/:chapterId` and `.book-content`.
  Manga, PDF, writeback and unpinned versions fail closed or remain inert.
- Managed reader authentication exchanges native CWA cookies, a Kavita access
  token, or exact Kavita OIDC cookie chunks only at `POST /bt-api/session`.
  The upstream proof is validated against the reader account endpoint and is
  never persisted. Ordinary API routes receive only an opaque random cookie
  with a maximum five-minute lifetime, exact-origin protections, and observed
  address/User-Agent binding.
- Non-loopback reader-session deployments require HTTPS. Provider credentials
  remain server-side and cache records retain only opaque/hash scopes.
- CWA and Kavita use separate `btctl` instances. The existing Community
  Applications combined profile remains CWA-only. Immutable local image names
  are reader-scoped (`local/cwa-translate` and `local/kavita-translate`) so a
  build for one connector cannot retag the image beneath the other instance.
- The proxy reaches the API only through the `translator-api` alias on the
  owned private network. It never targets the multi-homed API container name,
  which Docker can resolve on the external reader network and thereby bypass
  the exact trusted-proxy peer identity.

## Consequences

- Translation/provider code can support additional readers without weakening
  each reader's explicit compatibility and authentication boundary.
- A reader UI or account-contract change is a release-blocking compatibility
  event rather than an implicit best-effort promise.
- In-memory sessions intentionally disappear when the API restarts and renew
  frequently. The browser can exchange fresh reader proof without retaining a
  translator credential in JavaScript-accessible storage.
- Live overlay translation does not alter the underlying EPUB or Kavita data.
  Export, writeback and mobile/offline display require separate designs.

## Verification

Unit tests cover schema migration, exact configuration rejection, proof
allowlists, account probes, bounded sessions, cache scopes, DOM teardown and
one safe `401` replay. Real Chromium covers CWA and Kavita routes, DOM behavior
and SPA isolation. The runtime smoke builds the image and exercises stock-reader
proxying, native CWA and Kavita exchange, credential stripping and binding
failure. Physical stock-Unraid and real-Kavita browser acceptance remains a
promotion gate.
