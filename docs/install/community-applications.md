# Community Applications on Unraid

Community Applications is the simplest supported profile when a searchable CWA
or Kavita listing exists. It runs one combined non-root container (`BT_ROLE=all`)
that proxies stock Calibre-Web Automated, Kavita, or both, while publishing the
direct translation and SSE streaming API port (`8390`) alongside the reader proxy (`8385`).

If the listing is absent, its template does not pin an immutable digest, or the
host is outside the certified scope, use the source-built
[`btctl` profile](btctl.md). Do not install from an unreviewed XML URL or a
mutable `latest` image.

## Certified boundary

- Stock Unraid 7.3.2 on x86_64 (`linux/amd64`).
- Stock CWA 4.0.6 reachable on an explicitly selected Docker network.
- Native CWA sessions with default `TRUSTED_PROXY_COUNT=1`.
- Local OpenAI-compatible `/v1/chat/completions` provider.
- One combined `BT_ROLE=all` container running as `101:102` with private mode
  `0700` appdata.
- Host port `8385` maps to proxy port `8080`; direct API and SSE streaming port `8390` is published.
- An image reference pinned by immutable `sha256` digest.

Other versions or topologies may work but are not release-certified. Authentik
forwarded identity, v2.1.4 migration and independent API/proxy roles use
`btctl`.

## Before installation

Record the exact CWA container name, Docker network, public reader origin and
LLM endpoint. Create a private appdata directory and do not reuse a live legacy
database directory. A local LLM normally requires no API key.

Verify the listing's image field includes `@sha256:<digest>` and its support and
project links point to this repository. A tag alone is not sufficient.

## Install and configure

Open the searchable listing, review every field and set:

- the CWA container/upstream and shared network;
- the public reader origin and host proxy port;
- the local model and absolute provider URL;
- the private appdata path;
- native CWA-session authentication.

Leave the API port unpublished, keep disabled authentication off and never put
a CWA session, provider key or book text in the template fields or support
screenshots.

The profile derives `BT_AUTH_MODE=cwa_session` and validates the native session
through CWA's `/ajax/emailstat` contract. Keep CWA's **Allow Reverse Proxy
Authentication** setting off for this profile; header-based identity belongs to
the separate Authentik topology.

After Apply, read CWA through the translator proxy port or route the existing
reader domain to it. Keep OPDS and Kobo routes pointed directly at CWA.

## Acceptance

On the exact installed digest:

1. Confirm the container is healthy and runs as uid `101`, gid `102` with no
   extra capabilities.
2. Confirm only proxy port `8080` is mapped and host port `8390` is absent.
3. Sign in through the public reader origin, open a DRM-free EPUB and translate
   a paragraph with different source and target languages.
4. Change page, reload and verify the toolbar and translation still work.
5. Confirm browser storage and network requests expose no provider key or
   translator token.
6. Confirm direct CWA, OPDS and Kobo behavior remains unchanged.

The release runbook requires this physical acceptance before a new template is
announced or submitted. See the [compatibility matrix](../reference/compatibility.md)
and [troubleshooting guide](../operations/troubleshooting.md).

