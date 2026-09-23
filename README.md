# Book Translator Hub

<p align="center">
  <img src="docs/assets/hero-editorial.png" alt="Book Translator Hub — bilingual EPUB reading, illustrated by an open book" width="100%">
</p>

<p align="center">
  <a href="https://github.com/felixapel/book-translator-hub/releases/latest"><img src="https://img.shields.io/github/v/release/felixapel/book-translator-hub?style=flat-square" alt="Latest published release"></a>
  <a href="https://github.com/felixapel/book-translator-hub/actions"><img src="https://img.shields.io/badge/CI-gated-0ea5e9.svg?style=flat-square" alt="CI-gated"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--3.0-blue.svg?style=flat-square" alt="License: GPL-3.0"></a>
  <img src="https://img.shields.io/badge/Python-3.11-3776ab.svg?style=flat-square&logo=python&logoColor=white" alt="Python 3.11">
  <img src="https://img.shields.io/badge/Docker-Multi--Arch-2496ed.svg?style=flat-square&logo=docker&logoColor=white" alt="Docker">
  <a href="https://github.com/sponsors/felixapel"><img src="https://img.shields.io/badge/Sponsor-GitHub-ea4aaa.svg?style=flat-square&logo=githubsponsors&logoColor=white" alt="GitHub Sponsors"></a>
  <a href="https://ko-fi.com/felixapel"><img src="https://img.shields.io/badge/Donate-Ko--fi-ff5e5b.svg?style=flat-square&logo=kofi&logoColor=white" alt="Ko-fi"></a>
</p>

<p align="center">
  <b>Bilingual reading overlay and translation engine for stock Calibre-Web-Automated and Kavita EPUB readers, powered by local or cloud LLMs.</b>
</p>

---

<p align="center">
  <img src="docs/assets/bilingual-reading-editorial.png" alt="Concept illustration of aligned original and translated paragraphs beside an e-reader" width="100%">
  <br><sub>Editorial illustration of bilingual reading, not a product screenshot.</sub>
</p>

## ✨ What it does

- **Streaming and visible-work priority:** Streams eligible first-paragraph output and prioritizes visible paragraphs ahead of bounded background work. Actual latency depends on the reader, provider, model, network and cache state.
- **Bounded lookahead:** Can pre-translate a limited forward window while keeping visible requests ahead of background work. It is not a page-turn latency guarantee.
- **Layered cache:** Uses an in-memory reading cache and a private SQLite cache. Optional browser persistence is controlled by the server-owned reader configuration and may be unavailable when browser storage is constrained.
- **SQLite WAL configuration:** Uses WAL, bounded busy timeouts, a memory-mapped I/O window and page cache settings. Operators should measure cache behavior on their own storage and workload.
- **Language and reading controls:** Detects source language from reader metadata/HTML with a manual override, plus a target-language selector on the floating toolbar.
- **Manual activation:** Each book entry and page reload starts with translation OFF, even if this browser previously translated the book. Book text is sent for translation only after the reader turns the mode on.
- **E-Ink display mode:** Provides a high-contrast, low-motion presentation intended for compatible e-readers; verify it on the target browser and device.
- **Versioned reader connectors:** Integrates with stock [Calibre-Web-Automated](https://github.com/crocodilestick/Calibre-Web-Automated) and exact Kavita EPUB contracts without modifying upstream images. See the compatibility matrix for acceptance limits.
- **Server-side provider credentials:** Managed configurations keep API keys in the private server environment. Browser configuration omits provider keys; operators must protect their environment files and proxy boundary.

---

## 🚀 Supported installation

Choose a tag from the [published releases](https://github.com/felixapel/book-translator-hub/releases).
`VERSION` identifies the checked-out source; changes on `main` may not yet be
available in a published release. See the [release process](docs/maintainers/release.md)
for the acceptance and publication requirements.

The production path is the universal `btctl` hub. It builds an immutable local
image from an exact clean checkout and runs CWA, Kavita or both through
one hardened container while keeping separate internal API processes, caches,
keys and cookies:

```bash
git clone https://github.com/felixapel/book-translator-hub.git book-translator-hub
cd book-translator-hub
# Select an immutable published tag from the GitHub Releases page before installing.
git switch --detach vX.Y.Z
```

Copy the managed configuration outside the checkout and make it private:

```bash
install -d -m 0700 /absolute/private/path
cp .env.hub.example /absolute/private/path/book-translator-hub.env
chmod 0600 /absolute/private/path/book-translator-hub.env
```

Set each enabled reader's exact container, network, version, public origin and
storage paths. The managed template defaults to a local OpenAI-compatible
provider. Replace its placeholder endpoint and model in the private copy:

```dotenv
BT_ENABLE_CWA=true
BT_ENABLE_KAVITA=true
LLM_PROVIDER=local
LLM_MODEL=local-model
LLM_API_KEY=
BT_LOCAL_URL=http://local-llm:8000/v1/chat/completions
BT_BATCH_SIZE=5
BT_BATCH_SOURCE_TOKEN_BUDGET=0
BT_BATCH_MAX_TOKENS=8192
BT_MAX_BATCH_PARAGRAPHS=50
```

Named remote providers, including Gemini, use their fixed HTTPS API endpoints
and a server-side API key. Local and custom OpenAI-compatible backends are
also supported through environment variables; select a cloud provider only in
the private environment after reviewing its privacy and quota implications.
Then run:

```bash
./btctl plan --env /absolute/private/path/book-translator-hub.env
./btctl install --env /absolute/private/path/book-translator-hub.env --yes
./btctl doctor --env /absolute/private/path/book-translator-hub.env
```

`plan` validates and reports the intended resources without changing the reader
or deployment state. `install` commits state only after live postconditions pass.
`doctor` is read-only and every check must report `ok`.

Provider roles are selected entirely through the private environment, with
shared defaults and optional per-reader overrides. The split profile retains
its provider-only `btctl reconfigure` workflow. A hub provider change uses a
reviewed `uninstall` with the old environment followed by `install` with the
new one; translation data is retained, hub-owned session keys are regenerated,
and all reader processes restart coherently, so tabs may require a fresh
short-lived session.
See the [configuration reference](docs/reference/configuration.md). A ChatGPT,
Codex, Gemini or Antigravity consumer subscription is not an API credential.

Stock Unraid requires root, Bash, Docker and a full checkout including `.git`.
It does not require host Python, host Git or NerdTools to run `./btctl`; the
launcher uses a temporary containerized operator when needed. Linux hosts use
`BT_INSTALL_PROFILE=compose-existing`. Operators who need independent
container isolation or CWA Authentik-forwarded identity can retain the split
profile.

Community Applications uses a separate CWA-only combined-image profile. Install
it only from a searchable listing whose template pins an immutable image digest;
if no listing is present, use `btctl`. See the
[Community Applications guide](docs/install/community-applications.md).

## 🛡️ Runtime boundary

```text
Browser / reverse proxy -> injection proxy -> stock CWA or Kavita
                                  |
                                  +-> private translation API -> LLM
                                                     |
                                                     +-> SQLite cache
```

Managed native-reader profiles exchange existing CWA or Kavita proof for a
short-lived, opaque translator session. Raw reader credentials are confined to
the exact exchange endpoint; ordinary translation calls never receive them.
CWA strong-session binding and Kavita native/OIDC login are supported within
their documented boundaries. Do not publish the API, disable authentication,
or add a route that bypasses the managed proxy. Advanced CWA Authentik
deployments have a separate fail-closed profile and guide.

CWA `4.0.6` is the exact CWA contract reference; each deployed candidate still
requires browser acceptance. Kavita has separate exact `0.9.0.2` and
`0.9.1.4` contracts; the `0.9.1.4` native account/broker fixture does not
establish browser or OIDC acceptance. See the
[compatibility matrix](docs/reference/compatibility.md) before choosing a
reader version. Manga, PDF and library writeback are not supported.

## 💖 Sponsors & Community Support

Book Translator Hub is 100% free and open-source software built for the self-hosted and reading communities. Development, local GPU testing (vLLM/Ollama), and continuous integration are maintained independently.

If you find Book Translator Hub valuable for your daily reading, please consider supporting the project:

<p align="center">
  <a href="https://github.com/sponsors/felixapel">
    <img src="https://img.shields.io/badge/Sponsor%20on-GitHub%20Sponsors-ea4aaa?style=for-the-badge&logo=github&logoColor=white" alt="GitHub Sponsors">
  </a>
  &nbsp;&nbsp;
  <a href="https://ko-fi.com/felixapel">
    <img src="https://img.shields.io/badge/Support%20on-Ko--fi-ff5e5b?style=for-the-badge&logo=kofi&logoColor=white" alt="Support on Ko-fi">
  </a>
</p>

* **Bug Bounties & Hardware:** Supports purchasing hardware for testing on physical E-Ink devices and running local GPU benchmarks.
* **Feature Requests:** Backers can directly influence the roadmap for future reader integrations (Audiobookshelf, Komga, Readium).

## 📖 Documentation

- [Documentation map](docs/README.md)
- [Universal CWA and Kavita hub](docs/install/universal-hub.md)
- [Managed `btctl` install](docs/install/btctl.md)
- [Kavita managed install](docs/install/kavita.md)
- [Community Applications](docs/install/community-applications.md)
- [Authentik integration](docs/install/authentik.md)
- [Lifecycle and recovery](docs/operations/lifecycle.md)
- [Troubleshooting](docs/operations/troubleshooting.md)
- [Compatibility](docs/reference/compatibility.md)
- [Configuration](docs/reference/configuration.md)
- [Architecture](docs/reference/architecture.md)

## 🤝 Development and support

Read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[development guide](docs/maintainers/development.md) before changing the
project. Use the issue templates for reproducible bugs and feature proposals.
Report vulnerabilities through the private channel in [SECURITY.md](SECURITY.md).

Book Translator Hub is GPL-3.0 software with no telemetry, ads or subscription.
Support is optional through [Ko-fi](https://ko-fi.com/felixapel) or
[GitHub Sponsors](https://github.com/sponsors/felixapel). The project is not
affiliated with or endorsed by CWA, Kavita, Calibre, Google or any LLM provider.

See [LICENSE](LICENSE) for the license text.

