# ADR-021: Opt in to exact-edge client IP trust

- Status: Accepted
- Date: 2026-10-13
- Amends: [ADR-006](ADR-006-explicit-proxy-authority.md)
- Retains: [ADR-019](ADR-019-managed-proxy-boundaries.md)

## Context

Without an explicit edge trust contract, all browsers behind one reverse proxy
share that proxy's pre-auth admission key. Changing the address forwarded to
CWA also changes its strong-session context. These two uses must receive the
same observed address and exact User-Agent.

## Decision

`BT_EDGE_TRUSTED_PROXIES` is optional and empty by default. It identifies only
operator-controlled immediate edge peers for the managed nginx reader listener.
It accepts comma-separated exact IPv4/IPv6 addresses or `/32` and `/128` host
networks, with spaces around list elements. Validation rejects broad networks,
DNS names, wildcards, Unix sockets, scoped addresses, control characters, empty
elements and invalid addresses. Input is bounded to 4096 characters and 32
distinct peers. Addresses are canonicalized, deduplicated and sorted before
rendering or fingerprinting; rejected values are not included in errors.
Whole-value padding is rejected; spaces between list elements are allowed.

For configured peers, each reader listener renders `set_real_ip_from` entries,
`real_ip_header X-Forwarded-For` and `real_ip_recursive off`. The controlled edge
must emit its observed browser address as the final token in the final outgoing
XFF field, preferably normalizing to one field.
Additional CDN or proxy hops require a separate reviewed contract. A shared
Docker network alone grants no trust.

Reader, exact session exchange and ordinary API routes overwrite XFF and
X-Real-IP with the same resulting nginx address and preserve the exact
User-Agent. Untrusted peers cannot alter that address through request headers.
Absent or unparseable final addresses fall back to the socket peer under
nginx's address grammar. RealIP accepts some irregular spacing and trailing
commas; it does not validate the entire chain. Forwarded identity assertions
remain stripped.
The configured CWA login header is retained for hub restarts. Its name must not
alias managed context or credential headers, including User-Agent, so stripping
cannot compete with a required upstream assignment.

This setting is distinct from API-side `BT_TRUSTED_PROXIES`,
`BT_TRUSTED_PROXY_HOST`, legacy `BT_TRUST_PROXY`, identity allowlists and
`BT_EDGE_NETWORK`. Split, hub and standalone configurations pass it only to the
managed proxy boundary. Empty managed configuration preserves historical
fingerprints and retained environments; a selected trust set changes the
deployment fingerprint and appears in non-secret plans.

## Consequences

Enabling, changing or clearing this setting changes CWA and plugin-session
address binding. Operators must log in again and exchange a fresh plugin
session. Authenticated subject quotas, request budgets, consent and coalescing
keep their existing contracts.

An exact address is only as exclusive as the network path to it. Docker relays
host and other-network connections to a published port from the network
gateway, so trusting the gateway trusts all of them. The renderer warns when a
trusted peer is the container's default gateway, but does not refuse it: with
the reader port published on `127.0.0.1` only, the gateway carries just the
host's own connections. Unspecified, multicast and IPv4-mapped addresses are
rejected because they can never match an immediate peer.

This does not certify an arbitrary external edge or additional proxy chain.
Exact stock-reader, proxy and physical browser acceptance remains required
before a runtime release tag, as specified by ADR-019 and the release runbook.

## Implementation and verification

Implement validation and managed propagation first, then directive rendering,
actual-nginx adversarial coverage and operator documentation. Each increment
must pass its focused tests before the next. Independent auth/proxy review and
all protected exact-head CI gates precede merge.

Tests cover empty defaults and old configuration, bounded rejection before
output writes, canonical plans and fingerprints, both reader listeners,
untrusted and forged-leading XFF, duplicate/empty/malformed fields, IPv4/IPv6
header addresses and ports. The candidate's shipped nginx must include HTTP
RealIP support and pass configuration validation. Actual nginx and API tests
prove separate browser admission keys, identical IP/UA across reader and API
routes, CWA strong-session login/exchange/translation, failed changed bindings,
and credential/cookie isolation. A real Chromium flow through a controlled
fixture edge and the candidate nginx/API covers login, fresh plugin proof,
translation, chapter navigation and OFF reload without intercepting API routes.
The test-only reader DOM does not certify a stock EPUB implementation. Automated
contract fixtures complement the
physical stock-reader release gate.

The nginx selection contract is documented in the
[HTTP RealIP module](https://nginx.org/en/docs/http/ngx_http_realip_module.html).
