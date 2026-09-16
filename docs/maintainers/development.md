# Development Guide

This guide details how to work on the `book-translator` codebase.

## Backend Development

The backend is a Flask application running in python. 

### Local Setup

1. Create a virtual environment:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```
2. Install dependencies:
   ```bash
   python -m pip install --require-hashes --only-binary=:all: \
     -r requirements/requirements.txt
   python -m pip install --require-hashes --only-binary=:all: \
     -r requirements/requirements-audit.txt
   ```
3. Run the development server:
   ```bash
   BT_AUTH_MODE=token BT_API_TOKEN=local-development-only python3 server.py
   ```

### Running Tests

The backend test suite is self-contained — it mocks the LLM and the database
file, so it needs no running server, no API key, and no network access:
```bash
.venv/bin/python3 -m tests.python.test_translation
.venv/bin/python3 -m tests.python.test_hardening
.venv/bin/python3 -m coverage erase
.venv/bin/python3 -m coverage run --branch --source=. \
  --omit='.venv/*,tests/*,tools/*' -m unittest discover -v tests/python
.venv/bin/python3 -m coverage report --precision=1 --show-missing --fail-under=60
```

Always also check syntax/compile before committing:
```bash
git ls-files -z -- '*.py' | xargs -0 -r python3 -m py_compile
bash -n btctl install_unraid.sh scripts/*.sh
```

The installer contract is self-contained and uses disposable Git repositories;
it never contacts Docker or a live CWA instance:

```bash
python3 -m unittest -v \
  tests.python.test_btctl tests.python.test_btctl_container \
  tests.python.test_btctl_compose tests.python.test_btctl_unraid \
  tests.python.test_btctl_auth tests.python.test_btctl_lifecycle \
  tests.python.test_docs_contract
```

Use `./btctl plan --env /absolute/path/install.env --json` to inspect a clean
checkout. `btctl.py` is the internal Python entry point; operator documentation
and integration tests must use the public `./btctl` dispatcher. Its stock-Unraid
fallback may build temporary helper images and warm Docker cache, but `plan`
must not change deployment files, state, CWA, or running containers.

The real-Docker regression for a host with no Python is:

```bash
./scripts/btctl-bootstrap-smoke.sh "btctl-bootstrap-$(git rev-parse --short=12 HEAD)"
```

It runs the public dispatcher in a clean checkout, removes Python and Git from
the simulated host image, verifies that `plan --json` reports the exact commit,
and exercises Unraid install, doctor, and uninstall through the same fallback.

The rate-limit probe and benchmark modules hit a **live** API
(`BENCHMARK_URL`, default `http://127.0.0.1:8390`). Start the server with the
token-mode command above before running them. The rate-limit probe requires a
fresh limiter window and valid authentication.
It sends same-language requests, so it exercises authentication and admission
without calling the configured translation provider. Its default 310 probes
cover the default limit of 300; raise `--requests` if your deployment uses a
higher `BT_RATE_LIMIT_PER_MINUTE`:

```bash
BT_API_TOKEN='<token>' python3 -m tools.probes.rate_limit \
  --url http://127.0.0.1:8390 --requests 130 --timeout 5
```

For a managed native-reader proxy, first exchange valid native proof through
the exact `POST /bt-api/session` route. Pass the issued plugin cookie and the
same User-Agent through environment variables rather than the command line, and
run the probe from the same client address that performed the exchange. The
cookie expires in at most five minutes:

```bash
BT_RATE_LIMIT_TEST_COOKIE='__Host-bt-session=<opaque-value>' \
BT_RATE_LIMIT_TEST_USER_AGENT='Mozilla/5.0 ... exact browser value' \
  python3 -m tools.probes.rate_limit \
  --url https://books.example.test/bt-api
```

It exits nonzero on connection/authentication errors, unexpected statuses, or
if it does not observe both an admitted request and a `429` response. The probe
ignores inherited `HTTP_PROXY`/`HTTPS_PROXY` settings, refuses redirects and URL
credentials, streams no response body, and closes each response immediately so
the plugin cookie stays bound to the explicitly selected origin.

The two benchmark scripts enforce the same boundary and also fail on redirects,
non-2xx responses, or invalid JSON. Use one authentication mechanism only:

```bash
BT_API_TOKEN='<token>' python3 -m tools.benchmarks.benchmark \
  --url http://127.0.0.1:8390
BT_BENCHMARK_COOKIE='__Host-bt-session=<opaque-value>' \
BT_BENCHMARK_USER_AGENT='Mozilla/5.0 ... exact browser value' \
  python3 -m tools.benchmarks.benchmark_realistic \
  --url https://books.example.test/bt-api
```

Do not paste credentials into a URL or publish benchmark output containing
private endpoint names. A mismatched User-Agent, address or origin intentionally
fails closed; perform a new native-proof exchange rather than weakening the
binding.

## Frontend Development

The production frontend consists of `static/loader.js`,
`static/translator.js`, and `static/translator.css`. `overlay/read.html` is a
legacy CWA development fixture, not a production integration. CI reads the
exact supported LTS release from `.node-version`; use the same version locally.

### Syntax Validation & Tests

```bash
node -c static/translator.js   # syntax check
npm ci                         # exact package-lock.json dependency tree
npm test                       # runs tests/frontend/test_frontend.js
npx playwright install --with-deps --only-shell chromium
npm run test:e2e               # real Chromium: loader, DOM, network, a11y, consent
```

The browser suite starts localhost-only CWA and Kavita reader fixtures,
intercepts only their translation/session boundaries, and fails on browser
console errors, warnings, page exceptions, or failed requests. It covers the
CWA iframe and exact Kavita EPUB route/`.book-content` adapter, including SPA
teardown on unsupported routes. To reuse a compatible local Chromium instead
of Playwright's managed headless shell, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/absolute/path/to/chromium`.

## Updating Dependency Locks

`requirements/requirements.in` records runtime intent.
`requirements/requirements.txt` is the reviewed production lock; every direct
and transitive dependency is version-pinned and hashed. The auditor and lock
compiler have independent locks in the same directory so CI does not resolve
mutable tooling at runtime.

Regenerate all three locks with the currently approved compiler:

```bash
python3.11 -m venv /tmp/cwa-lock-tools
/tmp/cwa-lock-tools/bin/python -m pip install \
  --require-hashes --only-binary=:all: \
  -r requirements/requirements-compile.txt
LOCK_PYTHON=/tmp/cwa-lock-tools/bin/python \
  ./scripts/compile-requirements.sh
git diff -- requirements/
```

Review every version and hash change, run the complete test/container gate, and
commit the `.in` file and its generated lock together. To run dependency audits
locally, install `requirements/requirements-audit.txt` with the same two pip
safety flags and then run `./scripts/audit-deps.sh`.

### Manual Testing

The automated Chromium gate covers loader isolation, both reader adapters,
translation rendering, cloud-fallback consent, authentication replay and the
control accessibility tree. Real CWA/EPUB.js and Kavita/Angular compatibility
still require the pinned applications.

After any change to `getTranslatableElements`, paragraph detection, or
rendering, manually verify in a browser: open an EPUB in CWA, cycle
Original → Bilingual → Translated, change chapters/pages, and check Light /
Dark / Sepia themes (translation styling is injected into the reader
`<iframe>` — see `ensureIframeStyles` in `translator.js`).

For Kavita, use an exact supported stock version through the managed HTTPS
origin. `0.9.0.2` and `0.9.1.4` are separate contracts: split smoke retains
`0.9.0.2` by default, while hub smoke exercises `0.9.1.4` native-account
coverage. The latter requires a positive `id` and matching `kavitaVersion` in
the isolated fixture; it is not live browser or OIDC verification. Open the
exact `/library/.../series/.../book/...` EPUB route, exercise translation and
chapter navigation, then navigate to manga, PDF and library pages and confirm
the toolbar and observers are removed. Test native login and, for OIDC, perform
the version-specific browser check before treating it as supported. Do not use
private book text in evidence.

