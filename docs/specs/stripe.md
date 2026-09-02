# InkLink — Stripe architecture & escrow ADR

**Status:** Implemented (Sprint 1 hardening pass, 2026-09-02). Supersedes the
"Out of scope" list in [sprint_1_lite.md](sprint_1_lite.md) for the items
below; the roadmap's full Sprint 1 (`../roadmap.md`) is otherwise complete
except where noted.

---

## 1. Payment pattern: destination charges (unchanged)

InkLink charges the client on the **platform's** Stripe account with
`transfer_data.destination` pointing at the artist's Connect Express account
and `application_fee_amount` set to InkLink's commission. Stripe moves the
artist's share automatically at charge time — there is no manual transfer
step, and no code path uses `stripe.Transfer.create` or separate
charge-then-transfer.

## 2. ADR: keep destination charges, do not migrate to separated charges

**Decision:** stay on destination charges. Do not build the
"separated charges + delayed transfer" escrow pattern that
`../roadmap.md` Sprint 1 flagged as a 2-day decision point.

**Why:**
- The artist is paid at the moment of charge, before the tattoo session
  happens. A no-show or a bad session gives InkLink no automatic hold to
  claw back from — refunds/no-show handling rely on `reverse_transfer` +
  `refund_application_fee` on refund (implemented, see §4) or on the
  artist's Stripe balance covering a dispute.
- `../roadmap.md`'s own risk register calls this acceptable: *"at low
  volume destination charges with manual reversals is acceptable."*
  As of this writing InkLink has not yet onboarded its first real artist
  (`docs/launch_checklist.md` §"PRVNÍ TATÉR" is still pending) — there is
  no production booking volume to justify the migration risk yet.
- Migrating is a bigger, riskier change to a live payment path than
  anything else in this hardening pass — the roadmap itself estimated it
  could slip from 2 days to 5.

**Revisit when any of these happen:**
- First real (non-test) no-show or dispute where the lack of a payment
  hold caused an actual clawback problem (i.e. the artist's Connect
  balance couldn't cover a reversal).
- Booking volume grows enough that manual reversal risk stops being
  "low volume, acceptable" — no hard number is set; use judgment plus the
  above incident signal.
- A studio/enterprise customer requires payment hold as a contractual term.

**What changes if/when this gets revisited:** separated charges
(`stripe.Charge.create` on the platform account, no `transfer_data`) +
`stripe.Transfer.create` triggered manually once the booking reaches
`completed`. This needs its own spec before implementation — not scoped
here.

## 3. Booking state machine

Formalized in `server.py` as `BOOKING_STATUSES` / `BOOKING_TRANSITIONS` /
`transition_booking(conn, bid, to_state, ...)`. Replaces what used to be
~7 separate ad-hoc `UPDATE bookings SET status=...` call sites (webhook
handlers, `cancel_booking`, `complete_booking`, `mark_no_show`). Every
transition is a single atomic `UPDATE ... WHERE status IN (...)` — no
separate SELECT-then-UPDATE race — and every successful transition is
logged to `booking_status_log` (`booking_id, from_status, to_status,
changed_at`) for audit.

Key design choice: `disputed` is reachable from every other status
(a chargeback can land regardless of the booking's current status, and
silently dropping that update risks missing Stripe's dispute-response
deadline). Every other target status has a narrower, explicit set of
legal predecessors — see the table in `server.py` for the full mapping
and the comment above it for how each entry was derived from the
pre-existing per-endpoint guards.

## 4. Refunds

Both refund call sites (`cancel_booking`, `decide_refund_request`) now pass
`reverse_transfer=True, refund_application_fee=True` to `stripe.Refund.create`.
Without these, a destination-charge refund pulls from the *platform's*
Stripe balance instead of clawing back the artist's transfer, and InkLink
keeps its commission on money that was given back to the client — both are
now fixed. `refund_application_fee` refunds proportionally for partial
refunds (Stripe's own behavior), so this is correct for the 100/50/0 %
cancellation-window tiers without extra logic.

## 5. Disputes

`charge.dispute.created` freezes the artist's payouts
(`Account.modify(..., settings={'payouts': {'schedule': {'interval':
'manual'}}})`) in addition to marking the booking `disputed`.
`charge.dispute.closed` resumes daily payouts and moves the booking to
`completed` (dispute won — funds stay) or `refunded` (lost/other — client
keeps the money, same practical effect as a voluntary refund). This is an
approximation where the pre-dispute status wasn't necessarily `completed`;
revisit if that distinction ever matters for reporting.

## 6. Card country / economics reconciliation

Pre-payment economics always estimate `stripe_card_type='card_eea'`
(`pricing/config.py`). `payment_intent.succeeded` now reads
`charges.data[0].payment_method_details.card.country` and, if the card is
outside the EEA, inserts a `kind='adjust'` row in `economics_snapshots`
with the corrected `stripe_fee`/`inklink_net` (via the existing pure
`pricing.stripe_fee_for()` helper — no re-run of the full economics engine,
since the stored snapshot doesn't retain enough of the original input to
safely reconstruct it). This is a reporting-only correction: the Stripe
charge and `application_fee_amount` were already fixed at PI creation time
and are not touched retroactively.

## 7. Still open (not in this pass)

- `studio.subscription_tier` + `require_tier()` exist but aren't wired to
  any endpoint — no B2B-tier-gated routes exist until Sprint 2+.
- Currency-conversion surcharge detection (`stripe_currency_conv_applies`)
  is not automated — still requires manual/default handling.
- Remaining non-idempotent low-risk call: none known after this pass
  (`Account.create_login_link`, the `/api/_diag/*` account-creation calls,
  and all mutating PaymentIntent/Refund/Account/AccountLink calls now carry
  idempotency keys).
