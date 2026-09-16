# Architecture decision records

ADRs preserve decisions even when later records amend or supersede them.
Statuses describe the decision lifecycle; Git history preserves the original
discussion.

- [ADR-001: Gitea release authority](ADR-001-gitea-release-authority.md)
- [ADR-002: Split non-root runtime roles](ADR-002-split-non-root-runtime-roles.md)
- [ADR-003: Scoped private cache](ADR-003-scoped-private-cache.md)
- [ADR-004: Authentication boundaries](ADR-004-authentication-boundaries.md)
- [ADR-005: Cloud fallback consent](ADR-005-cloud-fallback-consent.md)
- [ADR-006: Explicit proxy authority](ADR-006-explicit-proxy-authority.md)
- [ADR-007: Signed release digests](ADR-007-sign-release-digests.md) — superseded.
- [ADR-008: Source-only releases](ADR-008-source-only-releases.md) — partially superseded.
- [ADR-009: Side-by-side cache schemas](ADR-009-side-by-side-cache-schemas.md)
- [ADR-010: `btctl` state and ownership](ADR-010-btctl-state-and-ownership.md)
- [ADR-011: Containerized Unraid bootstrap](ADR-011-containerized-unraid-bootstrap.md)
- [ADR-012: Community Applications image](ADR-012-community-applications-image.md)
- [ADR-013: Stock reader connector contract](ADR-013-stock-reader-connectors.md)
- [ADR-014: Configurable provider backends](ADR-014-configurable-provider-backends.md)
- [ADR-015: Universal one-container reader hub](ADR-015-universal-reader-hub.md)
- [ADR-016: Private raw Compose environments](ADR-016-private-compose-environments.md)
- [ADR-017: Adaptive cloud batching and explicit replay safety](ADR-017-adaptive-cloud-batching.md)
- [ADR-018: Decoupled reverse proxy architecture and draggable reader controls](ADR-018-decoupled-reverse-proxy-and-draggable-controls.md) — superseded.
- [ADR-019: Managed proxy boundaries for reader authentication](ADR-019-managed-proxy-boundaries.md)

New ADRs use the next number and include a single `Status` value (`Proposed`,
`Accepted`, `Deprecated` or `Superseded`) plus an ISO date. Amendments and
partial supersession belong in separate metadata lines and links.
