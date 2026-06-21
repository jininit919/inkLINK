# InkLink B2B SaaS — Implementation Roadmap

**Source:** [docs/audit_2026-05-24.md](audit_2026-05-24.md)
**Author:** Phase 1 planning pass (Claude Code)
**Status:** Draft — awaiting user approval

---

## TL;DR (opinionated)

- **Ship in this order:** Sprint 1 → 2 → 3 → 4 → 5 → 6 → 8. **Skip Sprint 7 (Inventory)** until paying customers explicitly ask.
- **Total effort if everything ships: ~83 days. Realistic with rework and customer feedback: 100–120 days.**
- **Sprint 1 is non-negotiable.** Until deposit PI works and state machine is in place, nothing else is safe to ship.
- **Cut SMS from Sprint 5** — email-only covers 80 % of value, saves 3 days and a Twilio-CZ-sender registration nightmare. Add SMS in v2 when demand is proven.
- **Build subscription-tier gating in Sprint 1**, not later — retrofitting tier checks across 8 sprints of endpoints is a nightmare we can avoid.

---

## Sprint 1 — Stripe hardening + foundation [MANDATORY]

**Effort: ~12 days**
**Goal:** Make production-grade. Until this lands, every live booking sits in `pending_payment` forever (audit finding #1) and out-of-order webhooks can corrupt state (audit finding #2).

| Deliverable | Days |
|---|---|
| Deposit PaymentIntent flow: `POST /api/bookings/<id>/create-payment-intent` + frontend Stripe Elements + idempotency_key + webhook reconciliation | 3 |
| Decide escrow pattern (separated charges + delayed transfer) vs. keep destination charges — **write ADR, then implement chosen path** | 2 |
| `BookingState` enum + `transition_booking(bid, to_state)` helper with allowed-transitions table + audit log | 2 |
| `studio_id` denormalization on bookings/slots + backfill from `studio_members` | 0.5 |
| `studio.subscription_tier` column + middleware: `require_tier(tier)` decorator wired on every B2B endpoint (defaults: free / studio / studio_pro) | 1 |
| Idempotency keys on remaining 4 mutating call sites (Connect Account.create, AccountLink × 2, balance PI) | 0.5 |
| `stripe.api_version = '2024-XX-XX'` pin (use latest stable at sprint start) | 0.5 |
| Dispute auto-freeze: `account.modify(payouts={'schedule': {'interval': 'manual'}})` + admin email/push + handle `charge.dispute.closed` | 1 |
| Card country detection on `payment_intent.succeeded` (`payment_method.card.country`) + plumb to economics snapshot | 0.5 |
| Application fee refund on full refunds (`reverse_transfer=True` when destination-charge model retained) | 0.5 |
| Tests: 11 cases from prior pricing spec + 5 new (state machine, idempotency replay, dispute freeze, deposit PI happy path, card-country fee variance) | 2 |
| Docs: `docs/specs/stripe.md` + state-machine diagram | 0.5 |

**Hard dependency:** Nothing — this is the foundation.

**Blocks:** Sprint 2 (booking model needs state machine), Sprint 4 (accounting needs reliable payment state).

---

## Sprint 2 — Booking system + calendar [HIGH PRIORITY]

**Effort: ~14 days**
**Goal:** The most-asked-for B2B feature. Currently artists juggle slots manually; this turns InkLink into a real calendar tool they can replace Google Calendar with.

| Deliverable | Days |
|---|---|
| `artist_availability` table (recurring windows: Mon–Sun, time ranges, valid_from/to) | 1 |
| `artist_blocked_time` table (vacations, sick days, one-off) | 0.5 |
| `BookingSlot` derivation: availability − bookings − buffers (compute on demand, cache short-lived) | 2 |
| Booking model additions: `session_number`, `parent_booking_id`, `buffer_before_minutes`, `buffer_after_minutes`, `internal_note`, `client_note` | 0.5 |
| Slot collision: first-commit wins, second gets 409 with `next_available_at` hint | 0.5 |
| Reschedule policy + flow: free if > 48 h before, else requires artist approval | 1 |
| Multi-session series: parent + N children, deposit on first, individual or full-prepaid configurable | 1.5 |
| Cancellation policy config per artist (free until X days; configurable refund tier) | 0.5 |
| Czech holidays auto-block (use `holidays` lib, refresh annually) | 1 |
| DST verification (UTC internal, display in `studios.timezone` or fall back to `Europe/Prague`) | 0.5 |
| API endpoints (`GET availability`, `POST bookings`, `PATCH reschedule`, `PATCH cancel`, `POST follow-up`) | 1.5 |
| Frontend in `artist-setup` (availability editor) + `my-bookings` (reschedule UI + series view) | 2 |
| Tests (slot collision, buffer respected, multi-session math, DST, holidays, reschedule policy) | 2 |
| Docs | 0.5 |

**SKIP for Sprint 2:** Room/station scheduling. Spec calls this tier-3-only; most CZ studios don't even track rooms. Build only if a customer specifically asks. Saves 2 days.

**Hard dependency:** Sprint 1 (state machine).

**Blocks:** Sprint 3 (CRM client history queries), Sprint 4 (invoice generation needs reliable completed status), Sprint 6 (analytics aggregates), Sprint 8 (marketing campaigns referencing bookings).

---

## Sprint 3 — Client database (CRM-lite) [HIGH PRIORITY]

**Effort: ~13 days**
**Goal:** Turn one-off `users` rows into a studio-scoped client database with notes, medical info, tattoo history. Single biggest B2B "wow" feature.

| Deliverable | Days |
|---|---|
| `clients` table (studio_id, user_id, tags, style_preferences, acquisition_source, lifetime_value_czk cached) — or extend `users` with studio-scoped view via `studio_clients` linking table | 1 |
| `client_notes` table (timestamped, studio-scoped, soft delete) | 1 |
| `tattoo_records` table (booking_id, date, artist, body location, healed_photo_url, aftercare_followup_status) | 1 |
| Medical notes encryption — Fernet with key from env, `medical_notes_encrypted` column, decrypt on read with audit log | 1 |
| Studio-scoped queries: `GET /api/studios/{id}/clients?search=&sort=` with `studio_id` enforcement | 1 |
| Client detail: `GET /api/clients/{id}/history` (bookings + tattoos + notes, all studio-scoped) | 1 |
| Per-client GDPR export (studio-issued, not user-issued) → ZIP scoped to one client | 0.5 |
| Per-client GDPR delete (null PII but preserve bookings — financial record) | 1 |
| Merge duplicate clients (artist accidentally created two for same person) | 1 |
| Frontend: client list (search, sort, tag filter), client detail (history + notes editor), tattoo record form | 3 |
| Tests (multi-tenancy enforcement, encryption round-trip, GDPR per-client export schema, merge semantics) | 1.5 |
| Docs | 0.5 |

**Hard dependency:** Sprint 1 (`studio_id` on bookings) + Sprint 2 (`tattoo_records.booking_id` FK).

**Blocks:** Sprint 6 (per-studio analytics), Sprint 8 (marketing campaigns targeting client segments).

---

## Sprint 4 — Accounting export (Czech-specific) [REVENUE UNLOCK]

**Effort: ~13 days**
**Goal:** Opens "Studio Pro" tier (3 000 Kč/mo) — accountants are who actually sign off on B2B subscriptions for studios.

| Deliverable | Days |
|---|---|
| `studio.tax_setup`: `legal_form`, `ico`, `dic`, `is_vat_payer`, `vat_rate_percent` | 0.5 |
| `invoices` table (sequential per-year numbering `YYYY-NNNN`, no gaps) | 1 |
| Invoice generation (one-per-booking for sólo, monthly grouped for studios) | 1.5 |
| **ISDOC XML** generator + schema validation (universal CZ accounting standard) | 2 |
| **Pohoda CSV** generator (most popular CZ accounting software) — research format first | 1 |
| **Money S3 CSV** generator | 1 |
| PDF invoice template (czech-locale formatting, optional for clients) | 1 |
| DPH (VAT) calc — 21 % on services, only for plátci DPH | 0.5 |
| API endpoints (`/api/studios/{id}/accounting/export?from&to&format`, `/api/invoices/{id}/pdf`) | 0.5 |
| Frontend (export page: filter, format picker, download; invoice preview) | 1.5 |
| Tests (VAT and non-VAT, ISDOC schema validation, sequence integrity, year-rollover) | 2 |
| Docs | 0.5 |

**OPINIONATED CALL:** This sprint has ~3 days of research overhead (Pohoda format, Money S3 format, ISDOC schema). **Consider hiring a Czech accountant consultant for half a day** — saves 1–2 days of guessing and we get correct from day 1. Worth 5–10 k CZK consultation fee.

**Hard dependency:** Sprint 1 (reliable booking completed_at + payment data) + Sprint 2 (multi-session bookings need to invoice correctly — series vs. per-session).

---

## Sprint 5 — Client communication (email + SMS) [DEFER SMS]

**Effort: ~7 days (email only) / 10 days (with SMS)**
**Goal:** Healing check-ins + automated reminders. Massive value at low engineering cost.

| Deliverable (EMAIL-ONLY scope) | Days |
|---|---|
| `communication_templates` + `communication_log` + `client_communication_preferences` | 1 |
| Template library (8 CZ email templates: booking confirm, 24h reminder, post-tattoo aftercare, day 3/7/14 healing, 6m/12m rebook) | 1 |
| Opt-in/out flow + GDPR-compliant audit | 0.5 |
| Scheduling logic + cron (daily check for due-to-send) | 1 |
| Bulk send rate limiting | 0.5 |
| Frontend (template editor with preview, send log, preference page for clients) | 2 |
| Tests | 1 |
| Docs | 0.5 |

**SMS (Twilio) — defer to v2** unless paying customer asks. Adds: Twilio CZ sender registration (~1 week of admin), per-SMS cost (~1.50 Kč), separate consent flow. **Save 3 days, ship v1 email-only.**

**Hard dependency:** Sprint 3 (client_communication_preferences linked to clients).

**Blocks:** Sprint 8 (campaign delivery channel).

---

## Sprint 6 — Analytics dashboard [POLISH]

**Effort: ~10 days**
**Goal:** Show studio admins the numbers. Lock-in feature — once they're using your dashboards, switching costs go up.

| Deliverable | Days |
|---|---|
| Aggregation views (revenue, bookings, repeat rate, no-shows) — refresh nightly via cron | 1 |
| Per-studio dashboard endpoints (6 canned metrics) | 2 |
| Per-artist dashboard (revenue split, style breakdown, repeat %) | 1 |
| Heatmap (peak hours / slow days) | 0.5 |
| Funnel (viewed → consult → booked → completed → no-show) | 0.5 |
| Frontend (6–8 canned dashboards using Chart.js or similar — no full BI) | 3 |
| Tests (mock booking data → expected aggregates, edge: 0 bookings empty state) | 1 |
| Docs | 0.5 |

**Hard dependency:** Sprint 2 (booking data) + Sprint 3 (client repeat rate calc).

---

## Sprint 7 — Inventory management [SKIP]

**Recommendation: DON'T BUILD until paying customer asks.**

Spec already flags this as lowest-priority. Czech studios manage inventory in Excel; they will not pay extra for a feature that adds friction to their existing workflow. Building it before there's demand is over-engineering.

**Effort if built later: ~8 days.** Defer this entire sprint. Save 8 days.

If a customer ever asks: build minimum (`inventory_items` + `inventory_usage` + low-stock email), not the full version with suppliers and supplier integration.

---

## Sprint 8 — Marketing automation [QUICK WIN]

**Effort: ~7.5 days**
**Goal:** Convert one-time clients into repeat customers. Highest ROI per dev day in the roadmap because many primitives already exist (welcome email sequence, referrals).

| Deliverable | Days |
|---|---|
| Campaign engine (cron-driven, reads templates, respects opt-in + frequency cap) | 2 |
| Campaigns out-of-box: 6m / 12m / 18m rebook reminders, birthday discount (10 % off), post-tattoo review request | 1.5 |
| Frequency cap (max 2 marketing comms/month/client) | 0.5 |
| A/B test infra: template variants + outcome tracking | 1.5 |
| Campaign management UI (pause/edit, see stats) | 2 |
| Tests | 1 |
| Docs | 0.5 |

**Already exists** (no work): welcome email sequence (shipped today), referral program (shipped today). Sprint 8 builds on these.

**Hard dependency:** Sprint 3 (clients) + Sprint 5 (delivery channel).

---

## Cross-cutting (every sprint)

Per spec:

1. **GDPR** — every feature touching client data has export + delete handlers and audit logs
2. **Multi-tenancy** — every query scoped to `studio_id`; aggressive tests for cross-studio leakage
3. **Soft deletes** — `deleted_at` on every entity with financial implications
4. **Audit trail** — `changed_by`, `changed_at` on financial records (consider a single `audit_log` table fed by triggers or a wrapper)
5. **Czech locale** — `DD.MM.YYYY`, `1 234,56 Kč`, decimal comma, thousands space
6. **i18n split** — Czech for user-facing errors, English for logs/dev errors
7. **Tier gating** — `studio.subscription_tier` check on every B2B endpoint, established in Sprint 1

These are NOT separate sprints — they're requirements within every sprint. Doing them retroactively is 3× the cost.

---

## Dependency graph

```
Sprint 1 (Stripe + state machine + studio_id + tier gating)
  ├─► Sprint 2 (Booking + calendar)
  │     ├─► Sprint 3 (CRM)
  │     │     ├─► Sprint 6 (Analytics — needs client data)
  │     │     └─► Sprint 8 (Marketing — needs clients + delivery)
  │     │
  │     └─► Sprint 4 (Accounting — needs reliable completion)
  │
  └─► Sprint 5 (Email comm — independent but lighter w/ CRM)
        └─► Sprint 8 (delivery channel)

Sprint 7 (Inventory) — skip until demand
```

---

## Suggested go-live milestones

| Milestone | After sprint(s) | Outcome |
|---|---|---|
| **MVP-prod** | 1 | InkLink can actually charge cards without losing bookings. State machine catches bugs early. Tier infra ready for future paid features. |
| **Studio tier soft-launch** | 1 + 2 | Sólo artists can sell. Studios use real calendars. Pricing: free (sólo) + 1 500 Kč/mo (studio) covered minimally. |
| **Studio Pro tier launch** | 1 + 2 + 3 + 4 | Full accounting unlock + CRM. Studios actually pay 3 000 Kč/mo. Revenue moment. |
| **Network effects layer** | 1 + 2 + 3 + 4 + 5 + 6 + 8 | Marketing automation + analytics + comms. Lock-in via switching cost. |

---

## Risk register

| Risk | Impact | Mitigation |
|---|---|---|
| Sprint 1 escrow pattern change is bigger than estimated (3 days could become 5) | Slips Sprint 2 start by a week | Write ADR first, keep destination-charges as fallback. Decide based on the volume — at low volume destination charges with manual reversals is acceptable. |
| Czech accounting format research stalls Sprint 4 | Misses revenue moment | Half-day accountant consultation early; budget 5–10 k CZK |
| Subscription tier infra in Sprint 1 adds 1 day of friction to every other sprint | +6–8 days total | Worth it — retrofit cost would be 3× |
| Twilio Czech sender registration takes longer than expected | SMS slips | Already deferred to v2; not blocking |
| Multi-tenancy data leak in Sprint 3 | Catastrophic for B2B trust | Dedicated cross-studio test suite. CI must run it on every PR. |

---

## Next step (per spec)

**Wait for user to approve roadmap and tell which sprint to start.**

If approved as written: start Sprint 1, write `/docs/specs/sprint_1_stripe.md` first, propose data model + API surface in chat, wait for approval before implementing.
