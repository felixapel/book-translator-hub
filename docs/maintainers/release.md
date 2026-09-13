# Release runbook

Gitea is the source, branch, tag and release authority. GitHub is a public
mirror and publishes the matching GHCR image used by Community Applications.
Release tags are protected, annotated and immutable. Never recreate or move a
published `v*` tag.

## Persistent prerequisites

- Gitea `main` rejects direct and force pushes and requires backend, frontend
  and Docker smoke contexts.
- `v*` tags are protected and restricted to the release operator.
- Gitea automatically deletes merged branches.
- The trusted Docker runner is online; Actions are not manually rerun to hide a
  failing or unavailable gate.
- GitHub `main` and release tags mirror exact Gitea objects.
- The GHCR package is public before anonymous digest verification.

## Prepare a candidate

Set release coordinates from repository truth:

```bash
VERSION=$(tr -d '\n' < VERSION)
TAG="v${VERSION}"
SHA=$(git rev-parse HEAD)
test "$(git branch --show-current)" != main
git status --short
```

Update `VERSION` and the top section of [CHANGELOG.md](../../CHANGELOG.md) only
when the release itself changes. Durable guides must remain version-neutral
except for the current checkout command in the public README. Candidate status,
acceptance notes and announcement drafts belong in the release issue or pull
request.

Run the maintained local gates from a clean checkout:

```bash
git diff --check
python3 scripts/check_docs.py
python3 -m tests.python.test_translation
python3 -m tests.python.test_hardening
git ls-files -z -- '*.py' | xargs -0 -r python3 -m py_compile
bash -n btctl install_unraid.sh scripts/*.sh
python3 -m coverage erase
python3 -m coverage run --branch --source=. \
  --omit='.venv/*,tests/*,tools/*' -m unittest discover -v tests/python
python3 -m coverage report --precision=1 --show-missing --fail-under=60
node -c static/translator.js
node -c static/loader.js
npm ci
npm audit --audit-level=high
npm test
npm run test:e2e
docker build -t "cwa-translate:${VERSION}-candidate" .
./scripts/container-smoke.sh "cwa-translate:${VERSION}-candidate" release
./scripts/btctl-lifecycle-smoke.sh "cwa-translate:${VERSION}-candidate" release
./scripts/hub-container-smoke.sh "cwa-translate:${VERSION}-candidate" release-hub
./scripts/btctl-bootstrap-smoke.sh release
./scripts/ca-container-smoke.sh "cwa-translate:${VERSION}-candidate" release-ca
python3 scripts/release_preflight.py --help
```

Use an independent review for authentication, migration, Docker privilege,
release-workflow or public-interface changes. A required failure blocks the
candidate; fix it on the branch and let protected CI evaluate the new commit.

## Merge and physical acceptance

Open one scoped Gitea pull request and record the problem, risk, validation and
rollback boundary. Merge only when all required contexts succeed on the exact
head. Then fast-forward the canonical checkout and confirm protected `main` CI
on the exact merge commit.

Before tagging a runtime change, run physical stock-Unraid and browser
acceptance on that exact commit. The source-built path must complete
`plan -> install -> doctor`; the reader must translate a non-sensitive DRM-free
EPUB through the public route. Record host, exact reader version and image,
browser, LLM, commit/digest and result in the release issue without secrets.

Do not copy browser results from a prior candidate. Re-run the authenticated
checks after each runtime-image or checkout change, and hard-refresh/sign in
again after reinstall because reader session keys are regenerated. A healthy
`/ping`, `/health` or `/ready` response is process evidence only, not proof of
translation or browser authentication.

Record the physical gate with this complete template:

```text
Candidate tag:
Commit and image digest:
Unraid version and architecture:
CWA image/tag and immutable image ID:
Kavita image/tag and immutable image ID:
Browser and version:
LLM provider/model:
plan -> install -> doctor:
CWA EPUB/auth/navigation/reload:
Kavita EPUB/auth/navigation/reload:
Manga/PDF inactivity:
CWA/Kavita ports, state, data, keys and backups isolated:
Rollback/fix-forward result:
Secrets/book content included: no
```

For Kavita, also prove stock v0.9.0.2, exact EPUB route and `.book-content`,
native login exchange, chapter navigation, reload, and inactivity on manga/PDF
routes. When CWA and Kavita coexist, prove their names, ports, state, data,
backups and lifecycle operations are isolated. Community Applications
candidates require their separate digest-pinned CWA-only checklist.

## Reader connector promotion sequence

Keep audit hardening and a new reader compatibility claim independently
releasable. Ship audit corrections in the next patch release after
its normal gates. Introduce any new reader connector as a release candidate only
after unit, Chromium, container and lifecycle gates pass on the exact candidate. Promote
to stable only after the physical reader checklist above is recorded on that exact code;
otherwise issue a new release candidate and retain candidate status.
Do not add Kavita to Community Applications as part of this sequence.

## Publish source and mirror

Run the repository preflight with the exact tag, commit and both remote names.
Create one local annotated tag object and verify its object ID and peeled commit.
Push that exact object to the public GitHub mirror first and verify both remote
IDs. Only then push the same tag object to authoritative Gitea; this ordering is
required because the Gitea tag workflow immediately verifies that the public
mirror already contains the identical immutable object. Create release records
from the matching changelog section. Gitea and GitHub source tags must match
exactly; source archives are generated by the forges and are not `btctl`
installation inputs because they lack `.git` identity evidence.

Do not publish a project image from Gitea. Dispatch the reviewed GitHub image
workflow only after GitHub contains the exact mirrored tag. Verify the emitted
GHCR digest anonymously and confirm image labels report the same version and
revision.

## Community Applications promotion

Treat publication as a second, reversible gate after the source release:

1. Build and publish the combined image from the immutable mirrored tag.
2. Record and anonymously verify its multi-platform manifest digest.
3. Update the separate public `unraid-templates` repository so the XML pins
   `ghcr.io/felixapel/cwa-ebook-translate-plugin@sha256:<digest>`.
4. Validate XML syntax, template fields, icon, support/project links and that no
   API port is exposed.
5. Install that exact template on physical Unraid and complete the browser
   acceptance checklist.
6. Submit or update Community Applications and confirm it is searchable.
7. Only then publish Reddit/forum announcements from the release issue.

If physical acceptance or listing review fails, remove or quarantine the
template update and fix forward. Do not delete or move the source tag or release
to conceal a packaging failure.

## Rollback and historical invariants

- Runtime rollback uses `btctl rollback` only for the exact journaled v2.1.4
  migration described in [lifecycle](../operations/lifecycle.md).
- A bad unreleased candidate is replaced by a new commit.
- A bad published release is superseded by a new patch version; its tag remains.
- CA/template publication is reversible independently of immutable source tags.
- Gitea and GitHub historical `v2.0.0` tag objects intentionally differ. Never
  rewrite either; [ADR-001](../decisions/ADR-001-gitea-release-authority.md)
  records the authority decision.

