# ADR-019: Managed proxy boundaries for reader authentication

- Status: Accepted
- Date: 2026-09-16
- Supersedes: [ADR-018](ADR-018-decoupled-reverse-proxy-and-draggable-controls.md)
- Amends: [ADR-004](ADR-004-authentication-boundaries.md), [ADR-013](ADR-013-stock-reader-connectors.md)

## Context

ADR-018 described an external master proxy that injects the reader loader and
routes `/bt-api/` to a pure API role. That pattern can preserve an independent
reader route during translator maintenance, but it did not define the
authentication, HTTPS, cookie, forwarding-header, compression or browser
acceptance conditions that make the route safe. It also made an availability
claim that cannot be established by an architecture document.

The maintained hub and split profiles already provide a reviewed same-origin
boundary: the API is private, native reader proof reaches only the exact
session-exchange route, and normal translation requests carry only the
short-lived opaque plugin cookie.

## Decision

The universal hub remains the recommended managed topology. The split profile
remains the managed option when separate containers or the reviewed Authentik
forwarded-identity profile are required.

An operator may build an external proxy integration, but it is outside the
managed and certified contract until it has its own reviewed configuration and
acceptance evidence. It must, at minimum:

- keep the translation API unpublished and reachable only through the intended
  same-origin HTTPS route;
- preserve exact public-origin checks and opaque-session cookie attributes;
- ensure reader proof reaches only `POST /bt-api/session`, and strip native
  credentials from ordinary translation routes;
- remove browser-supplied forwarding and identity headers before establishing
  any trusted values;
- validate loader injection against response compression, route scope and one
  loader per page; and
- prove the exact reader, browser, proxy, commit and image through the browser
  acceptance checklist before promotion.

No external routing design guarantees reader availability during an outage or
upgrade. Operators remain responsible for their proxy, reader and network
failure boundaries.

The shared-memory heartbeat configuration and draggable controls from ADR-018
remain valid implementation decisions; this ADR supersedes only its external
reverse-proxy deployment guidance.

## Consequences

- Documentation describes the supported managed profiles before any
  operator-owned external integration.
- An external proxy route cannot be presented as a security, availability or
  release-acceptance shortcut.
- A future supported external-proxy profile requires its own generated
  configuration, tests and physical browser evidence.

## Verification

The existing managed contracts cover exact-origin session exchange, credential
stripping, bounded opaque sessions and browser acceptance requirements. They do
not certify an arbitrary external proxy configuration.
