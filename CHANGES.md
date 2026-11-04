# Version 8.2.10: staff app without save buttons
- **Photos save the moment they are taken or chosen** (no "Capture and upload" button). The guided one-tap capture is unchanged.
- **The customer's signature saves itself** about a second after the pen is lifted (accidental touches are ignored); the "Save signature" button is gone.
- **The damage mark saves with its photo.** The employee taps the vehicle for the position, picks the type, and takes the photo. If the position was not tapped yet, nothing is saved and the screen says so.
- **The handover form autosaves while typing** (plate, make, model, colour, fuel, odometer, keys, belongings, notes, "no damage"). It is stored on the server, so it survives the page reload after each photo, a phone swap, and a lost connection (a copy stays on the device and is sent when the network is back). New column `inspections.draft_details`: run `flask db upgrade` (revision e2f6b8d4a153). The draft is deleted when the inspection is completed.
- **One final button:** "Complete and check in" (arrival) or "Complete and check out" (departure) finishes the inspection, moves the booking on and sends the PDF report. A checklist shows what is still missing (photos, signature, odometer, damage declaration) and the button stays disabled until everything is there.
- New endpoint `POST /staff/inspections/<id>/draft` (staff login and CSRF header required, tenant-scoped, refused for completed inspections).
- Tests: `tests/test_staff_autosave.py` (6). Full suite: 245 pass; the 5 failures are the same as in the original 8.2.5.5 zip.

# Version 8.2.9: final hardening release (includes everything from 8.2.6 to 8.2.8)
- **Security:** Pillow raised to 13.0.0 (the previous 12.x had 37 known vulnerabilities in `pip-audit`; `requirements.txt` now pins `Pillow>=13.0.0,<14`). After the upgrade `pip-audit` reports no known vulnerabilities and `bandit` reports no medium or high findings. Production start-up still refuses a missing, short or default `SECRET_KEY`.
- **Security:** the "keep what the customer typed" data after a refused booking is size-limited (1.4 KB budget). Sessions are 4 KB cookies; an oversized one would be dropped by the browser and the customer would lose the session. Test: `test_saved_form_data_never_overflows_the_session_cookie`.
- **Staff app fix:** photo upload no longer fails when the browser has Background Sync disabled (it threw and the upload was aborted with "could not be saved in the offline queue"). Sync is now optional.
- Staff photo screen: the labels odometer, fuel level and customer signature are translated (de, nl, fr, es, ar).
- Everything from 8.2.6 to 8.2.8: shuttle per person with a seat limit of 2, only bookable spaces shown to customers, friendly page for a stale CSRF token, extras in the partner API (`GET /v1/extras`, `extras` on quotes and bookings), nothing to retype after a refused booking.
- Test status: 239 pass. The 5 failing tests (`test_ui_translations` x4, `test_alert_sentences_follow_the_language`) also fail in the original 8.2.5.5 zip.

# Version 8.2.8: no retyping after a refused booking
- When checkout refuses a booking (for example a 3rd shuttle person, missing terms, a space that just changed), the customer returns to the hold page with everything already typed in: name, phone, plate, make/model, notes, shuttle choice, number of persons and flight details. Shown once, then discarded.
- Test: `tests/test_shuttle_per_person_and_visible_spaces.py::test_refused_booking_keeps_what_the_customer_typed`.

# Version 8.2.7: extras in the partner API
- `GET /v1/extras?lot_id=` lists a lot's extras (airport shuttle, cleaning) with price, per-person flag and seat limit.
- `POST /v1/quotes` and `POST /v1/bookings` accept `extras: [{"id": 3, "quantity": 2, "details": "LH123"}]`; the quote returns `extras_total`, the booking `total_amount` and its `extras`. The shuttle limit (2 persons) is enforced by the server. Needs no new key scope (`products:read`, `quotes:write`, `bookings:write`).
- Test: `tests/test_api_extras.py`. The separate owner website that uses this API is delivered as `parkpilot-partner-site`.

# Version 8.2.6: shuttle per person, only bookable spaces, checkout 400
- Airport shuttle is charged **per person** (10.00 EUR x persons) with a **seat limit of 2 persons incl. luggage**. New extra field `max_persons` (back office: "Maximum persons per booking"); the checkout shows "2 x 10.00 EUR = 20.00 EUR" and the total updates as the number changes. The server refuses more than the limit. Seed shuttles are per person, max 2.
  Existing shuttle extras that used the old "2 persons included" bundle keep their price (a flat price never scales with the number of persons): open the extra in Extras, set **Persons included = 0** and tick "Price is per person". `flask db upgrade` adds the column and sets the limit 2 on per-person shuttles.
- The booking page (list and map) no longer shows reserved, soon-free or blocked spaces, only bookable ones; legend shortened; new text when nothing is free for the chosen dates. A selected space that gets taken meanwhile is still reported to the customer.
- Checkout "400 Bad Request": a stale/missing CSRF token (cookie overwritten by another app on 127.0.0.1, expired session, form open in two tabs) now returns the customer to the hold with a message instead of a bare 400. Local sessions use the cookie name `parkpilot_session` so other local apps cannot overwrite it.
- Removed a duplicated `show-all` button (duplicate id) on the booking page.
- New tests: `tests/test_shuttle_per_person_and_visible_spaces.py`.

## Security hardening (October 2026)

- Content-Security-Policy with per-request nonces (no inline scripts), HSTS 2 years, no-store on pages with customer data.
- Sessions: bound to the password (a password change signs out every other session), deactivated users are signed out
  immediately, 12 h absolute / 120 min idle lifetime, `__Host-` cookie in production, session fixation closed.
- Login: locked / unknown / wrong-password look identical (no account enumeration), constant-time unknown-user path.
- Password policy everywhere (12+ chars, common-password and email/name checks) and a self-service "Change password" page.
- Rate limits on login, holds, booking, cancellation, ticket and QR links (Redis-backed when configured).
- Webhook delivery connects to the validated IP (DNS-rebinding / SSRF fix).
- Staff alerts no longer show customer names, amounts or payment events.
- CI: pip-audit and bandit; Dependabot; SECRET_KEY >= 32 characters enforced in production.

# Version 8.2.5: fixes from iPhone screenshots
- Login page redesigned in 3D: the live 3D parking lot (cars driving in, plane, bay colours) fills the background, the sign-in form sits on a glass card that flies in, tilts with the pointer with a following light, a floating logo badge and a shimmering button. Touch devices and reduced-motion get the scene without tilt/animation. New `static/js/login-3d.js`; `home-scene.js` gets a `data-center` option.
- Notification chime: no more "AudioContext was not allowed to start" console warning. Sound is unlocked only by a real gesture (click, key, touch end; touchstart does not count) and stays silent until then.
- Notification panel on phones now spans the width under the header (it was cut off on the left); toasts sit above the staff tab bar.
- Booking page: pickup/return date fields overflowed the card on iPhone (iOS ignores width on date inputs); they now fit, and stack on narrow phones.
- Back office on phones: the menu was a long wrapped list taking half the screen; it is now one scrolling row. Top bar: tenant name truncates, language picker no longer cut off to "D", sign-out is an icon button.
- Staff app on phones: the desktop menu and the bottom tab bar both showed (a CSS specificity bug), duplicating "Sign out"; the desktop menu is now hidden, the header is one compact row, and the tab bar's sign-out no longer renders as a big red pill.
- Known, not changed: the "Local test: a phone can only open this QR..." hint on the ticket page is English-only and shown whenever `dev_hint` is on.

# Version 8.2.4: responsive pass + booking button guard
- Viewport now uses `viewport-fit=cover`, so the fixed booking bar clears the iPhone home indicator and landscape notch.
- Booking button is now present at every width: at exactly 900px neither the header button nor the fixed bar showed; the header button now hides at <=899px only.
- Tablets (600-899px) get a larger 3D stage, two-column feature cards and a centred, width-capped booking bar; phones get single-column cards, full-width hero buttons and stacked footer.
- Lot tour: 4:3 stage and smaller captions on phones, horizontally scrolling step pills, 44px touch targets.
- New test `tests/test_booking_button_responsive.py` fails if the hero, header or fixed booking button is removed.
# Version 8.2: homepage on phones, legal pages
- The 3D parking lot was practically invisible on phones (camera far away + dark fog + dark gradient). Phones now get a closer camera, the fog pushed back and the lot on its own stage above the text.
- The coloured bays (green free / red booked / amber held) were buried under the asphalt (y 0.04 < 0.05) and never showed on any device; fixed.
- Impressum / Datenschutz / AGB: the white-lettered logo sat on a light page (corner, barely readable). The pages now use the same dark header band as the booking pages.
- Homepage navigation redesigned: glass bar, pill links with animated underline, compact language switch, header reserve button. Phones (up to 760px) keep the original plain header; tablets get a menu button.
- Reserve button: always visible on phones through a fixed bar (hides only once the hero button is fully on screen). The 3D lot has its own row above the headline on phones.
- The button only rendered when `PUBLIC_TENANT_SLUG` matched a tenant; the code default was `duesseldorf-airport-parking` while the seed creates `luxury-parking`. Default corrected, and the homepage falls back to the first bookable lot if the slug matches nothing.

# Version 8.1: migration fix
- `flask db upgrade` failed with `CircularDependencyError` when the database already contained some of the new tenant columns (e.g. `logo_data`). Migrations `b7e1d3a9c452` and `c9a4e6b21f85` now skip columns, tables and indexes that already exist, so they work on fresh and partly-created databases.
- `requirements.txt`: `pytest` and `python-dotenv` were glued onto one line; fixed.

# Version 8: logo, key tags, shuttle bundle, notification centre
- Logo upload; logo on invoices and key tags. Invoice shows stay days, bundle lines and a Menge explanation (also in the admin).
- Printable key tag (owner and staff). Owner can generate/download any invoice from the booking.
- Shuttle: price includes 2 persons, additional persons priced separately (no price = no additional persons). Migration `c9a4e6b21f85`.
- Notification centre: alerts table, bell, toasts, chime on new bookings, overview page with charts.
- Fixes found while rechecking: untranslated manual form errors, raw payment status in two templates, form-error extraction, `_` coverage.

# Version 7: one language everywhere
- Interface translation layer for everything that was English-only: back office, staff app and its scripts, login, error pages, flash messages, form labels and validation errors, inspection PDF and email. 1,222 texts in de, nl, fr, es, ar (English is the source).
- Language switch (cookie) on back office, staff app and login; tenant default language otherwise; RTL for Arabic.
- `flask i18n-missing`, completeness/leak tests, `python-dotenv` added so `.env` loads automatically.

# Version 6: invoices, notifications, billing, widget, reports
- **Invoices & credit notes** (PDF, gapless numbers, VAT split, snapshots); download for customers, owners and API; kept when a booking is deleted.
- **Customer notices** on reserved, paid, cancelled, refunded, checked in/out and a pre-arrival reminder: email with the right PDF, optional SMS/WhatsApp (Twilio), delivery log with resend, no duplicates.
- **Phase 9**: 14-day trial, Starter/Pro/Business via Stripe Billing, quotas (spaces, bookings, API), platform subscriptions overview.
- **Phase 10**: embeddable widget (`/widget.js`), WordPress plugin, integration guide (JS/PHP/Python).
- **Phase 11**: reports (revenue, occupancy, extras, discounts, VAT) with CSV, reminder command.
- Migration `b7e1d3a9c452`. Behaviour change: the confirmation email now carries two PDFs (ticket + invoice).
- New tests: invoices/notifications, phase 9, 10, 11 (written but not executed in the build environment: run `pytest` first).

# Version 5: roles, owner tools, extras, tickets

**Roles** admin (platform super user) > owner (the parking) > staff (workers)
- Only the admin issues API keys and webhooks, as approved bundles (booking widget, read-only reports, full integration). Bundles never include key or webhook management. Owners see their keys read-only and send a request; the admin approves and shares the key once. New admin page: Admin > API & webhooks.
- Owners no longer see API keys/webhooks/rotate; they get "API access" (request + status) and API usage.
- Staff see only the staff app.

**Owner back office** (Admin > Bookings)
- Create, view, edit, move (dates/space, conflict-checked, optional re-pricing), set payment, check in/out, cancel (with card refund), delete, resend the confirmation email.
- Extras catalogue (Admin > Extras): airport shuttle and car cleaning, price per lot, shown at checkout, added to the total.
- Tables scroll instead of overflowing and stack as cards on phones.

**Customer booking**
- Extras at checkout (with flight/passenger details). Booking page: large scannable QR (short payload, 4-module quiet zone), full-screen QR, PDF ticket, QR PNG, calendar file, directions, duration quick-picks on the reserve page.
- Confirmation email now carries the PDF ticket automatically.

**Staff app**
- Back button, Home, Admin link (owners) and a bottom tab bar on phones: no more getting lost in the installed app.
- Booking page shows one clear next step (start photos > check in > return photos > check out) and the extras to do (shuttle, cleaning) with Mark done; the dashboard lists open extras.
- Guided photo capture: one big button, per-angle hints, progress bar, auto-advance without page reloads, offline queue kept.

**Technical**: migration `c3d8a7e5b210` (api_access_requests, extras, booking_extras). Tests: 101.

# What changed in this version

**Security**
- Login brute-force protection: per-account lock (5 failures, 15 min) and per-IP throttle; same message for unknown email and wrong password.
- HSTS (production), Permissions-Policy, COOP, and `Cache-Control: no-store` on `/admin` and `/staff`.
- Generic error pages (JSON under `/v1`) for 403/404/405/413/429/500; 500s are logged and the transaction is rolled back.

**Data integrity**
- PostgreSQL exclusion constraint `ex_bookings_no_space_overlap`: two active bookings can never overlap on one space, whatever the application does.
- New migration `a1f0c5d9e301` (also adds the login lockout columns). Upgrade with `flask --app wsgi db upgrade`.

**Quality and operations**
- Tests: 95 (was 83). New: lockout, headers, error pages, translation key/placeholder parity, database guard, 8-way parallel reservation race.
- Suite runs on SQLite, PostgreSQL (`TEST_DATABASE_URL`) and a migration-built schema (`TEST_USE_MIGRATIONS=1`).
- Dockerfile, `docker-compose.yml` (web + Postgres + Redis), `.dockerignore`, GitHub Actions CI, `.gitattributes`.
- `render.yaml` lists every optional setting (Stripe, SMTP, S3, SITE_URL); README no longer claims Stripe is missing.
- `migrations/env.py` no longer uses a call that Flask-SQLAlchemy 3.2 removes.

# Luxury Parking branding and homepage
- New logo set in `app/static/img/` (SVG + PNG): horizontal (dark/light), stacked, mark, app icon, favicon. Used in hero, booking flow, footer, favicon, staff PWA icon.
- Homepage sections from luxuryparking.de: About, Benefits, How it works, Location & contact (address, phone, e-mail, Google Maps link, Instagram, Facebook), FAQ (6 site questions + hold), CTA, richer footer. All six languages.
- `app/business.py`: public business profile with defaults from the site; override with `BIZ_*` env vars (`BIZ_LOGO=0` disables the bundled logo).
- Impressum data filled in `.env` / `.env.example` (`LEGAL_*`), JSON-LD Organization + FAQ extended, seed defaults (lot "Luxury Parking Ratingen", navy/gold colours).

## Lot tour (virtual walkthrough)
- Home page now has a photo walkthrough (`templates/public/_lot_tour.html`, `static/css/lot-tour.css`, `static/js/lot-tour.js`).
- Photos: `static/img/lot/*.webp` (responsive sizes, license plates blurred).
- Text in all 6 languages (`tour.*` keys). `ImageGallery` added to the home page JSON-LD (`_lot_gallery_schema` in `blueprints/public.py`).

## Spaces and shuttle pricing
- New lot / Edit lot: "Number of spaces" (add or remove; removal skips spaces that have bookings). New lot also takes covered and EV charging counts.
- Spaces page: bulk set Covered / EV charging / size / status. Lots list links to Spaces.
- Extras: "Price is per person" option (migration f5c2a8d4b913 marks existing shuttle extras per-person). Checkout asks for the number of passengers; Shuttle Standard seed price is 10.00 per person.
