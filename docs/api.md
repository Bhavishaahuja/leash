# Leash API: orders

What the frontend can rely on. Field names here are stable. Adding a field isn't a breaking change, so ignore ones you don't recognize. Anything that renames or removes a field gets called out to Shachi first.

Backend source: `backend/app/orders.py` and `backend/app/review.py`.

## Authentication

There are four kinds of caller. Sending the wrong credential, or none, returns `401`.

| Caller | Sends | Can reach |
|---|---|---|
| Shopping agent | `X-API-Key: <AGENT_API_KEY>` | `POST /mandates`, `POST /orders`, `POST /orders/{id}/authorize`, `GET /agent/orders/{id}` |
| Merchant dashboard | `Authorization: Bearer <session token>` | `GET /orders`, `GET /orders/{id}`, `GET /mandates`, `GET /mandates/{id}`, `GET /stats`, `POST /orders/{id}/review-link`, `POST /demo/orders`, every `/disputes` route |
| Merchant scripts | `X-API-Key: <MERCHANT_API_KEY>` | The same as the dashboard |
| Human on the confirm page | The token in the URL, nothing else | `GET /review/{token}`, `POST /review/{token}/opened`, `POST /review/{token}/approve`, `POST /review/{token}/reject` |

A credential only works on its own row. The agent key can't read orders, and a dashboard session can't call `POST /orders`. Its one way to place an order is `POST /demo/orders`, which only builds the four fixed demo scenarios.

`GET /health` and `POST /auth/session` need nothing.

Browsers can only call the API from a site listed in `FRONTEND_ORIGINS`, which defaults to `http://localhost:5173`. `http://127.0.0.1:5173` is a different origin and gets blocked, so open the dev server on `localhost`. The allowed request headers are `Authorization`, `X-API-Key` and `Content-Type`.

**The frontend must never contain an API key or the password.** Anything in the Vite bundle is readable by whoever loads the page, and a test fails if `frontend/` mentions one. The dashboard logs in instead, and the confirm page only has its link.

### Logging in: `POST /auth/session`

The dashboard asks the human for the password and sends it here.

```json
{ "password": "the demo password" }
```

Success is a `200`:

```json
{
  "session_token": "1793934245.3f9a...",
  "expires_at": "2026-11-05T23:04:05Z"
}
```

Send it as `Authorization: Bearer <session_token>` on merchant routes. Keep it in memory or `sessionStorage`, not `localStorage` and not anywhere in the build. Treat `session_token` as opaque, don't parse it. Don't keep the password.

| Code | When | What to do |
|---|---|---|
| `401` on login | Wrong password | Ask again |
| `401` on a merchant route | The session expired after 8 hours or was ended | Drop the token and show the login |
| `429` | Over 10 tries a minute from one address, right or wrong. `Retry-After` says how many seconds | Ask them to wait |

## How a human reviews an order

1. An order lands in `needs_review`.
2. The dashboard calls `POST /orders/{id}/review-link` and gets a token. This is the only time the token is ever returned, so hold on to it long enough to send it.
3. The human opens a link to the confirm page, which calls `GET /review/{token}`.
4. Once the page has rendered, it calls `POST /review/{token}/opened` once. That starts the attention clock.
5. They tap, and the page calls `POST /review/{token}/approve` or `/reject`.

The confirm page needs to take the token from its URL. The current `/confirm/:orderId` route should become `/confirm/:token`.

## Two states on every order

An order has two separate state fields. Show both.

| Field | Answers | Values |
|---|---|---|
| `status` | Where is the money? | `created`, `authorized`, `captured`, `voided`, `failed` |
| `review_state` | Who decided? | `needs_review`, `auto_approved`, `approved`, `rejected`, or `null` |

`review_state` is `null` until there's a hold to decide about. It's also `null` on orders captured or voided before this field existed.

The combinations you'll see:

| `status` | `review_state` | Meaning | Show |
|---|---|---|---|
| `created` | `null` | Waiting for the buyer to approve at `payer_action_url` | Waiting on buyer |
| `authorized` | `needs_review` | Money is on hold and a human has to decide | A way to get a review link |
| `captured` | `auto_approved` | Clean match, captured with no human involved | Done |
| `captured` | `approved` | A human approved, money taken | Done |
| `voided` | `rejected` | A human rejected, hold released | Done |
| `failed` | `null` | PayPal wouldn't place the hold | Failed |
| `failed` | `needs_review` | PayPal declined the capture | Failed |

Review links only work when `status` is `authorized` and `review_state` is `needs_review`.

## The order object

`GET /orders` returns a list of these, newest first, and `GET /orders/{id}` returns one. Order fields are at the top level, with `mandate`, `intent_check` and `approval` as named objects.

```json
{
  "id": 42,
  "mandate_id": 7,
  "paypal_order_id": "5O190127TN364715T",
  "authorization_id": "0AW2184448108334S",
  "capture_id": null,
  "void_result": null,
  "vault_id": "abc123",
  "merchant": null,
  "items": [
    {
      "name": "Trail runner",
      "color": "red",
      "size": "women's 9",
      "quantity": 1,
      "unit_price": "110.00"
    }
  ],
  "total": "120.00",
  "shipping_amount": "10.00",
  "currency": "USD",
  "status": "authorized",
  "review_state": "needs_review",
  "authorization_expires_at": "2026-12-03T01:56:55Z",
  "watch_mode": "normal",
  "created_at": "2026-11-05T15:04:05Z",
  "mandate": {
    "id": 7,
    "buyer_label": "Sam",
    "raw_request": "Black trail runners, women's size 9, up to $120, from Fernwood",
    "rules": { "item": "trail runners", "color": "black", "currency": "USD" },
    "max_price": "120.00",
    "max_price_inclusive": true,
    "merchant": "Fernwood",
    "expires_at": "2026-11-08T23:59:59Z",
    "created_at": "2026-11-05T15:00:00Z"
  },
  "intent_check": {
    "id": 91,
    "verdict": "mismatch",
    "risk_score": 90,
    "reason": "Ordered red, mandate says black",
    "mismatched_fields": ["color"],
    "rule_results": [
      {
        "rule": "price",
        "passed": true,
        "reason": "Order is $120.00 ($110.00 item + $10.00 shipping), within the $120.00 cap"
      },
      { "rule": "merchant", "passed": true, "reason": "Mandate store matches Fernwood Supply Co" },
      { "rule": "expiry", "passed": true, "reason": "Mandate is valid until 2026-11-08 23:59 UTC" },
      { "rule": "currency", "passed": true, "reason": "Both are in USD" }
    ],
    "latency_ms": 1940,
    "checked_by": "ai",
    "created_at": "2026-11-05T15:04:07Z"
  },
  "approval": null
}
```

### Fields that need care

- **Money** (`total`, `shipping_amount`, `unit_price`, `max_price`) is a string like `"120.00"`. Don't parse it to a float for anything but display.
- **Times** are ISO 8601 strings in UTC.
- **`capture_id`** is set once money is taken. **`void_result`** is PayPal's status, normally `"VOIDED"`, once a hold is released. Both can be `null`.
- **`shipping_amount`** is `null` on orders from before it was saved.
- **`merchant`** is `null` for an order at Fernwood Supply Co, which is every agent order. Only `POST /demo/orders` sets it.
- **`max_price`** can be in another currency, so don't hardcode `$`. Read `mandate.rules.currency`. If that's `null`, treat it as USD.
- **`max_price_inclusive`**: `true` means "up to" and `false` means "under".
- **`intent_check`** is `null` before the order is checked. It's also `null` if saving the result failed, and that order will be in `needs_review` with nothing to explain why, so handle a missing reason.
- **`approval`** is `null` until the confirm page reports it rendered. It then exists with `action: null` until they decide. Auto-approved orders never have one.
- **`watch_mode`** is `normal` or `sharp`: whether the rubber-stamp watch was on when the order was checked. Never `null`. Orders that were never checked are `normal`. Agents never see it. See [The rubber-stamp watch](#the-rubber-stamp-watch).

### `intent_check`

| Field | Type | Notes |
|---|---|---|
| `verdict` | `match`, `mismatch`, `unsure` | |
| `risk_score` | int or `null` | 0 to 100, higher means more likely the human didn't want this |
| `reason` | string | One line, written for the human. Show this one |
| `mismatched_fields` | list or `null` | What differs. Rule names (`price`, `merchant`, `expiry`, `currency`) when `checked_by` is `rules`. Otherwise `item`, `color`, `size`, `quantity`, `extra_item`, `substitution` or `instructions_in_order` |
| `rule_results` | list or `null` | Every hard rule, including the ones that passed. Empty if the rules themselves crashed |
| `latency_ms` | int or `null` | How long the whole check took |
| `checked_by` | `rules`, `ai` | Note: this used to be `claude` |

`reason` can end in `(+2 more)` when several rules failed. The full list is in `rule_results`.

In sharp mode, `reason` reads "Mandate said X, order has Y", under 12 words, with no `(+N more)`. An order the watch let through and captured has `reason` set to exactly `Let through: low risk while rubber-stamp watch was on`. Its `verdict` and `risk_score` are still what the check said.

### `approval`

| Field | Type | Notes |
|---|---|---|
| `action` | `approve`, `reject` or `null` | `null` means opened but not decided yet |
| `opened_at` | time | Stamped by the server on the first `POST /review/{token}/opened`. Later calls don't move it |
| `decided_at` | time or `null` | When the tap reached the server |
| `decision_ms` | int or `null` | `decided_at` minus `opened_at`, on the server's clock. This is the attention number. `0` means the page never reported it opened |
| `client_decision_ms` | int or `null` | What the client said it measured. Not trusted, don't show it as the real number |
| `decided_by` | string or `null` | Always `null` for now, there are no logins |

## Merchant endpoints

### `GET /orders?limit=50`

The order object, newest first. `limit` is capped at 200.

### `GET /orders/{id}`

The order object, plus the full history. `intent_check` and `approval` are the newest entries in these lists, so prefer those two.

```json
{ "decisions": [ ], "approvals": [ ] }
```

### `POST /orders/{id}/review-link`

Creates a review link and returns it. No body. Success is a `201`:

```json
{
  "review_token": "kL0x...43 characters...",
  "expires_at": "2026-12-03T01:56:55Z"
}
```

This response is the only place the token ever appears. It isn't stored, so it can't be read back from `GET /orders`.

Calling this again makes a new token and kills the old one. That's how to recover a lost link, and also how to recover after a `502` that says to ask for a new link. Don't call it on every page load, or a link already sent to someone stops working.

`409` if the order isn't waiting for review, a payment is in progress, or the PayPal hold has expired. `404` if there's no such order.

### `GET /mandates?limit=50`

A list of mandates, newest first, in the same shape as `GET /mandates/{id}`. `limit` is capped at 200.

### `GET /mandates/{id}`

The mandate, including `rules` and `max_price`. `raw_request` is the shopper's own words. `rules` holds the parsed fields, including `item`, `color`, `size` and `quantity`, and `max_price`, `merchant` and `expires_at` are at the top level.

### `GET /stats`

Counts for the dashboard, plus where the rubber-stamp watch stands.

```json
{
  "total_orders": 42,
  "auto_captured": 30,
  "needs_review": 2,
  "approved": 7,
  "voided": 3,
  "median_decision_ms": 1500,
  "fast_streak": 3,
  "rubber_stamp_active": true,
  "fast_threshold_ms": 2000,
  "recent_decision_ms": [300, 900, 1500, 8000, 4000]
}
```

| Field | Notes |
|---|---|
| `auto_captured` | Orders Leash captured on its own (`review_state` is `auto_approved`) |
| `needs_review` | Holds waiting on a human right now |
| `approved` | A human tapped approve |
| `voided` | Every order whose hold was released |
| `median_decision_ms` | Over every human decision. `null` until there is one |
| `fast_streak` | Fast approvals in a row, newest first |
| `rubber_stamp_active` | `true` once `fast_streak` reaches 3. The next order is checked in sharp mode |
| `fast_threshold_ms` | An approval under this counts as fast |
| `recent_decision_ms` | The last 10 human decisions, newest first. An entry can be `null` on old rows |

## The rubber-stamp watch

A human who approves escalated orders without reading them isn't really reviewing anything. So when the last 3 human decisions were all approvals under 2 seconds (`decision_ms`, the server's number), the watch turns on. One reject or one slow approval turns it off.

While it's on, orders are checked in sharp mode:

- **Still always escalated:** any failed hard rule, any `mismatch`, and any check that couldn't run (Claude down or timed out).
- **Otherwise escalated only if** `risk_score` is 60 or more. Below that, the order is captured and its `reason` says it was let through.
- **Reasons are sharper:** "Mandate said black, order has olive", so the next look is a real one.

The watch is global, not per shopper.

## Disputes

When a buyer disputes a captured order, Leash files its records with PayPal as evidence: what the buyer authorized, what was ordered, how it was checked, and who released the money. All four routes are merchant only.

The usual flow is: sync, open the evidence (this drafts the summary), read it, then submit.

### The dispute object

```json
{
  "id": 3,
  "paypal_dispute_id": "PP-R-ABC-10001234",
  "order_id": 12,
  "reason": "UNAUTHORISED",
  "status": "WAITING_FOR_SELLER_RESPONSE",
  "evidence_submitted": false,
  "evidence_submitted_at": null,
  "evidence_type": null,
  "has_summary": true,
  "created_at": "2026-11-06T14:00:00Z"
}
```

- **`reason`** and **`status`** are PayPal's own strings, stored as they come. Show them as is. `status` is as of the last sync or submit, so it can be behind PayPal.
- **`has_summary`**: a summary has been drafted, so submit can go.
- **`evidence_type`** is what was filed, like `OTHER`. `null` until then.

### `GET /disputes?limit=50`

Dispute objects, newest first. `limit` is capped at 200.

### `POST /disputes/sync`

No body. For every captured order, asks PayPal for disputes on its capture ID, and saves or updates them. A dispute only counts if PayPal's details name that capture as the seller transaction. Success is a `200`:

```json
{ "orders_checked": 12, "disputes_found": 1, "created": 1, "updated": 0, "errors": [] }
```

A PayPal failure on one order doesn't stop the rest. It shows up in `errors` as `{"order_id": 4, "message": "..."}`. This makes PayPal calls per captured order, so it can take a few seconds.

### `GET /disputes/{id}/evidence`

The evidence packet plus a summary for the PayPal reviewer. Nothing is sent to PayPal.

```json
{
  "dispute": { "...": "the dispute object" },
  "packet": {
    "dispute": { "paypal_dispute_id": "PP-R-ABC-10001234", "reason": "UNAUTHORISED", "status": "WAITING_FOR_SELLER_RESPONSE" },
    "mandate": { "id": 7, "raw_request": "Black trail runners, women's size 9, up to $120, from Fernwood Supply Co.", "rules": { }, "max_price": "120.00", "max_price_inclusive": true, "merchant": "Fernwood Supply Co", "expires_at": null, "created_at": "..." },
    "order": { "id": 12, "store": "Fernwood Supply Co", "items": [ ], "total": "98.00", "shipping_amount": "0.00", "currency": "USD", "status": "captured", "paypal_order_id": "...", "capture_id": "...", "created_at": "..." },
    "intent_check": { "verdict": "match", "risk_score": 5, "reason": "Matches the mandate.", "mismatched_fields": [], "checked_by": "ai", "checked_at": "...", "watch_mode": "normal" },
    "decision": { "released_by": "automatic capture", "review_state": "auto_approved", "action": "capture", "decision_ms": null, "decided_at": "..." }
  },
  "summary": "The buyer told their agent: ..."
}
```

- **`decision`**: for a human approval, `released_by` is `human review`, with `decision_ms` (the server's own number), `opened_at` and `decided_at`. For an automatic capture, `decided_at` is the check's time, since the capture follows it straight away.
- **`intent_check`** can be `null` if no check was saved.
- The packet leaves out `buyer_label`.
- **`summary`** is drafted by Claude from the packet and saved on the dispute: 3 to 6 plain sentences, 1500 characters at most. It sticks to what's in the packet, including facts that don't help the merchant. It's written for a PayPal reviewer, so it puts the packet's fields in plain English ("a risk rating of 100 out of 100", "Leash was in its normal review mode") and never uses a field name like `watch_mode`. Each call drafts a new one, and the newest is what submit sends. Once evidence is submitted, this returns the summary that was sent and doesn't draft again.
- `502` if the summary couldn't be drafted, including when a draft still contains one of the packet's field names. Try again. `404` if there's no such dispute.

### `POST /disputes/{id}/submit-evidence`

No body. Sends PayPal a PDF of the packet, with the saved summary as the notes. Success is a `200` with the dispute object, now with `evidence_submitted: true`.

Before sending, it reads the dispute from PayPal and checks:

| Check | If it fails |
|---|---|
| Evidence not already submitted | `409`, `detail.evidence_submitted_at` says when |
| No other submit in progress | `409`. A submit that crashed frees up after 2 minutes |
| A summary was drafted | `409`, open `GET /disputes/{id}/evidence` first |
| PayPal's status is `WAITING_FOR_SELLER_RESPONSE` **and** its details have a `provide-evidence` link | `409` with `detail.status` and `detail.provide_evidence_link`, so the page can say which. The row gets PayPal's latest status |
| PayPal asked for an evidence type Leash can fill | `409` with `detail.requested_types` (see below) |

The evidence type is the first one PayPal requested from the seller. If it requested nothing, it's `OTHER`, and the notes start with a sentence saying so. Leash never sends `PROOF_OF_FULFILLMENT`, `PROOF_OF_REFUND` or `PROOF_OF_RETURN`, because PayPal requires tracking or refund IDs for those and Leash has none. If those are all PayPal asked for, the merchant has to answer in PayPal's Resolution Center.

If PayPal rejects the evidence, the response is a `502` with the same `paypal_*` fields as the order endpoints, and nothing is marked as submitted, so it can be tried again.

## Review endpoints

These take the token in the path and nothing else. Any error's `detail` has a `message` that's safe to show the human.

### `GET /review/{token}`

What the confirm page shows. It's read only and changes nothing, so a link preview or a refresh is harmless.

```json
{
  "id": 42,
  "items": [
    { "name": "Trail runner", "color": "red", "size": "women's 9", "quantity": 1, "unit_price": "110.00" }
  ],
  "total": "120.00",
  "shipping_amount": "10.00",
  "currency": "USD",
  "status": "authorized",
  "review_state": "needs_review",
  "authorization_expires_at": "2026-12-03T01:56:55Z",
  "mandate": {
    "buyer_label": "Sam",
    "raw_request": "Black trail runners, women's size 9, up to $120, from Fernwood"
  },
  "intent_check": {
    "verdict": "mismatch",
    "risk_score": 90,
    "reason": "Ordered red, mandate says black",
    "mismatched_fields": ["color"]
  }
}
```

`intent_check` can be `null`, so handle a missing reason. `authorization_expires_at` is also when the link stops working.

| Code | When |
|---|---|
| `200` | The link is good |
| `404` | No such link, or a newer one replaced it |
| `410` | The link expired, was already used, or the order isn't waiting for review anymore |

### `POST /review/{token}/opened`

Starts the attention clock. Call it once, after the page has rendered and the order is really on screen. Don't call it when fetching the data, and don't call it from anywhere a link preview could reach. No body, and success is a `204` with nothing in it.

Only the first call counts, so a refresh calling it again doesn't reset the clock. If it's never called, approving still works and records `decision_ms` as `0`, so don't let a failed call here block the buttons.

It returns the same `404` and `410` as `GET /review/{token}`.

### `POST /review/{token}/approve` and `POST /review/{token}/reject`

Approve captures the money and reject releases the hold. The body is optional. If you send `decision_ms` it's saved as `client_decision_ms`, and has to be a whole number from 0 to 2147483647.

```json
{ "decision_ms": 4200 }
```

Success is a `200` with the same shape as `GET /review/{token}`, showing the new `status` and `review_state`.

| Code | When | What to do |
|---|---|---|
| `404` | No such link, or a newer one replaced it | Say the link isn't valid |
| `409` | Already used, not waiting for review, or a payment is in progress | Show the current state from `detail` |
| `410` | The link expired | Say it expired |
| `422` | `decision_ms` is invalid | A frontend bug |
| `502` | PayPal failed. The order is unchanged | Offer to try again with the same link, it's safe |

The `409` and most `502` bodies carry the current states:

```json
{
  "detail": {
    "message": "PayPal capture failed, the order is still waiting for review",
    "status": "authorized",
    "review_state": "needs_review"
  }
}
```

Two `502` cases are different:

- **PayPal declined the capture:** `status` comes back as `failed`, and trying again gets a `409`.
- **PayPal succeeded but saving failed:** only `message` is present. The link is used up, so the merchant has to create a new one after a couple of minutes. Going again with it is safe and won't charge twice.

Double taps are safe. Only one goes through, and the others get a `409`. Still disable the button while a request is in flight.

## Removed routes

These now return `404`. `captureOrder` and `voidOrder` in `frontend/src/api.ts` should be replaced with calls to the review endpoints.

- `POST /orders/{id}/capture`
- `POST /orders/{id}/void`
- `POST /orders/{id}/approve`
- `POST /orders/{id}/reject`

## Agent endpoints

The agent only ever sees where an order stands. It never gets a token, a reason, or mandate details, so it can't read why it was flagged and adjust.

### `POST /orders` and `POST /orders/{id}/authorize`

These return the status object after the check and any auto-capture, so they can take several seconds.

```json
{
  "id": 42,
  "status": "authorized",
  "review_state": "needs_review",
  "total": "120.00"
}
```

`POST /orders` adds `payer_action_url` when there's no `vault_id`, since the buyer has to approve at PayPal before there's a hold.

A network failure talking to PayPal is a `502` with a `message`, and the `paypal_*` fields in `detail` are `null`. After a `502`, `POST /orders/{id}/authorize` can be retried, and `POST /orders` leaves that order as `failed`.

### `GET /agent/orders/{id}`

The same status object. This is how the agent finds out what a human decided.

## Demo endpoints

### `POST /demo/orders`

Merchant only. Builds an order from a mandate's rules and runs it through the same path as `POST /orders`: a real sandbox hold, the intent check, then auto-decide. It can take around 30 seconds, so show a loading state.

```json
{ "mandate_id": 7, "scenario": "wrong_color" }
```

| `scenario` | What it builds | Refused when the mandate has |
|---|---|---|
| `clean` | Matching item and attributes, about 80% of the cap ($50.00 if there's no cap), same store | No item, or values that can't make a valid order |
| `wrong_color` | The same, with a different color | No item, or no color |
| `over_cap` | The same, about 15% over the cap | No item, or no cap |
| `wrong_store` | The same, at a different fictional store | No item, or no store |

Success is a `201` with the order object plus `decisions` and `approvals`, like `GET /orders/{id}`. The verdict, risk score and reason are in `intent_check`, which can be `null`. "Needs review" is `review_state`, not `status`: that order has `status: "authorized"` and `review_state: "needs_review"`.

`clean` is only clean on a live USD mandate. An expired mandate or another currency still gets flagged, as it should.

| Code | When |
|---|---|
| `404` | No such mandate |
| `422` | Unknown scenario, or the mandate has nothing for the scenario to break. `detail` is a string safe to show unless the scenario itself was invalid |
| `502` | PayPal failed, same as `POST /orders` |
| `503` | `DEMO_VAULT_ID` isn't set on the API |

### `DELETE /demo/seeded`

Merchant only, used by `backend/scripts/seed_demo.py --reset`. Deletes the seeded orders and mandates, which are the ones with a `buyer_label` starting with `seed-demo:`. Success is a `200` with `deleted_orders` and `deleted_mandates`.

It's a `409` with `open_order_ids` in `detail` while any of those orders still has an open hold, so nothing gets forgotten at PayPal. Void those first.
