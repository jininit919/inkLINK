"""Pricing configuration — single source of truth for all rates.

Do NOT hardcode any of these values elsewhere in the codebase. If a rate
needs to change, change it here. All amounts in CZK (Czech crowns) unless
the variable name explicitly says haler/cents.

Stripe API expects amounts in haler (smallest currency unit). 1 CZK = 100
haler. Conversion happens at the Stripe API boundary only.
"""
from decimal import Decimal

# ───────────────────────────────────────────────────────────────────────
# Artist commission — tiered by gross tattoo price
# ───────────────────────────────────────────────────────────────────────
# Tier boundaries are INCLUSIVE on the lower bound (price >= boundary
# moves to that tier). 2 999 CZK → 12% tier; 3 000 CZK → 8% tier; etc.
COMMISSION_TIERS = (
    (Decimal('0'),     Decimal('0.12')),   # < 3 000 → 12%
    (Decimal('3000'),  Decimal('0.08')),   # 3 000 ≤ x < 10 000 → 8%
    (Decimal('10000'), Decimal('0.05')),   # ≥ 10 000 → 5%
)

# Floor: even if the tiered % gives less, we always take at least this much.
# Why: Stripe fee + ops cost on small bookings would otherwise leave us
# net-negative. Founding artists in days 1–30 are exempt (their entire
# commission is waived as a launch incentive — we accept the loss).
MIN_COMMISSION_CZK = Decimal('200')


# ───────────────────────────────────────────────────────────────────────
# Client service fee — added on top of artist's price
# ───────────────────────────────────────────────────────────────────────
SERVICE_FEE_RATE   = Decimal('0.03')   # 3 % of gross price
SERVICE_FEE_CAP_CZK = Decimal('150')   # absolute max — bookings > 5 000 CZK hit cap


# ───────────────────────────────────────────────────────────────────────
# Founding artist program
# ───────────────────────────────────────────────────────────────────────
# Day 1 = first day after their first completed booking (when "clock starts").
# Days 1–30: zero commission (we eat the floor, intentionally).
# Days 31–90: flat 5 % regardless of price tier, no floor.
# Day 91+: standard tiered logic applies.
FOUNDING_ARTIST_FREE_DAYS  = 30
FOUNDING_ARTIST_FLAT_DAYS  = 90   # 31..90 inclusive
FOUNDING_ARTIST_FLAT_RATE  = Decimal('0.05')


# ───────────────────────────────────────────────────────────────────────
# Founding client program
# ───────────────────────────────────────────────────────────────────────
# First N client signups get service fee waived forever.
# After this cap, no more founding clients are created — the flag is set
# at signup time by checking COUNT(*) of existing founding_clients.
FOUNDING_CLIENT_MAX = 500


# ───────────────────────────────────────────────────────────────────────
# Stripe fees (operational cost, NOT revenue)
# ───────────────────────────────────────────────────────────────────────
# These values are estimates verified against the Stripe dashboard. Update
# if Stripe changes their rate card or we negotiate a custom rate.
#
# Pre-payment: use card_eea as projection. Post-payment: re-snapshot using
# the actual charges.data[0].payment_method_details.card.country from the
# webhook (CZ/EEA codes = card_eea, anything else = card_non_eea).
STRIPE_FEES = {
    'card_eea': {
        'percentage':  Decimal('0.015'),  # 1.5 %
        'fixed_haler': 600,                # 6 CZK = 600 haler
    },
    'card_non_eea': {
        'percentage':  Decimal('0.034'),  # 3.4 %
        'fixed_haler': 600,
    },
    'currency_conv': Decimal('0.01'),  # +1 % if card is non-CZK
    'connect_fee':   Decimal('0'),     # 0 for Standard accounts; Express has no per-transfer fee
}


# ───────────────────────────────────────────────────────────────────────
# Discount engine
# ───────────────────────────────────────────────────────────────────────
# Inviolable rule: discounts are NEVER taken from the artist. They come
# out of InkLink's commission only. Hard cap: 60 % of the commission for
# that specific transaction. This prevents promo abuse from going net-negative.
DISCOUNT_MAX_PCT_OF_COMMISSION = Decimal('0.60')

# Standard discount amounts
WELCOME_DISCOUNT_CZK  = Decimal('200')   # first-time client only
REFERRAL_BONUS_CZK    = Decimal('300')   # both referrer and referred get this


# ───────────────────────────────────────────────────────────────────────
# Booking lifecycle — cooldown between completion and artist payout
# ───────────────────────────────────────────────────────────────────────
# Hours to wait between booking moving to 'completed' state and the
# Stripe Transfer being created. Window absorbs chargeback risk during
# the first day. Set to 0 to disable.
# Note: with destination charges (current architecture), funds flow to
# artist immediately at payment, so this is informational only until we
# migrate to separated charges.
PAYOUT_COOLDOWN_HOURS = 24


# ───────────────────────────────────────────────────────────────────────
# Dispute / chargeback
# ───────────────────────────────────────────────────────────────────────
# Stripe charges this when we lose a dispute. Roughly $15 USD, varies by
# region. Booked as a separate cost line in the economics dashboard.
STRIPE_DISPUTE_FEE_CZK = Decimal('350')
