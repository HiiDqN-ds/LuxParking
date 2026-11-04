# ParkPilot integration guide

Four ways to put your parking on another website, from easiest to most flexible.
Your own site uses exactly the same API, so there is no second system.

## 1. Hosted booking page (no code)
Link to `https://YOUR-HOST/<lang>/reserve/<tenant-slug>/<lot-slug>`. Logo and colours come from *Admin > Brand & domain*.
Add `?pickup_local=2026-12-01T08:00&return_local=2026-12-08T18:00` to pre-fill the dates.

## 2. Widget (one script line and one div)
```html
<div id="parkpilot-widget"></div>
<script async src="https://YOUR-HOST/widget.js"
        data-key="pk_..." data-lot="1" data-tenant="your-slug" data-lot-slug="your-lot"
        data-color="#14385C" data-lang="en"></script>
```
Visitors pick dates, see availability and the lowest price, and continue on the hosted page (payment and personal data
never touch your site). Requirements: a **public** key (`pk_`) from the platform admin, and your website's origin
(`https://www.example.com`) in that key's *allowed origins*. Languages: de, en, nl, fr, es, ar.

## 3. WordPress plugin
Upload `integrations/wordpress/parkpilot-booking.zip` (Plugins > Add New > Upload), activate, fill in
*Settings > ParkPilot*, then add `[parkpilot]` to a page. Optional attributes: `lang`, `color`, `lot`, `lot_slug`.
Only public keys are accepted by the plugin.

## 4. REST API
Base `https://YOUR-HOST/v1`, Bearer authentication, JSON, interactive docs at `/v1/docs`, schema at `/v1/openapi.json`.
`pk_` keys read the map/availability and create holds/bookings from browsers; `sk_` keys have full rights and must stay on
your server. Send an `Idempotency-Key` header (8-200 chars) on every POST so a retry never books twice.
Errors always look like `{"error": {"code": "...", "message": "..."}}`.
Plan limits answer `402 plan_limit_reached`.

Typical booking flow: `GET /availability` -> `POST /quotes` -> `POST /holds` (10 minutes) -> `POST /bookings`.
`POST /bookings` emails the customer the confirmation with ticket and invoice; pass `"send_notifications": false` to
send your own messages. `GET /bookings/{id}/invoice` returns the invoice PDF (`/credit-note` after a refund).

### JavaScript
```js
const API = "https://YOUR-HOST/v1", H = { Authorization: "Bearer pk_...", "Content-Type": "application/json" };
const av = await (await fetch(`${API}/availability?lot_id=1&from=2026-12-01T08:00:00Z&to=2026-12-08T18:00:00Z`, { headers: H })).json();
const space = av.spaces.find(s => s.selectable);
const hold = await (await fetch(`${API}/holds`, { method: "POST", headers: { ...H, "Idempotency-Key": crypto.randomUUID() },
  body: JSON.stringify({ lot_id: 1, space_id: space.id, from: "2026-12-01T08:00:00Z", to: "2026-12-08T18:00:00Z", email: "ana@example.com" }) })).json();
```

### PHP (server side, secret key)
```php
$ch = curl_init('https://YOUR-HOST/v1/bookings/123/invoice');
curl_setopt_array($ch, [CURLOPT_HTTPHEADER => ['Authorization: Bearer sk_...'], CURLOPT_RETURNTRANSFER => true]);
file_put_contents('invoice-123.pdf', curl_exec($ch));
```

### Python (server side, secret key)
```python
import requests, uuid
API, H = "https://YOUR-HOST/v1", {"Authorization": "Bearer sk_..."}
booking = requests.post(f"{API}/bookings", headers={**H, "Idempotency-Key": str(uuid.uuid4())}, timeout=15, json={
    "hold_id": 42, "first_name": "Ana", "email": "ana@example.com", "phone": "+491511234567"}).json()["booking"]
pdf = requests.get(f"{API}/bookings/{booking['id']}/invoice", headers=H, timeout=15).content
```

## Webhooks
Signed events: `booking.created`, `booking.paid`, `booking.cancelled`, `booking.checked_in`, `booking.checked_out`,
`inspection.completed`, `dispute.opened`. Verify the signature header before trusting a payload (see *Admin > Webhooks*).

## Extras (airport shuttle, cleaning)
`GET /v1/extras?lot_id=1` lists the extras of a lot: `id`, `kind`, `name`, `price`, `per_person`, `max_persons`.
Add them to a quote or booking: `"extras": [{"id": 3, "quantity": 2, "details": "LH123"}]`.
Per-person extras cost price × quantity; the server refuses a quantity above `max_persons` (shuttle: 2).
The quote returns `extras_total`; the booking returns `total_amount` and its `extras`.
A complete example client is the separate project `parkpilot-partner-site`.
