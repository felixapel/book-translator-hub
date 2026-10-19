# Managed `btctl` installation

`btctl` remains the lifecycle authority for the supported split deployment. It
builds one immutable local image from an exact clean checkout, runs separate
non-root proxy and API roles, and never edits or owns the stock reader
container.

New ordinary installs should use the
[universal hub guide](universal-hub.md). Keep this split topology when separate
container security/restart boundaries or CWA Authentik-forwarded identity are
required. Existing split states remain supported and are the source of the
explicit `migrate-topology` workflow; they are never rewritten implicitly.
That offline credential-preserving migration must run as root even when its
source profile is `compose-existing`.

```text
browser / reverse proxy -> <install>-proxy -> stock CWA or Kavita
                                   |
                                   +-> cwa-translate-api -> LLM
                                                |
                                                +-> private cache
```

## Choose a profile

| Profile | Operator | Requirements |
|---|---|---|
| `unraid` | root | Stock Unraid, Bash, local Docker socket, full Git checkout; host Python, host Git and NerdTools are not required to run `./btctl`. |
| `compose-existing` | trusted Docker-capable account | Linux, Docker Engine plus Compose 2.30.0 or newer, and the same private primary group/account for every lifecycle command. |

Both profiles require a supported stock reader on a named Docker network, its
exact version, private state/data/backup paths outside this checkout and an LLM
reachable from Docker. CWA 4.x is the stable fresh-install tier. Exactly CWA
3.1.4 is accepted only as the source of the v2.1.4 migration. Kavita has a
separate pinned candidate boundary in the [Kavita guide](kavita.md).

Keep the complete checkout including `.git` and select an annotated release
tag. A forge ZIP or tarball cannot prove source identity and is unsupported.
The launcher is a root trust boundary on Unraid; obtain it and the checkout from
the same reviewed release.

## Configure

Create a private copy of the example outside the repository:

```bash
install -d -m 0700 /absolute/private/path
cp .env.example /absolute/private/path/cwa-translate.env
chmod 0600 /absolute/private/path/cwa-translate.env
```

At minimum, set:

```dotenv
BT_INSTALL_PROFILE=unraid
BT_INSTALL_NAME=cwa-translate
BT_INGRESS_MODE=published
BT_PROXY_PORT=8385
BT_AUTH_PROFILE=cwa-session
BT_PUBLIC_ORIGIN=https://books.example.com
BT_READER_TYPE=cwa
BT_READER_UPSTREAM=http://calibre-web-automated:8083
BT_READER_CONTAINER=calibre-web-automated
BT_READER_NETWORK=cwa_default
BT_READER_VERSION=4.0.6
BT_CWA_IDENTITY_HEADER=Remote-User
BT_STATE_DIR=/absolute/private/path/state
BT_DATA_DIR=/absolute/private/path/data
BT_BACKUP_DIR=/absolute/private/path/backups
LLM_PROVIDER=local
LLM_MODEL=gemma4-12b
BT_LOCAL_URL=http://192.168.1.50:8000/v1/chat/completions
LLM_API_KEY=
```

For Compose, change `BT_INSTALL_PROFILE=compose-existing` and use paths writable
through the trusted operator's private group. For Unraid, use existing
`/mnt/user/<share>` or `/mnt/<pool>` roots and set
`BT_UNRAID_TEMPLATE_DIR=/boot/config/plugins/dockerMan/templates-user`.
`btctl` refuses misspelled share/pool roots.

An exclusive share (Settings → Global Share Settings → Permit exclusive shares)
turns `/mnt/user/<share>` into Unraid's link `../<pool>/<share>`. `btctl`
follows only that exact link: it checks the pool directory, binds it to the
configured `/mnt/user` path and keeps refusing every other symbolic link. The
same environment file therefore works whether or not the share is exclusive.

New Compose installs record split state schema 3. Their generated JSON contains
only paths to raw mode-`0600` `api.env` and `proxy.env` files, never provider
credentials. Former schema-2 inline-environment deployments remain available
to `doctor` and `uninstall`, but must be uninstalled and reinstalled before
provider-only reconfiguration can use the new artifact boundary.

For CWA, `BT_READER_UPSTREAM` must be exactly
`http://<BT_READER_CONTAINER>:8083`. The legacy `CWA_UPSTREAM`,
`BT_CWA_CONTAINER`, `BT_CWA_NETWORK` and `BT_CWA_VERSION` aliases remain valid
only for CWA and must not conflict with reader-neutral values.
`BT_CWA_IDENTITY_HEADER` must match CWA's configured reverse-proxy login header;
the managed proxy strips client-supplied copies before forwarding to CWA. A
local provider normally leaves `LLM_API_KEY` empty.

The split-profile template defaults to local so updating an existing deployment
never exports book text by surprise. Gemini can be used as the complete backend
without a local model or fallback:

```dotenv
LLM_PROVIDER=gemini
LLM_MODEL=gemini-3.5-flash-lite
LLM_API_KEY=<Gemini API key>
LLM_CUSTOM_ENDPOINT=
LLM_CUSTOM_API_KEY=
LLM_FALLBACK_PROVIDER=
LLM_FALLBACK_MODEL=
LLM_FALLBACK_API_KEY=
LLM_FALLBACK_CUSTOM_ENDPOINT=
LLM_FALLBACK_CUSTOM_API_KEY=
BT_LOCAL_URL=
```

Gemini uses Google's fixed OpenAI-compatible endpoint documented in the
[Gemini API reference](https://ai.google.dev/gemini-api/docs/openai). An API
key is distinct from a consumer Gemini, ChatGPT, Codex or Antigravity
subscription; managed deployments do not import browser sessions or consumer
subscription credentials. A local or remote fallback may be added separately
after reviewing the consent and retry boundaries in the
[configuration reference](../reference/configuration.md); it is never required
for Gemini.

For another public OpenAI-compatible service, set
`LLM_PROVIDER=openai-compatible`, leave `LLM_API_KEY` empty and use the
dedicated `LLM_CUSTOM_ENDPOINT` and `LLM_CUSTOM_API_KEY`. Custom URLs must use
public HTTPS and the exact `/v1/chat/completions` path. Private/LAN endpoints
belong under `local`.

For Kavita, use a second private environment and follow the complete
[Kavita installation contract](kavita.md). Do not convert an active CWA
installation in place or share its install name, public origin, state, data or
backup directories.

## Authentication and ingress

`cwa-session` is the CWA default. It exchanges selected native CWA cookies
against the exact authenticated JSON endpoint for a short-lived plugin session
and supports `config_session=1` by preserving the browser User-Agent and the
address observed by the managed proxy. Keep CWA's default
`TRUSTED_PROXY_COUNT=1` and do not create a bypass to the API.

`reader-session` is required for Kavita and may also be selected explicitly for
CWA. It uses the same opaque-session boundary; the reader-specific guide defines
which native proof and account endpoint are accepted. HTTPS is mandatory outside
loopback development.

`authentik-forwarded` is a separate advanced topology. It requires
`docker-edge`, an exact trusted identity peer and the generated direct API edge
route. Follow the [Authentik guide](authentik.md). Disabled authentication,
browser shared tokens, broad trusted CIDRs and a published API are not managed
fallbacks.

In `published` mode, only the proxy maps a host port. In `docker-edge`, neither
role is published and the configured external edge network must already exist.
The private API network uses no fixed subnet.

## Plan, install and verify

Validate before changing deployment state:

```bash
./btctl plan --env /absolute/private/path/cwa-translate.env
```

Review source/image identity, reader evidence, roles, ports, networks, paths and
ownership. Then install and verify:

```bash
./btctl install --env /absolute/private/path/cwa-translate.env --yes
./btctl doctor --env /absolute/private/path/cwa-translate.env
```

On Unraid, `./install_unraid.sh /absolute/private/path/cwa-translate.env` is an
equivalent root-only convenience wrapper.

On a host without Python 3.11 or Git, the Bash launcher builds a short-lived,
socket-free source exporter and a separate operator image containing Python,
Git and the Docker CLI. The exporter receives the checkout read-only, disables
Git replacement refs, verifies the clean commit and streams its archive. Only
the verified operator receives command-specific mounts and, when required, the
local Docker socket. `plan` and `auth-snippet` receive no socket. The bootstrap
may fetch pinned base images and warm ordinary build cache, but `plan` creates
no deployment state or runtime resources.

The Docker socket is equivalent to root. The Unraid fallback is root-only,
forces `/var/run/docker.sock`, ignores remote Docker contexts and never mounts
the socket into the exporter. The production image is separate and runs both
long-lived roles as uid `101`, gid `102` with a read-only root filesystem and no
capabilities.

## Generated artifacts

The Compose profile writes a private mode-`0600` Compose JSON document and
state. The Unraid profile writes API/proxy DockerMan templates that reference
private role environment files and the immutable local image. The additional
network cannot be represented safely in DockerMan's single-network form, so do
not press **Apply** on generated templates; use `btctl` for lifecycle changes.

## Change providers without replacing the proxy

Copy the current private environment file, change only provider variables and
review the redacted provider cutover:

```bash
./btctl reconfigure --env /absolute/private/path/new-provider.env
./btctl reconfigure --env /absolute/private/path/new-provider.env --yes
./btctl doctor --env /absolute/private/path/new-provider.env
```

`reconfigure` rejects changes to reader, ingress, auth, storage, image or
network identity. It snapshots both private configurations, journals no raw
key or endpoint, replaces only the API role and requires every configured
backend plus reader authentication/cache dependencies to pass. State is
published only after verification. A failed or interrupted cutover restores
the old private environment and API role before a fresh retry. The proxy,
private network, data, installation UUID, connector UUID and on-disk session
key remain unchanged. In-memory five-minute reader sessions are process-local,
so an API replacement may require one automatic session exchange in an open
reader tab.

## Acceptance

- `doctor` succeeds with every check `ok`.
- The API has no host `PortBindings`; only the proxy is published in
  `published` mode and neither role is published in `docker-edge`.
- Direct stock-reader service routes continue to work; CWA OPDS/Kobo and
  Kavita non-browser clients remain outside the overlay.
- Through `BT_PUBLIC_ORIGIN`, sign in, open a DRM-free EPUB, translate with
  different source and target languages, change page and reload.
- The toolbar survives reload and browser storage exposes no translator token
  or provider key.
- Authentik installations also complete the guide's public-path checks.
- Kavita installations also complete the exact-version, EPUB-route and
  unsupported-route checks in the [Kavita guide](kavita.md).

For `doctor`, `adopt`, migration, rollback, uninstall and failure recovery, use
the [lifecycle guide](../operations/lifecycle.md). Do not recover by exposing
the API, disabling auth, editing state or applying generated templates.
