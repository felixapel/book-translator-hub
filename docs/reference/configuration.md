# Configuration reference

Managed operators should copy the template for their topology outside the
checkout and edit only that copy. The recommended universal topology uses
[`.env.hub.example`](../../.env.hub.example); the advanced split topology uses
[`.env.example`](../../.env.example). `btctl` validates the file, derives
internal role settings and redacts secrets from its output. Never commit the
edited file.

Set `BT_TOPOLOGY=hub`, `BT_ENABLE_CWA` and/or `BT_ENABLE_KAVITA`, then provide
each enabled reader's `BT_<READER>_PUBLIC_ORIGIN`, `READER_UPSTREAM`,
`READER_CONTAINER`, `READER_NETWORK`, `READER_VERSION`, `AUTH_PROFILE`,
`READER_CONNECTOR_ID` and `PUBLISHED_PORT`. Hub authentication is exactly
`reader-session`; Authentik-forwarded identity remains split-only.

Shared `LLM_*` and `BT_LOCAL_URL` values apply to every enabled reader. A
present `BT_CWA_LLM_*`/`BT_CWA_LOCAL_URL` or
`BT_KAVITA_LLM_*`/`BT_KAVITA_LOCAL_URL` replaces the corresponding shared
value for only that reader. Present empty secrets deliberately clear inherited
secrets. `BT_MAX_CONCURRENT` and `BT_MAX_UPSTREAM_INFLIGHT` are total hub
budgets; either omit reader allocations for an automatic fair split or define
all enabled-reader allocations without exceeding the total.

## Managed installation inputs

| Variable | Purpose |
|---|---|
| `BT_INSTALL_PROFILE` | `unraid` or `compose-existing`. The stock reader always remains external. |
| `BT_INSTALL_NAME` | Stable prefix for owned translator resources. Choose once. |
| `BT_INGRESS_MODE` | `published` exposes only the proxy; `docker-edge` publishes neither role. |
| `BT_PROXY_PORT` | Host port for the proxy in `published` mode. |
| `BT_EDGE_NETWORK` | Existing edge network required by `docker-edge`. |
| `BT_AUTH_PROFILE` | `cwa-session` for CWA, `reader-session` for Kavita, or CWA-only `authentik-forwarded`. |
| `BT_PUBLIC_ORIGIN` | Exact browser-facing origin, including scheme and optional port. |
| `BT_READER_TYPE` | `cwa` or `kavita`; defaults to `cwa` for schema-1 configuration compatibility. |
| `BT_READER_UPSTREAM` | Exact stock reader origin: CWA port `8083` or Kavita port `5000`. |
| `BT_READER_CONTAINER` | Exact running stock reader container name. |
| `BT_READER_NETWORK` | One existing Docker network joined by the reader. |
| `BT_READER_VERSION` | Exact reader version observed by the install. Kavita accepts only `0.9.0.2`. |
| `BT_READER_IMAGE_ID` | Optional exact `sha256:<64 lowercase hex>` runtime image ID. Required when the reader container uses a mutable tag and has no exact application-version label. |
| `BT_CWA_IDENTITY_HEADER` | Exact reverse-proxy identity header configured in CWA; the managed proxy strips client copies. |
| `BT_STATE_DIR` | Private lifecycle state outside the checkout. |
| `BT_DATA_DIR` | Private translation data outside the checkout. |
| `BT_BACKUP_DIR` | Backup root outside appdata/data. |
| `BT_UNRAID_TEMPLATE_DIR` | DockerMan user-template directory for the Unraid profile. |
| `LLM_PROVIDER` | `local`, a fixed named adapter, or `openai-compatible`. The split example defaults to local; the hub example selects Gemini explicitly. |
| `LLM_MODEL` | Provider model identifier. |
| `BT_LOCAL_URL` | Shared absolute `/v1/chat/completions` endpoint when either role is `local`. |
| `LLM_API_KEY` | Primary named-provider credential; empty for `local` and `openai-compatible`, which uses its dedicated key variable. |
| `LLM_CUSTOM_ENDPOINT` | Primary `openai-compatible` public HTTPS URL with exact `/v1/chat/completions` path. |
| `LLM_CUSTOM_API_KEY` | Dedicated primary custom-endpoint credential. |
| `LLM_FALLBACK_PROVIDER` | Optional distinct fallback provider. |
| `LLM_FALLBACK_MODEL` | Required with a fallback provider. |
| `LLM_FALLBACK_API_KEY` | Fallback named-provider credential. |
| `LLM_FALLBACK_CUSTOM_ENDPOINT` | Dedicated fallback custom endpoint. |
| `LLM_FALLBACK_CUSTOM_API_KEY` | Dedicated fallback custom credential. |
| `BT_IDENTITY_PROXY_IP` | Exact `/32` or `/128` trusted identity peer for Authentik mode. |
| `BT_AUTHENTIK_VERSION` | Exact accepted Authentik version for the advanced profile. |
| `BT_AUTHENTIK_OUTPOST_URL` | Private outpost URL for generated edge configuration. |
| `BT_REVERSE_PROXY` | Supported edge type for `auth-snippet`. |
| `BT_LEGACY_CONTAINER` | Exact combined legacy container used only by `upgrade`. |
| `BT_LEGACY_DATA_DIR` | Its exact bind-mounted data directory. |

For CWA only, `CWA_UPSTREAM`, `BT_CWA_CONTAINER`, `BT_CWA_NETWORK` and
`BT_CWA_VERSION` are accepted as legacy aliases. Do not set both forms to
different values. Kavita forbids every CWA alias and requires an empty
`BT_CWA_IDENTITY_HEADER`. See the [Kavita guide](../install/kavita.md) for a
complete isolated environment.

## Translation runtime settings

These variables are the supported operator-facing part of the low-level image
interface. Managed installs derive authentication, role, network and browser
settings; override tuning values only with measured evidence. Variables not
listed here are internal implementation details, not additional supported
configuration.

| Variable | Default | Boundary |
|---|---:|---|
| `BT_ROLE` | `auto` | `hub` for the universal topology; `api` or `proxy` for managed split installs; `all` only for the certified, digest-pinned Community Applications profile. |
| `PORT` | `8390` | API listen port inside the container. |
| `BT_PROXY_PORT` | `8080` | Proxy listen port inside the container. |
| `BT_CWA_MAX_BODY_SIZE` | `2g` | Finite nginx upload limit for CWA traffic. |
| `BT_MAX_CONCURRENT` | `2` | Group workers inside one batch request. |
| `BT_BATCH_SIZE` | `5` | Maximum paragraphs per browser request after the first visible paragraph and per grouped provider call. Managed browser values are restricted to `1..50`. |
| `BT_BATCH_SOURCE_TOKEN_BUDGET` | `0` | Approximate source-token ceiling per provider group. `0` preserves count-only grouping; a paragraph over the ceiling is allowed only as a singleton. |
| `BT_CLIENT_PREFETCH_GAP_MS` | `0` | Delay between starts of opt-in background prefetch requests, restricted to `0..10000`. Visible translation is never paced by this setting. |
| `BT_MAX_BATCH_PARAGRAPHS` | `50` | Maximum paragraphs accepted in one batch request. Managed installs require `BT_BATCH_SIZE` not to exceed this API limit. |
| `BT_MAX_PARAGRAPH_CHARS` | `8000` | Maximum characters accepted in one paragraph; input is rejected, never truncated. |
| `BT_CACHE_SCOPE_MAX_CHARS` | `512` | Maximum length of each book/chapter scope input before hashing. |
| `BT_MAX_CONTENT_LENGTH` | `2097152` | WSGI request-body ceiling in bytes. |
| `BT_MAX_TOKENS` | `4096` | Single-paragraph output ceiling. |
| `BT_BATCH_MAX_TOKENS` | `8192` | Grouped output ceiling. |
| `BT_OUTPUT_TOKEN_FACTOR` | `2.0` | Proportional output cap. |
| `BT_OUTPUT_TOKEN_FLOOR` | `256` | Minimum requested output tokens. |
| `BT_CONTEXT_WINDOW` | `0` | Surrounding paragraphs included as non-translated context. |
| `BT_TIMEOUT` | `60` | Per-provider-call timeout in seconds. |
| `BT_MAX_UPSTREAM_INFLIGHT` | `2` | Process-wide provider concurrency cap. |
| `BT_UPSTREAM_QUEUE_TIMEOUT` | `2` | Wait for a provider slot before `503`. |
| `BT_MAX_UPSTREAM_RESPONSE_BYTES` | `1048576` | Maximum decompressed provider response. |
| `BT_REQUEST_MAX_ATTEMPTS` | `20` | Total provider calls, including bounded malformed-envelope recovery. |
| `BT_REQUEST_MAX_INPUT_BYTES` | `5000000` | Cumulative prompt bytes per API request. |
| `BT_REQUEST_MAX_OUTPUT_TOKENS` | `163840` | Cumulative reserved output tokens per API request. |
| `BT_REQUEST_DEADLINE_SECONDS` | `90` | Absolute request deadline. |
| `BT_SINGLEFLIGHT_MAX_ENTRIES` | `1024` | Active deduplicated operation cap. |

The certified Community Applications profile is CWA-only. Kavita uses the
universal hub or separate managed `api` and `proxy` roles.

A malformed grouped response receives one fresh-envelope retry. If it remains
malformed, paragraphs are recovered sequentially inside the same finite request
budget. Successful paragraph recovery is not written under the grouped cache
contract. Ordinary paragraph failures are returned per paragraph; exhausted
work budgets fail the request before starting more provider work.

`BT_BATCH_SIZE` is a maximum, not a promise. The reader requests the first
visible paragraph alone, then uses the configured maximum. The API greedily
forms stable groups in document order and stops at either the count limit or
`BT_BATCH_SOURCE_TOKEN_BUDGET`. Its deterministic estimate accounts for denser
CJK scripts. A single long paragraph is never truncated merely to satisfy the
group budget. Setting the source budget to `0` is the immediate compatibility
rollback.

Successful batch responses add aligned `error_codes` and
`retry_after_seconds` arrays. A terminal provider `429` becomes the sanitized
per-segment code `provider_rate_limited`; the reader does not replay it
automatically because provider admission may already have occurred. HTTP `429`
responses are automatically retried only when the API explicitly returns
`retry_safe: true` with an `api_admission` or `auth_admission` scope.

Both the recommended universal hub and advanced split `btctl` topology validate
these relationships during `plan`, propagate the API controls only to the API
process and expose only batch size/prefetch pacing to the browser proxy. The
standalone Compose example accepts the same environment names.

## Authentication and network settings

| Variable | Default | Boundary |
|---|---:|---|
| `BT_AUTH_MODE` | `token` | `reader_session`, legacy low-level `cwa_session`, `forwarded`, `token` or development-only `disabled`. Managed native-reader installs derive `reader_session`. |
| `BT_ALLOW_INSECURE_AUTH` | `false` | Second acknowledgement required for `disabled`; never use in production. |
| `BT_API_TOKEN` | empty | Required by token mode; shared tenant and compatibility use only. |
| `BT_CWA_AUTH_URL` | empty | Exact CWA `/ajax/emailstat` URL for session validation. |
| `BT_CWA_AUTH_COOKIE_NAMES` | `session,remember_token` | Only cookies permitted to leave the API for the CWA probe. |
| `BT_CWA_AUTH_TIMEOUT_SECONDS` | `2` | Probe timeout. |
| `BT_CWA_AUTH_CACHE_TTL_SECONDS` | `15` | Short positive and negative validation-cache TTL. |
| `BT_CWA_AUTH_CACHE_MAX_ENTRIES` | `10000` | Validation-cache cap. |
| `BT_CWA_AUTH_MAX_INFLIGHT` | `8` | Distinct concurrent CWA probe cap. |
| `BT_CWA_AUTH_MAX_RESPONSE_BYTES` | `262144` | Maximum decompressed CWA probe response. |
| `BT_READER_TYPE` | empty | Managed `reader_session` reader contract: `cwa` or `kavita`. |
| `BT_READER_AUTH_URL` | empty | Exact managed account probe: CWA `/ajax/emailstat` or Kavita `/api/Account`. |
| `BT_READER_VERSION` | empty | Exact version included in the fail-closed browser and API contract. |
| `BT_READER_CONTRACT_VERSION` | empty | Derived connector contract; never operator-invented. |
| `BT_READER_CONNECTOR_ID` | empty | Installation UUID binding opaque sessions to one connector. |
| `BT_READER_AUTH_TIMEOUT_SECONDS` | `2` | Timeout for the native reader account probe. |
| `BT_READER_AUTH_MAX_RESPONSE_BYTES` | `262144` | Maximum decompressed reader account response. |
| `BT_SESSION_KEY_PATH` | `/app/data/reader_session_key` | Private owned 256-bit connector key created by lifecycle code. |
| `BT_SESSION_TTL_SECONDS` | `300` | Opaque session lifetime; values above five minutes are rejected. |
| `BT_SESSION_MAX_ENTRIES` | `10000` | In-memory opaque session cap. |
| `BT_IDENTITY_TRUSTED_PROXIES` | empty | Exact trusted peers for forwarded identity. |
| `BT_AUTH_RATE_LIMIT_PER_MINUTE` | `300` | Pre-authentication attempts per observed client. |
| `BT_AUTH_MAX_INFLIGHT_PER_CLIENT` | `2` | Concurrent pre-authentication requests per observed client. |
| `BT_RATE_LIMIT_PER_MINUTE` | `120` | Successful API requests per authenticated subject. |
| `BT_RATE_LIMIT_RETRY_AFTER` | `10` | `Retry-After` value returned on `429`. |
| `BT_RATE_LIMIT_MAX_CLIENTS` | `10000` | Active limiter-bucket cap. |
| `BT_TRUSTED_PROXIES` | empty | Reviewed peers allowed to provide observed client context. |
| `BT_TRUSTED_PROXY_HOST` | empty | Managed single proxy authority for strong CWA sessions. |
| `BT_TRUST_PROXY` | `false` | Unsafe legacy rate-limit compatibility; not an auth authority. |
| `BT_ALLOWED_ORIGINS` | local defaults | Exact origins for low-level cross-origin deployments. |
| `BT_ALLOW_PRIVATE_LAN` | `true` | Broad private-origin convenience for non-cookie modes only. |

The managed proxy replaces inbound forwarding chains with the peer it observed.
Raw reader proof can reach only the exact session-exchange route. The broker
accepts selected CWA cookies, a Kavita access bearer, or exact Kavita OIDC
cookie chunks, validates the stock account endpoint and discards the proof.
Ordinary requests receive only the opaque plugin cookie; the bearer and all
other reader cookies are stripped. After authentication, quotas and cache scope
use an opaque server-owned subject, not a request-supplied address or header.

## Cache and fallback settings

| Variable | Default | Boundary |
|---|---:|---|
| `DB_PATH` | `translations.db` | SQLite path; production images use `/app/data/translations.db`. |
| `BT_CACHE_TTL_DAYS` | `90` | Mandatory expiry. |
| `BT_CACHE_MAX_ENTRIES` | `100000` | Mandatory row cap. |
| `BT_CACHE_HIT_FLUSH_THRESHOLD` | `100` | Batched hit-counter flush threshold. |
| `BT_CACHE_HARDEN_EXISTING_DIR` | `false` | Production image enables private existing-directory checks. |
| `LLM_FALLBACK_PROVIDER` | empty | Optional secondary provider. |
| `LLM_FALLBACK_MODEL` | empty | Secondary model. |
| `LLM_FALLBACK_API_KEY` | empty | Secondary credential. |
| `LLM_CUSTOM_ENDPOINT` | empty | Primary custom public HTTPS chat-completions endpoint. |
| `LLM_CUSTOM_API_KEY` | empty | Credential used only by the primary custom endpoint. |
| `LLM_FALLBACK_CUSTOM_ENDPOINT` | empty | Fallback custom public HTTPS chat-completions endpoint. |
| `LLM_FALLBACK_CUSTOM_API_KEY` | empty | Credential used only by the fallback custom endpoint. |

Remote fallback is fail-closed: each `POST /translate` or
`POST /translate/batch` request must set the JSON boolean
`allow_cloud_fallback: true` before book text can leave a local primary path.
The authenticated `GET /provider-policy` response exposes only `local`/`remote`
locality plus an opaque per-process generation. Every official-reader
translation echoes that policy; stale generations receive `409` before cache or
provider work, then the reader refetches policy without replaying book text.
The reader stores fallback consent only in the current tab. A remote primary
always shows an active-cloud warning; a second remote fallback still requires
the tab switch. Provider names, models, URLs and keys never enter that response.

Named-provider endpoints are fixed in code. `openai-compatible` is always
remote, requires its dedicated key, rejects credentials/query/fragment in the
URL, rejects non-public destinations at runtime, disables redirects and scopes
cache identity to a digest of the endpoint. Runtime DNS is bounded by the work
budget and the transport connects to the vetted address while retaining TLS
hostname verification, closing DNS-rebinding races. Use `local` for LAN services.

The named adapters are `openai`, `anthropic`, `gemini`, `groq`, `together`,
`minimax`, `deepseek` and `openrouter`. For Gemini, the hub example uses the
stable `gemini-3.5-flash-lite` model and a Google AI Studio/project API key;
`BT_LOCAL_URL` may remain empty. Restrict the key to the Gemini API and keep it
only in the private server-side environment. Consumer ChatGPT, Codex, Gemini or
Antigravity subscriptions and browser sessions are not supported API auth.

Automatic retry and fallback are limited to connection/DNS timeouts and HTTP
`408`, `429`, `500`, `502`, `503` and `504`. Configuration failures, TLS
certificate failures and terminal `4xx` responses such as `400`, `401`, `403`,
`404`, `409` and `422` do not fail over. A malformed bounded provider response
may use an allowed fallback but is not retried blindly on the same provider.

Cache schema v2 stores translations plus scoped one-way hashes, not source
paragraphs, reader credentials or raw user/book identifiers. The legacy v1
table stays side by side for rollback but is never read by v2.

`BT_CACHE_DIR` and `BT_CACHE_OPERATOR_GROUP_ACCESS` are lifecycle-internal
filesystem controls. Managed operators set `BT_DATA_DIR`; `btctl` and the image
derive those internal values and their ownership policy.


### Real-Time Streaming and High-Capacity Caching (v2.4.0+)

| Variable | Default | Purpose |
|---|---|---|
| `BT_CLIENT_PREFETCH_GAP_MS` | `1000` | Lookahead prefetch pacing delay to prevent upstream thundering herds. |
| `BT_MAX_UPSTREAM_INFLIGHT` | `8` | Maximum concurrent requests admitted to local GPU or remote LLM backends. |
| `BT_CACHE_TTL_DAYS` | `90` | Automatic expiration window for SQLite translation cache entries. |
| `BT_CACHE_MAX_ENTRIES` | `100000` | Hard cap on total cached segments before oldest entries are pruned. |

The server automatically enables SQLite Write-Ahead Logging (`WAL`) mode with a `256MB` memory-mapped I/O window (`mmap_size = 268435456`) and `64MB` page cache (`cache_size = -64000`), ensuring response times below `0.5ms` for cached hits.
