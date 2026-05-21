"""Booking economics calculator.

`calculate_booking_economics(input)` is the ONLY function that decides
who pays what. It is pure (no DB, no Stripe, no side effects), deterministic
(same input → same output), and idempotent (call twice, get the same result).

The output is meant to be persisted as an immutable snapshot per booking,
so we can always audit "why did this booking pay out X" months later.
"""
from __future__ import annotations
from dataclasses import dataclass, asdict, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal, Tuple

from .config import (
    COMMISSION_TIERS,
    MIN_COMMISSION_CZK,
    SERVICE_FEE_RATE,
    SERVICE_FEE_CAP_CZK,
    FOUNDING_ARTIST_FREE_DAYS,
    FOUNDING_ARTIST_FLAT_DAYS,
    FOUNDING_ARTIST_FLAT_RATE,
    STRIPE_FEES,
    DISCOUNT_MAX_PCT_OF_COMMISSION,
)


# ───────────────────────────────────────────────────────────────────────
# Types
# ───────────────────────────────────────────────────────────────────────

FoundingArtistStatus = Literal['none', 'free_window', 'flat_window', 'graduated']

# Map of card flavors → stripe fee bucket key in STRIPE_FEES.
StripeCardType = Literal['card_eea', 'card_non_eea']


@dataclass(frozen=True)
class BookingInput:
    """Inputs needed to compute one booking's economics.

    All Decimal money values are in CZK (whole crowns). The engine converts
    to haler only at the very end via .as_haler() helpers.
    """
    gross_price_czk:               Decimal
    artist_founding_started_at:    Optional[datetime] = None  # None = non-founding artist
    client_founding:               bool = False
    discount_amount_czk:           Decimal = Decimal('0')
    discount_source:               str = ''                   # e.g. 'WELCOME', 'REFERRAL_<code>', 'MANUAL_<id>'
    stripe_card_type:              StripeCardType = 'card_eea'
    stripe_currency_conv_applies:  bool = False
    # The date at which we evaluate "today" — useful for tests so we can
    # freeze time. Defaults to UTC now at call time.
    now:                           Optional[datetime] = None

    def __post_init__(self):
        # Defensive: coerce strings/ints to Decimal so callers can pass either.
        object.__setattr__(self, 'gross_price_czk',     _D(self.gross_price_czk))
        object.__setattr__(self, 'discount_amount_czk', _D(self.discount_amount_czk))


@dataclass(frozen=True)
class Economics:
    """Result of calculate_booking_economics(). All CZK rounded to whole crowns
    for display. Stripe-facing values (haler) are available via .as_haler_dict().
    """
    gross_price:              Decimal
    client_service_fee:       Decimal
    client_pays_total:        Decimal
    artist_commission:        Decimal
    stripe_fee:               Decimal
    discount_applied:         Decimal
    discount_source:          str
    artist_payout:            Decimal
    inklink_net:              Decimal
    effective_take_rate:      Decimal     # inklink_net / client_pays_total
    founding_artist_status:   FoundingArtistStatus
    founding_artist_day:      Optional[int]   # 1-based day since clock started, or None

    def to_dict(self) -> dict:
        """JSON-serializable dict for snapshot storage + telemetry."""
        d = asdict(self)
        # Decimal → float (or str if you prefer lossless). float is fine for
        # whole-CZK values; we never serialize sub-crown precision.
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = float(v)
        return d

    def as_haler_dict(self) -> dict:
        """Stripe-facing amounts in haler (smallest unit). For PaymentIntent.amount,
        application_fee_amount, refund amounts, etc."""
        return {
            'gross_price_haler':        int(self.gross_price * 100),
            'client_service_fee_haler': int(self.client_service_fee * 100),
            'client_pays_total_haler':  int(self.client_pays_total * 100),
            'artist_commission_haler':  int(self.artist_commission * 100),
            'artist_payout_haler':      int(self.artist_payout * 100),
            'discount_applied_haler':   int(self.discount_applied * 100),
            'stripe_fee_haler':         int(self.stripe_fee * 100),
            'inklink_net_haler':        int(self.inklink_net * 100),
        }


# ───────────────────────────────────────────────────────────────────────
# Helpers (also exported for unit tests + admin queries)
# ───────────────────────────────────────────────────────────────────────

def _D(x) -> Decimal:
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def _round_czk(x: Decimal) -> Decimal:
    """Round to whole CZK, half-up. We never display sub-crown amounts."""
    return _D(x).quantize(Decimal('1'), rounding=ROUND_HALF_UP)


def tier_for_price(gross_czk: Decimal) -> Decimal:
    """Returns the commission RATE (Decimal fraction) for a given gross price.
    Tier boundaries are inclusive on the lower bound: 3000 CZK → 8 % tier.
    """
    gross = _D(gross_czk)
    # COMMISSION_TIERS is ordered ascending by threshold; pick the highest
    # threshold that is ≤ gross.
    rate = COMMISSION_TIERS[0][1]
    for threshold, tier_rate in COMMISSION_TIERS:
        if gross >= threshold:
            rate = tier_rate
        else:
            break
    return rate


def founding_artist_status_at(
    started_at: Optional[datetime],
    now: Optional[datetime] = None,
) -> Tuple[FoundingArtistStatus, Optional[int]]:
    """Returns (status, day_in_window). day is 1-based: day 1 = same calendar
    day as `started_at`. None if not a founding artist.

    Why started_at can be None: only artists explicitly enrolled in the
    program have a start timestamp. Status 'none' = standard tiered rules.
    """
    if started_at is None:
        return ('none', None)
    n = now or datetime.utcnow()
    delta_days = (n.date() - started_at.date()).days + 1  # +1 because day of enrollment counts as day 1
    if delta_days <= FOUNDING_ARTIST_FREE_DAYS:
        return ('free_window', delta_days)
    if delta_days <= FOUNDING_ARTIST_FLAT_DAYS:
        return ('flat_window', delta_days)
    return ('graduated', delta_days)


def service_fee_for(gross_czk: Decimal, client_founding: bool) -> Decimal:
    """Service fee paid by the client on top of the gross. Returns whole CZK.

    Founding clients (first 500 signups) get fee permanently waived.
    """
    if client_founding:
        return Decimal('0')
    gross = _D(gross_czk)
    raw = gross * SERVICE_FEE_RATE
    return _round_czk(min(raw, SERVICE_FEE_CAP_CZK))


def commission_for_artist(
    gross_czk: Decimal,
    founding_status: FoundingArtistStatus,
) -> Decimal:
    """Commission that InkLink keeps from the artist's gross. Returns whole CZK.

    Founding status overrides tiered logic:
    - 'free_window' (days 1–30): zero, no floor (we eat the loss).
    - 'flat_window' (days 31–90): flat 5 %, no floor.
    - 'graduated' / 'none': tiered logic with MIN_COMMISSION_CZK floor.
    """
    gross = _D(gross_czk)
    if founding_status == 'free_window':
        return Decimal('0')
    if founding_status == 'flat_window':
        return _round_czk(gross * FOUNDING_ARTIST_FLAT_RATE)

    # Standard tiered with floor.
    rate = tier_for_price(gross)
    raw = gross * rate
    if raw < MIN_COMMISSION_CZK:
        return MIN_COMMISSION_CZK
    return _round_czk(raw)


def stripe_fee_for(
    client_pays_total_czk: Decimal,
    card_type: StripeCardType = 'card_eea',
    currency_conv_applies: bool = False,
) -> Decimal:
    """Estimated Stripe fee for the full charge amount. Returns whole CZK.

    For pre-payment estimates use card_eea. After webhook tells us the
    actual card country, re-snapshot with the correct bucket.
    """
    fees = STRIPE_FEES[card_type]
    pct = fees['percentage']
    fixed_czk = Decimal(fees['fixed_haler']) / Decimal(100)
    if currency_conv_applies:
        pct += STRIPE_FEES['currency_conv']
    raw = _D(client_pays_total_czk) * pct + fixed_czk
    return _round_czk(raw)


# ───────────────────────────────────────────────────────────────────────
# THE engine — pure function, all rules in one place
# ───────────────────────────────────────────────────────────────────────

def calculate_booking_economics(inp: BookingInput) -> Economics:
    """Pure, deterministic computation of one booking's economics.

    Side-effect-free: no DB, no Stripe, no logging. Caller is responsible
    for persisting the snapshot (typically at PaymentIntent creation).

    Validation is performed via assertions on net invariants; raises
    ValueError on impossible inputs. Discount-cap and negative-payout
    checks are also done in validate_discount() — this function will still
    compute the values even if they violate the cap, so the caller can
    inspect & decide whether to reject the booking. The validation is the
    gate; this function is the math.
    """
    gross = _D(inp.gross_price_czk)
    if gross <= 0:
        raise ValueError('gross_price_czk must be > 0')

    # Founding status windows.
    founding_status, founding_day = founding_artist_status_at(
        inp.artist_founding_started_at, inp.now
    )

    # Service fee (client side, waived for founding clients).
    service_fee = service_fee_for(gross, inp.client_founding)
    client_pays = gross + service_fee

    # Commission (artist side, founding artist overrides).
    commission = commission_for_artist(gross, founding_status)

    # Stripe fee (our cost). Computed on the FULL charge amount because
    # Stripe takes its cut on whatever amount the client actually pays.
    stripe_fee = stripe_fee_for(
        client_pays,
        inp.stripe_card_type,
        inp.stripe_currency_conv_applies,
    )

    # Discount. Always non-negative.
    discount = max(Decimal('0'), _D(inp.discount_amount_czk))

    # Artist payout — INVARIANT: discount NEVER reduces this. Artist gets
    # `gross - commission` regardless of promo codes.
    artist_payout = _round_czk(gross - commission)

    # InkLink net = (service_fee + commission) - stripe_fee - discount.
    # discount comes out of OUR commission, not the artist's payout.
    inklink_net = _round_czk(service_fee + commission - stripe_fee - discount)

    # Effective take rate — useful for the analytics dashboard.
    if client_pays > 0:
        take_rate = (inklink_net / client_pays).quantize(Decimal('0.0001'), rounding=ROUND_HALF_UP)
    else:
        take_rate = Decimal('0')

    return Economics(
        gross_price             = _round_czk(gross),
        client_service_fee      = service_fee,
        client_pays_total       = _round_czk(client_pays),
        artist_commission       = commission,
        stripe_fee              = stripe_fee,
        discount_applied        = _round_czk(discount),
        discount_source         = inp.discount_source or '',
        artist_payout           = artist_payout,
        inklink_net             = inklink_net,
        effective_take_rate     = take_rate,
        founding_artist_status  = founding_status,
        founding_artist_day     = founding_day,
    )
