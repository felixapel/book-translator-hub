# Compatibility matrix

This matrix separates code-level support from configurations exercised by the
release gates. “Contract-supported” means `btctl` accepts and validates the
topology. “CI-certified” means automated tests exercise it on every candidate.
Anything else is not a promise until its acceptance checks are added.

## Calibre-Web-Automated

| Component | Status | Boundary |
|---|---|---|
| CWA 4.x | Contract-supported, Tier 1 | An exact stable version and matching running image tag/label are required. The release reference is CWA `4.0.6`; a future 4.x UI change still requires browser acceptance before promotion. |
| CWA 3.1.4 | Legacy migration only | Accepted only as the source of `btctl upgrade`; it is not a fresh v2.2 runtime target. |
| Other CWA 3.x or pre-release/mutable tags | Rejected | `btctl plan` fails before a production-image build or deployment mutation. A stock-host bootstrap may still warm ordinary Docker build cache. |
| Stock CWA container | Required | The managed proxy-injection topology does not replace templates, mount overlay files into CWA, or own the CWA container. |

The project tracks the stable CWA reader contract using the
[CWA v4.0.6 reference release](https://github.com/crocodilestick/Calibre-Web-Automated/releases/tag/v4.0.6).

## Kavita

| Component | Status | Boundary |
|---|---|---|
| Stock Kavita 0.9.x (v0.9.0.2 - v0.9.1.4+) | Contract and CI certified | Stock releases `0.9.0.2` through `0.9.1.4+`. Supports both OIDC `reader_session` and direct same-origin proxies (`cwa_session`). Use the [Kavita guide](../install/kavita.md). |
| DRM-free EPUB web reader | Candidate support | Only `/library/:libraryId/series/:seriesId/book/:chapterId` with `.book-content`; translations are a live browser overlay and are not written to Kavita or the EPUB. |
| Kavita manga, PDF, OPDS, mobile/offline clients and writeback | Rejected or inactive | The loader remains inert on non-EPUB routes. No file mutation or alternate client integration is implemented. |
| Forked/custom Kavita frontend or authentication plugin | Not certified | The connector targets the stock route, DOM and `/api/Account` behavior only. |

The source boundary is the
[Kavita v0.9.0.2 release](https://github.com/Kareadita/Kavita/releases/tag/v0.9.0.2)
at the [exact reviewed commit](https://github.com/Kareadita/Kavita/commit/6bcd5689385d0e96824982d843c54f15ce784ddc).
CWA and Kavita connectors may share one universal hub container, but always
retain distinct origins, ports, connector IDs, cookies, databases and keys.
Split installations remain supported when separate container boundaries matter.

## Host and container runtime

| Environment | Status | Notes |
|---|---|---|
| Stock Unraid 7.3.2 on x86_64 | Managed and acceptance-targeted | The automated gate runs public `./btctl` plan/install/doctor/uninstall without host Python or Git. Use `BT_INSTALL_PROFILE=unraid` as root with Docker, Bash, and a full Git checkout; NerdTools is not required. Each reader needs separate physical-host and browser acceptance before promotion. |
| Community Applications on Unraid 7.3.2 x86_64 | Contract and CI certified; listing-dependent | The combined non-root profile supports CWA 4.0.6, native CWA sessions and a local OpenAI-compatible LLM. Only proxy port 8080 is mapped; API port 8390 remains private. Install only from a searchable listing with an anonymously verified digest-pinned image. See [Community Applications](../install/community-applications.md). |
| Linux with an existing Compose-managed reader | Managed and CI contract-tested | Use `BT_INSTALL_PROFILE=compose-existing`; CWA or Kavita stays external to the generated private Compose document. A Docker-capable non-root account is supported when the same account and private primary group are used for every lifecycle command. |
| Docker Engine with `docker compose` plugin | Required for Compose profile | The current development audit used Docker `29.6.1` and Compose `5.3.1`. CI also builds and exercises the image on a real Docker runner. No lower minimum is claimed without a matching gate. |
| ARM64 Linux/Unraid | Not yet CI-certified | The source build may work where pinned base/package inputs resolve, but promotion requires an ARM build and runtime smoke gate. |
| Native Windows or macOS | Not a managed production target | A Linux Docker host/VM may be used, but there is no native installer or release acceptance matrix. |

Managed roles require Docker health checks, read-only root filesystems, tmpfs,
capability dropping and bind-mount semantics. Split roles additionally require
an owned internal API network; hub APIs bind only to container loopback.
Alternative container engines are unsupported unless they reproduce and test
those exact contracts.

## Browser and reader

| Client | Status | Notes |
|---|---|---|
| Current Chromium | CI-certified | Real Playwright scenarios cover loader isolation, source/target selection, translation, authentication transport, accessibility, console errors, and network failures. |
| Chrome / Edge based on current Chromium | Expected compatible | Run the same public-origin acceptance checklist on the actual client before relying on it. |
| Firefox and Safari/WebKit | Not yet CI-certified | No release-blocking browser scenario currently proves them; report reproducible issues rather than assuming parity. |
| DRM-free EPUB in the CWA web reader | Supported | DRM-encrypted content cannot be parsed by CWA or this overlay. |
| DRM-free EPUB in stock Kavita 0.9.x (v0.9.0.2 - v0.9.1.4+) | CI-certified | Real Chromium covers the exact top-level `.book-content` reader, route-derived scope, navigation teardown and authentication replay. Verified live in production. |

## Authentication and reverse proxies

| Topology | Status | Boundary |
|---|---|---|
| Universal hub reader sessions | Recommended | One container may enable CWA, Kavita or both. Each reader keeps an exact public origin/port and isolated five-minute opaque session cookie. APIs stay on loopback. |
| Native CWA session, same-origin proxy | Recommended and CI-certified | `BT_AUTH_PROFILE=cwa-session`; CWA v4.0.6 with `config_session=1`, reverse-proxy-header login disabled, and its default `TRUSTED_PROXY_COUNT=1` is covered by unit and container regression fixtures. Selected cookies are exchanged for a five-minute maximum opaque plugin session bound to the proxy-observed address/User-Agent. The API has no host port. Custom trusted-proxy hop counts are not yet certified. |
| Kavita native bearer or stock OIDC cookie, same-origin proxy | CI-certified candidate | `BT_AUTH_PROFILE=reader-session`; proof reaches only `POST /bt-api/session`, is validated by exact `/api/Account`, then discarded. Ordinary API calls receive only the opaque plugin cookie. Refresh tokens and arbitrary cookies are rejected. HTTPS is required outside loopback. |
| Authentik forwarded identity | Managed advanced split-only profile | Requires `docker-edge`, exact `/32` or `/128` edge peer, a patched Authentik version, and the generated direct API route. It is rejected by the hub. See [Authentik](../install/authentik.md). |
| Nginx edge | Generated and contract-tested | Merge the fragment into the existing HTTPS/Authentik server configuration. SWAG and Nginx Proxy Manager still require product-specific config validation. |
| Traefik edge | Generated and contract-tested | Existing entrypoint, TLS, certificate, and Authentik settings remain operator-owned. |
| Caddy edge | Generated and contract-tested | Merge the handler inside the existing Authentik-protected site block. |
| Disabled auth, browser shared token, broad trusted subnet, published API | Rejected by managed profiles | These are not compatibility fallbacks. Fix the identity edge instead. |

## LLM providers

| Provider type | Status | Notes |
|---|---|---|
| Local OpenAI-compatible chat completions | Supported | vLLM, Ollama, LM Studio, and llama.cpp are supported through an absolute `/v1/chat/completions` URL. `LLM_API_KEY` may remain empty. A local service is optional, not a prerequisite for cloud providers. |
| OpenAI, Anthropic, Gemini, Groq, Together, MiniMax, DeepSeek, OpenRouter | Supported named adapters | Endpoints are fixed and credentials remain server-side. The hub example uses stable `gemini-3.5-flash-lite` as a complete backend. A remote primary is shown as active; remote fallback requires per-tab consent. `/health/deep` succeeds only when every configured backend passes. |
| Public custom OpenAI-compatible servers | Contract-compatible, not automatically certified | Use provider ID `openai-compatible`, a dedicated key and public HTTPS exact `/v1/chat/completions` path. Redirects, ambient proxies and private/reserved destinations are rejected. |
| Consumer ChatGPT/Codex/Gemini/Antigravity subscriptions | Unsupported | Interactive product subscriptions are not server API credentials. Use a normal provider API key; do not export browser sessions or subscription tokens. |

The CI suite uses mocked provider boundaries and never spends a real cloud key
or local GPU request. Real-provider acceptance belongs to the target deployment
and should use a short non-sensitive text before translating a book.

## Promotion rule

A configuration outside the CI-certified cells can be useful, but it is not a
release guarantee. Before declaring it supported, add a reproducible test or
record the exact host, reader tag and commit, browser, edge, LLM server,
commands, and results
in the deployment acceptance evidence.
