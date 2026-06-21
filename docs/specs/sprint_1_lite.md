# Sprint 1 LITE — Stripe deposit charging (minimum to ship)

**Goal:** Make live-mode booking actually charge the deposit. Until this lands, every production booking sits in `pending_payment` forever (audit finding #1).

**Effort:** ~5 days of focused work. Status of each item:

| Item | Days | Verdict |
|---|---|---|
| Deposit PaymentIntent creation in `POST /api/bookings` | 1 | MUST |
| Stripe Elements payment page (`/pay/<booking_id>`) | 1.5 | MUST |
| Idempotency keys on 4 missing call sites | 0.25 | MUST |
| `stripe.api_version` pin | 5 min | MUST |
| Retry endpoint for failed PI | 0.25 | MUST |
| Tests | 1.5 | MUST |
| Docs | 0.5 | MUST |

**Out of scope for this sprint** (parked for later): state machine refactor, `studio_id` denormalization, subscription tier gating, separated charges escrow, dispute auto-freeze, card country detection, application fee refund on cancellation.

---

## 1. Data model

**No schema changes.** All needed columns already exist on `bookings`:
- `stripe_payment_intent_id` (server.py:798)
- `status` (current statuses: `pending_payment`, `confirmed`, `completed`, `cancelled_client/artist`, `no_show`, `refunded`, `disputed`, `payment_failed`)
- `currency` (defaults to `'CZK'`)
- `deposit_cents`, `platform_fee_cents`

This is intentional — minimum viable change, no migration to roll out.

---

## 2. API surface

### 2.1 Modified: `POST /api/bookings`

**Current behavior** (server.py:4972): inserts booking with `status='pending_payment'` and `stripe_payment_intent_id=NULL` (in live mode) → broken.

**New behavior:**

1. Insert booking row (same as today).
2. **If live mode** (`STRIPE_SECRET_KEY` set AND artist has `stripe_charges_enabled`):
   - Call `stripe.PaymentIntent.create(...)` with:
     - `amount=deposit_cents`
     - `currency='czk'`
     - `application_fee_amount=platform_fee_cents`
     - `transfer_data={'destination': artist.stripe_account_id}`
     - `metadata={'inklink_booking_id': bid, 'inklink_kind': 'deposit'}`
     - `idempotency_key=f'deposit-{slot_id}-{user_id}-{day}'` (`day` = today's UTC date, so same slot+user+day = same PI; next day = new key)
     - `description=f'InkLink — záloha za rezervaci #{bid}'`
   - Update booking row: `stripe_payment_intent_id = pi.id`
3. **If demo mode** (no Stripe key OR no Connect): unchanged — `status='confirmed'` directly, no PI.

**Response (new fields added):**

```json
{
  "id": 1234,
  "status": "pending_payment",
  "payment": {
    "mode": "live",                    // "live" | "demo"
    "client_secret": "pi_...secret_...", // ONLY in live mode
    "publishable_key": "pk_live_...",    // ONLY in live mode
    "payment_url": "/pay/1234"           // landing page that loads Elements
  }
}
```

In demo mode, `payment` is `{"mode": "demo"}`.

**Error responses (existing 400/403/409 unchanged).** New: `502 {"error": "Stripe nedostupný, zkus za chvíli."}` if Stripe API fails. In that case the booking row is **not inserted** (transaction rolled back) — client retries cleanly without orphan booking.

### 2.2 New: `POST /api/bookings/<bid>/retry-payment-intent`

For when the first PI failed (`payment_intent.payment_failed` webhook fired and status moved to `payment_failed`). Lets client retry without re-creating the booking.

**Auth:** booking's client_id only.
**Allowed states:** `pending_payment`, `payment_failed`. Reject anything else with 409.
**Idempotency:** `f'deposit-retry-{bid}-{attempt_n}'` — server tracks attempt count via `add_col('bookings', 'payment_attempts INTEGER DEFAULT 0')` (one column add).

Wait — that IS a schema change. Adding it because retry without idempotency rotation would re-use the failed PI's idempotency key and Stripe returns the same failed PI.

**Schema addition:** `bookings.payment_attempts INTEGER DEFAULT 0` (single column).

**Response:**

```json
{
  "client_secret": "pi_..._secret_...",
  "publishable_key": "pk_live_...",
  "payment_url": "/pay/1234"
}
```

### 2.3 New: `GET /pay/<bid>` (HTML page) + `GET /api/pay/<bid>` (JSON for frontend)

`/pay/<bid>` serves a new page `public/deposit-pay.html` (modeled on existing `balance-pay.html`) that:
- Loads Stripe.js
- Calls `GET /api/pay/<bid>` for `{client_secret, amount_cents, artist_name, when}`
- Renders Stripe Elements card form
- On submit: `stripe.confirmCardPayment(client_secret, {payment_method: {card: cardElement}})`
- Success → redirect to `/my-bookings`
- Failure → display Stripe error, button to retry calls `POST /api/bookings/<bid>/retry-payment-intent`

`GET /api/pay/<bid>` — public (no auth required, the URL is the capability). Returns:

```json
{
  "id": 1234,
  "amount_cents": 150000,
  "currency": "CZK",
  "client_secret": "pi_..._secret_...",
  "publishable_key": "pk_live_...",
  "artist_name": "Jana K.",
  "artist_studio": "Black Sails Tattoo",
  "when": "2026-06-15T14:00:00",
  "design_note": "Vlk na předloktí",
  "status": "pending_payment"
}
```

Returns `404` if booking doesn't exist. Returns `410` if booking already confirmed/cancelled (frontend redirects to `/my-bookings`).

### 2.4 Webhook handler — no API change, just state cleanup

`payment_intent.succeeded` (server.py:7761): unchanged.

`payment_intent.payment_failed` (server.py:7800): currently sets status to `payment_failed`. Confirm + add telemetry event so admin sees retry rate.

### 2.5 Idempotency key additions (4 sites)

| File:line | Call | New idempotency_key |
|---|---|---|
| server.py:6425 (`_create_balance_charge`) | `stripe.PaymentIntent.create` | `f'balance-{booking_id}-{int(time.time()/86400)}'` (per-day) |
| server.py:7569 (`connect_onboard`) | `stripe.Account.create` | `f'connect-account-{user_id}'` |
| server.py:7589 (`connect_onboard`) | `stripe.AccountLink.create` | `f'connect-link-onboard-{user_id}-{day}'` |
| server.py:7616 (`connect_refresh`) | `stripe.AccountLink.create` | `f'connect-link-refresh-{user_id}-{day}'` |

### 2.6 Stripe API version pin

Right after `stripe.api_key = STRIPE_SECRET_KEY` (server.py:130):

```python
stripe.api_version = '2024-12-18.acacia'  # use latest stable at sprint start; bump deliberately in PRs
```

---

## 3. Frontend

### 3.1 New page: `public/deposit-pay.html`

Copied from `balance-pay.html` template; only differences:
- Calls `/api/pay/<bid>` not `/api/balance-pay/<bid>`
- Title: "Zaplatit zálohu" instead of "Zaplatit doplatek"
- Body copy adjusted: záloha terminology + storno pravidla mention (96 h = 100 %, 48 h = 50 %)

### 3.2 Modified: booking create flow in `profile.html`

Current: after `POST /api/bookings` success → alert + reload.
New: if response has `payment.mode === 'live'` → redirect to `payment.payment_url`. Demo unchanged (alert + reload).

### 3.3 Modified: `my-bookings.html`

Add "Zaplatit zálohu" button next to bookings with `status='pending_payment'` (currently no payment CTA visible there). Link to `/pay/<bid>`.

---

## 4. Test plan

New tests in `tests/test_e2e.py` (or new `tests/test_stripe_deposit.py`):

| # | Name | Asserts |
|---|---|---|
| 1 | `test_create_booking_returns_demo_mode_without_stripe` | No `STRIPE_SECRET_KEY` → response has `payment.mode = 'demo'`, booking confirmed |
| 2 | `test_create_booking_live_mode_creates_pi` | With mocked Stripe → PI created, `client_secret` returned, booking status `pending_payment` |
| 3 | `test_create_booking_idempotent_same_day` | POST same slot twice in same day → same `stripe_payment_intent_id` returned (one PI in Stripe) |
| 4 | `test_create_booking_stripe_error_rolls_back` | Stripe raises → no booking row in DB, 502 returned |
| 5 | `test_retry_payment_intent_uses_new_key` | After `payment_failed`, retry → different PI ID, `payment_attempts` incremented |
| 6 | `test_retry_rejected_on_completed_booking` | Booking in `confirmed/completed/cancelled_*` → retry returns 409 |
| 7 | `test_get_api_pay_returns_safe_fields` | Public endpoint exposes only display fields, no `client_id`, no `email`, no admin notes |
| 8 | `test_get_api_pay_410_after_confirm` | Booking confirmed → endpoint returns 410 |
| 9 | `test_api_version_is_pinned` | `stripe.api_version` is set (not None) |
| 10 | `test_idempotency_keys_present` | mock that captures all `stripe.X.create(**kwargs)` calls → assert every mutating call has `idempotency_key` |

**Test infrastructure:** add a `unittest.mock` shim around `stripe.PaymentIntent.create`, `stripe.Account.create`, `stripe.AccountLink.create` so tests don't hit the network. Use `monkeypatch` style. Existing tests already run in demo mode (no key) — keep that path green.

**Suite target after Sprint 1 LITE:** 78 (current) + 10 = **88 tests passing**.

---

## 5. Rollout plan

1. **PR1**: Schema migration (`payment_attempts` column) + idempotency keys + API version pin. Low risk, ship first.
2. **PR2**: Deposit PI creation in `POST /api/bookings` + `deposit-pay.html` + `/api/pay/<bid>`. Behind env flag `ENABLE_DEPOSIT_PI=1`. Test in dev with Stripe test mode keys.
3. **PR3**: Frontend redirect to `/pay/<bid>` after successful booking POST. Behind same flag.
4. **PR4**: Enable in prod by setting `ENABLE_DEPOSIT_PI=1` on Railway. Monitor Sentry + Stripe dashboard for 24 h. **First real booking will validate.**
5. If working: keep enabled. If broken: unset env flag, code path falls back to current `pending_payment` (which is still broken, but no worse than today).

Feature flag is for fast rollback only. Once stable for a week, remove the flag in a cleanup PR.

---

## 6. Open questions for user

Before implementation:

1. **Stripe API version** — pinning to `2024-12-18.acacia` (currently latest stable as of audit). Confirm or pick different?
2. **Idempotency key TTL strategy** — using `f'deposit-{slot_id}-{user_id}-{day}'` where `day` rotates daily. Means same user retrying same slot **next day** gets a fresh PI. Alternative: stable key forever, retries always go to same PI (could be locked to a failed state). **I prefer daily rotation** but flag this as a decision.
3. **Demo mode behavior** — keep as-is (booking auto-confirms without PI) or move it to a sandbox that simulates PI lifecycle? **I prefer keeping current behavior** — demo mode is for local dev only, not user-facing.
4. **Retry endpoint auth** — currently `client_id` only. Should artist also be able to trigger retry on behalf of client (e.g., "I see your card failed, here's a new link")? **I default to no** — privacy + artist shouldn't see card-failure details.
5. **Frontend Elements vs Checkout** — proposed: Stripe Elements inline (more control, matches paper-mode design). Alternative: Stripe Checkout hosted page (less code, less design fit). **I prefer Elements** unless you want to ship 2 days faster with Checkout.

---

## 7. Definition of done

- [ ] 10 new tests pass, full suite 88 green
- [ ] Real Stripe test-mode booking goes pending_payment → confirmed end-to-end
- [ ] Sentry has 0 errors from `/api/bookings` or `/pay/<bid>` for 48 h post-deploy
- [ ] `processed_stripe_events` has `payment_intent.succeeded` and `payment_intent.payment_failed` entries from test runs
- [ ] No regression in 78 existing tests
- [ ] `docs/specs/stripe.md` written (replaces some of this doc + current Stripe section in PROD_OPS)

---

## Wait point

**This is a proposal, not committed work.** Before I write code, confirm:

1. Roadmap is approved.
2. Sprint 1 LITE scope as above is what you want.
3. Open questions (section 6) — your answers.

Then I implement in 4 PRs as listed.
