# Contributing to Book Translator Hub

Thanks for helping improve the project. Keep changes narrow, explain the user
problem, and include a regression test for behavior changes.

## Development setup

1. Fork and clone the repository, then create a short-lived branch.
2. Install Python 3.11 and Node from `.node-version`.
3. Create a virtual environment and install the reviewed lock:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   python -m pip install --require-hashes --only-binary=:all: \
     -r requirements/requirements.txt
   python -m pip install --require-hashes --only-binary=:all: \
     -r requirements/requirements-audit.txt
   npm ci
   ```

Tests use fakes and do not need an LLM key. Keep real credentials in a private
environment file outside the checkout; never commit `.env`, cookies, book text,
provider keys, or generated deployment state.

## Required checks

Run the smallest relevant test while developing, then the maintained gates
before opening a pull request:

```bash
python3 -m tests.python.test_translation
python3 -m tests.python.test_hardening
python3 -m coverage erase
python3 -m coverage run --branch --source=. \
  --omit='.venv/*,tests/*,tools/*' -m unittest discover -v tests/python
python3 -m coverage report --precision=1 --show-missing --fail-under=60
git ls-files -z -- '*.py' | xargs -0 -r python3 -m py_compile
bash -n btctl install_unraid.sh scripts/*.sh
node -c static/translator.js
node -c static/loader.js
npm ci
npm audit --audit-level=high
npm test
npx playwright install --with-deps --only-shell chromium
npm run test:e2e
```

Container or installer changes must also run the applicable smoke commands in
the [release runbook](docs/maintainers/release.md). Live benchmark scripts
require explicit authentication; see the
[development guide](docs/maintainers/development.md).

Dependency updates must change the relevant `requirements/requirements*.in`
file, regenerate
the committed hash lock with `scripts/compile-requirements.sh`, and include the
reviewed diff. Do not hand-edit generated requirement locks.

## Pull requests

- Explain the problem, approach, risk, and verification evidence.
- State the exact tag or commit used for runtime reproductions.
- Add or update tests before changing behavior.
- Keep frontend changes compatible with light, dark, sepia, and black reader
  themes and exercise the real Chromium suite.
- Do not include raw `/metrics`, logs, cookies, paths, book text, or provider
  responses without redacting private data.

For bugs, use the issue template and begin with `./btctl doctor --json` on
managed installs. Security reports belong in the private channel described in
`SECURITY.md`, never a public issue.

