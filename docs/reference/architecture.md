# Architecture Overview

This document details the architecture of the `book-translator` plugin.

## Architecture decisions

The indexed [architecture decision records](../decisions/README.md) preserve
the security, release, cache and deployment rationale, including superseded
decisions.

## Overview

The plugin operates as a decoupled overlay in front of a stock reader. Reader
connectors isolate upstream-specific routes, DOM and authentication from the
translation/cache/provider core. There are three deployment profiles:

1. **Universal hub (`btctl`, recommended).** One non-root container runs a
   distinct Gunicorn API process per enabled reader plus one namespaced nginx
   listener per reader. APIs bind only to loopback and use separate databases,
   session keys, connector IDs and cookies. `BT_ENABLE_CWA` and
   `BT_ENABLE_KAVITA` enable either or both without changing the image. Any
   child exit stops the complete container. This simplifies deployment at the
   cost of one shared compromise and restart boundary. See
   [ADR-015](../decisions/ADR-015-universal-reader-hub.md).
2. **Decoupled reverse proxy architecture (SWAG / master proxy).** The master
   reverse proxy fronts stock reader instances (CWA, Kavita) directly on their
   native ports and injects `<script src="/bt-static/loader.js"></script>` via
   HTTP `sub_filter`. The translation hub operates in pure API mode
   (`BT_ROLE=api`, `CWA_UPSTREAM=""`) serving `/bt-api/` and static assets under
   `/bt-static/`. This fully isolates reader uptime from translator lifecycle
   and eliminates redundant internal proxy worker overhead. See
   [ADR-018](../decisions/ADR-018-decoupled-reverse-proxy-and-draggable-controls.md).
3. **Managed split profile (`btctl`, advanced isolation).** Two isolated non-root
   containers run the same release image with `BT_ROLE=proxy` and
   `BT_ROLE=api`. nginx sits in front of a **stock** CWA or pinned Kavita
   instance (`BT_READER_UPSTREAM`). HTML responses get a
   single `<script src="/bt-static/loader.js">` tag injected before `</head>`;
   `loader.js` self-guards to one certified reader route and loads the overlay.
   CWA uses its `/read/` EPUB route and iframe/EPUB.js adapter. Kavita v0.9.0.2
   uses the exact `/library/:libraryId/series/:seriesId/book/:chapterId` route
   and top-level `.book-content` adapter. The API is
   reachable same-origin under `/bt-api/`, so CORS never applies. Because only
   one tag is injected instead of maintaining a reader fork, upstream template
   updates become explicit compatibility events. See `proxy/nginx.conf.template`
   and `docker-entrypoint.sh`.

   In managed native-reader mode, raw reader proof is forwarded only to exact
   `POST /bt-api/session`. `reader_session.py` allowlists CWA cookies, a Kavita
   native access bearer, or exact Kavita OIDC cookie chunks and validates the
   pinned account endpoint. It persists none of that proof. The response is a
   random, HttpOnly, SameSite-strict plugin cookie valid for at most five
   minutes and bound to connector, origin, proxy-observed address and
   User-Agent. Ordinary API locations remove Authorization and reduce cookies
   to that plugin cookie. `BT_PUBLIC_ORIGIN` fixes the forwarded host/scheme;
   inbound forwarding headers are discarded, the observed peer becomes the
   only forwarded client hop, and reader uploads have an operator-configurable
   finite body cap.
3. **Community Applications combined profile (listing-gated).** One non-root
   container runs `BT_ROLE=all`, exposes only its proxy port and keeps API port
   `8390` private. This is production-supported only when the searchable
   listing pins a certified immutable image digest and the host matches the
   [documented CWA-only boundary](../install/community-applications.md).
   Kavita uses the universal hub or split `btctl` profile.

`overlay/read.html` remains a legacy development fixture for investigating CWA
template compatibility. Mounting it into CWA and publishing the API
cross-origin is not a supported production installation method.

The hub preserves the logical proxy/API boundary in separate processes and
reader namespaces. The split profile places those processes in separate
containers when stronger containment or Authentik direct API routing is needed.

```
Browser ──► proxy role (:8080) ──► stock reader (CWA :8083 / Kavita :5000)
                │
                ├── /bt-api/session ──► native proof validation
                └── /bt-api/* ──► API role (:8390) ──► LLM provider
                                         │
                                         └── SQLite volume (/app/data)
```

## Deployment control plane

`btctl` validates a strict environment file and derives an immutable local
image identity from the reader type, `VERSION`, and the clean checkout SHA.
CWA and Kavita use distinct local image repositories so concurrent connector
instances cannot move each other's immutable tag. The deterministic `plan`
declares every resource and its ownership before Docker is touched. Lifecycle
state is private, atomic, schema-versioned, and contains no secrets. This
separates source, configuration, mutable translation data, and backups so an
update of one cannot silently replace another.

Both runtime roles also join the external reader network, so their Docker
container names are intentionally not used for the authenticated internal hop.
The proxy targets `translator-api` and the API trusts `translator-proxy`; those
aliases exist only on the owned private network.

The Compose adapter writes a private JSON-form Compose document (JSON is a
Compose-compatible YAML subset), validates it with the local Compose plugin,
and starts only the two translator services. Live image IDs, installation
labels, networks, health, and port bindings must match before state is
committed. Recovery adoption is read-only with respect to Docker and requires
the same pre-existing evidence. Migration recovery performs that adoption
before it can preserve or rename a target tree, so a crash after runtime start
cannot detach an active API container from its bind source.

## Component Breakdown

### Frontend (`loader.js` and `translator.js`)

- **Bootstrap and session exchange**: `loader.js` validates the server-owned
  browser contract and exact current route before exchanging native reader
  proof or loading overlay assets. It observes SPA history changes and remains
  inert on Kavita manga/PDF and unrelated pages.
- **Reader adapters**: CWA uses iframe document checking and `epub.js`
  rendition hooks (`relocated`, `rendered`). Kavita discovers only the current
  `.book-content`, derives book/chapter cache scope from numeric route segments,
  observes Angular DOM replacement and tears down on navigation.
- **Translation Management**: Coordinates Instant Viewport Rush (concurrent
  micro-batches for top 3 visible paragraphs), real-time Server-Sent Events
  (SSE) token streaming for the primary visible paragraph (~160ms TTFT), and
  Zero-Wait Directional Lookahead prefetching. Background whole-chapter prefetch
  is managed through a bounded, non-secret server-owned browser contract. Only
  explicit pre-provider admission `429`s are replayed automatically.
- **Client Cache**: Dual-layer architecture combining instant in-memory maps,
  synchronous `localStorage` preference settings, and high-capacity asynchronous
  `IndexedDB` (`BookTranslatorDB` / `translations_v1`) for whole-book offline
  caching without browser quota errors. Keys include release, languages, book,
  chapter, and stable DOM position so repeated text in different literary contexts
  cannot collide.

### Backend (`book-translator-api`)
- **Authentication (`auth.py`, `reader_session.py`)**: Fails closed in token,
  managed reader-session, legacy CWA-session, or trusted-forwarded mode before
  cache/provider work. Native reader proof is isolated to exchange; opaque
  sessions are in-memory, bounded and short-lived. Subjects become connector-
  scoped hashes and never expose upstream user ids to cache or metrics.
- **Flask Server (`server.py`)**: Exposes translation endpoints `/translate`,
  `/translate/batch`, and real-time SSE streaming `/translate/stream` along with
  metrics and health probes. Only shallow
  liveness/readiness routes bypass authentication. Observability uses a fixed
  schema for HTTP classes and bounded auth, admission, provider, deadline, and
  partial-batch outcomes; it never creates labels from request or book data.
- **SQLite Cache (`cache.py`)**: Schema v2 keys include tenant, book, chapter,
  provider, model, prompt/protocol fingerprint, group context, languages, and
  source hash. Source paragraphs and raw tenant/book/chapter identifiers are not
  stored. TTL/cap are mandatory and group hits are atomic, so cached paragraphs
  cannot alter the context seen by a later provider call.
- **LLM Client (`translator.py`)**: Multi-provider wrapper that supports batch
  translation prompts with dynamic context windows (`BT_CONTEXT_WINDOW`).
  Stable document-order grouping is bounded by paragraph count and an optional
  deterministic source-token budget; fixed provider-attempt and `429` counters
  allow operators to evaluate request reduction without content-derived labels.
  Named cloud adapters own fixed HTTPS endpoints and server-side credentials;
  `local` and public `openai-compatible` endpoints are explicit alternatives,
  not mandatory fallback layers. The hub may inherit one provider policy for
  both readers or apply isolated per-reader overrides.
  Remote fallback providers require explicit consent on each request; requests
  with different consent policies never share cache lookup or in-flight work.
