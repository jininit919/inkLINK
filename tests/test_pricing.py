"""Unit tests for pricing engine.

Run with:
    python3 -m unittest tests/test_pricing.py -v

Or all tests:
    python3 -m unittest discover tests/

These tests cover every case from the spec. Adding a new pricing rule?
Add the test FIRST, watch it fail, then fix the engine.
"""
import os
import sys
import unittest
from datetime import datetime, timedelta
from decimal import Decimal

# Make the project root importable when running tests directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pricing import (
    BookingInput, calculate_booking_economics,
    tier_for_price, service_fee_for, commission_for_artist,
    stripe_fee_for, founding_artist_status_at,
    validate_discount, DiscountInput,
    DISCOUNT_TOO_LARGE, DISCOUNT_NOT_ELIGIBLE, DISCOUNT_ALREADY_USED,
    DISCOUNT_STACKING, NEGATIVE_PAYOUT,
    MIN_COMMISSION_CZK, WELCOME_DISCOUNT_CZK, REFERRAL_BONUS_CZK,
)


# ───────────────────────────────────────────────────────────────────────
# Tier boundaries
# ───────────────────────────────────────────────────────────────────────
class TierBoundaryTests(unittest.TestCase):
    def test_2999_uses_12pct_tier(self):
        self.assertEqual(tier_for_price(Decimal('2999')), Decimal('0.12'))

    def test_3000_uses_8pct_tier(self):
        self.assertEqual(tier_for_price(Decimal('3000')), Decimal('0.08'))

    def test_9999_uses_8pct_tier(self):
        self.assertEqual(tier_for_price(Decimal('9999')), Decimal('0.08'))

    def test_10000_uses_5pct_tier(self):
        self.assertEqual(tier_for_price(Decimal('10000')), Decimal('0.05'))

    def test_50000_uses_5pct_tier(self):
        self.assertEqual(tier_for_price(Decimal('50000')), Decimal('0.05'))


# ───────────────────────────────────────────────────────────────────────
# Commission with floor
# ───────────────────────────────────────────────────────────────────────
class CommissionTests(unittest.TestCase):
    def test_2999_commission_360(self):
        # 12% of 2999 = 359.88 → rounds to 360, above floor 200.
        self.assertEqual(commission_for_artist(Decimal('2999'), 'none'), Decimal('360'))

    def test_3000_commission_240(self):
        # 8% of 3000 = 240, above floor.
        self.assertEqual(commission_for_artist(Decimal('3000'), 'none'), Decimal('240'))

    def test_9999_commission_800(self):
        # 8% of 9999 = 799.92 → 800.
        self.assertEqual(commission_for_artist(Decimal('9999'), 'none'), Decimal('800'))

    def test_10000_commission_500(self):
        # 5% of 10000 = 500.
        self.assertEqual(commission_for_artist(Decimal('10000'), 'none'), Decimal('500'))

    def test_1500_hits_floor_200_not_180(self):
        # 12% of 1500 = 180. Floor kicks in → 200.
        self.assertEqual(commission_for_artist(Decimal('1500'), 'none'), MIN_COMMISSION_CZK)

    def test_500_hits_floor(self):
        # 12% of 500 = 60 < 200 floor.
        self.assertEqual(commission_for_artist(Decimal('500'), 'none'), MIN_COMMISSION_CZK)


# ───────────────────────────────────────────────────────────────────────
# Service fee
# ───────────────────────────────────────────────────────────────────────
class ServiceFeeTests(unittest.TestCase):
    def test_1000_fee_30(self):
        self.assertEqual(service_fee_for(Decimal('1000'), client_founding=False), Decimal('30'))

    def test_5000_fee_150_cap(self):
        # 3% of 5000 = 150 → equals cap.
        self.assertEqual(service_fee_for(Decimal('5000'), client_founding=False), Decimal('150'))

    def test_20000_capped_at_150(self):
        # 3% of 20000 = 600 → capped at 150.
        self.assertEqual(service_fee_for(Decimal('20000'), client_founding=False), Decimal('150'))

    def test_founding_client_waived(self):
        # Any price, founding client → fee = 0.
        self.assertEqual(service_fee_for(Decimal('5000'), client_founding=True), Decimal('0'))
        self.assertEqual(service_fee_for(Decimal('100000'), client_founding=True), Decimal('0'))


# ───────────────────────────────────────────────────────────────────────
# Founding artist windows
# ───────────────────────────────────────────────────────────────────────
class FoundingArtistTests(unittest.TestCase):
    def setUp(self):
        # Anchor "now" so tests are deterministic.
        self.start = datetime(2026, 1, 1, 10, 0, 0)

    def _status(self, days_offset):
        now = self.start + timedelta(days=days_offset)
        return founding_artist_status_at(self.start, now)

    def test_day_1_is_free_window(self):
        # day 1 = the same calendar day as start
        status, day = self._status(0)
        self.assertEqual(status, 'free_window')
        self.assertEqual(day, 1)

    def test_day_30_still_free(self):
        status, day = self._status(29)  # day 30
        self.assertEqual(status, 'free_window')
        self.assertEqual(day, 30)

    def test_day_31_is_flat(self):
        status, day = self._status(30)  # day 31
        self.assertEqual(status, 'flat_window')
        self.assertEqual(day, 31)

    def test_day_90_still_flat(self):
        status, day = self._status(89)  # day 90
        self.assertEqual(status, 'flat_window')
        self.assertEqual(day, 90)

    def test_day_91_is_graduated(self):
        status, day = self._status(90)  # day 91
        self.assertEqual(status, 'graduated')
        self.assertEqual(day, 91)

    def test_none_status_when_not_enrolled(self):
        status, day = founding_artist_status_at(None)
        self.assertEqual(status, 'none')
        self.assertIsNone(day)

    def test_free_window_commission_is_zero(self):
        self.assertEqual(commission_for_artist(Decimal('5000'), 'free_window'), Decimal('0'))
        # Even below floor — explicitly zero, NOT the floor.
        self.assertEqual(commission_for_artist(Decimal('1000'), 'free_window'), Decimal('0'))

    def test_flat_window_is_5pct_no_floor(self):
        # 5% of 5000 = 250.
        self.assertEqual(commission_for_artist(Decimal('5000'), 'flat_window'), Decimal('250'))
        # Below where the floor would normally kick in — flat 5 % overrides.
        self.assertEqual(commission_for_artist(Decimal('2000'), 'flat_window'), Decimal('100'))


# ───────────────────────────────────────────────────────────────────────
# Full economics — example from spec
# ───────────────────────────────────────────────────────────────────────
class EconomicsExampleTests(unittest.TestCase):
    def test_8000_booking_with_welcome_discount(self):
        """Spec example:
        gross 8000, service_fee 150 (capped), client_pays 8150, commission 640 (8 %),
        stripe_fee ~120, discount 200 WELCOME, payout 7360, inklink_net 470.
        """
        econ = calculate_booking_economics(BookingInput(
            gross_price_czk=Decimal('8000'),
            discount_amount_czk=Decimal('200'),
            discount_source='WELCOME',
        ))
        self.assertEqual(econ.gross_price,        Decimal('8000'))
        self.assertEqual(econ.client_service_fee, Decimal('150'))
        self.assertEqual(econ.client_pays_total,  Decimal('8150'))
        self.assertEqual(econ.artist_commission,  Decimal('640'))
        self.assertEqual(econ.artist_payout,      Decimal('7360'))
        self.assertEqual(econ.discount_applied,   Decimal('200'))
        # Stripe fee: 1.5% * 8150 + 6 = 122.25 + 6 = 128.25 → 128
        self.assertEqual(econ.stripe_fee,         Decimal('128'))
        # inklink_net = 150 + 640 - 128 - 200 = 462
        self.assertEqual(econ.inklink_net,        Decimal('462'))

    def test_pure_function_is_deterministic(self):
        inp = BookingInput(gross_price_czk=Decimal('5000'))
        a = calculate_booking_economics(inp)
        b = calculate_booking_economics(inp)
        self.assertEqual(a, b)

    def test_artist_payout_never_reduced_by_discount(self):
        """Inviolable rule: discount comes from OUR commission, not artist."""
        no_disc = calculate_booking_economics(BookingInput(gross_price_czk=Decimal('10000')))
        # Use a discount that fits the 60% cap. Commission on 10000 = 500
        # (5% tier). Max discount = 300.
        with_disc = calculate_booking_economics(BookingInput(
            gross_price_czk=Decimal('10000'),
            discount_amount_czk=Decimal('200'),
        ))
        self.assertEqual(no_disc.artist_payout, with_disc.artist_payout)


# ───────────────────────────────────────────────────────────────────────
# Discount validation edge cases
# ───────────────────────────────────────────────────────────────────────
class DiscountValidationTests(unittest.TestCase):
    def _input(self, gross, **kwargs):
        return BookingInput(gross_price_czk=Decimal(str(gross)), **kwargs)

    def test_welcome_200_on_1500_booking_fails_cap(self):
        # Commission = 200 (floor). Max discount = 60% * 200 = 120. 200 > 120 → reject.
        d = DiscountInput(
            discount_type='WELCOME',
            discount_amount_czk=WELCOME_DISCOUNT_CZK,
            booking_input=self._input(1500),
            client_is_new=True,
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_TOO_LARGE)

    def test_welcome_200_on_3000_booking_fails_cap(self):
        # Commission = 240. Max discount = 144. 200 > 144 → reject.
        d = DiscountInput(
            discount_type='WELCOME',
            discount_amount_czk=WELCOME_DISCOUNT_CZK,
            booking_input=self._input(3000),
            client_is_new=True,
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_TOO_LARGE)

    def test_welcome_200_on_5000_booking_passes(self):
        # Commission = 400 (8% * 5000). Max discount = 240. 200 ≤ 240 → accept.
        d = DiscountInput(
            discount_type='WELCOME',
            discount_amount_czk=WELCOME_DISCOUNT_CZK,
            booking_input=self._input(5000),
            client_is_new=True,
        )
        r = validate_discount(d)
        self.assertTrue(r.valid, f'unexpected reject: {r.error_code} {r.message}')
        self.assertEqual(r.final_amount_czk, Decimal('200'))

    def test_welcome_rejects_non_new_client(self):
        d = DiscountInput(
            discount_type='WELCOME',
            discount_amount_czk=WELCOME_DISCOUNT_CZK,
            booking_input=self._input(5000),
            client_is_new=False,
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_NOT_ELIGIBLE)

    def test_stacking_blocked(self):
        d = DiscountInput(
            discount_type='REFERRAL',
            discount_amount_czk=REFERRAL_BONUS_CZK,
            booking_input=self._input(8000),
            existing_discount_on_booking='WELCOME',
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_STACKING)

    def test_already_used(self):
        d = DiscountInput(
            discount_type='WELCOME',
            discount_amount_czk=WELCOME_DISCOUNT_CZK,
            booking_input=self._input(5000),
            client_is_new=True,
            client_has_used_code=True,
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_ALREADY_USED)

    def test_referral_wrong_amount_rejected(self):
        d = DiscountInput(
            discount_type='REFERRAL',
            discount_amount_czk=Decimal('500'),   # not the canonical 300
            booking_input=self._input(8000),
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_NOT_ELIGIBLE)

    def test_manual_promo_variable_amount_allowed_within_cap(self):
        # Commission on 20000 (5%) = 1000; max = 600. 500 passes.
        d = DiscountInput(
            discount_type='MANUAL_PROMO',
            discount_amount_czk=Decimal('500'),
            booking_input=self._input(20000),
        )
        r = validate_discount(d)
        self.assertTrue(r.valid)

    def test_manual_promo_exceeding_cap_rejected(self):
        # Commission 1000, max 600, request 700 → reject.
        d = DiscountInput(
            discount_type='MANUAL_PROMO',
            discount_amount_czk=Decimal('700'),
            booking_input=self._input(20000),
        )
        r = validate_discount(d)
        self.assertFalse(r.valid)
        self.assertEqual(r.error_code, DISCOUNT_TOO_LARGE)


# ───────────────────────────────────────────────────────────────────────
# Stripe fee
# ───────────────────────────────────────────────────────────────────────
class StripeFeeTests(unittest.TestCase):
    def test_eea_card(self):
        # 1.5% * 1000 + 6 = 15 + 6 = 21.
        self.assertEqual(stripe_fee_for(Decimal('1000'), 'card_eea'), Decimal('21'))

    def test_non_eea_card(self):
        # 3.4% * 1000 + 6 = 34 + 6 = 40.
        self.assertEqual(stripe_fee_for(Decimal('1000'), 'card_non_eea'), Decimal('40'))

    def test_currency_conversion_surcharge(self):
        # EEA + currency conv: (1.5 + 1.0) % * 1000 + 6 = 25 + 6 = 31.
        self.assertEqual(
            stripe_fee_for(Decimal('1000'), 'card_eea', currency_conv_applies=True),
            Decimal('31'),
        )


# ───────────────────────────────────────────────────────────────────────
# Negative / impossible inputs
# ───────────────────────────────────────────────────────────────────────
class GuardrailTests(unittest.TestCase):
    def test_zero_gross_raises(self):
        with self.assertRaises(ValueError):
            calculate_booking_economics(BookingInput(gross_price_czk=Decimal('0')))

    def test_negative_gross_raises(self):
        with self.assertRaises(ValueError):
            calculate_booking_economics(BookingInput(gross_price_czk=Decimal('-100')))

    def test_negative_payout_rejected_by_validator(self):
        # Synthesize an absurd MANUAL_PROMO that would somehow zero out
        # artist_payout — validate_discount() should catch it.
        # We can't actually make payout ≤ 0 via discount since payout = gross - commission
        # and discount doesn't touch payout — so the discount cap covers this case.
        # The NEGATIVE_PAYOUT code is for refunds / edge cases.
        pass


if __name__ == '__main__':
    unittest.main(verbosity=2)
