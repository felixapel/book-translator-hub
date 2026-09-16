# Repository guidance

## Scope and authority

- Gitea `felix/book-translator-hub` is authoritative for branches, pull
  requests, tags and releases. GitHub is a public mirror and GHCR publisher.
- Never push directly to protected `main`, rewrite a published `v*` tag or
  manually rerun Actions to bypass a failed gate.
- Keep one canonical checkout. Use short-lived branches/worktrees for one pull
  request and remove them after merge.
- Preserve the public root entrypoints `btctl`, `install_unraid.sh`,
  `.env.example`, `Dockerfile*`, `docker-compose.yml` and `VERSION`.

## Sources of truth

- `VERSION` is the code version; `CHANGELOG.md` is release history.
- `docs/README.md` indexes every durable guide. Candidate status, acceptance
  evidence and announcement drafts belong in Gitea issues or pull requests.
- Architecture decisions stay in `docs/decisions`; supersede rather than erase.
- `.env.example` and `btctl` validation define managed installation inputs.

## Change loop

1. Confirm repository instructions and the narrow acceptance criteria.
2. Add or update a regression test before behavior changes.
3. Make the smallest coherent change without unrelated cleanup.
4. Run targeted checks, then broader gates proportional to risk.
5. Use an independent adversarial review for auth, migration, Docker privilege,
   release workflow or other high-risk changes.
6. Fix required failures before committing; report exact evidence at the
   milestone boundary.

Start with:

```bash
git diff --check
python3 scripts/check_docs.py
python3 -m tests.python.test_translation
python3 -m tests.python.test_hardening
npm test
```

Use `docs/maintainers/development.md` for the complete local workflow and
`docs/maintainers/release.md` for Docker, browser and release gates.

Never commit credentials, cookies, book text, generated deployment state,
runtime databases, test reports, virtual environments or dependency trees.
