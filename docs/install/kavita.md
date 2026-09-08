# Kavita managed installation

Kavita support can use the recommended [universal hub](universal-hub.md) or the
same `btctl` split deployment as CWA: one stock reader,
one injection proxy and one private translation API. It does not fork Kavita,
mount files into its container or write translated text back to the library.

This integration supports stock [Kavita](https://www.kavitareader.com/)
releases across the `0.9.x` series (including v0.9.0.2 through v0.9.1.4+),
compatible with both OIDC `reader_session` brokering and direct reverse-proxy
same-origin configurations.
Only the web EPUB route
`/library/<libraryId>/series/<seriesId>/book/<chapterId>` and its
`.book-content` DOM are active. Manga, PDF, OPDS, mobile apps, writeback and
other Kavita versions are outside the accepted contract.

## Split-profile isolation from CWA

When using the split profile, CWA and Kavita may use the same source checkout
and image but must be separate installations. Give each one a distinct install
name, public port/origin, state directory, data directory and backup directory.
The universal hub instead derives separate reader subdirectories and keys from
one data root. Never point two split API roles at one SQLite directory or one
`reader_session_key`.

```text
browser -> kavita-translate-proxy -> stock Kavita :5000
                    |
                    +-> kavita-translate-api -> LLM
                                      |
                                      +-> private Kavita translation data
```

Keep the existing CWA environment file unchanged. Copy [`.env.example`](../../.env.example)
to a second private file and replace its CWA reader block with:

```dotenv
BT_INSTALL_NAME=kavita-translate
BT_AUTH_PROFILE=reader-session
BT_PUBLIC_ORIGIN=https://kavita-books.example.com

BT_READER_TYPE=kavita
BT_READER_UPSTREAM=http://kavita:5000
BT_READER_CONTAINER=kavita
BT_READER_NETWORK=kavita_default
BT_READER_VERSION=0.9.0.2
BT_READER_IMAGE_ID=sha256:<64 lowercase hex from docker inspect>
BT_CWA_IDENTITY_HEADER=

BT_STATE_DIR=/mnt/user/appdata/kavita-translate/state
BT_DATA_DIR=/mnt/user/appdata/kavita-translate/data
BT_BACKUP_DIR=/mnt/user/backups/kavita-translate
```

Use the real container and network names. `BT_READER_UPSTREAM` must be exactly
`http://<BT_READER_CONTAINER>:5000`. HTTPS is mandatory for a non-loopback
`BT_PUBLIC_ORIGIN`; the session cookie uses the `__Host-` boundary.
If the container image is tagged `latest`, first verify Kavita itself reports
exactly `0.9.0.2`, then copy the immutable value from
`docker inspect --format '{{.Image}}' <BT_READER_CONTAINER>` into
`BT_READER_IMAGE_ID`. Installation fails if the running image changes. An exact
application-version tag or label remains sufficient when no image ID is set.

Then use the normal lifecycle:

```bash
./btctl plan --env /absolute/private/path/kavita-translate.env
./btctl install --env /absolute/private/path/kavita-translate.env --yes
./btctl doctor --env /absolute/private/path/kavita-translate.env
```

Review `plan` before installing. It must identify `reader_type` as `kavita`,
the exact version as `0.9.0.2`, and two resources named from
`kavita-translate`. `doctor` must report every check as `ok`.

The provider block is reader-neutral. For a fast cloud backend without a local
GPU dependency, use the Gemini-only example in the recommended
[universal hub](universal-hub.md) or the split-profile example in the
[managed installation guide](btctl.md). A local backend or fallback is optional.
The Kavita toolbar obtains an
authenticated locality-only policy: it shows that cloud translation is active
without exposing provider/model/URL/key details. If the local service is the
primary and a remote service is the fallback, the remote fallback remains off
until the reader enables it for that tab.

On a split installation, provider changes use the provider-only transaction:

```bash
./btctl reconfigure --env /absolute/private/path/kavita-new-provider.env
./btctl reconfigure --env /absolute/private/path/kavita-new-provider.env --yes
```

The API process is replaced, but the Kavita proxy, connector identity, data and
on-disk session key are preserved. An already open reader may perform one new
opaque-session exchange after the cutover.

The universal hub intentionally changes provider configuration as one coherent
container generation. Follow its documented old-environment `uninstall`, new-
environment `install` and `doctor` sequence instead of running `reconfigure`.

## Authentication boundary

Sign in to Kavita through `BT_PUBLIC_ORIGIN`, then open a supported EPUB. The
loader submits either Kavita's native access token or its exact OIDC session
cookie only to `POST /bt-api/session`. The API validates that proof against
Kavita's `/api/Account`, discards it and issues an opaque, `HttpOnly`,
`SameSite=Strict` translator cookie for at most five minutes. Ordinary
translation routes strip the Kavita bearer token and cookies and accept only
that plugin cookie. The opaque session is also bound to the proxy-observed
client address and browser User-Agent.

The native refresh token is never read. No Kavita credential is persisted in
SQLite, lifecycle state, generated environment files or browser translator
configuration. OIDC deployments must keep the stock account endpoint reachable
at the configured internal Kavita upstream; custom authentication plugins are
not certified.

## Browser acceptance

Before relying on the connector:

1. Confirm stock Kavita reports exactly `0.9.0.2` and `doctor` is fully green.
2. Record the exact deployed checkout commit and immutable hub image digest,
   then sign in through the configured HTTPS origin. After reinstalling or
   replacing the image, force a hard reload and sign in again because the
   reader session key is regenerated.
3. Open a DRM-free EPUB at the exact
   `/library/<positive-id>/series/<positive-id>/book/<positive-id>` route.
   In DevTools, verify `GET /bt-config.json` is `200` with
   `Cache-Control: no-store`, the config says `reader_type: kavita` and
   `reader_version: 0.9.0.2`, and exactly one loader is mounted.
4. Verify `POST /bt-api/session` returns `200` with the exact Kavita identity
   and an opaque expiry of at most five minutes. The provider key, native
   Kavita token, refresh token and book text must not appear in the response,
   generated config or browser storage.
5. Translate one short, non-sensitive paragraph through the same-origin
   `POST /bt-api/translate/batch`, change language/display mode, move between
   chapters and reload the page.
6. Navigate to a manga, PDF or non-reader page and confirm no translator
   loader, toolbar or translation request is sent.
7. Confirm a separate CWA installation, if present, still has its own names,
   network attachment, state/data directories, SQLite database, session key,
   cookie and lifecycle state.

Automated Chromium and real-container gates cover the route, DOM, native-token
exchange, credential stripping and SPA teardown. Physical stock-Unraid and
real-Kavita browser acceptance is still required before the Kavita profile is
promoted from candidate to stable support. Community Applications does not
install this profile; use `btctl`.
