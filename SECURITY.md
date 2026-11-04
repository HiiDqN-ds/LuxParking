# ParkPilot security notes

## What is enforced in the code
| Area | Control |
|---|---|
| Transport | HTTPS only in production (secure cookies, HSTS 2 years, `upgrade-insecure-requests`) |
| Browser | Nonce-based CSP (no inline scripts, `frame-ancestors 'none'`, `object-src 'none'`), nosniff, strict referrer policy, COOP, restrictive Permissions-Policy |
| Auth | scrypt password hashes, 12+ char policy with common-password / email / name checks, brute-force lockout per account and per IP, uniform login errors, constant-time unknown-user path |
| Sessions | HttpOnly, SameSite=Lax, `__Host-` prefix in production, 12 h absolute + 120 min idle, strong session protection, bound to the password hash, deactivation takes effect immediately |
| CSRF | Flask-WTF on every state-changing form; API uses bearer keys (CSRF-exempt by design); Stripe webhooks verify signatures |
| Tenancy | Every ORM query scoped by tenant; role checks (platform owner / owner / staff); staff alerts redacted |
| API keys | Only SHA-256 hashes stored, constant-time compare, per-key origin allow-list, scopes, per-minute limits, idempotency keys |
| Webhooks | HTTPS only, HMAC-signed, no redirects, destination IP validated **at connect time** (DNS-rebinding safe) |
| Uploads | Images re-encoded with Pillow, size and pixel limits, no SVG |
| Rate limiting | Login, holds, booking, cancel, ticket / QR / document links (set `RATELIMIT_STORAGE_URI` to Redis when running several workers) |
| Supply chain | `pip-audit` + `bandit` in CI, Dependabot weekly |

## Operator checklist (things code cannot do for you)
1. `SECRET_KEY`: 32+ random characters, stored only in the host's secret store. Rotating it signs everyone out and invalidates customer ticket links and webhook secrets.
2. Set `RATELIMIT_STORAGE_URI` (Redis) before running more than one worker.
3. Upgrade the free Render database/web plans before taking real bookings; enable automated backups and test a restore.
4. Use unique ADMIN / SEED_ADMIN passwords (start-up refuses weak ones in production) and change them after first login.
5. Turn on 2FA on GitHub, Render, Stripe, your email and DNS accounts: they can all reach production.
6. Move inspection photos to private S3-compatible storage with encryption at rest.
7. Keep `/v1/docs` in mind: it loads Swagger UI from unpkg (see below).

## Known gaps / recommended next steps
- **No two-factor authentication for back-office users yet.** This is the biggest remaining item for company customers (TOTP + recovery codes, mandatory for owners and platform admins; SSO/SAML later).
- `/v1/docs` loads Swagger UI from a CDN with a relaxed CSP; vendor the files into `static/vendor` and drop the exception.
- Customer ticket links stay valid 365 days; shorten or expire them after `return_at` plus the retention period.
- Encrypt sensitive columns (phone numbers, plates) at application level if customers demand it; add an audit-log export and retention policy.
- Commission an independent penetration test before onboarding large customers, and add a vulnerability-disclosure contact (`/.well-known/security.txt`).
- Add a WAF / DDoS layer (Cloudflare or similar) in front of the app.

Report vulnerabilities privately to the maintainer; do not open public issues for them.

## Audit status (8.2.9)
- `pip-audit -r requirements.txt`: no known vulnerabilities (Pillow 13.0.0).
- `bandit -r app -ll`: no medium or high findings.
- Re-run both before every release: `pip install bandit pip-audit && pip-audit -r requirements.txt && bandit -r app -ll`.
