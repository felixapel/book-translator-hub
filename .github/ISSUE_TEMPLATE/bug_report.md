---
name: Bug report
about: Report a reproducible problem without exposing private data
title: "[BUG] "
labels: bug
assignees: ''
---

## What happened?

Describe the visible problem and the expected behavior.

## Reproduction

List the smallest reliable sequence, including reader mode and whether it
happens on one book or all books. Do not attach copyrighted book text.

## Deployment evidence

- Project version: exact tag or commit
- Install path: Community Applications / `btctl` Unraid / Compose / other
- Reader and version: Calibre-Web Automated (CWA) / Kavita (specify version & image tag):
- Unraid or host OS version:
- Browser and version:
- Auth profile: `reader-session` (CWA/Kavita), `token`, `authentik-forwarded`, or `disabled` (private LAN):
- LLM provider, server, and model (no key):

For a managed install, run `./btctl doctor --env /private/path/install.env --json` and paste only the failed checks after removing private paths. Include
the relevant bounded log lines and HTTP status, not a complete `/metrics`
dump. Remove cookies, tokens, provider keys, book text, public IPs, and private
paths.

## Additional context

Screenshots are welcome after checking that they contain no private book text.

