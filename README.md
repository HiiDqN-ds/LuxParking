# ParkPilot

Multi-tenant airport parking booking platform (Flask + PostgreSQL). The foundation
covers tenant isolation, auth/roles, lot and space administration, configurable pricing,
the initial booking/hold/payment lifecycle services, and a public live parking map with
ten-minute space holds. Public pages support German, English, Dutch, French, Spanish,
Arabic, and Turkish with localized metadata, RTL layout, and a multilingual sitemap.
Turkish currently covers the core booking flow; untranslated catalog entries fall back to English. The staff
app adds signed booking QR codes, required vehicle inspections, private photo evidence,
damage disputes, and an offline photo-upload queue.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt   # runtime + pytest, pip-audit, bandit
cp .env.example .env            # then export the variables, or set them in your shell
export DATABASE_URL=sqlite:///parkpilot.db ADMIN_PASSWORD=change-me-too SEED_OWNER_EMAIL=you@example.com
flask --app wsgi db upgrade     # create tables
flask --app wsgi seed           # tenant + owner + lot + 50 spaces (idempotent)
flask --app wsgi run --debug    # http://127.0.0.1:5000/admin/login
pytest
```

## Deploy to Render
1. Push this folder to a GitHub repo.
2. Render: New > Blueprint > select the repo (reads `render.yaml`).
3. Set `SEED_OWNER_EMAIL` and `ADMIN_PASSWORD` when prompted. First start migrates and seeds.
4. Log in at `https://<your-service>.onrender.com/admin/login`.

## Layout
- `app/models.py` models + automatic tenant scoping (read this first)
- `app/blueprints/` `public`, `auth`, `admin`
- `app/services/availability.py` availability and server-side price quotes
- `app/services/bookings.py` tenant-scoped hold, booking, and payment-state operations
- `app/services/inspections.py` tenant-scoped inspection, evidence, dispute, and retention operations
- `app/services/inspection_reports.py` inspection PDF generation and SMTP delivery
- `app/blueprints/staff.py` authenticated staff PWA and booking inspection workflow
- `app/static/js/booking.js` date-driven map updates, selection, countdown, and live refresh
- `app/static/js/staff-queue.js` IndexedDB-backed retry queue for inspection photos
- `app/templates/`, `app/static/css/app.css` templates and design tokens
- `migrations/` Alembic, `tests/` pytest, `render.yaml` + `start.sh` deployment

## Test, containers and CI
```bash
pytest                                                    # SQLite, fastest
TEST_DATABASE_URL=postgresql://user:pass@localhost/pp_test pytest   # PostgreSQL, like production
docker compose up --build                                 # web + Postgres + Redis on :10000
```
GitHub Actions (`.github/workflows/ci.yml`) runs the suite on PostgreSQL and SQLite and checks
that migrations apply and match the models on every push and pull request. Run the PostgreSQL
variant before deploying: it also exercises the database double-booking guard.

## Security notes
- **Login protection:** 5 wrong passwords lock that account for 15 minutes, and an IP address
  with more than 20 failed attempts in that window is blocked (`LOGIN_MAX_FAILURES`,
  `LOGIN_IP_MAX_FAILURES`, `LOGIN_LOCK_MINUTES`). Messages never reveal whether an email exists.
- **No double bookings:** the booking service locks the space row, and on PostgreSQL the
  `ex_bookings_no_space_overlap` exclusion constraint makes overlapping active bookings on one
  space impossible even if application code were wrong. This is why production must use PostgreSQL.
- **Headers:** HSTS in production, `Permissions-Policy`, `Cross-Origin-Opener-Policy`, and
  `Cache-Control: no-store` on `/admin` and `/staff` pages.
- **Errors:** unexpected errors show a generic page (JSON under `/v1`), are logged with a stack
  trace, and roll back the database transaction.
- Translation catalogs are tested for identical keys and placeholders in every language.

## Rules for later phases
Every tenant-owned model must include `tenant_id` and be added to `SCOPED` in
`models.py`. Services must also filter reads and writes by an explicit tenant ID;
bulk UPDATE/DELETE statements are not auto-scoped and must filter by `tenant_id`.

Booking services reserve a parking space for a half-open time interval
`[pickup_at, return_at)`. Holds expire after ten minutes, and PostgreSQL serializes
competing allocations by locking the space row. `create_payment_record` only stores lifecycle state; card payments go through Stripe
Checkout (`app/services/stripe_payments.py`, enabled by `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET`). Booking totals passed to the service must come from trusted
server-side pricing.

The public map is served at `/<locale>/reserve/<tenant-slug>/<lot-slug>` for localized
pages, while `/reserve/<tenant-slug>/<lot-slug>` remains as an English-compatible URL.
The landing page at `/` negotiates its language from the browser's `Accept-Language`
header and provides a manual language selector. Translation catalogs live in
`app/translations/`; adding a supported locale requires a catalog and adding its code
to `SUPPORTED_LOCALES`. RTL direction is enabled for Arabic, Hebrew, Persian, and Urdu.

Set `SITE_URL` to the canonical HTTPS origin in production (for example,
`https://parking.example.com`) so canonical and alternate-language links, JSON-LD,
robots.txt, and sitemap.xml use the public host rather than the incoming request host.
The crawler endpoints are `/robots.txt` and `/sitemap.xml`; the sitemap contains the
localized home and active-lot reservation pages with hreflang alternates. Temporary
holds are marked `noindex,nofollow`. Before launch, verify the sitemap in Google Search
Console, confirm canonical host ownership, and keep Google Business Profile name,
address, hours, and booking URL consistent with the actual parking operation. Do not
add review markup or rating claims until real customer ratings are available.

A lot's configured HTTPS or same-origin background image is rendered beneath its SVG
spaces. Availability notifications are emitted by the booking lifecycle services
after successful commits, so hold, booking, cancellation, and check-out changes refresh
every connected map in that lot's tenant-scoped room. Set `PUBLIC_TENANT_SLUG` to choose
which tenant's lot the landing page promotes. Every EUR lot uses the same global base
tariff: stays shorter than seven actual 24-hour days cost EUR 50 total; stays of seven
days or more cost EUR 10 per started 24-hour period. This tariff takes precedence over
configured lot rates, space rates, and price rules. Extras are additional, and an
applicable discount can reduce the total. Lots in other currencies continue to use a
positive per-space daily rate, falling back to the lot daily rate; each started 24-hour
period is charged once. Their highest-priority applicable `price_rule` overrides the
base quote for the whole stay; ties prefer a space-specific rule, then the lowest rule
ID. A weekend rule matches any overlap with Saturday or Sunday in the tenant timezone.
Season and holiday rules require explicit start and end dates and match when the stay
overlaps that date window. The `day` period matches any date. Rule amounts can be a flat
total, per started 24-hour period, or per started hour. Unconfigured non-EUR rates are
shown as such and cannot be held. Temporary holds do not create a confirmed booking or
collect payment. The map uses Socket.IO notifications plus a 20-second availability
refresh fallback. The default single threaded worker supports WebSockets; set
`SOCKETIO_MESSAGE_QUEUE` to a shared Redis URL before increasing `WEB_CONCURRENCY` so
availability notifications reach clients connected to other workers.

## Staff inspections and evidence

After applying migrations, owners and staff can use `/staff/` to find a booking,
display its signed QR code, complete arrival/departure inspections, review damage
disputes, check customers in or out, and create a walk-in space hold. A walk-in hold
expires after ten minutes and can be released early; it does not create a booking or
collect payment. Check-in is blocked until the arrival
inspection has every tenant-required photo and a customer signature; check-out
requires a completed departure inspection. Departure damage must be matched manually
to an arrival mark of the same type or it opens a dispute. An open dispute prevents
the booking's evidence from being purged.

Staff-role accounts see only reserved or currently checked-in vehicles. They can
look up those bookings for arrival/departure operations and manage their inspection
photos and vehicle details; customer identity/contact, payment status, lot identity,
historical/cancelled bookings, walk-in inventory, and disputes remain owner-only.
Owners retain the full staff workspace.

During arrival inspection, staff can enter a vehicle's registration, make, model, and
color to create or update its reusable vehicle profile. The required arrival photos
are linked to that profile rather than copied; the profile's photo gallery follows
the same configurable evidence-retention period and remains available while an
associated dispute is open.

Inspection photos are kept in **private** S3-compatible object storage. Configure
`INSPECTION_S3_BUCKET`, `INSPECTION_S3_REGION`, and (when required by the provider)
`INSPECTION_S3_ENDPOINT_URL`, `INSPECTION_S3_ACCESS_KEY_ID`, and
`INSPECTION_S3_SECRET_ACCESS_KEY`. Grant the application identity only the object
read/write/delete permissions it needs; do not make the bucket public. The default
per-image limit is 8 MiB and the pixel limit is 12 million; adjust
`INSPECTION_MAX_PHOTO_BYTES` and `INSPECTION_MAX_IMAGE_PIXELS` if needed.

The PDF report is sent synchronously after inspection completion. Configure
`SMTP_HOST`, `SMTP_PORT`, and `SMTP_FROM`; port 587 uses STARTTLS and port 465 uses
implicit TLS. `SMTP_USERNAME` and `SMTP_PASSWORD` are optional for relays that do
not require authentication. If delivery fails, the inspection remains completed and
staff can retry report delivery from its inspection page.

Evidence retention defaults to 90 days and can be changed in the owner inspection
settings. Schedule the following command at least daily to enforce the configured
retention period; it skips evidence associated with open disputes:

```bash
flask --app wsgi purge-inspection-evidence
```

The staff PWA requires HTTPS (except on localhost). Pending offline photo uploads
are stored in that browser's IndexedDB until they upload successfully; use a trusted,
access-controlled device and reconnect before signing out when possible. The PWA
queues photo uploads, not inspection form edits. Camera QR scanning depends on the
browser's `BarcodeDetector` support; staff can use the booking-number field when
camera scanning is unavailable.

The S3-compatible backend and SMTP delivery should be tested with the actual
deployment credentials before production. Automated tests mock those external
services; they do not verify provider permissions, email deliverability, or network
availability.

## Administration

Tenant owners can review booking and payment-state summaries at `/admin/`, search
reservations at `/admin/bookings`, export up to 5,000 filtered bookings as CSV at
`/admin/bookings.csv`, inspect monthly space occupancy at `/admin/calendar`, and
create or deactivate staff accounts at `/admin/team`. They can also manage hashed
API credentials at `/admin/api-keys`, webhook endpoints and delivery history at
`/admin/webhooks`, and per-endpoint API request totals at `/admin/usage`. API tokens
and webhook signing secrets are displayed only in the creation/rotation response and
are not persisted in the browser session. CSV exports are tenant-scoped, limited to
5,000 rows, and escape spreadsheet formula prefixes.

Owners can configure priority-ordered date pricing at `/admin/pricing`, non-stacking
percentage or fixed discount codes at `/admin/discounts`, and logo/colors/custom-domain
metadata at `/admin/brand`. Discount values, dates, minimum booking amount, and optional
redemption limits are operator-configured; no price or discount amount is seeded.
`POST /v1/quotes` accepts one optional `discount_code`, and booking creation recalculates
the discount and atomically checks any redemption limit before converting the hold.
Tenant translation overrides are managed at `/admin/translations`; they only override
existing catalog keys and must preserve that language's interpolation placeholders.
Blog articles and FAQs are managed at `/admin/content`. Published entries are served at
`/<locale>/content/<tenant-slug>/<blog|faq>/<slug>`, included in the multilingual
sitemap, and contribute FAQ structured data. A saved custom domain is metadata only:
DNS, TLS certificates, and host routing still require deployment-provider setup.

Platform-owner accounts (role `platform_owner`, with no tenant) receive the platform
overview at `/admin/`. It aggregates tenant, lot, booking, paid booking-value by
currency, and API request counts and allows account activation or suspension. Paid
booking value is not platform subscription revenue; Stripe Billing and subscription
revenue are part of Phase 9. Suspended tenant users cannot access tenant pages or sign
in, API keys for suspended tenants are refused, and public reservation pages for
suspended tenants are not listed or served.

To provision the first platform owner, run
`flask --app wsgi create-platform-owner --email admin@example.com`; the CLI prompts
for a confirmed password of at least 12 characters and never echoes it. There is
intentionally no public sign-up or self-promotion route.

## Customer booking flow
Home -> live map -> 10-minute hold -> checkout (`/reserve/.../holds/<id>`) -> booking page with QR (`/booking/<token>`, emailed link, cancel until 48h before pick-up). Card payments need `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET`; point the Stripe webhook at `/webhooks/stripe` (events: `checkout.session.completed`, `.expired`, `.async_payment_succeeded`, `.async_payment_failed`, `charge.refunded`). Without Stripe, customers pay at arrival. Confirmation emails need `SMTP_HOST` and `SMTP_FROM`. Set `SEED_DAY_RATE` (e.g. `9.90`) to give a fresh seed a starting daily price.

## Run on your own computer (easiest)
1. Install Python 3.11 or newer (Windows: tick "Add python.exe to PATH").
2. Windows: double-click `run-windows.bat`. Mac/Linux: run `bash run-mac-linux.sh`.
3. The first start installs everything and opens http://127.0.0.1:5000/. Admin: http://127.0.0.1:5000/admin/login.
Settings are in `.env` (local only, never publish it). Customers pay at arrival until you add Stripe keys; no emails are sent until you add SMTP settings.

## Going live at luxuryparking.de
Use `render.yaml` (SITE_URL, tenant name and slug are already set). Set a new `ADMIN_PASSWORD` and `SEED_OWNER_EMAIL` in Render, add the domain in Render's Custom Domains, and keep `APP_ENV=production`.

## Roles
- **Admin** (platform super user): `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` in `.env`. Creates API keys and webhooks for parkings as bundles and approves owner requests. Log in at `/admin/login`.
- **Owner** (the parking): bookings (create, edit, move, cancel, delete), lots, spaces, prices, extras (shuttle, cleaning), team, brand, content, vehicle checks. Requests API access from the admin.
- **Staff** (workers): staff app only: check-in/out, photos, extras to complete.

## Email (booking confirmation with PDF)
Without an email server the booking still works and the customer gets the ticket on the booking page. To send emails, fill in `.env`:
```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USERNAME=luxuryparkinglp@gmail.com
SMTP_PASSWORD=<Google App Password>
SMTP_FROM=luxuryparkinglp@gmail.com
```
Gmail needs 2-step verification and an App Password (Google Account > Security > App passwords). Test it: `flask --app wsgi test-email you@example.com`. A business email service (Brevo, Mailgun, IONOS) is better for real customers.


## Invoices (Rechnung), credit notes and customer emails
Every confirmed booking gets an invoice (`RE-000001`, gapless per tenant) as a PDF; a refund issues a credit note
(`GS-000001`). Customers download them from their booking page, owners from the booking or *Admin > Invoices*
(filters, CSV export), partners via `GET /v1/bookings/{id}/invoice`. An open invoice follows the booking (extras, move,
re-price); a paid one is frozen; cancelling unpaid cancels it; deleting a booking keeps its invoices.
Set seller address, VAT ID and VAT rate under *Admin > Invoices > Seller details & VAT* (`DEFAULT_VAT_RATE`, 19, is
only a default: confirm it with your tax advisor; one rate per tenant, prices are gross).

The customer is emailed in their language, with the matching PDF, on: reserved (ticket + invoice), paid (invoice),
cancelled, refunded (credit note), checked in, checked out (invoice + review link) and shortly before arrival
(`flask --app wsgi send-reminders`, run hourly; `REMINDER_HOURS`). Every attempt is stored in `notifications` (shown on
the booking page, with resend buttons), so nothing is sent twice. Failed or unconfigured delivery never breaks a booking.
Optional: `OWNER_NOTIFY_EMAIL` blind copy, billing address gets a Cc, SMS/WhatsApp through Twilio (`TWILIO_*`,
`SMS_EVENTS`, `WHATSAPP_EVENTS`; WhatsApp business messages need approved templates).

## Plans and billing (phase 9)
Tenants flagged `billing_exempt` (your own parking; every tenant that existed before the upgrade) are unlimited. New
tenants get a 14-day trial, then need a Starter/Pro/Business subscription (Stripe Billing, `/admin/billing`, portal for card
and cancellation). Without one, new bookings, new spaces and API calls are refused (`402 plan_limit_reached`); failed
payments keep working while Stripe retries. Limits per plan are placeholders (`PLAN_LIMITS_JSON`), prices live only in
Stripe (`STRIPE_PRICE_*`). Webhook: `/webhooks/stripe-billing` with `STRIPE_BILLING_WEBHOOK_SECRET`.

## Widget, WordPress, reports (phases 10-11)
See `docs/INTEGRATION.md` (widget, WordPress plugin in `integrations/wordpress/`, REST examples). Reports with CSV export
are at *Admin > Reports*.


## One language per page (interface translation)
Public pages use the language in the URL (`/de/`, `/en/` ...). The owner back office, the staff app, login, error pages,
flash messages, form labels, validation errors, the staff scripts, invoices, emails and the inspection PDF are all written in
**one** language at a time: the visitor's choice (language switch, remembered in a cookie), otherwise the tenant's default
language. Languages: de, en, nl, fr, es, ar, tr (Arabic switches the page to right-to-left).

- Templates use `{{ _('English text') }}`, Python uses `ui('English text %(x)s', x=...)`; catalogs are `app/translations/ui/<locale>.json`.
- A new language = one `ui/<locale>.json` file plus the locale in `SUPPORTED_LOCALES`. `flask --app wsgi i18n-missing --write`
  lists/creates the missing entries.
- `tests/test_ui_translations.py` fails if any text is missing in any language, if a placeholder differs, or if an owner/staff
  page still shows English in German or Arabic, so mixed pages cannot ship.
- Not translated on purpose: the legal pages (Impressum, AGB, Datenschutz are German legal texts), API error messages (for
  developers) and the offline fallback page of the service worker.
- `.env` is loaded automatically (`python-dotenv` is in requirements.txt).


## Logo, key tags, shuttle rule and notification centre
- **Logo on invoices:** *Admin > Brand & domain > Logo for invoices and key tags* (PNG/JPEG/WebP up to 1 MB, re-encoded to a small PNG).
  It is printed in the header of every invoice, credit note and key tag.
- **Invoices by hand:** on every booking the owner can *Generate invoice (PDF)* (first time) or *Download invoice (PDF)*; the list
  with filters and CSV is *Admin > Invoices*. *Menge* = number of units charged on a line; amount = Menge x unit price (explained on the
  invoice itself and in a help box on the Invoices page).
- **Key tag:** *Print key tag* on the booking (owner and staff app) opens a 100 x 70 mm PDF with the signed QR code (the one the staff
  app scans), plate, car, space, driver's name, dates, key count, fuel, odometer, damage count and belongings from the arrival
  inspection. Phone numbers and e-mail addresses are never printed. Print at 100 % on label paper or cut it out.
- **Shuttle: 2 persons included.** An extra with "Persons included" > 0 is a bundle: the price covers that many persons (2 for the
  shuttle), every additional person costs the "Price per additional person". Leave that price empty to accept no more than the
  included persons (nothing is invented). Existing per-person shuttles were converted to bundles of 2 persons with the old price
  as the bundle price: **review the price and set the additional-person price** under *Admin > Extras*. Past bookings keep their pricing.
- **Notification centre:** the bell (owner and staff app) polls every 8 seconds, shows a toast and plays a short chime when a booking is made
  (sound on/off in the bell; browsers allow sound only after the first click on the page). *Notification centre* has today's numbers,
  a 14-day bar chart, a status donut and the activity timeline. Events: new booking, payment, cancellation, refund, check-in/out, dispute.


## Forgot password (email link or SMS code)

- Page: `/admin/forgot-password` (linked from the sign-in page). The person enters an email or a phone number and picks "link by email" or "code by text message".
- Email link: single-use, valid 30 minutes. SMS code: 6 digits, valid 10 minutes, locked after 5 wrong guesses.
- The answer is identical for every account (nothing reveals whether an email or number exists). A new request within 60 seconds is ignored, and a newer request cancels the older one. Inactive users and suspended parkings get nothing.
- Only a keyed hash of the link secret or code is stored (table `password_resets`). A successful reset sets the new password, lifts the lockout, cancels other open requests and signs out all existing sessions.
- Phone numbers (`users.phone`, +country code) are set under Account > Phone number, by the owner for staff in Admin > Team, or with `SEED_OWNER_PHONE` / `SEED_ADMIN_PHONE`. `DEFAULT_PHONE_COUNTRY_CODE` turns numbers that start with 0 into international format.
- Delivery uses the existing `SMTP_*` and `TWILIO_*` settings. In development without them the link or code is logged to the terminal; in production it never is.
- Run `flask --app wsgi db upgrade` once (migration `b8d4f1a9c305`); the run scripts already do.


## AI chat assistant (public pages)

- A chat button appears on the home page and the booking page. `POST /chat/<tenant>/<lot>` (JSON, CSRF-protected, 8 per minute and 80 per hour per IP) returns the answer.
- Claude (`ANTHROPIC_MODEL`, default `claude-haiku-5-5`) answers from a fact sheet built per request from the database: price examples (calculated by the same booking engine as the website, so the chat never quotes a price the site would not charge), price rules, extras, published FAQ entries, address and contact. Nothing about customers or bookings is sent, and other parkings' data is never included.
- The visitor's text is only sent as chat messages, never into the instructions. Message count, length and reply length are capped. `AI_CHAT_DAILY_LIMIT` caps AI replies per day and worker.
- Without `ANTHROPIC_API_KEY`, when the AI is unreachable or the daily limit is reached, the visitor gets a plain answer made of the same facts (price examples, extras, booking link, contact) instead of an error.
- Conversations are not stored on the server; the browser keeps the current chat in `sessionStorage` until the tab closes ("New chat" clears it). The visible notice tells visitors that it is an AI and not to share payment or personal data.
- Privacy: the visitor's chat text is processed by Anthropic when AI mode is on. Mention this in the privacy policy (/datenschutz) and, if required in your setup, sign a data-processing agreement with Anthropic.
- Translations: public texts are the `chat.*` keys in `app/translations/*.json`; fallback answers are in `app/translations/ui/*.json`.


## My profile (`/admin/account`)

- One page for every role with four sections: **Photo**, **Personal details** (name, phone), **Email address**, **Password**. Staff get it in the staff layout, owners and platform admins in the admin layout. The old `/admin/account/password` address redirects to it.
- **Photo:** PNG, JPEG or WebP up to 3 MB and 25 megapixels. It is checked with Pillow, rotated by its EXIF flag, cropped to a 256x256 square and stored as WebP in `users.avatar_data`; re-encoding removes all metadata and anything hidden in the file. The browser checks type and size before uploading and shows a live preview. Photos are served at `/admin/avatar/<id>` to the person, colleagues of the same parking and platform admins only (login required, cached privately).
- **Email:** needs the current password, must be unique, is stored in lower case, cancels open password-reset requests and sends a notice to the old address (`SMTP_*`; logged in development).
- **Password:** as before (every other session is signed out).
- Migration `c9e5a2b7d106` adds the photo columns; `flask --app wsgi db upgrade` applies it (the run scripts do this).
- Development logins from `.env` (`SEED_SYNC_PASSWORDS=1`) are re-applied only when the related `.env` values change (fingerprint in `instance/seed_sync`), so edits made on the profile page survive restarts.

- Design: icons are inline SVG from `templates/_icons.html` (`{{ icon('camera') }}`; no external files, works with the strict CSP). Styles are the `.acct-*` rules in `static/css/app.css`, behaviour is `static/js/account.js` (drop zone, strength meter, toasts, scroll tracking). All animation is CSS or `requestAnimationFrame` and is disabled under `prefers-reduced-motion`.


## Developer portal and API contract

- `/developers` (DE/EN): quickstart, reference, webhooks, security & GDPR, changelog; `/developers/openapi.json`, `/developers/postman.json`. Served entirely from this server (no CDN, no third-party script). `/v1/docs` redirects to `/developers/reference`.
- The OpenAPI 3.1 description lives in `app/api_spec.py`; `app/developer_docs.py` renders it, `app/developer_samples.py` holds the code samples.
- `tests/test_api_contract.py` calls the real endpoints and validates every response against the published schemas, checks that every route is documented and vice versa, and verifies the signing flow. `tests/test_developer_portal.py` renders all pages, checks that no third-party resource is loaded, runs the signature-check samples and syntax-checks the code samples (node/php when installed). Change the API and the spec together or these tests fail.
- Every API response has `X-Request-Id` (a caller's id is kept), `X-RateLimit-*` and `X-ParkPilot-API-Version`; errors repeat the id in `error.request_id`. `GET /v1/health` is public.
- Payments: `STRIPE_PAYMENT_METHOD_TYPES` empty = all methods enabled in the Stripe Dashboard.
