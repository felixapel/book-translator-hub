# Troubleshooting

These checks apply to the recommended universal hub, the managed split
deployment and, where stated, the CWA-only certified Community Applications
profile. Do not expose the API, add a browser token, broaden a trusted proxy
range, or disable authentication to make an error disappear.

## Start with the deployment evidence

For any managed `btctl` hub or split deployment, run the read-only doctor from
the same clean checkout and private environment file used to install:

```bash
./btctl doctor --env /absolute/private/path/deployment.env
```

For structured output that is easier to share after removing private paths:

```bash
./btctl doctor --env /absolute/private/path/deployment.env --json
```

Every check must be `ok`. Doctor validates the saved plan, version+commit image,
owned containers/network, exact reader evidence, health, runtime environment,
authentication profile, published ports, and generated artifacts. Fix its first
failed check before debugging the browser.

A Community Applications install has no `btctl` state. Do not diagnose it with
`btctl doctor`. In the Unraid Docker tab, open the translator container's
**Logs** and **Edit** screens. Record its exact image digest, configured user,
published ports, appdata bind, network, and environment after removing provider
secrets. First confirm user `101:102`, a bind to `/app/data`, only container port
8080 published, and no mapping for 8390. Then work from the first startup or
request error in the container log. The remaining checks in this guide apply to
all topologies unless a paragraph explicitly limits its scope.

## Application is missing from Community Applications

The Community Applications path exists only through a public digest-pinned
GHCR image and an approved searchable CA listing.
If **CWA eBook Translate** is not returned by Community Applications search,
stop: the listing is not yet public or has been withdrawn. Do not install an
XML from Git history, a cached template URL, or a mutable image tag. Use the
source-built `btctl` path from the latest published tag, or wait for the listing
to return.

## `env: python3: No such file or directory` on Unraid

Current `./btctl` automatically uses Docker when host Python 3.11+ is absent;
stock Unraid does not need Python, NerdTools, or host Git to execute it. If this
message comes from the public launcher, confirm that the checkout is current,
that `btctl` is executable, and that you did not invoke the internal
`btctl.py` directly.

The fallback must run as `root`, reach the Docker daemon, and see a full clean
Git checkout including `.git`. If Unraid has no Git client, prepare the exact
commit with Claude Code or another Git-capable machine and copy the whole
checkout. A downloaded source archive alone is insufficient. The first command
may fetch pinned base layers and warm Docker build cache while it prepares the
temporary operator; that is expected even for `plan`.

The fallback intentionally requires the local Unix socket at
`/var/run/docker.sock` and ignores remote Docker contexts. If that socket is
missing, fix the local Docker service or permissions; do not point an Unraid
install at a remote daemon whose bind-mount paths refer to another machine.

Lifecycle commands also require `/run/cwa-translate-btctl-locks` to be a real
root-owned directory with mode `0700`. `btctl` creates it when absent. If it
rejects an existing object there, remove or repair that object only after
confirming no other `btctl` operation is running; do not bypass the lock.

## Toolbar is missing

1. Open the reader through `BT_PUBLIC_ORIGIN`, not the reader's direct port. A
   stock reader reached directly intentionally has no overlay.
2. Hard-refresh once (`Ctrl+Shift+R` or `Cmd+Shift+R`).
   After a hub reinstall or image replacement, sign out and back in as well:
   the regenerated reader session key invalidates old translator sessions.
3. In Browser DevTools, confirm `GET /bt-config.json` returns `200`, a JSON
   object, and `Cache-Control: no-store`. A missing or invalid managed config
   makes the loader fail closed; page variables cannot override it.
4. Confirm the reader page loads `/bt-static/loader.js` and that the Console
   has no `[BookTranslator]` error.
5. For CWA, confirm the path begins `/read/`. For Kavita, the only accepted
   path is `/library/<positive-id>/series/<positive-id>/book/<positive-id>`.
   Manga, PDF and library pages intentionally do not mount the toolbar.
6. If a reverse proxy is present, confirm its browser-reader route points to the
   managed injection proxy. Keep non-browser OPDS/Kobo/mobile routes pointed
   directly at the stock reader.

## Translation requests return 401 with `cwa-session`

The default profile accepts a native CWA session, not merely an Authentik edge
cookie. Sign out and back into CWA, then verify the browser sends the CWA
session cookie to the same public origin.

CWA with `config_session=1` binds that session to both the address it observed
at login and the browser `User-Agent`. The managed v2.2 proxy deliberately
overwrites `X-Forwarded-For` with one observed address on the CWA and API paths,
and the API replays that same address and `User-Agent` to the auth probe. Check
that all reader traffic—including login—uses the managed proxy, that no other
proxy rewrites `User-Agent` between those paths, and that the API has no direct
public/LAN bypass. A configured managed proxy with missing, chained, malformed,
or untrusted `X-Forwarded-For` fails closed with `401` before contacting CWA.
`BT_TRUST_PROXY=true` cannot authorize this header.

The certified CWA v4.0.6 topology uses its default
`TRUSTED_PROXY_COUNT=1`. A custom value makes CWA select a different address
from the forwarding chain and can invalidate every strong session; restore the
default or validate that custom topology separately before relying on it.

If the installed checkout predates this strong-session fix, rebuild both roles
from the current exact commit before testing again. Do not work around the
problem with `BT_AUTH_MODE=disabled`; that removes the API authentication
boundary instead of repairing it.

If Authentik authenticates the browser but CWA never creates a session that its
`/ajax/emailstat` endpoint accepts, use the separate
`authentik-forwarded` topology in the [Authentik guide](../install/authentik.md). Do not switch to
anonymous mode and do not put a shared secret in browser storage.

A `503` instead of `401` means the API could not safely evaluate CWA as the
authority. Check that the CWA container is running, `BT_READER_UPSTREAM` and
`BT_READER_NETWORK` are exact, and the selected auth endpoint is reachable from
the API container. On a managed split install, `doctor` catches topology and
runtime drift. On Community Applications, verify the CWA network and auth URL in
the Unraid Edit screen. In both profiles, API logs contain the bounded authority
failure without session-cookie contents.

## Translation requests return 401 with Kavita

Kavita requires `BT_AUTH_PROFILE=reader-session`, stock version `0.9.0.2`, and
an HTTPS public origin outside loopback. Sign out and back in through that
origin, open the exact EPUB route, and inspect `POST /bt-api/session` first.

- Native login requires a bounded access token in Kavita's own `kavita-user`
  local-storage object. The connector reads its `token` field only; it never
  reads or sends a refresh token.
- Stock OIDC login requires the exact `.AspNetCore.Cookies` cookie, including
  contiguous `C1`, `C2`, ... chunks when ASP.NET split it. Unrelated cookies
  are not forwarded to the account probe.
- The exchange must return `200` with `reader_type: "kavita"`, exact version
  `0.9.0.2`, and `expires_in` no greater than 300. The cookie itself is HttpOnly
  and therefore correctly absent from JavaScript storage.
- A `401` means the native proof, version, origin, observed address or
  User-Agent did not match. A `503` means `/api/Account` was unreachable or
  returned a malformed/oversized response. `doctor` catches container,
  network, version and generated-environment drift.

Do not copy a token into translator configuration or relax the proxy. Ordinary
`/bt-api/*` routes intentionally strip Kavita Authorization and cookies and
accept only the opaque plugin session. The frontend performs one fresh exchange
and one replay after an expired-session `401`; repeated failures require a new
Kavita login or topology repair.

## Translation requests return 401 with `authentik-forwarded`

Check all of these as one security contract:

- the request enters the edge's generated `/bt-api/` route;
- the Authentik outpost URL is reachable and the configured version is patched;
- the edge overwrites `X-authentik-uid` from the outpost response;
- the edge removes `Cookie`, `X-BT-Subject`, and `X-BT-Roles` before the API;
- the live edge address on `BT_EDGE_NETWORK` exactly matches
  `BT_IDENTITY_PROXY_IP` as `/32` or `/128`;
- neither translator role publishes a host port.

An edge-container IP change intentionally causes a fail-closed `401`. Restore
the reserved address or use the old matching environment to run managed
`uninstall`, then edit the peer and run `plan`, `install`, and `doctor`. The
completed uninstall evidence is archived and translation data is preserved.
Never replace the exact peer with an entire Docker subnet. Regenerate the
reviewed fragment with:

```bash
./btctl auth-snippet --env /absolute/private/path/cwa-translate.env
```

## Toolbar loads but translation fails

- Source and target must differ. Choose the book language in Settings and the
  output language in the toolbar.
- In DevTools Network, inspect the JSON error from `/bt-api/translate/batch`.
  The browser should call a same-origin relative route, not a host API port.
- Inside Docker, `localhost` means the API container. Set `BT_LOCAL_URL` to an
  address reachable from that container, normally the LLM host's LAN address.
- Local OpenAI-compatible endpoints for vLLM, Ollama, LM Studio, and llama.cpp
  normally end in `/v1/chat/completions`. Confirm the configured model name
  exists on that server.
- `LLM_API_KEY` stays empty for a keyless local server. Cloud provider keys are
  server-side only and must not appear in `/bt-config.json`, browser storage,
  generated Unraid XML, or `state.json`.
- `/ping`, `/health`, and `/ready` are deliberately shallow. Use one short,
  non-sensitive translation through the authenticated public route to prove a
  real provider before translating a book. `/health/deep` is also authenticated
  and spends provider capacity.

For Gemini, set `LLM_PROVIDER=gemini`, use a currently supported model ID such
as `gemini-3.5-flash-lite`, provide a Google AI Studio or project API key, and
leave `BT_LOCAL_URL` empty unless a separate local role actually uses it. The
endpoint is fixed by the adapter. A `400` usually means an invalid model/request
contract, `401` or `403` means the key is invalid, blocked or not permitted for
the Gemini API, `404` means the model is unavailable to that API/project, and
`429` means quota or rate limiting. Check the key in Google AI Studio without
printing it, and do not replace it with a Gemini or Antigravity browser-session
credential.

After changing the managed environment, do not recreate a role with an ad-hoc
`docker run`; use the documented lifecycle so state and ownership remain
verifiable.

## Occasional malformed batch envelopes from a local model

A local model can translate the text correctly but occasionally return JSON
that does not match the strict segment envelope. The API log then records
`segment envelope invalid attempt=1/2` without logging the prompt, generated
IDs, or book text. This validation is intentional: do not loosen the parser,
because it prevents one generated translation from being assigned to another
paragraph.

The current server retries that group once with new opaque IDs. If the second
envelope is also malformed, it translates the group's paragraphs sequentially.
Both stages use the original request's deadline, attempt, input-byte,
output-token, global-inflight, cloud-consent, and singleflight boundaries.
Successful paragraph recovery returns in the same `200` response while the
shared work budget remains available, but is not stored under the grouped-prompt
cache contract; a later healthy grouped request may populate that cache.
Ordinary individual provider failures do not cancel healthy sibling groups.
Work-budget exhaustion remains fatal and may terminate the shared request.

Use `/metrics` to distinguish `envelope_retry_groups`,
`envelope_retry_recovered_groups`, `paragraph_fallback_groups`, and recovered or
failed fallback segments. These are fixed process-local counters and contain no
book-derived labels. If the request budget is exhausted, the API returns the
normal bounded `503` without starting another provider call. If only one
individual recovery fails for another provider reason, that paragraph gets a
sanitized translation-error marker and the reader exposes its explicit retry
action instead of discarding healthy siblings.

The reader never automatically retries these failures. Only a bounded HTTP
`429` carrying `retry_safe: true` and an explicit pre-provider admission scope
is retried automatically; use the visible manual retry for every other
failure. A Gemini/provider `429` inside a successful partial batch is reported
as `provider_rate_limited` with bounded retry metadata and is terminal until
the user retries, because the browser cannot prove the provider did no work.

If recovery happens frequently, verify the configured model supports the
OpenAI-compatible chat contract and has enough output capacity, then lower
`BT_BATCH_SIZE` one step at a time. `BT_BATCH_SIZE=1` avoids multi-paragraph
envelopes at the cost of one provider call per paragraph. Keep
`BT_REQUEST_MAX_ATTEMPTS`, output limits, and the request deadline finite; do
not hide a systematic provider problem by making them unlimited.

## 502 or 504 after the reader was recreated

The injection proxy resolves `BT_READER_UPSTREAM` when its Nginx process starts.
If CWA or Kavita was recreated with a new address, restart the complete universal
hub, only the proxy role in a managed split install, or the single Community
Applications container. Rerun `doctor` for every managed topology. Do not
recreate the stock reader or split API role for this symptom.

Also confirm the reader still joins the exact `BT_READER_NETWORK` and that its
running image supplies the exact `BT_READER_VERSION` tag/label expected by the
plan.

## Rate-limited or slow translation

“Rate limited — waiting” means bounded admission is working. Avoid repeatedly
toggling translation or reloading, which creates more queued work. Measure the
actual local/provider latency with a short text first, then adjust only one
bounded setting at a time.

Start with the fixed-cardinality `/metrics` fields `provider_calls`,
`batch_groups_total`, `batch_paragraphs_total`,
`batch_group_size_buckets` and `batch_group_source_token_buckets`.
Use `translation_latency_ms_buckets` to detect tail movement without exposing
request, reader or book labels. Calculate an approximate percentile from the
cumulative fixed buckets rather than relying only on `average_latency_ms`.
`batch_paragraphs_total / batch_groups_total` describes planned grouping before
cache lookup. Compare provider attempts over a controlled fresh-cache canary,
not as an unconditional live ratio: cached plans and provider health probes do
not represent translation calls one-for-one. `provider_calls.rate_limited`
distinguishes provider `429`s from the API's own
`outcomes.api_rate_limited` admission counter. Counters are process-local and
reset when the API process restarts.

For an RPM-limited cloud model, raise `BT_BATCH_SIZE` gradually and use a
positive `BT_BATCH_SOURCE_TOKEN_BUDGET` so unusually long paragraphs cannot
create an unbounded prompt. `BT_CLIENT_PREFETCH_GAP_MS` paces only opt-in
background work. Keep `BT_MAX_UPSTREAM_INFLIGHT`, request deadline, attempt and
output-token limits finite. `BT_BATCH_SOURCE_TOKEN_BUDGET=0` restores
count-only grouping immediately. Project quota is shared with consumers the
service cannot observe, so this project intentionally does not claim to be a
project-wide quota coordinator.

## Translation formatting, duplicates, or stale behavior

- Hard-refresh and confirm the Console reports the current `BT_UI_VERSION`.
- There must be only one `/bt-static/loader.js` instance. A legacy CWA overlay
  plus the managed injection proxy can load two translators; remove the legacy
  template/file mounts after preserving rollback evidence.
- Change source or target language to cancel stale work cleanly. A page turn
  intentionally discards results from the previous reader generation.
- If headings or paragraphs are missed, capture a minimal DRM-free EPUB and the
  element structure. Do not share copyrighted book content or a full private
  library database.

## XML parsing error or garbage characters

The EPUB is commonly DRM-encrypted. Check for the standard marker:

```bash
unzip -l "book.epub" | grep META-INF/encryption.xml
```

If present, CWA's web reader receives encrypted chapter bytes and cannot parse
them; the translator cannot repair or decrypt the file. Use a legally obtained
DRM-free EPUB.

## Install, state, or migration recovery

- A failed `plan` creates no deployment state or runtime resources. On a host
  without Python, the temporary bootstrap may still warm Docker build cache.
  Correct the named field and rerun it.
- A failed fresh install removes only newly created translator runtime
  resources and the newly created session key, and writes no successful
  `state.json`; the stock reader and translation database stay external. If
  cleanup completes, `install-attempt.json` records `status=cleaned` and the
  unchanged install command may retry against that exact retained data. Do not
  edit the journal: a different checkout, configuration, profile or resource
  plan is rejected. `status=cleanup-failed` requires inspection and repair of
  every recorded cleanup error before another install.
- If `state.json` alone was lost while the complete labeled split runtime is
  healthy, use `./btctl adopt --env ...`. It rejects partial or unlabeled
  resources and never converts a v2.1.4 combined container.
- For an exact v2.1.4 source, use `./btctl upgrade --env ... --yes`. If
  acceptance fails after a completed migration, use
  `./btctl rollback --env ... --yes`; never start both versions against one
  data directory.
- If the host stopped during `prepared`, `snapshot-complete`, or a re-upgrade,
  rerun the same `btctl upgrade` command. The journal verifies exact identities.
  When a complete healthy v2.2 runtime already exists, the command adopts that
  exact labeled cutover and finishes the journal without moving its live data
  bind. When no v2.2 runtime exists, it preserves incomplete work trees under
  clearly named `.preserved` paths and advances to a new numbered attempt.
  Partial or mismatched runtime resources stop recovery before any data tree is
  renamed. Do not delete preserved trees until the new runtime passes acceptance.
- For Compose permission errors, rerun with the same Docker-capable account and
  primary group used for install. Do not apply a recursive public `chmod` or
  change uid `101` manually; the managed one-shot helper restores the private
  `2750` directory and `0640` file contract before startup.
- A successful rollback may mark `target_reupgrade_status=unavailable` when the
  preserved v2.2 tree is absent or fails integrity/read checks. The legacy
  service is restored, but re-upgrade remains fail-closed until that target is
  repaired or restored from trusted evidence.
- `./btctl uninstall --env ... --yes` removes only owned runtime and preserves
  CWA, data, backups, the local image, and state evidence. It is retryable after
  an interrupted removal.

## Collecting a useful issue report

Include the exact source commit or image digest, `VERSION`, host/profile, reader
type and exact image tag, reverse-proxy type, browser, and the smallest relevant
log window.
For managed hub or split installs, also include the redacted first failed doctor
check; for Community Applications, include the first container startup or
request error instead. Remove cookies, Authentik headers, public IPs, private
filesystem paths, book text, and all LLM credentials before sharing.

## Reader pages load slowly or UI feels sluggish

If Calibre-Web or Kavita takes many seconds to load initial pages, check your
reverse proxy compression and script injection attributes:

1. **Reverse proxy compression:** When using `sub_filter` with `proxy_set_header Accept-Encoding ""`,
   upstream services return uncompressed responses so the proxy can match `</head>`.
   Ensure your edge proxy (e.g., SWAG, Nginx, Traefik) has `gzip on;` enabled with
   `gzip_proxied any;` and comprehensive MIME types. Without proxy-level compression,
   massive single-page application bundles (such as Kavita's ~838 KB JavaScript bundle
   and ~490 KB stylesheet) are sent in uncompressed plain text on every page load.
2. **Deferred script execution:** Ensure the injected script tag specifies `defer`:
   `<script src="/bt-static/loader.js" defer></script>`. Without `defer`, the browser
   blocks DOM parsing and delays First Contentful Paint while awaiting the script and
   its `/bt-config.json` network roundtrip.

## Translation overlay does not appear in Kavita

1. **Check `/bt-config.json`:** Verify that `GET https://your-kavita-domain/bt-config.json`
   returns `"readerType":"kavita"` and a supported `readerVersion` (such as `"0.9.1.4"`).
   If `readerType` is missing or set to `cwa`, the loader stays inert on Kavita routes.
2. **Supported routes:** The overlay activates exclusively when an EPUB is opened
   at `/library/:libraryId/series/:seriesId/book/:chapterId`. Non-EPUB views, manga,
   PDFs, and library overview pages are intentionally ignored to prevent overhead.
3. **Check DevTools Console:** Ensure no error like `unsupported reader version contract`
   is logged.
