# Universal CWA and Kavita hub

The universal hub is the simplest production topology: one immutable image,
one hardened container and one browser-facing port for each enabled stock
reader. CWA, Kavita or both can be selected without rebuilding the image.

```text
books origin  -> hub :8080 -> stock CWA
                     |-> CWA API :8391 (loopback) -> CWA cache/provider

kavita origin -> hub :8081 -> stock Kavita
                     |-> Kavita API :8392 (loopback) -> Kavita cache/provider
```

The internal processes and data are isolated by reader, but the container is a
single security and restart boundary. Keep the split topology when independent
container containment is more important than deployment simplicity or when
using CWA Authentik-forwarded identity.

## Configure

Create a private file outside the checkout:

```bash
install -d -m 0700 /absolute/private/path
cp .env.hub.example /absolute/private/path/book-translator-hub.env
chmod 0600 /absolute/private/path/book-translator-hub.env
```

Set the exact state/data/backup paths, reader container names, existing Docker
networks, public origins and ports. The three storage directories must be
absolute, disjoint persistent paths. For every enabled reader, the hostname in
`BT_<READER>_READER_UPSTREAM` must exactly equal
`BT_<READER>_READER_CONTAINER`; the hub uses that existing network to reach the
stock container. Generate a different connector UUID for each reader with
`uuidgen`, replacing the example IDs before an install. Disable a reader with
exactly `false`; variables for a disabled reader are ignored by the runtime.

For Unraid, use `BT_INSTALL_PROFILE=unraid` and real paths below an existing
`/mnt/user/<share>/` or `/mnt/<pool>/` boundary. `btctl` creates one raw Docker
container and does not depend on the Compose plugin. For Linux Compose, keep
`compose-existing` and use Docker Compose 2.30.0 or newer. The generated
Compose JSON contains no environment values; `hub.env` is a separate raw,
mode-`0600` artifact below `BT_STATE_DIR`.

The sample defaults to a local OpenAI-compatible provider with a placeholder
internal hostname. Replace it with the exact local endpoint and
model in the private copy. Select a named remote provider only when its
server-side API key is available in that private file. For example:

```dotenv
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.5-flash-lite
LLM_API_KEY=<Google AI Studio or project API key>
BT_LOCAL_URL=
BT_BATCH_SIZE=10
BT_BATCH_SOURCE_TOKEN_BUDGET=450
BT_BATCH_MAX_TOKENS=1200
BT_CLIENT_PREFETCH_GAP_MS=1000
BT_MAX_BATCH_PARAGRAPHS=50
```

[`gemini-3.5-flash-lite`](https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash-lite)
is a stable Gemini API model intended for low-latency, high-throughput work.
Create a dedicated key in Google AI Studio, [restrict it to the Gemini
API](https://ai.google.dev/gemini-api/docs/api-key), place it only in the
mode-`0600` environment file and never put it in a Compose file, command line,
browser setting or support log. The Gemini adapter owns its fixed HTTPS
endpoint; no endpoint variable is required.

The first visible paragraph translates alone. Tune batching only after measuring
the chosen provider; `BT_BATCH_SOURCE_TOKEN_BUDGET=0` and
`BT_CLIENT_PREFETCH_GAP_MS=0` preserve count-only grouping without delayed
prefetch.

For a local backend use `LLM_PROVIDER=local`, leave `LLM_API_KEY` empty and set
the exact `BT_LOCAL_URL`. Per-reader overrides use
`BT_CWA_LLM_PROVIDER`, `BT_CWA_LLM_MODEL`, `BT_CWA_LLM_API_KEY` and
`BT_CWA_LOCAL_URL`, or their `BT_KAVITA_*` equivalents. Omit an override to
inherit the shared value. An explicitly empty override clears an inherited
secret. Consumer subscriptions and browser sessions are not supported API
credentials.

## Install and verify with `btctl`

```bash
./btctl plan --env /absolute/private/path/book-translator-hub.env
./btctl install --env /absolute/private/path/book-translator-hub.env --yes
./btctl doctor --env /absolute/private/path/book-translator-hub.env
./btctl doctor --env /absolute/private/path/book-translator-hub.env --deep
```

The ordinary doctor is quota-free and verifies local structure only. Add
`--deep` deliberately when every configured provider may receive one bounded
health probe.

On stock Unraid without compatible host Python/Git, the existing containerized
launcher supports plan, install, doctor and uninstall. It validates exact
storage mounts and uses the local Docker socket only for lifecycle commands.

To change providers, retain two private files: the exact current environment
for removal/rollback and a reviewed replacement. First run `uninstall --yes`
with the current environment, then `plan` and `install --yes` with the
replacement private environment.
Uninstall removes only the verified hub container, removes the two hub-owned
`reader_session_key` credentials, and preserves translation databases and other
reader data. The next install regenerates private keys and archives the completed
ownership state before committing the new coherent generation.

If the replacement fails, the old environment and retained image/data are the
rollback boundary: uninstall any successfully committed replacement with its
own environment, reinstall the old environment, run `doctor`, then refresh open
reader tabs. Never edit generated state to make the provider identity match.

An unmanaged Compose quick start is also available:

```bash
BT_HUB_ENV_FILE=/absolute/private/path/book-translator-hub.env \
  docker compose \
  --env-file /absolute/private/path/book-translator-hub.env \
  -f docker-compose.hub.yml up -d --build
```

The static Compose file declares both reader networks and ports, so use
`btctl` for a clean single-reader deployment.

## Migrate existing split deployments

Do not stop or rename containers manually. Use the private `state` directories
from the existing CWA and Kavita `btctl` installs:

```bash
./btctl migrate-topology \
  --env /absolute/private/path/book-translator-hub.env \
  --from-state /absolute/private/path/cwa-state \
  --from-state /absolute/private/path/kavita-state \
  --yes
./btctl doctor --env /absolute/private/path/book-translator-hub.env
```

Run `migrate-topology` as root, including with the `compose-existing` profile.
Managed split runtimes keep their reader session keys owned by runtime UID 101
at mode `0600`; root can preserve those credentials without weakening their
permissions. Fresh hub installation and ordinary split lifecycle commands keep
their existing operator requirements.

Migration also requires host Python 3.11+ and Git because the launcher must
lock and mount multiple independently selected source states exactly. It
records every container ID and initial running status, stops only those
resources, performs an offline SQLite checkpoint, copies each reader tree
atomically and commits
schema-3 state only after the hub passes health and dependency probes. A retry
uses the durable per-reader copy manifests, adopts an exact copy published just
before an interrupted journal write, and recovers a hub whose state commit
completed before the journal commit. A first attempt requires an empty hub data
root; resumes reject every entry not owned by the recorded reader copy operation.
If the verified hub is active but the final journal commit fails, its source
runtimes remain stopped; rerun the same command to complete that commit
idempotently.

Rollback removes only the verified owned hub, preserves its copied data and
restarts only source roles recorded as running before cutover:

```bash
./btctl rollback-topology \
  --env /absolute/private/path/book-translator-hub.env \
  --yes
```

After either cutover or rollback, open reader tabs may need one automatic
session exchange because sessions are process-local and intentionally short.

## Required checks before use

Run `doctor` after installing, then verify both readers through their configured
public HTTPS origins. Confirm that only the configured proxy ports are
published, each reader uses its own database, session key and cookie, and a
DRM-free EPUB translates after native reader login. The internal APIs remain
container-loopback endpoints; process health alone is not browser acceptance.

### Browser gate for an exact candidate

The browser gate belongs to the exact checkout commit and immutable runtime
image under review. A green `doctor`, health endpoint, automated Chromium run,
or successful session on an older image does not replace an authenticated
browser check on the deployed candidate. After an uninstall/install or image
replacement, the hub regenerates reader session keys; sign in again and do not
rely on a cached reader tab.

For every enabled reader, use the public HTTPS origin (never the stock reader
port) and verify on the exact candidate:

1. `GET /bt-config.json` returns `200`, `Cache-Control: no-store`, and the
   expected reader type/version. The page loads one translator loader only.
2. The authenticated `POST /bt-api/session` returns `200` with the expected
   reader type/version and an opaque expiry of at most five minutes. Provider
   keys, native reader tokens and book text must not appear in the response,
   configuration or browser storage.
3. A short, non-sensitive paragraph produces a successful same-origin
   `POST /bt-api/translate/batch`; then change language/display mode, navigate
   between chapters and reload the EPUB.
4. An unsupported manga, PDF or non-reader route mounts no toolbar, loads no
   translator request and sends no translation request.
5. CWA and Kavita remain isolated: each has its own public route, state/data
   directory, SQLite database, session key, cookie and backup boundary.

`/ping`, `/health` and `/ready` prove process health only; they do not prove
authenticated translation. Keep release acceptance evidence outside tracked
guides.
