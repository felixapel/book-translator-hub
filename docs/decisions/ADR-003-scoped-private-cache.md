# ADR-003: Use an atomically scoped private cache

- Status: Accepted
- Date: 2026-07-12
- Amended by: [ADR-009](ADR-009-side-by-side-cache-schemas.md), [ADR-020](ADR-020-stable-cwa-reader-identity.md)

## Context

The original paragraph cache keyed only model, text, and languages. Translation
also depends on provider identity, prompt/protocol, surrounding group context,
book/chapter, and user. Partial hits removed paragraphs before the remaining
prompt was built. SQLite and browser storage had unlimited retention by default
and preserved readable source text.

## Decision

Use schema v2 and never serve v1 rows as v2. A key covers tenant, book, chapter,
provider, model, prompt/protocol fingerprint, exact group context, languages,
and source content. Persist only hashed tenant/book/chapter identifiers and the
translated result. A batch group is served only when every member is present
under one contract; otherwise the original group is translated atomically.

TTL and row caps are positive mandatory settings. New cache directories use
`0700`; DB/WAL/SHM use `0600`. Hit counters are buffered so normal hits do not
commit SQLite writes. Browser persistence is disabled by default and uses the
same release/language/book/chapter plus DOM-position separation when explicitly
enabled.

## Consequences

- Existing `translations` v1 rows remain cold under v2. The physical table is
  left intact for rollback; schema-v2 rows live in `translations_v2` as defined
  by ADR-009. Operators may remove v1 only after accepting the new release and
  its rollback window.
- Context-sensitive correctness and tenant privacy take precedence over reuse
  across unrelated books or request groupings.
- The server-owned authenticated subject is the tenant namespace. CWA sessions
  and trusted forwarded identities are isolated; shared-token mode deliberately
  shares one tenant and anonymous mode is explicit development-only behavior.
  ADR-020 narrows a CWA tenant from one session to one verified CWA user.
