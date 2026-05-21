"""Discount validation engine.

Validates a proposed discount BEFORE the booking is created in Stripe.
The actual amount is then passed to calculate_booking_economics() which
distributes it (always from InkLink's commission, never from artist).

Error codes (string constants) are stable — surface them in admin alerts
and frontend error toasts.
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional, Literal

from .config import (
    DISCOUNT_MAX_PCT_OF_COMMISSION,
    WELCOME_DISCOUNT_CZK,
    REFERRAL_BONUS_CZK,
)
from .economics import (
    BookingInput, calculate_booking_economics, _D,
)

# Error codes — keep these as string constants so admin/dashboard can
# filter/aggregate. NEVER change their values; add new codes if needed.
DISCOUNT_TOO_LARGE       = 'DISCOUNT_TOO_LARGE'
DISCOUNT_NOT_ELIGIBLE    = 'DISCOUNT_NOT_ELIGIBLE'
DISCOUNT_ALREADY_USED    = 'DISCOUNT_ALREADY_USED'
DISCOUNT_STACKING        = 'DISCOUNT_STACKING'
NEGATIVE_PAYOUT          = 'NEGATIVE_PAYOUT'
NEGATIVE_NET             = 'NEGATIVE_NET'


DiscountType = Literal['WELCOME', 'REFERRAL', 'MANUAL_PROMO']


@dataclass(frozen=True)
class DiscountInput:
    """Inputs needed to validate one discount application.

    booking_input: the same BookingInput we'll pass to calculate_booking_economics,
    but with discount_amount_czk=0 — validation will set it.
    """
    discount_type:        DiscountType
    discount_amount_czk:  Decimal
    booking_input:        BookingInput
    # Client-state we need to verify eligibility against:
    client_is_new:        bool = False   # for WELCOME
    client_has_used_code: bool = False   # for any per-code uniqueness
    existing_discount_on_booking: Optional[str] = None  # for stacking check


@dataclass(frozen=True)
class ValidationResult:
    valid:        bool
    error_code:   Optional[str] = None
    message:      str = ''
    # Returned for accepted discounts so caller knows the final amount that
    # actually fits the cap (we don't auto-truncate; we reject).
    final_amount_czk: Decimal = Decimal('0')


def validate_discount(d: DiscountInput) -> ValidationResult:
    """Validates a discount against business rules.

    Returns ValidationResult with valid=False + error_code on rejection.
    On acceptance, final_amount_czk is the discount amount that will be
    applied (always == requested amount, no auto-truncation — fail loud).
    """
    amount = _D(d.discount_amount_czk)
    if amount <= 0:
        return ValidationResult(valid=False, error_code=DISCOUNT_NOT_ELIGIBLE,
                                message='Discount amount must be positive')

    # No stacking.
    if d.existing_discount_on_booking:
        return ValidationResult(
            valid=False, error_code=DISCOUNT_STACKING,
            message=f'Booking already has discount {d.existing_discount_on_booking}'
        )

    # Per-code uniqueness (WELCOME = once per client; REFERRAL = once per
    # referred party). Caller is responsible for checking the DB and
    # passing client_has_used_code=True if so.
    if d.client_has_used_code:
        return ValidationResult(
            valid=False, error_code=DISCOUNT_ALREADY_USED,
            message='Client has already used this discount code'
        )

    # Per-type eligibility.
    if d.discount_type == 'WELCOME':
        if not d.client_is_new:
            return ValidationResult(
                valid=False, error_code=DISCOUNT_NOT_ELIGIBLE,
                message='WELCOME is for new clients only'
            )
        # WELCOME amount is fixed by config; if caller passed a different
        # amount, reject — admin shouldn't override.
        if amount != WELCOME_DISCOUNT_CZK:
            return ValidationResult(
                valid=False, error_code=DISCOUNT_NOT_ELIGIBLE,
                message=f'WELCOME discount must be exactly {WELCOME_DISCOUNT_CZK} CZK'
            )

    if d.discount_type == 'REFERRAL':
        if amount != REFERRAL_BONUS_CZK:
            return ValidationResult(
                valid=False, error_code=DISCOUNT_NOT_ELIGIBLE,
                message=f'REFERRAL bonus must be exactly {REFERRAL_BONUS_CZK} CZK'
            )

    # MANUAL_PROMO: amount is variable, admin-issued. No specific amount check.

    # Cap: discount ≤ 60 % of commission. Compute commission via the engine
    # so we use the EXACT same logic that will run at payment time.
    test_input = BookingInput(
        gross_price_czk            = d.booking_input.gross_price_czk,
        artist_founding_started_at = d.booking_input.artist_founding_started_at,
        client_founding            = d.booking_input.client_founding,
        discount_amount_czk        = Decimal('0'),
        discount_source            = '',
        stripe_card_type           = d.booking_input.stripe_card_type,
        now                        = d.booking_input.now,
    )
    test_econ = calculate_booking_economics(test_input)
    commission = test_econ.artist_commission
    max_allowed = commission * DISCOUNT_MAX_PCT_OF_COMMISSION

    if amount > max_allowed:
        return ValidationResult(
            valid=False, error_code=DISCOUNT_TOO_LARGE,
            message=(
                f'Discount {amount} CZK exceeds 60 % of commission '
                f'({commission} CZK → max {max_allowed} CZK)'
            ),
        )

    # Final invariant check: simulate the booking WITH the discount and
    # confirm net is non-negative + payout is positive.
    with_discount = BookingInput(
        gross_price_czk            = d.booking_input.gross_price_czk,
        artist_founding_started_at = d.booking_input.artist_founding_started_at,
        client_founding            = d.booking_input.client_founding,
        discount_amount_czk        = amount,
        discount_source            = d.discount_type,
        stripe_card_type           = d.booking_input.stripe_card_type,
        now                        = d.booking_input.now,
    )
    final_econ = calculate_booking_economics(with_discount)
    if final_econ.artist_payout <= 0:
        return ValidationResult(
            valid=False, error_code=NEGATIVE_PAYOUT,
            message=f'Resulting artist_payout would be {final_econ.artist_payout} CZK'
        )
    # Net-negative is allowed ONLY during free founding artist window
    # (we explicitly accept losing money there to bootstrap supply).
    if final_econ.inklink_net < 0 and final_econ.founding_artist_status != 'free_window':
        return ValidationResult(
            valid=False, error_code=NEGATIVE_NET,
            message=f'Resulting inklink_net would be {final_econ.inklink_net} CZK'
        )

    return ValidationResult(valid=True, final_amount_czk=amount)
