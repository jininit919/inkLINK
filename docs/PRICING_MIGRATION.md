# Pricing Engine — Migration Guide

This document covers the rollout of the tiered commission / discount / founding
program system. **Read all of it before deploying** — there are operational
steps (founding-artist enrollment, founding-client backfill) that aren't
automatic.

## What's changing

### Before
- Flat 8 % commission, hardcoded as `PLATFORM_COMMISSION_PCT = 8` in `server.py`
- No service fee
- No discount engine
- No founding-artist/client programs
- Stripe destination charges (`application_fee_amount` + `transfer_data`)
- Funds flow directly to artist's Connect account at payment time

### After (Fáze A — this commit)
- New `pricing/` module with pure functions for all calculations
- `pricing.config.py` is the **single source of truth** for rates
- New DB tables: `economics_snapshots`, `processed_stripe_events`, `referrals`,
  `discount_codes`, `discount_redemptions`, `telemetry_events`
- New columns on `users`: `founding_artist`, `founding_client`,
  `founding_artist_started_at`, `account_credit_cents`
- `PLATFORM_COMMISSION_PCT = 8` **still works** — existing booking code
  uses it. The new engine runs in parallel until Fáze B integrates it.
- 41 unit tests passing (`python3 -m unittest tests.test_pricing -v`)

### After (Fáze B — next commit)
- Booking creation calls `calculate_booking_economics()` and stores snapshot
- `application_fee_amount` derived from the engine's `artist_commission`
- Webhook handlers added: `payment_intent.succeeded`, `charge.refunded`,
  `charge.dispute.created` — with idempotency via `processed_stripe_events`
- Telemetry events emitted at every state transition

### After (Fáze C — final commit)
- Admin UI for issuing MANUAL_PROMO codes
- Client-facing UI for entering discount code at checkout
- KPI dashboard at `/admin/kpis` reading from `economics_snapshots`
- Founding-artist enrollment UI
- Reconciliation cron job (daily diff vs Stripe balance)

## Operational checklist before Fáze B

### 1. Enroll founding artists (manual SQL)

The first ~20 artists who joined get founding status. Find them by signup
order or community-flagged. To enroll:

```sql
UPDATE users
SET founding_artist = 1,
    founding_artist_started_at = NULL  -- clock starts at first completed booking
WHERE id IN (...)  -- list of artist IDs
  AND is_artist = 1;
```

The clock (`founding_artist_started_at`) is set automatically by the engine
when their first booking moves to `completed` state — leave it `NULL` at
enrollment. If you need to enroll a long-tenured artist retroactively, set
the timestamp manually to control where they land in the 0/5/tiered window.

### 2. Backfill founding clients (first 500 signups)

```sql
UPDATE users
SET founding_client = 1
WHERE id IN (
  SELECT id FROM users
  WHERE is_artist = 0
  ORDER BY created_at ASC
  LIMIT 500
);
```

After this, the **signup endpoint** in `server.py` should be updated (Fáze B)
to automatically grant `founding_client = 1` until 500 total exist:

```python
# At end of POST /api/register:
count = conn.execute('SELECT COUNT(*) AS c FROM users WHERE founding_client = 1').fetchone()
if count['c'] < FOUNDING_CLIENT_MAX:
    conn.execute('UPDATE users SET founding_client = 1 WHERE id = ?', (new_user_id,))
```

### 3. Verify configuration

Open `pricing/config.py` and confirm:

- `COMMISSION_TIERS` matches our published pricing (12 / 8 / 5 %)
- `MIN_COMMISSION_CZK = 200`
- `SERVICE_FEE_RATE = 0.03`, `SERVICE_FEE_CAP_CZK = 150`
- `STRIPE_FEES['card_eea']` matches the actual Stripe rate card (verify
  in the Stripe dashboard — they revise this occasionally)

### 4. Run tests in CI

Add to `Procfile` / Railway build hook or run locally before deploying:

```bash
python3 -m unittest tests.test_pricing -v
# Expect: Ran 41 tests in 0.00Xs, OK
```

A single failing test = do not deploy.

## Rollback plan

If something breaks in production:

1. The new `pricing/` module is read-only from `server.py`'s perspective
   in Fáze A — there is **no integration point yet**. Existing booking
   flow is untouched. Rollback = `git revert` the pricing commit; no
   DB schema rollback needed (new columns/tables are harmless if unused).
2. Fáze B will integrate via a `USE_NEW_PRICING_ENGINE` env flag (TBD).
   To disable in production: set `USE_NEW_PRICING_ENGINE=0` in Railway
   env, restart. Falls back to the legacy 8 % path.

## Schema diff (this commit)

```sql
-- New columns on users
ALTER TABLE users ADD COLUMN founding_artist INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN founding_client INTEGER DEFAULT 0;
ALTER TABLE users ADD COLUMN founding_artist_started_at TEXT DEFAULT NULL;
ALTER TABLE users ADD COLUMN account_credit_cents INTEGER DEFAULT 0;

-- New tables
CREATE TABLE economics_snapshots (...);
CREATE TABLE processed_stripe_events (...);
CREATE TABLE referrals (...);
CREATE TABLE discount_codes (...);
CREATE TABLE discount_redemptions (...);
CREATE TABLE telemetry_events (...);
```

All `CREATE` statements are idempotent (`IF NOT EXISTS`). All `ALTER`s go
through the `add_col()` helper which catches duplicates.

## Known gaps / explicit non-goals for Fáze A

- **No Stripe migration to separated charges.** Founder explicitly chose
  to keep destination charges (`application_fee_amount` + `transfer_data`).
  Real escrow with delayed transfers is deferred. The engine computes the
  numbers; Fáze B wires them into `application_fee_amount`.
- **No 24h payout cooldown.** With destination charges, funds flow to
  artist immediately. The cooldown config (`PAYOUT_COOLDOWN_HOURS`) is
  reserved for when we migrate.
- **No native idempotency keys on Stripe API calls yet** — added in Fáze B.
- **No webhook handlers for `payment_intent.succeeded` etc.** — Fáze B.

## Questions / weird edge cases

- **Q:** A founding artist completes their first booking on day 0 — what's
  the commission?
  **A:** Day 0 = day 1 in the engine (`founding_artist_status_at` returns
  `day=1` immediately). Commission is 0.
- **Q:** Client uses WELCOME, then the booking is refunded. Does the code
  reset?
  **A:** No — by design. `discount_redemptions` row persists. We don't
  give them a second WELCOME for gaming refunds.
- **Q:** What if an artist is also a client (uses their own service)?
  **A:** That's a separate user account (artists don't book themselves).
  If a special case arises, manual SQL fix.
