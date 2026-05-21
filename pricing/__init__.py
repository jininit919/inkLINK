"""InkLink pricing module.

Pure functions for fee/commission/discount calculation, telemetry, and
admin KPI queries. Separated from server.py to keep business rules
isolated, testable, and reviewable.

Public API:
- pricing.calculate_booking_economics(input) -> Economics
- pricing.validate_discount(input) -> ValidationResult
- pricing.emit_event(name, payload, conn=None)
- pricing.admin_kpis(conn, start, end, city=None, tier=None) -> dict

Constants live in pricing.config. Do NOT import constants directly into
server.py — go through the config module so we have one place to change
rates.
"""

from .config import (
    COMMISSION_TIERS,
    MIN_COMMISSION_CZK,
    SERVICE_FEE_RATE,
    SERVICE_FEE_CAP_CZK,
    FOUNDING_ARTIST_FREE_DAYS,
    FOUNDING_ARTIST_FLAT_DAYS,
    FOUNDING_ARTIST_FLAT_RATE,
    FOUNDING_CLIENT_MAX,
    STRIPE_FEES,
    DISCOUNT_MAX_PCT_OF_COMMISSION,
    WELCOME_DISCOUNT_CZK,
    REFERRAL_BONUS_CZK,
)
from .economics import (
    BookingInput,
    Economics,
    calculate_booking_economics,
    tier_for_price,
    service_fee_for,
    commission_for_artist,
    stripe_fee_for,
    founding_artist_status_at,
)
from .discounts import (
    DiscountInput,
    ValidationResult,
    validate_discount,
    DISCOUNT_TOO_LARGE,
    DISCOUNT_NOT_ELIGIBLE,
    DISCOUNT_ALREADY_USED,
    DISCOUNT_STACKING,
    NEGATIVE_PAYOUT,
    NEGATIVE_NET,
)
from .telemetry import emit_event

__all__ = [
    'COMMISSION_TIERS', 'MIN_COMMISSION_CZK',
    'SERVICE_FEE_RATE', 'SERVICE_FEE_CAP_CZK',
    'FOUNDING_ARTIST_FREE_DAYS', 'FOUNDING_ARTIST_FLAT_DAYS',
    'FOUNDING_ARTIST_FLAT_RATE', 'FOUNDING_CLIENT_MAX',
    'STRIPE_FEES', 'DISCOUNT_MAX_PCT_OF_COMMISSION',
    'WELCOME_DISCOUNT_CZK', 'REFERRAL_BONUS_CZK',
    'BookingInput', 'Economics',
    'calculate_booking_economics',
    'tier_for_price', 'service_fee_for', 'commission_for_artist',
    'stripe_fee_for', 'founding_artist_status_at',
    'DiscountInput', 'ValidationResult', 'validate_discount',
    'DISCOUNT_TOO_LARGE', 'DISCOUNT_NOT_ELIGIBLE', 'DISCOUNT_ALREADY_USED',
    'DISCOUNT_STACKING', 'NEGATIVE_PAYOUT', 'NEGATIVE_NET',
    'emit_event',
]
