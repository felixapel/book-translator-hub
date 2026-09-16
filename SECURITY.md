# Security Policy

## Supported Versions

Only the latest release (and `main`) receives security fixes.

## Reporting a Vulnerability

Please report vulnerabilities privately via **GitHub Security Advisories**
("Report a vulnerability" on the repository's Security tab) rather than a
public issue. If that form is unavailable, email
`felixguillermoapel@gmail.com` with the subject `Book Translator Hub security report`.
Do not include credentials, session cookies, book text, or provider keys unless
we explicitly arrange a safe transfer. You should get a first response within
7 days.

## Scope notes for self-hosters

- The API fails startup by default unless an authentication authority is
  configured. The universal hub uses reader-session exchange for CWA and
  Kavita. Managed split installs also support native CWA sessions or `forwarded`
  behind an identity proxy whose exact peer is allowlisted. `token` is a shared-
  tenant compatibility mode; `disabled` is development-only.
- CWA reader-session exchange forwards only the selected CWA authentication
  cookies to its validation probe. Ambient browser cookies, including
  `cf_clearance`, are ignored and never reach the translation API or CWA probe.
- In `forwarded` mode the identity proxy must strip incoming `X-BT-Subject` and
  `X-BT-Roles` before setting trusted values. Never publish a bypass route to
  the API. The bundled injection proxy strips these headers and cannot serve as
  the trusted identity hop; route `/bt-api` directly through the identity
  proxy. In `cwa_session` mode credentialed CORS permits exact configured
  origins only; a private-subnet wildcard is deliberately ignored. The CWA
  probe must target the exact authenticated `/ajax/emailstat` path and return a
  bounded JSON task list. Browser requests in `token` mode omit credentials.
  `forwarded` mode uses `credentials: include` so the identity proxy can receive
  its cookie; it must remain on the configured public origin and the identity
  proxy remains responsible for validating that cookie.
- Provider API keys remain in the private server-side environment supplied to
  the container and are never sent to reader configuration or browser storage.
  Protect environment files as secrets, restrict cloud keys to the required API
  and never use a consumer subscription/browser session as an API credential.
  Cache schema v2 stores translated results and
  one-way source/scope hashes, not source paragraphs, raw identities, or reader
  credentials. Provider prompts still leave the host when a cloud provider is
  configured; see the fallback/privacy warning in the configuration guide.

