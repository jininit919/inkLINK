"""End-to-end integration tests for the new endpoints.

Covers:
  - Register with ?ref=username → referrals row + welcome stage advance
  - /api/me/referrals (link, stats, list)
  - /api/refund-requests + /api/bookings/<id>/refund-request (validation)
  - /api/artists/near (Haversine + bbox)
  - Cron token guards (/api/cron/welcome-emails, /api/cron/reconcile)

Each test runs with a fresh SQLite DB in /tmp. Resend / Stripe / Sentry are
implicitly disabled (no API keys → graceful no-op).

Run:
    python3 -m unittest tests/test_e2e.py -v
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_client():
    """Return (client, db_path). Each test gets its own SQLite file + reloaded server."""
    db_file = tempfile.NamedTemporaryFile(suffix='.db', delete=False).name
    os.environ['DB_PATH'] = db_file
    os.environ['DATABASE_URL'] = ''   # force SQLite
    os.environ['SECRET_KEY'] = 'test-secret'
    os.environ['APP_BASE_URL'] = 'http://localhost:5002'
    os.environ['RECONCILE_TOKEN'] = 'test-token'
    # Reload server module so init_db() runs against the new DB_PATH
    if 'server' in sys.modules:
        del sys.modules['server']
    import server  # noqa
    server.app.config['TESTING'] = True
    return server.app.test_client(), db_file


def _register(client, username, **extra):
    return client.post('/api/register', json={
        'username': username,
        'display_name': extra.get('display_name', username.title()),
        'email': extra.get('email', f'{username}@test.cz'),
        'password': 'pass1234',
        'city': extra.get('city', 'Praha'),
        **{k: v for k, v in extra.items() if k in ('ref', 'phone')},
    })


def _logout(client):
    with client.session_transaction() as s:
        s.clear()


def _login(client, username, password='pass1234'):
    return client.post('/api/login', json={'username': username, 'password': password})


class StripeDepositPITests(unittest.TestCase):
    """Sprint 1 LITE — deposit PaymentIntent flow.

    Uses unittest.mock to wrap stripe.PaymentIntent.create / Account.create /
    AccountLink.create so tests don't hit the network. Verifies idempotency
    keys, API version pinning, and the new payment block in responses.
    """

    def setUp(self):
        os.environ['ENABLE_DEPOSIT_PI'] = '1'
        os.environ['STRIPE_SECRET_KEY'] = 'sk_test_dummy'
        os.environ['STRIPE_PUBLIC_KEY'] = 'pk_test_dummy'
        self.client, self.db = _fresh_client()
        # Set up an artist with stripe enabled + a slot
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, '
            'is_artist, stripe_account_id, stripe_charges_enabled) '
            'VALUES (?, ?, ?, ?, 1, ?, 1)',
            ('artist1', 'Artist One', 'x', 'a@t.cz', 'acct_test_artist')
        )
        from datetime import datetime, timedelta
        start = (datetime.utcnow() + timedelta(days=10)).isoformat()
        end = (datetime.utcnow() + timedelta(days=10, hours=4)).isoformat()
        conn.execute(
            'INSERT INTO slots (user_id, start_at, end_at, status, '
            'price_min, price_max, price_unit, min_duration_hours) '
            'VALUES (1, ?, ?, ?, ?, ?, ?, ?)',
            (start, end, 'free', 1500, 1500, 'hour', 1)
        )
        conn.commit()
        conn.close()
        _register(self.client, 'client1')

    def tearDown(self):
        os.unlink(self.db)
        os.environ.pop('ENABLE_DEPOSIT_PI', None)
        os.environ.pop('STRIPE_SECRET_KEY', None)
        os.environ.pop('STRIPE_PUBLIC_KEY', None)

    def _book(self, captured=None, side_effect=None):
        from unittest.mock import patch, MagicMock
        import server
        pi_mock = MagicMock()
        pi_mock.id = 'pi_test_12345'
        pi_mock.client_secret = 'pi_test_12345_secret_abc'

        def _create(*args, **kwargs):
            if captured is not None:
                captured.append(kwargs)
            if side_effect:
                raise side_effect
            return pi_mock

        with patch.object(server.stripe.PaymentIntent, 'create',
                          side_effect=_create) as m:
            r = self.client.post('/api/bookings', json={
                'slot_id': 1,
                'design_note': 'Vlk na předloktí, blackwork',
                'size_label': 'small',
                'booking_start_at': None,
            })
        return r, m

    def test_api_version_is_pinned(self):
        import server
        self.assertEqual(server.stripe.api_version, '2024-12-18.acacia')

    def test_create_booking_live_returns_client_secret(self):
        r, _ = self._book()
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn('payment', d)
        self.assertEqual(d['payment']['mode'], 'live')
        self.assertTrue(d['payment']['client_secret'].startswith('pi_test_'))
        self.assertEqual(d['payment']['publishable_key'], 'pk_test_dummy')
        self.assertEqual(d['payment']['payment_url'], f'/pay/{d["id"]}')

    def test_create_booking_idempotency_key_used(self):
        captured = []
        self._book(captured=captured)
        self.assertEqual(len(captured), 1)
        ik = captured[0].get('idempotency_key', '')
        # Format: deposit-{slot_id}-{user_id}-{day}
        self.assertTrue(ik.startswith('deposit-'), f'got {ik!r}')
        self.assertEqual(ik.count('-'), 3)

    def test_create_booking_stripe_error_keeps_booking_pending(self):
        import stripe as _s
        # Booking still inserted, payment block carries error
        r, _ = self._book(side_effect=_s.error.StripeError('Network down'))
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn('error', d['payment'])

    def test_retry_payment_intent_rotates_key(self):
        captured = []
        from unittest.mock import patch, MagicMock
        import server
        pi_mock = MagicMock()
        pi_mock.id = 'pi_retry_1'
        pi_mock.client_secret = 'pi_retry_1_secret_x'

        def _create(*args, **kwargs):
            captured.append(kwargs)
            return pi_mock

        # First booking
        self._book()
        # Force status to payment_failed
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bookings SET status='payment_failed' WHERE id=1")
        conn.commit(); conn.close()

        with patch.object(server.stripe.PaymentIntent, 'create',
                          side_effect=_create):
            r = self.client.post('/api/bookings/1/retry-payment-intent')

        self.assertEqual(r.status_code, 200, f'{r.status_code} {r.data[:200]}')
        d = r.get_json()
        self.assertEqual(d['attempt'], 2)
        self.assertTrue(captured[-1]['idempotency_key'].startswith('deposit-retry-1-'))

    def test_retry_rejected_on_completed_booking(self):
        self._book()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bookings SET status='completed' WHERE id=1")
        conn.commit(); conn.close()
        r = self.client.post('/api/bookings/1/retry-payment-intent')
        self.assertEqual(r.status_code, 409)

    def test_retry_forbidden_for_non_client(self):
        self._book()
        _logout(self.client)
        # Register a different user (username must be alphanumeric+underscore)
        rr = _register(self.client, 'other2')
        self.assertEqual(rr.status_code, 200, f'register failed: {rr.data[:200]}')
        r = self.client.post('/api/bookings/1/retry-payment-intent')
        self.assertEqual(r.status_code, 403)

    def test_api_pay_returns_safe_fields(self):
        from unittest.mock import patch, MagicMock
        import server
        self._book()
        # Mock the PI retrieve call too
        pi_mock = MagicMock()
        pi_mock.client_secret = 'pi_test_12345_secret_abc'
        with patch.object(server.stripe.PaymentIntent, 'retrieve',
                          return_value=pi_mock):
            r = self.client.get('/api/pay/1')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        # Safe display fields exposed
        self.assertIn('artist_name', d)
        self.assertIn('amount_cents', d)
        self.assertIn('client_secret', d)
        # Sensitive fields NOT exposed
        self.assertNotIn('client_id', d)
        self.assertNotIn('email', d)
        self.assertNotIn('phone', d)

    def test_api_pay_410_after_confirm(self):
        self._book()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bookings SET status='confirmed' WHERE id=1")
        conn.commit(); conn.close()
        r = self.client.get('/api/pay/1')
        self.assertEqual(r.status_code, 410)

    def test_pay_page_serves_html(self):
        self._book()
        r = self.client.get('/pay/1')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'js.stripe.com/v3', r.data)
        self.assertIn(b'card-element', r.data)


class StripeHardeningTests(unittest.TestCase):
    """P0 hardening: webhook out-of-order guards, application-fee refund on
    cancellation/refund, and the no-show endpoint."""

    def setUp(self):
        os.environ['ENABLE_DEPOSIT_PI'] = '1'
        os.environ['STRIPE_SECRET_KEY'] = 'sk_test_dummy'
        os.environ['STRIPE_PUBLIC_KEY'] = 'pk_test_dummy'
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, '
            'is_artist, stripe_account_id, stripe_charges_enabled) '
            'VALUES (?, ?, ?, ?, 1, ?, 1)',
            ('artist1', 'Artist One', 'x', 'a@t.cz', 'acct_test_artist')
        )
        from datetime import datetime, timedelta
        future_start = (datetime.utcnow() + timedelta(days=10)).isoformat()
        future_end = (datetime.utcnow() + timedelta(days=10, hours=4)).isoformat()
        past_start = (datetime.utcnow() - timedelta(days=1)).isoformat()
        past_end = (datetime.utcnow() - timedelta(hours=20)).isoformat()
        for start, end, status in ((future_start, future_end, 'free'), (past_start, past_end, 'booked')):
            conn.execute(
                'INSERT INTO slots (user_id, start_at, end_at, status, '
                'price_min, price_max, price_unit, min_duration_hours) '
                'VALUES (1, ?, ?, ?, ?, ?, ?, ?)',
                (start, end, status, 1500, 1500, 'hour', 1)
            )
        conn.commit()
        conn.close()
        _register(self.client, 'client1')  # becomes user id 2, session logged in

    def tearDown(self):
        os.unlink(self.db)
        os.environ.pop('ENABLE_DEPOSIT_PI', None)
        os.environ.pop('STRIPE_SECRET_KEY', None)
        os.environ.pop('STRIPE_PUBLIC_KEY', None)

    def _insert_booking(self, slot_id, status, pi_id, deposit_cents=15000):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO bookings (slot_id, artist_id, client_id, status, '
            'deposit_cents, currency, stripe_payment_intent_id) '
            'VALUES (?, 1, 2, ?, ?, ?, ?)',
            (slot_id, status, deposit_cents, 'CZK', pi_id)
        )
        conn.commit()
        bid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return bid

    def _status_of(self, bid):
        import sqlite3
        conn = sqlite3.connect(self.db)
        status = conn.execute('SELECT status FROM bookings WHERE id=?', (bid,)).fetchone()[0]
        conn.close()
        return status

    def _webhook(self, event_type, obj, event_id):
        return self.client.post('/api/stripe/webhook', json={
            'id': event_id, 'type': event_type, 'data': {'object': obj},
        })

    def test_refund_webhook_does_not_overwrite_cancelled(self):
        # cancel_booking already recorded refund_cents synchronously with the
        # more specific cancelled_client status — the resulting charge.refunded
        # webhook shouldn't downgrade that to the generic 'refunded'.
        bid = self._insert_booking(1, 'cancelled_client', pi_id='pi_guard_1')
        r = self._webhook('charge.refunded', {
            'id': 'ch_1', 'payment_intent': 'pi_guard_1', 'amount_refunded': 15000,
        }, 'evt_refund_1')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status_of(bid), 'cancelled_client')

    def test_refund_webhook_resolves_dispute(self):
        # A dispute resolved by refunding the client IS a legal transition —
        # confirms the guard isn't over-broad.
        bid = self._insert_booking(1, 'disputed', pi_id='pi_guard_1b')
        r = self._webhook('charge.refunded', {
            'id': 'ch_1b', 'payment_intent': 'pi_guard_1b', 'amount_refunded': 15000,
        }, 'evt_refund_1b')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status_of(bid), 'refunded')

    def test_dispute_webhook_guard_on_replay_with_new_event_id(self):
        from unittest.mock import patch
        import server
        bid = self._insert_booking(1, 'confirmed', pi_id='pi_guard_2')
        with patch.object(server.stripe.Account, 'modify'):
            r1 = self._webhook('charge.dispute.created', {
                'id': 'dp_1', 'payment_intent': 'pi_guard_2', 'reason': 'fraudulent',
            }, 'evt_dispute_1')
            self.assertEqual(r1.status_code, 200)
            self.assertEqual(self._status_of(bid), 'disputed')
            # Same dispute, different event_id (bypasses the outer event_id dedup) —
            # the status guard should still make this a no-op, not an error.
            r2 = self._webhook('charge.dispute.created', {
                'id': 'dp_1', 'payment_intent': 'pi_guard_2', 'reason': 'fraudulent',
            }, 'evt_dispute_2')
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(self._status_of(bid), 'disputed')

    def test_dispute_created_freezes_artist_payouts(self):
        from unittest.mock import patch
        import server
        bid = self._insert_booking(1, 'confirmed', pi_id='pi_guard_freeze')
        with patch.object(server.stripe.Account, 'modify') as m:
            r = self._webhook('charge.dispute.created', {
                'id': 'dp_2', 'payment_intent': 'pi_guard_freeze', 'reason': 'fraudulent',
            }, 'evt_dispute_freeze')
        self.assertEqual(r.status_code, 200)
        m.assert_called_once_with(
            'acct_test_artist', settings={'payouts': {'schedule': {'interval': 'manual'}}}
        )

    def test_dispute_closed_won_resumes_payouts_and_completes_booking(self):
        from unittest.mock import patch
        import server
        bid = self._insert_booking(1, 'disputed', pi_id='pi_guard_won')
        with patch.object(server.stripe.Account, 'modify') as m:
            r = self._webhook('charge.dispute.closed', {
                'id': 'dp_3', 'payment_intent': 'pi_guard_won', 'status': 'won',
            }, 'evt_dispute_won')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status_of(bid), 'completed')
        m.assert_called_once_with(
            'acct_test_artist', settings={'payouts': {'schedule': {'interval': 'daily'}}}
        )

    def test_dispute_closed_lost_marks_refunded(self):
        from unittest.mock import patch
        import server
        bid = self._insert_booking(1, 'disputed', pi_id='pi_guard_lost')
        with patch.object(server.stripe.Account, 'modify'):
            r = self._webhook('charge.dispute.closed', {
                'id': 'dp_4', 'payment_intent': 'pi_guard_lost', 'status': 'lost',
            }, 'evt_dispute_lost')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._status_of(bid), 'refunded')

    def _insert_initial_snapshot(self, bid, client_pays_total=1530.0):
        import sqlite3, json
        from decimal import Decimal
        from pricing import stripe_fee_for
        snapshot = {
            'gross_price': 1500.0, 'client_service_fee': 30.0,
            'client_pays_total': client_pays_total, 'artist_commission': 180.0,
            'stripe_fee': float(stripe_fee_for(Decimal(str(client_pays_total)), card_type='card_eea')),
            'discount_applied': 0.0, 'discount_source': '', 'artist_payout': 1350.0,
            'inklink_net': 100.0, 'effective_take_rate': 0.065,
            'founding_artist_status': 'none', 'founding_artist_day': None,
        }
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO economics_snapshots (booking_id, kind, snapshot) VALUES (?, 'initial', ?)",
            (bid, json.dumps(snapshot))
        )
        conn.commit()
        conn.close()
        return snapshot

    def _adjust_snapshots(self, bid):
        import sqlite3, json
        conn = sqlite3.connect(self.db)
        rows = conn.execute(
            "SELECT snapshot FROM economics_snapshots WHERE booking_id=? AND kind='adjust'", (bid,)
        ).fetchall()
        conn.close()
        return [json.loads(r[0]) for r in rows]

    def test_card_country_reconciliation_on_non_eea_card(self):
        from decimal import Decimal
        from pricing import stripe_fee_for
        bid = self._insert_booking(1, 'pending_payment', pi_id='pi_guard_cc')
        self._insert_initial_snapshot(bid)
        r = self._webhook('payment_intent.succeeded', {
            'id': 'pi_guard_cc',
            'metadata': {'inklink_booking_id': str(bid)},
            'charges': {'data': [{'payment_method_details': {'card': {'country': 'US'}}}]},
        }, 'evt_pi_cc')
        self.assertEqual(r.status_code, 200)
        adjusts = self._adjust_snapshots(bid)
        self.assertEqual(len(adjusts), 1)
        self.assertEqual(adjusts[0]['stripe_card_type'], 'card_non_eea')
        self.assertEqual(adjusts[0]['card_country'], 'US')
        expected_fee = float(stripe_fee_for(Decimal('1530'), card_type='card_non_eea'))
        self.assertAlmostEqual(adjusts[0]['stripe_fee'], expected_fee, places=2)

    def test_card_country_no_adjust_snapshot_for_eea_card(self):
        bid = self._insert_booking(1, 'pending_payment', pi_id='pi_guard_cc2')
        self._insert_initial_snapshot(bid)
        r = self._webhook('payment_intent.succeeded', {
            'id': 'pi_guard_cc2',
            'metadata': {'inklink_booking_id': str(bid)},
            'charges': {'data': [{'payment_method_details': {'card': {'country': 'CZ'}}}]},
        }, 'evt_pi_cc2')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._adjust_snapshots(bid), [])

    def test_cancel_refund_reverses_transfer_and_fee(self):
        from unittest.mock import patch, MagicMock
        import server
        bid = self._insert_booking(1, 'confirmed', pi_id='pi_guard_3')
        refund_mock = MagicMock()
        refund_mock.id = 're_test_1'
        captured = {}

        def _create(*args, **kwargs):
            captured.update(kwargs)
            return refund_mock

        _login(self.client, 'client1')
        with patch.object(server.stripe.Refund, 'create', side_effect=_create):
            r = self.client.post(f'/api/bookings/{bid}/cancel')
        self.assertEqual(r.status_code, 200, f'{r.status_code} {r.data[:200]}')
        self.assertTrue(captured.get('reverse_transfer'))
        self.assertTrue(captured.get('refund_application_fee'))

    def test_mark_no_show_success(self):
        bid = self._insert_booking(2, 'confirmed', pi_id='pi_guard_4')  # slot 2 = past
        with self.client.session_transaction() as s:
            s['user_id'] = 1  # artist1
        r = self.client.post(f'/api/bookings/{bid}/mark-no-show')
        self.assertEqual(r.status_code, 200, f'{r.status_code} {r.data[:200]}')
        self.assertEqual(self._status_of(bid), 'no_show')

    def test_mark_no_show_rejected_before_slot_time(self):
        bid = self._insert_booking(1, 'confirmed', pi_id='pi_guard_5')  # slot 1 = future
        with self.client.session_transaction() as s:
            s['user_id'] = 1
        r = self.client.post(f'/api/bookings/{bid}/mark-no-show')
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self._status_of(bid), 'confirmed')


# StudioIdInsertTests (dříve StudioIdBackfillTests) žijí u ostatních
# booking testů na konci souboru — potřebují _Sprint2Base.


class ReferralTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_register_with_ref_creates_referral_row(self):
        r1 = _register(self.client, 'alice')
        self.assertEqual(r1.status_code, 200)
        _logout(self.client)
        r2 = _register(self.client, 'bob', ref='alice')
        self.assertEqual(r2.status_code, 200)

        import sqlite3
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM referrals').fetchall()
        conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['code'], 'alice')
        self.assertIsNone(rows[0]['credit_granted_at'])

    def test_self_referral_is_ignored(self):
        # Bob signs up with ref=bob (himself) — should NOT create a referral.
        r = _register(self.client, 'bob', ref='bob')
        self.assertEqual(r.status_code, 200)
        import sqlite3
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM referrals').fetchall()
        conn.close()
        self.assertEqual(len(rows), 0)

    def test_unknown_ref_is_ignored(self):
        r = _register(self.client, 'bob', ref='nonexistent-user-xyz')
        self.assertEqual(r.status_code, 200)
        import sqlite3
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        rows = conn.execute('SELECT * FROM referrals').fetchall()
        conn.close()
        self.assertEqual(len(rows), 0)

    def test_me_referrals_returns_link_and_stats(self):
        _register(self.client, 'alice')
        _logout(self.client)
        _register(self.client, 'bob', ref='alice')
        _logout(self.client)
        _login(self.client, 'alice')
        r = self.client.get('/api/me/referrals')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn('ref=alice', d['link'])
        self.assertEqual(d['total_signups'], 1)
        self.assertEqual(d['total_granted'], 0)
        self.assertEqual(d['bonus_czk'], 300)
        self.assertEqual(d['referred'][0]['username'], 'bob')
        self.assertEqual(d['referred'][0]['status'], 'signed_up')


class RefundRequestTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()
        _register(self.client, 'alice')

    def tearDown(self):
        os.unlink(self.db)

    def test_short_reason_rejected(self):
        r = self.client.post('/api/bookings/9999/refund-request', json={'reason': 'short'})
        self.assertEqual(r.status_code, 400)

    def test_long_reason_rejected(self):
        r = self.client.post('/api/bookings/9999/refund-request',
                             json={'reason': 'x' * 2000})
        self.assertEqual(r.status_code, 400)

    def test_nonexisting_booking_404(self):
        r = self.client.post('/api/bookings/9999/refund-request',
                             json={'reason': 'A valid 20+ char reason here.'})
        self.assertEqual(r.status_code, 404)

    def test_unauth_returns_401(self):
        _logout(self.client)
        r = self.client.post('/api/bookings/1/refund-request',
                             json={'reason': 'A valid 20+ char reason here.'})
        self.assertEqual(r.status_code, 401)

    def test_list_returns_empty_for_new_user(self):
        r = self.client.get('/api/refund-requests')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), [])


class GeoNearTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_empty_response(self):
        r = self.client.get('/api/artists/near?lat=50.0&lng=14.4&km=25')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['count'], 0)
        self.assertEqual(d['km'], 25)
        self.assertAlmostEqual(d['center']['lat'], 50.0, places=4)

    def test_invalid_lat_400(self):
        r = self.client.get('/api/artists/near?lat=999&lng=14&km=25')
        self.assertEqual(r.status_code, 400)

    def test_non_numeric_lat_400(self):
        r = self.client.get('/api/artists/near?lat=abc&lng=14&km=25')
        self.assertEqual(r.status_code, 400)

    def test_km_default(self):
        r = self.client.get('/api/artists/near?lat=50&lng=14')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['km'], 25)

    def test_km_clamped(self):
        r = self.client.get('/api/artists/near?lat=50&lng=14&km=99999')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['km'], 500)  # clamp upper

    def test_artist_in_radius_returned(self):
        # Bypass register — write artist directly with coords near Praha
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, lat, lng, is_artist) '
            'VALUES (?, ?, ?, ?, ?, ?, 1)',
            ('jana', 'Jana Tatérka', 'x', 'jana@t.cz', 50.085, 14.435)
        )
        conn.commit(); conn.close()
        # Within 5 km of Wenceslas square (50.080, 14.428)
        r = self.client.get('/api/artists/near?lat=50.080&lng=14.428&km=5')
        d = r.get_json()
        self.assertEqual(d['count'], 1)
        self.assertEqual(d['artists'][0]['username'], 'jana')
        self.assertIn('distance_km', d['artists'][0])
        self.assertLess(d['artists'][0]['distance_km'], 5)

    def test_artist_outside_radius_excluded(self):
        # Artist in Brno (>180 km from Praha)
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, lat, lng, is_artist) '
            'VALUES (?, ?, ?, ?, ?, ?, 1)',
            ('petr', 'Petr', 'x', 'petr@t.cz', 49.195, 16.607)
        )
        conn.commit(); conn.close()
        r = self.client.get('/api/artists/near?lat=50.080&lng=14.428&km=50')
        self.assertEqual(r.get_json()['count'], 0)


class GdprExportTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()
        _register(self.client, 'alice')

    def tearDown(self):
        os.unlink(self.db)

    def test_export_unauthenticated_401(self):
        _logout(self.client)
        r = self.client.get('/api/me/export')
        self.assertEqual(r.status_code, 401)

    def test_export_returns_zip(self):
        r = self.client.get('/api/me/export')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'application/zip')
        self.assertIn('attachment', r.headers.get('Content-Disposition', ''))

    def test_export_zip_contents(self):
        import io, zipfile, json
        r = self.client.get('/api/me/export')
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        for required in ('profile.json', 'bookings_as_client.json', 'README.txt',
                         'export_metadata.json', 'referrals_made.json', 'refund_requests.json'):
            self.assertIn(required, names)
        profile = json.loads(zf.read('profile.json'))
        # Never export auth secrets
        self.assertNotIn('password_hash', profile)
        self.assertNotIn('verify_code', profile)
        self.assertEqual(profile['username'], 'alice')


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()
        _register(self.client, 'alice')

    def tearDown(self):
        os.unlink(self.db)

    def test_delete_requires_confirm_username(self):
        r = self.client.post('/api/me/delete', json={})
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/api/me/delete', json={'confirm_username': 'wrong'})
        self.assertEqual(r.status_code, 400)

    def test_delete_success_clears_session(self):
        r = self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn('purge_at', d)
        self.assertEqual(d['grace_days'], 30)
        # Session cleared → /api/me returns null
        r = self.client.get('/api/me')
        self.assertEqual(r.get_json(), None)

    def test_delete_idempotent(self):
        self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        # Re-login (still in grace)
        _login(self.client, 'alice')
        r = self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        self.assertEqual(r.status_code, 409)

    def test_cancel_deletion(self):
        self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        _login(self.client, 'alice')
        r = self.client.post('/api/me/delete-cancel')
        self.assertEqual(r.status_code, 200)
        r = self.client.get('/api/me')
        self.assertIsNone(r.get_json().get('deletion_requested_at'))

    def test_me_endpoint_exposes_purge_date(self):
        self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        _login(self.client, 'alice')
        r = self.client.get('/api/me')
        d = r.get_json()
        self.assertIsNotNone(d.get('deletion_requested_at'))
        self.assertIsNotNone(d.get('deletion_purge_at'))

    def test_cron_token_required(self):
        r = self.client.get('/api/cron/account-deletions')
        self.assertEqual(r.status_code, 403)
        r = self.client.get('/api/cron/account-deletions?token=nope')
        self.assertEqual(r.status_code, 403)

    def test_cron_purges_after_grace(self):
        # Request deletion
        self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        # Backdate the request in DB so it's past grace
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET deletion_requested_at=? WHERE username=?',
                     ('2025-01-01T00:00:00', 'alice'))
        conn.commit(); conn.close()
        # Trigger cron
        r = self.client.get('/api/cron/account-deletions?token=test-token')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d['purged_count'], 1)
        # Verify anonymization
        conn = sqlite3.connect(self.db); conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT username, display_name, email, deleted_at FROM users WHERE id=1').fetchone()
        conn.close()
        self.assertEqual(row['username'], 'deleted-1')
        self.assertEqual(row['display_name'], 'Smazaný účet')
        self.assertEqual(row['email'], '')
        self.assertIsNotNone(row['deleted_at'])
        # Login with old credentials must fail
        _logout(self.client)
        r = self.client.post('/api/login', json={'username': 'alice', 'password': 'pass1234'})
        self.assertEqual(r.status_code, 401)

    def test_cron_skips_within_grace(self):
        self.client.post('/api/me/delete', json={'confirm_username': 'alice'})
        r = self.client.get('/api/cron/account-deletions?token=test-token')
        self.assertEqual(r.json['purged_count'], 0)


class ICalFeedTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()
        _register(self.client, 'alice')

    def tearDown(self):
        os.unlink(self.db)

    def test_token_endpoint_returns_subscribe_url(self):
        r = self.client.get('/api/me/calendar-token')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn('token', d)
        self.assertIn('/calendar/', d['subscribe_url'])
        self.assertTrue(d['subscribe_url'].endswith('.ics'))

    def test_token_unauth_401(self):
        _logout(self.client)
        r = self.client.get('/api/me/calendar-token')
        self.assertEqual(r.status_code, 401)

    def test_public_feed_serves_valid_ics(self):
        tok = self.client.get('/api/me/calendar-token').get_json()['token']
        r = self.client.get(f'/calendar/{tok}.ics')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, 'text/calendar')
        body = r.data.decode('utf-8')
        self.assertIn('BEGIN:VCALENDAR', body)
        self.assertIn('END:VCALENDAR', body)
        self.assertIn('PRODID:-//InkLink//Calendar//EN', body)

    def test_public_feed_invalid_token_404(self):
        r = self.client.get('/calendar/short.ics')
        self.assertEqual(r.status_code, 404)
        r = self.client.get('/calendar/notarealtokennotarealtoken.ics')
        self.assertEqual(r.status_code, 404)

    def test_regenerate_rotates_token(self):
        old = self.client.get('/api/me/calendar-token').get_json()['token']
        new = self.client.post('/api/me/calendar-token').get_json()['token']
        self.assertNotEqual(old, new)
        # Old token no longer works
        r = self.client.get(f'/calendar/{old}.ics')
        self.assertEqual(r.status_code, 404)
        # New token does
        r = self.client.get(f'/calendar/{new}.ics')
        self.assertEqual(r.status_code, 200)


class CronGuardTests(unittest.TestCase):
    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_welcome_emails_no_token_403(self):
        r = self.client.get('/api/cron/welcome-emails')
        self.assertEqual(r.status_code, 403)

    def test_welcome_emails_wrong_token_403(self):
        r = self.client.get('/api/cron/welcome-emails?token=nope')
        self.assertEqual(r.status_code, 403)

    def test_welcome_emails_correct_token_no_resend_503(self):
        r = self.client.get('/api/cron/welcome-emails?token=test-token')
        # Without RESEND_API_KEY → graceful 503 (not 500, not 200)
        self.assertEqual(r.status_code, 503)

    def test_reconcile_correct_token_200(self):
        r = self.client.get('/api/cron/reconcile?token=test-token')
        self.assertEqual(r.status_code, 200)

    def test_reconcile_x_cron_token_header_works(self):
        r = self.client.get('/api/cron/reconcile',
                            headers={'X-Cron-Token': 'test-token'})
        self.assertEqual(r.status_code, 200)


# ── Sprint 2: booking system + calendar ──────────────────────────────────────

class _Sprint2Base(unittest.TestCase):
    """Sdílený setup: tatér (id 1) + klient (id 2, přihlášený).

    Časy se počítají ze `server._prague_now_naive()`, ne z `datetime.now()` —
    server proti pražskému wall-clocku validuje "termín není v minulosti",
    takže test musí počítat ze stejné osy, ať běží na jakémkoli stroji.
    """

    def setUp(self):
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, email, is_artist) "
            "VALUES ('artist1','Artist One','x','a@t.cz',1)")
        conn.commit()
        conn.close()
        _register(self.client, 'client1')  # user id 2, zůstává přihlášený

    def tearDown(self):
        os.unlink(self.db)

    # — pomocníci —

    def _now(self):
        import server
        return server._prague_now_naive()

    def _day_at(self, days_ahead, hour, minute=0):
        base = self._now() + timedelta(days=days_ahead)
        return base.replace(hour=hour, minute=minute, second=0, microsecond=0)

    def _mk_slot(self, start, end, buf_before=0, buf_after=0, min_dur=1, artist_id=1):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max, "
            "price_unit, min_duration_hours, buffer_before_minutes, buffer_after_minutes) "
            "VALUES (?,?,?,'free',1000,1000,'hour',?,?,?)",
            (artist_id, start.isoformat(), end.isoformat(), min_dur, buf_before, buf_after))
        conn.commit()
        sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return sid

    def _mk_block(self, start, end, artist_id=1, reason='dovolená'):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO artist_blocked_time (artist_id, start_at, end_at, reason) VALUES (?,?,?,?)',
            (artist_id, start.isoformat(), end.isoformat(), reason))
        conn.commit()
        bid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return bid

    def _book(self, slot_id, start, duration_hours=2, note='Vlk na předloktí'):
        return self.client.post('/api/bookings', json={
            'slot_id': slot_id, 'booking_start_at': start.isoformat(),
            'duration_hours': duration_hours, 'design_note': note,
        })

    def _as_artist(self):
        with self.client.session_transaction() as s:
            s['user_id'] = 1

    def _as_client(self):
        with self.client.session_transaction() as s:
            s['user_id'] = 2

    def _booking_row(self, bid):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        row = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
        conn.close()
        return dict(row) if row else None


class BufferCollisionTests(_Sprint2Base):
    """Buffer = odstup mezi rezervacemi (úklid/příprava)."""

    def test_booking_inside_buffer_rejected(self):
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18), buf_after=30)
        r1 = self._book(slot, self._day_at(5, 12))          # 12:00–14:00
        self.assertEqual(r1.status_code, 200, r1.data[:300])
        r2 = self._book(slot, self._day_at(5, 14, 15))      # 14:15 — uvnitř 30min bufferu
        self.assertEqual(r2.status_code, 409, r2.data[:300])

    def test_booking_after_buffer_allowed(self):
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18), buf_after=30)
        self._book(slot, self._day_at(5, 12))               # 12:00–14:00
        r2 = self._book(slot, self._day_at(5, 14, 30))      # přesně na hraně bufferu
        self.assertEqual(r2.status_code, 200, r2.data[:300])

    def test_buffers_snapshotted_onto_booking(self):
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18), buf_before=15, buf_after=30)
        r = self._book(slot, self._day_at(5, 12))
        bid = r.get_json()['id']
        row = self._booking_row(bid)
        self.assertEqual(row['buffer_before_minutes'], 15)
        self.assertEqual(row['buffer_after_minutes'], 30)


class BlockedTimeTests(_Sprint2Base):
    """Blokace volna platí napříč všemi sloty tatéra."""

    def test_booking_into_blocked_window_rejected(self):
        self._mk_block(self._day_at(6, 10), self._day_at(6, 12))
        slot = self._mk_slot(self._day_at(6, 9), self._day_at(6, 18))
        r = self._book(slot, self._day_at(6, 11))  # zasahuje do blokace
        self.assertEqual(r.status_code, 409, r.data[:300])

    def test_booking_outside_blocked_window_allowed(self):
        self._mk_block(self._day_at(6, 10), self._day_at(6, 12))
        slot = self._mk_slot(self._day_at(6, 9), self._day_at(6, 18))
        r = self._book(slot, self._day_at(6, 13))
        self.assertEqual(r.status_code, 200, r.data[:300])

    def test_block_rejected_when_active_booking_in_window(self):
        slot = self._mk_slot(self._day_at(7, 10), self._day_at(7, 18))
        self._book(slot, self._day_at(7, 12))
        self._as_artist()
        r = self.client.post('/api/blocked-time', json={
            'start_at': self._day_at(7, 11).isoformat(),
            'end_at': self._day_at(7, 15).isoformat(),
        })
        self.assertEqual(r.status_code, 409, r.data[:300])

    def test_delete_blocked_time_is_owner_only(self):
        block_id = self._mk_block(self._day_at(8, 10), self._day_at(8, 12))
        self._as_client()  # klient (id 2) není vlastník
        r = self.client.delete(f'/api/blocked-time/{block_id}')
        self.assertEqual(r.status_code, 404)
        self._as_artist()
        r = self.client.delete(f'/api/blocked-time/{block_id}')
        self.assertEqual(r.status_code, 200)


class ReschedulePolicyTests(_Sprint2Base):

    def _mk_confirmed_booking(self, days_ahead, hour):
        slot = self._mk_slot(self._day_at(days_ahead, 9), self._day_at(days_ahead, 18))
        r = self._book(slot, self._day_at(days_ahead, hour))
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()['id']

    def test_client_reschedule_far_ahead_applies_immediately(self):
        bid = self._mk_confirmed_booking(10, 12)
        target = self._mk_slot(self._day_at(11, 9), self._day_at(11, 18))
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(11, 14).isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertTrue(r.get_json()['applied'])
        self.assertTrue(self._booking_row(bid)['booking_start_at'].startswith(
            self._day_at(11, 14).isoformat()[:13]))

    def test_client_reschedule_late_creates_pending_request(self):
        bid = self._mk_confirmed_booking(1, 12)  # < 48 h → pozdě
        original_start = self._booking_row(bid)['booking_start_at']
        target = self._mk_slot(self._day_at(2, 9), self._day_at(2, 18))
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(2, 14).isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.data[:300])
        body = r.get_json()
        self.assertFalse(body['applied'])
        self.assertEqual(body['status'], 'pending')
        # Časy rezervace se nesmí změnit, dokud tatér nerozhodne
        self.assertEqual(self._booking_row(bid)['booking_start_at'], original_start)

    def test_artist_reschedule_late_applies_immediately(self):
        bid = self._mk_confirmed_booking(1, 12)
        target = self._mk_slot(self._day_at(2, 9), self._day_at(2, 18))
        self._as_artist()
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(2, 14).isoformat(),
        })
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertTrue(r.get_json()['applied'])

    def test_artist_approves_pending_request(self):
        bid = self._mk_confirmed_booking(1, 12)
        target = self._mk_slot(self._day_at(2, 9), self._day_at(2, 18))
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(2, 14).isoformat(),
        })
        rid = r.get_json()['request_id']
        self._as_artist()
        d = self.client.post(f'/api/reschedule-requests/{rid}/decide', json={'decision': 'approve'})
        self.assertEqual(d.status_code, 200, d.data[:300])
        self.assertTrue(self._booking_row(bid)['booking_start_at'].startswith(
            self._day_at(2, 14).isoformat()[:13]))

    def test_artist_rejects_pending_request_leaves_booking_alone(self):
        bid = self._mk_confirmed_booking(1, 12)
        original_start = self._booking_row(bid)['booking_start_at']
        target = self._mk_slot(self._day_at(2, 9), self._day_at(2, 18))
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(2, 14).isoformat(),
        })
        rid = r.get_json()['request_id']
        self._as_artist()
        d = self.client.post(f'/api/reschedule-requests/{rid}/decide', json={'decision': 'reject'})
        self.assertEqual(d.status_code, 200)
        self.assertEqual(self._booking_row(bid)['booking_start_at'], original_start)

    def test_reschedule_respects_blocked_time(self):
        bid = self._mk_confirmed_booking(10, 12)
        target = self._mk_slot(self._day_at(11, 9), self._day_at(11, 18))
        self._mk_block(self._day_at(11, 13), self._day_at(11, 16))
        r = self.client.patch(f'/api/bookings/{bid}/reschedule', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(11, 14).isoformat(),
        })
        self.assertEqual(r.status_code, 409, r.data[:300])

    def test_duplicate_pending_request_rejected(self):
        bid = self._mk_confirmed_booking(1, 12)
        target = self._mk_slot(self._day_at(2, 9), self._day_at(2, 18))
        payload = {'new_slot_id': target, 'booking_start_at': self._day_at(2, 14).isoformat()}
        self.client.patch(f'/api/bookings/{bid}/reschedule', json=payload)
        r2 = self.client.patch(f'/api/bookings/{bid}/reschedule', json=payload)
        self.assertEqual(r2.status_code, 409, r2.data[:300])


class MultiSessionTests(_Sprint2Base):

    def _mk_parent(self):
        slot = self._mk_slot(self._day_at(10, 9), self._day_at(10, 18))
        r = self._book(slot, self._day_at(10, 12))
        self.assertEqual(r.status_code, 200, r.data[:300])
        return r.get_json()['id']

    def test_follow_up_is_artist_only(self):
        parent = self._mk_parent()
        target = self._mk_slot(self._day_at(20, 9), self._day_at(20, 18))
        r = self.client.post(f'/api/bookings/{parent}/follow-up', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(20, 12).isoformat(),
            'duration_hours': 2})
        self.assertEqual(r.status_code, 403, r.data[:300])

    def test_follow_up_links_series_with_zero_deposit(self):
        parent = self._mk_parent()
        target = self._mk_slot(self._day_at(20, 9), self._day_at(20, 18))
        self._as_artist()
        r = self.client.post(f'/api/bookings/{parent}/follow-up', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(20, 12).isoformat(),
            'duration_hours': 2})
        self.assertEqual(r.status_code, 200, r.data[:300])
        child = self._booking_row(r.get_json()['id'])
        self.assertEqual(child['parent_booking_id'], parent)
        self.assertEqual(child['session_number'], 2)
        self.assertEqual(child['deposit_cents'], 0)

    def test_third_session_points_at_first_not_second(self):
        parent = self._mk_parent()
        self._as_artist()
        s2 = self._mk_slot(self._day_at(20, 9), self._day_at(20, 18))
        r2 = self.client.post(f'/api/bookings/{parent}/follow-up', json={
            'new_slot_id': s2, 'booking_start_at': self._day_at(20, 12).isoformat(),
            'duration_hours': 2})
        child2 = r2.get_json()['id']
        s3 = self._mk_slot(self._day_at(30, 9), self._day_at(30, 18))
        r3 = self.client.post(f'/api/bookings/{child2}/follow-up', json={
            'new_slot_id': s3, 'booking_start_at': self._day_at(30, 12).isoformat(),
            'duration_hours': 2})
        child3 = self._booking_row(r3.get_json()['id'])
        self.assertEqual(child3['parent_booking_id'], parent)  # ne child2
        self.assertEqual(child3['session_number'], 3)

    def test_cancelling_child_refunds_nothing_and_leaves_parent(self):
        parent = self._mk_parent()
        target = self._mk_slot(self._day_at(20, 9), self._day_at(20, 18))
        self._as_artist()
        child_id = self.client.post(f'/api/bookings/{parent}/follow-up', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(20, 12).isoformat(),
            'duration_hours': 2}).get_json()['id']
        self._as_client()
        r = self.client.post(f'/api/bookings/{child_id}/cancel')
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(r.get_json()['refund_cents'], 0)
        self.assertEqual(self._booking_row(parent)['status'], 'confirmed')


class CancellationPolicyOverrideTests(_Sprint2Base):
    """Nejdůležitější regrese sprintu: tatér BEZ override musí dostat
    přesně stávající chování ze Sprintu 1 (96 h / 48 h)."""

    def _cancel_hours_before(self, hours, full=None, half=None):
        if full is not None or half is not None:
            import sqlite3
            conn = sqlite3.connect(self.db)
            conn.execute('UPDATE users SET cancel_refund_full_hours=?, cancel_refund_half_hours=? WHERE id=1',
                         (full, half))
            conn.commit()
            conn.close()
        start = self._now() + timedelta(hours=hours)
        slot = self._mk_slot(start - timedelta(hours=1), start + timedelta(hours=6))
        r = self._book(slot, start)
        self.assertEqual(r.status_code, 200, r.data[:300])
        bid = r.get_json()['id']
        c = self.client.post(f'/api/bookings/{bid}/cancel')
        self.assertEqual(c.status_code, 200, c.data[:300])
        return c.get_json()['refund_pct']

    def test_default_policy_unchanged_from_sprint1(self):
        self.assertEqual(self._cancel_hours_before(120), 100)  # > 96 h
        self.assertEqual(self._cancel_hours_before(72), 50)    # 48–96 h
        self.assertEqual(self._cancel_hours_before(10), 0)     # < 48 h

    def test_artist_override_is_used(self):
        # Přísnější tatér: plný refund až od 200 h, poloviční od 150 h
        self.assertEqual(self._cancel_hours_before(120, full=200, half=150), 0)
        self.assertEqual(self._cancel_hours_before(160, full=200, half=150), 50)
        self.assertEqual(self._cancel_hours_before(220, full=200, half=150), 100)


class CzechHolidayWarningTests(_Sprint2Base):

    def test_holiday_slot_creates_but_warns(self):
        import server
        year = self._now().year + 1
        self._as_artist()
        r = self.client.post('/api/slots', json={
            'start_at': f'{year}-01-01T10:00:00', 'end_at': f'{year}-01-01T18:00:00',
            'price_min': 1000, 'price_max': 1500, 'price_unit': 'hour',
        })
        self.assertEqual(r.status_code, 200, r.data[:300])
        warnings = r.get_json().get('holiday_warnings') or []
        self.assertTrue(any(w['date'] == f'{year}-01-01' for w in warnings), warnings)

    def test_ordinary_day_has_no_warning(self):
        year = self._now().year + 1
        self._as_artist()
        r = self.client.post('/api/slots', json={
            'start_at': f'{year}-03-10T10:00:00', 'end_at': f'{year}-03-10T18:00:00',
            'price_min': 1000, 'price_max': 1500, 'price_unit': 'hour',
        })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json().get('holiday_warnings') or [], [])

    def test_moving_easter_monday_detected(self):
        import server
        from datetime import date
        # Velikonoční pondělí 2027 = 29. 3. — pohyblivý svátek, hardcoded
        # seznam by ho nezvládl.
        warns = server._cz_holiday_warnings([date(2027, 3, 29)])
        self.assertEqual(len(warns), 1)
        self.assertIn('pondělí', warns[0]['name'].lower())


class StudioIdInsertTests(_Sprint2Base):
    """bookings.studio_id se vyplňuje při INSERTu, ne backfillem.

    Dřív to dělal `UPDATE ... WHERE studio_id IS NULL` na každém startu
    procesu — nové rezervace tak měly NULL až do restartu a vstup tatéra do
    studia zpětně přepsal celou jeho historii. Účetnictví ten sloupec čte,
    takže musí zůstat snapshotem okamžiku rezervace.
    """

    def _put_artist_in_studio(self, artist_id=1, slug='studio1'):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO studios (slug, name) VALUES (?, 'Studio One')", (slug,))
        studio_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO studio_members (studio_id, artist_id) VALUES (?,?)',
                     (studio_id, artist_id))
        conn.commit()
        conn.close()
        return studio_id

    def test_studio_artist_booking_gets_studio_id_at_insert(self):
        studio_id = self._put_artist_in_studio()
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18))
        r = self._book(slot, self._day_at(5, 12))
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertEqual(self._booking_row(r.get_json()['id'])['studio_id'], studio_id)

    def test_solo_artist_booking_studio_id_null(self):
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18))
        r = self._book(slot, self._day_at(5, 12))
        self.assertIsNone(self._booking_row(r.get_json()['id'])['studio_id'])

    def test_follow_up_inherits_parent_studio_id(self):
        studio_id = self._put_artist_in_studio()
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18))
        parent = self._book(slot, self._day_at(5, 12)).get_json()['id']
        target = self._mk_slot(self._day_at(20, 10), self._day_at(20, 18))
        self._as_artist()
        child = self.client.post(f'/api/bookings/{parent}/follow-up', json={
            'new_slot_id': target, 'booking_start_at': self._day_at(20, 12).isoformat(),
            'duration_hours': 2}).get_json()['id']
        self.assertEqual(self._booking_row(child)['studio_id'], studio_id)

    def test_joining_studio_later_does_not_rewrite_history(self):
        import server
        slot = self._mk_slot(self._day_at(5, 10), self._day_at(5, 18))
        bid = self._book(slot, self._day_at(5, 12)).get_json()['id']
        self.assertIsNone(self._booking_row(bid)['studio_id'])
        self._put_artist_in_studio()
        server.init_db()   # simuluje restart procesu
        self.assertIsNone(self._booking_row(bid)['studio_id'],
                          'rezervace z doby před vstupem do studia se nesmí přepsat')


# ── Sprint 3: CRM ────────────────────────────────────────────────────────────

class _CrmBase(unittest.TestCase):
    """Fixture pro CRM testy.

    artist1 (id 1) — sólo
    artist2 (id 2) + artist3 (id 3) — studio A
    artist4 (id 4) — studio B
    client1 (id 5) — klient s účtem
    admin1  (id 6) — platformní admin (is_admin=1)
    """

    def setUp(self):
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        for uname, is_artist, is_admin in (
                ('artist1', 1, 0), ('artist2', 1, 0), ('artist3', 1, 0),
                ('artist4', 1, 0), ('client1', 0, 0), ('admin1', 0, 1)):
            conn.execute(
                'INSERT INTO users (username, display_name, password_hash, email, is_artist, is_admin) '
                'VALUES (?,?,?,?,?,?)',
                (uname, uname.title(), 'x', f'{uname}@t.cz', is_artist, is_admin))
        conn.execute("INSERT INTO studios (slug, name) VALUES ('studio-a','Studio A')")
        studio_a = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO studios (slug, name) VALUES ('studio-b','Studio B')")
        studio_b = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute("INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?,2,'admin')", (studio_a,))
        conn.execute("INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?,3,'member')", (studio_a,))
        conn.execute("INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?,4,'admin')", (studio_b,))
        conn.commit()
        conn.close()
        self.studio_a, self.studio_b = studio_a, studio_b

    def tearDown(self):
        os.unlink(self.db)

    def _as(self, uid):
        with self.client.session_transaction() as s:
            s['user_id'] = uid

    def _logout(self):
        with self.client.session_transaction() as s:
            s.clear()

    def _mk_client_for(self, artist_id, name='Jana Nováková', user_id=None):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO clients (artist_id, user_id, name, created_by) VALUES (?,?,?,?)',
            (artist_id, user_id, name, artist_id))
        conn.commit()
        cid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return cid


class CrossStudioLeakageTests(_CrmBase):
    """Roadmapa označuje únik napříč tenanty za katastrofické riziko.
    Tabulkově přes KAŽDÝ endpoint, který bere client_id."""

    def setUp(self):
        super().setUp()
        # Klient patří artist2 (studio A)
        self.cid = self._mk_client_for(2)
        self._as(2)
        self.nid = self.client.post(f'/api/clients/{self.cid}/notes',
                                    json={'body': 'tajná poznámka'}).get_json().get('id')

    def _endpoints(self):
        return [
            ('GET',    f'/api/clients/{self.cid}',         None),
            ('PATCH',  f'/api/clients/{self.cid}',         {'note': 'x'}),
            ('POST',   f'/api/clients/{self.cid}/notes',   {'body': 'x'}),
        ]

    def _call(self, method, path, payload):
        fn = getattr(self.client, method.lower())
        return fn(path, json=payload) if payload is not None else fn(path)

    def test_solo_artist_cannot_see_other_artists_client(self):
        self._as(1)
        for method, path, payload in self._endpoints():
            with self.subTest(endpoint=f'{method} {path}'):
                self.assertEqual(self._call(method, path, payload).status_code, 404)

    def test_other_studio_cannot_see_client(self):
        self._as(4)
        for method, path, payload in self._endpoints():
            with self.subTest(endpoint=f'{method} {path}'):
                self.assertEqual(self._call(method, path, payload).status_code, 404)

    def test_logged_out_gets_401(self):
        self._logout()
        for method, path, payload in self._endpoints():
            with self.subTest(endpoint=f'{method} {path}'):
                self.assertEqual(self._call(method, path, payload).status_code, 401)

    def test_platform_admin_does_not_bypass_crm_scope(self):
        # Admin čte telemetrii; zdravotní data tudy dostupná být nesmí.
        self._as(6)
        for method, path, payload in self._endpoints():
            with self.subTest(endpoint=f'{method} {path}'):
                self.assertEqual(self._call(method, path, payload).status_code, 404)

    def test_same_studio_colleague_can_see_client(self):
        self._as(3)
        r = self.client.get(f'/api/clients/{self.cid}')
        self.assertEqual(r.status_code, 200, r.data[:300])

    def test_note_authorized_through_parent_client_not_own_id(self):
        # Nejčastější místo úniku: potomek autorizovaný podle vlastního id.
        self.assertIsNotNone(self.nid)
        self._as(4)
        self.assertEqual(self.client.delete(f'/api/client-notes/{self.nid}').status_code, 404)
        self._as(1)
        self.assertEqual(self.client.patch(f'/api/client-notes/{self.nid}',
                                           json={'body': 'hacked'}).status_code, 404)

    def test_client_list_never_includes_other_tenants(self):
        self._mk_client_for(1, name='Sólo klient')
        self._mk_client_for(4, name='Klient studia B')
        self._as(2)
        names = [c['name'] for c in self.client.get('/api/clients').get_json()['clients']]
        self.assertIn('Jana Nováková', names)
        self.assertNotIn('Sólo klient', names)
        self.assertNotIn('Klient studia B', names)


class CrmScopeTests(_CrmBase):
    """Počítaná viditelnost vs. denormalizovaný sloupec — tyhle dva testy
    jsou důvod, proč clients nemá studio_id."""

    def test_leaving_studio_revokes_visibility(self):
        cid = self._mk_client_for(2)
        self._as(3)
        self.assertEqual(self.client.get(f'/api/clients/{cid}').status_code, 200)
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM studio_members WHERE artist_id=3')
        conn.commit(); conn.close()
        self.assertEqual(self.client.get(f'/api/clients/{cid}').status_code, 404)

    def test_joining_studio_grants_visibility(self):
        cid = self._mk_client_for(2)
        self._as(1)
        self.assertEqual(self.client.get(f'/api/clients/{cid}').status_code, 404)
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?,1,'member')",
                     (self.studio_a,))
        conn.commit(); conn.close()
        self.assertEqual(self.client.get(f'/api/clients/{cid}').status_code, 200)

    def test_solo_artist_sees_only_own(self):
        own = self._mk_client_for(1, name='Můj klient')
        self._mk_client_for(2, name='Cizí klient')
        self._as(1)
        clients = self.client.get('/api/clients').get_json()['clients']
        self.assertEqual([c['id'] for c in clients], [own])


class ClientAutoLinkTests(_CrmBase):
    """Rezervace zakládá klientský řádek; podruhé už ne."""

    def _mk_slot_and_book(self, artist_id, day_offset, hour):
        import sqlite3, server
        now = server._prague_now_naive()
        start = (now + timedelta(days=day_offset)).replace(hour=hour, minute=0, second=0, microsecond=0)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max, "
            "price_unit, min_duration_hours) VALUES (?,?,?,'free',1000,1000,'hour',1)",
            (artist_id, start.isoformat(), (start + timedelta(hours=8)).isoformat()))
        conn.commit()
        sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        self._as(5)  # client1
        return self.client.post('/api/bookings', json={
            'slot_id': sid, 'booking_start_at': start.isoformat(),
            'duration_hours': 2, 'design_note': 'Vlk'})

    def _client_rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute('SELECT * FROM clients').fetchall()]
        conn.close()
        return rows

    def test_booking_creates_client_row(self):
        r = self._mk_slot_and_book(1, 5, 10)
        self.assertEqual(r.status_code, 200, r.data[:300])
        rows = self._client_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['artist_id'], 1)
        self.assertEqual(rows[0]['user_id'], 5)
        self.assertEqual(rows[0]['acquisition_source'], 'inklink')

    def test_second_booking_same_artist_does_not_duplicate(self):
        self._mk_slot_and_book(1, 5, 10)
        self._mk_slot_and_book(1, 6, 10)
        self.assertEqual(len(self._client_rows()), 1)

    def test_different_artist_gets_own_client_row(self):
        self._mk_slot_and_book(1, 5, 10)
        self._mk_slot_and_book(2, 6, 10)
        self.assertEqual(sorted(r['artist_id'] for r in self._client_rows()), [1, 2])

    def test_partial_unique_index_blocks_duplicate(self):
        import sqlite3
        self._mk_slot_and_book(1, 5, 10)
        conn = sqlite3.connect(self.db)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('INSERT INTO clients (artist_id, user_id, created_by) VALUES (1,5,1)')
            conn.commit()
        conn.close()

    def test_linked_client_contact_comes_from_user_account(self):
        self._mk_slot_and_book(1, 5, 10)
        self._as(1)
        c = self.client.get('/api/clients').get_json()['clients'][0]
        self.assertEqual(c['contact_source'], 'user')
        self.assertEqual(c['email'], 'client1@t.cz')


class TattooRecordTests(_CrmBase):
    """Záznam o tetování je historie práce. Autorizace jde vždy přes klienta;
    booking_id je jinak volný ukazatel do cizích rezervací."""

    def setUp(self):
        super().setUp()
        self.cid = self._mk_client_for(1, user_id=5)
        self._as(1)

    def _mk(self, **kw):
        payload = {'session_date': '2027-03-04', 'body_location': 'předloktí',
                   'style': 'blackwork', 'price_czk': 4500}
        payload.update(kw)
        return self.client.post(f'/api/clients/{self.cid}/tattoo-records', json=payload)

    def test_create_and_read_back_in_detail(self):
        r = self._mk()
        self.assertEqual(r.status_code, 200)
        detail = self.client.get(f'/api/clients/{self.cid}').get_json()
        self.assertEqual(len(detail['tattoo_records']), 1)
        rec = detail['tattoo_records'][0]
        self.assertEqual(rec['body_location'], 'předloktí')
        self.assertEqual(rec['price_czk'], 4500)
        self.assertEqual(rec['artist_id'], 1)

    def test_record_without_booking_is_allowed(self):
        # Práce z doby před InkLinkem — hlavní důvod, proč je booking_id NULLable.
        self.assertEqual(self._mk(booking_id=None).status_code, 200)

    def test_session_date_is_required_and_validated(self):
        r = self.client.post(f'/api/clients/{self.cid}/tattoo-records',
                             json={'body_location': 'ruka'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._mk(session_date='4. 3. 2027').status_code, 400)

    def test_foreign_booking_cannot_be_attached(self):
        # Rezervace artist4 (cizí studio) nesmí jít navázat na mého klienta.
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO bookings (artist_id, client_id, slot_id, status, '
                     'booking_start_at, booking_end_at, duration_hours) '
                     "VALUES (4, 5, 1, 'confirmed', '2027-03-04T10:00', '2027-03-04T12:00', 2)")
        conn.commit()
        bid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        r = self._mk(booking_id=bid)
        self.assertEqual(r.status_code, 400)

    def test_records_are_invisible_across_tenants(self):
        rid = self._mk().get_json()['id']
        for uid in (2, 4, 6):  # jiné studio, cizí studio, platformní admin
            self._as(uid)
            self.assertEqual(
                self.client.patch(f'/api/tattoo-records/{rid}',
                                  json={'style': 'hacked'}).status_code, 404)
            self.assertEqual(
                self.client.delete(f'/api/tattoo-records/{rid}').status_code, 404)
        self._logout()
        self.assertEqual(self.client.delete(f'/api/tattoo-records/{rid}').status_code, 401)

    def test_patch_leaves_unmentioned_fields_alone(self):
        rid = self._mk().get_json()['id']
        r = self.client.patch(f'/api/tattoo-records/{rid}', json={'style': 'fineline'})
        self.assertEqual(r.status_code, 200)
        rec = self.client.get(f'/api/clients/{self.cid}').get_json()['tattoo_records'][0]
        self.assertEqual(rec['style'], 'fineline')
        self.assertEqual(rec['body_location'], 'předloktí')  # nevymazáno
        self.assertEqual(rec['price_czk'], 4500)

    def test_studio_colleague_sees_but_cannot_delete(self):
        # artist2 a artist3 jsou ve studiu A; klient patří artist2.
        cid = self._mk_client_for(2, name='Klient studia')
        self._as(2)
        rid = self.client.post(f'/api/clients/{cid}/tattoo-records',
                               json={'session_date': '2027-05-05'}).get_json()['id']
        self._as(3)
        self.assertEqual(len(self.client.get(f'/api/clients/{cid}')
                             .get_json()['tattoo_records']), 1)
        self.assertEqual(self.client.delete(f'/api/tattoo-records/{rid}').status_code, 403)
        self._as(2)
        self.assertEqual(self.client.delete(f'/api/tattoo-records/{rid}').status_code, 200)


class ClientGdprTests(_CrmBase):
    """Výmaz musí odstranit PII a nechat účetnictví. Nejsledovanější řádek
    je bookings.internal_note — tam tatéři reálně píšou zdravotní údaje."""

    def setUp(self):
        super().setUp()
        import sqlite3
        self.cid = self._mk_client_for(1, user_id=5)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE clients SET phone='777111222', note='má ráda jehly 7RL' "
                     'WHERE id=?', (self.cid,))
        conn.execute('INSERT INTO bookings (artist_id, client_id, slot_id, status, '
                     'booking_start_at, booking_end_at, duration_hours, total_price_cents, '
                     'design_note, internal_note) '
                     "VALUES (1, 5, 1, 'completed', '2027-02-01T10:00', '2027-02-01T13:00', "
                     "3, 900000, 'růže na předloktí', 'alergie na latex, volat po 18:00')")
        conn.commit()
        conn.close()
        self._as(1)
        self.client.post(f'/api/clients/{self.cid}/notes', json={'body': 'přišla pozdě'})
        self.rid = self.client.post(
            f'/api/clients/{self.cid}/tattoo-records',
            json={'session_date': '2027-02-01', 'body_location': 'levé předloktí',
                  'description': 'růže, 3 h', 'price_czk': 9000}).get_json()['id']

    def _rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        out = {
            'client':  dict(conn.execute('SELECT * FROM clients WHERE id=?', (self.cid,)).fetchone()),
            'notes':   conn.execute('SELECT COUNT(*) FROM client_notes WHERE client_id=?',
                                    (self.cid,)).fetchone()[0],
            'record':  dict(conn.execute('SELECT * FROM tattoo_records WHERE id=?',
                                         (self.rid,)).fetchone()),
            'booking': dict(conn.execute('SELECT * FROM bookings WHERE id=1').fetchone()),
        }
        conn.close()
        return out

    def test_export_contains_client_notes_records_and_bookings(self):
        r = self.client.get(f'/api/clients/{self.cid}/export')
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(len(d['notes']), 1)
        self.assertEqual(len(d['tattoo_records']), 1)
        self.assertEqual(len(d['bookings']), 1)

    def test_erase_requires_typed_confirmation(self):
        r = self.client.post(f'/api/clients/{self.cid}/erase', json={'confirm': 'ano'})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self._rows()['notes'], 1)  # nic se nestalo

    def test_erase_scrubs_pii_and_keeps_accounting(self):
        r = self.client.post(f'/api/clients/{self.cid}/erase', json={'confirm': 'VYMAZAT'})
        self.assertEqual(r.status_code, 200)
        rows = self._rows()

        # Klient: PII pryč VČETNĚ user_id (ponechaný odkaz re-identifikuje).
        self.assertIsNone(rows['client']['user_id'])
        self.assertEqual(rows['client']['phone'], '')
        self.assertEqual(rows['client']['note'], '')
        self.assertTrue(rows['client']['anonymized_at'])

        # Poznámky tvrdě smazané.
        self.assertEqual(rows['notes'], 0)

        # Záznam rozdělený: popis těla pryč, účetní kostra zůstala.
        self.assertEqual(rows['record']['body_location'], '')
        self.assertEqual(rows['record']['description'], '')
        self.assertEqual(rows['record']['price_czk'], 9000)
        self.assertEqual(rows['record']['session_date'], '2027-02-01')

        # Rezervace: peníze zůstávají, poznámky ne.
        self.assertEqual(rows['booking']['total_price_cents'], 900000)
        self.assertEqual(rows['booking']['status'], 'completed')
        self.assertEqual(rows['booking']['design_note'], '')
        self.assertEqual(rows['booking']['internal_note'], '')

    def test_erase_is_not_repeatable(self):
        self.client.post(f'/api/clients/{self.cid}/erase', json={'confirm': 'VYMAZAT'})
        r = self.client.post(f'/api/clients/{self.cid}/erase', json={'confirm': 'VYMAZAT'})
        self.assertEqual(r.status_code, 409)

    def test_plain_studio_member_cannot_erase(self):
        cid = self._mk_client_for(2)          # klient patří artist2 (admin studia A)
        self._as(3)                            # artist3 je řadový člen studia A
        self.assertEqual(self.client.get(f'/api/clients/{cid}').status_code, 200)  # vidí
        r = self.client.post(f'/api/clients/{cid}/erase', json={'confirm': 'VYMAZAT'})
        self.assertEqual(r.status_code, 403)   # ale nemaže

    def test_studio_admin_can_erase_colleagues_client(self):
        cid = self._mk_client_for(3)          # klient řadového člena
        self._as(2)                            # admin studia A
        r = self.client.post(f'/api/clients/{cid}/erase', json={'confirm': 'VYMAZAT'})
        self.assertEqual(r.status_code, 200)

    def test_erase_is_invisible_across_tenants(self):
        for uid in (2, 4, 6):
            self._as(uid)
            r = self.client.post(f'/api/clients/{self.cid}/erase', json={'confirm': 'VYMAZAT'})
            self.assertEqual(r.status_code, 404)
        self.assertEqual(self._rows()['notes'], 1)


class ClientMergeTests(_CrmBase):
    """Slučování v1 vyžaduje shodný artist_id — 'čí je pak klient' je otázka
    vlastnictví dat, ne UI."""

    def setUp(self):
        super().setUp()
        self.target = self._mk_client_for(1, name='Jana N.')
        self.source = self._mk_client_for(1, name='Jana Nováková')
        self._as(1)

    def test_children_move_and_source_disappears(self):
        self.client.post(f'/api/clients/{self.source}/notes', json={'body': 'ze zdroje'})
        self.client.post(f'/api/clients/{self.source}/tattoo-records',
                         json={'session_date': '2027-01-01'})
        r = self.client.post(f'/api/clients/{self.target}/merge',
                             json={'source_id': self.source})
        self.assertEqual(r.status_code, 200)
        d = self.client.get(f'/api/clients/{self.target}').get_json()
        self.assertEqual(len(d['notes']), 1)
        self.assertEqual(len(d['tattoo_records']), 1)
        self.assertEqual(self.client.get(f'/api/clients/{self.source}').status_code, 404)

    def test_empty_target_fields_are_filled_from_source(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE clients SET phone='777111222' WHERE id=?", (self.source,))
        conn.commit(); conn.close()
        self.client.post(f'/api/clients/{self.target}/merge', json={'source_id': self.source})
        d = self.client.get(f'/api/clients/{self.target}').get_json()
        self.assertEqual(d['phone'], '777111222')
        self.assertEqual(d['name'], 'Jana N.')  # neprázdné pole cíle vyhrává

    def test_cannot_merge_across_artists(self):
        other = self._mk_client_for(2)
        self._as(2)
        r = self.client.post(f'/api/clients/{other}/merge', json={'source_id': self.target})
        self.assertEqual(r.status_code, 404)   # na cizího klienta ani nevidí

    def test_cannot_merge_two_different_accounts(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE clients SET user_id=5 WHERE id=?', (self.target,))
        conn.execute('UPDATE clients SET user_id=6 WHERE id=?', (self.source,))
        conn.commit(); conn.close()
        r = self.client.post(f'/api/clients/{self.target}/merge', json={'source_id': self.source})
        self.assertEqual(r.status_code, 400)

    def test_cannot_merge_into_self(self):
        r = self.client.post(f'/api/clients/{self.target}/merge', json={'source_id': self.target})
        self.assertEqual(r.status_code, 400)


class I18nKeyTests(unittest.TestCase):
    """Chybějící klíč se v UI projeví jako syrové 'as.langEn' místo textu.
    Vzniká tiše: str.replace() při nesedící kotvě nic nenahradí a nic neohlásí,
    což se při zavádění překladů stalo. Proto to hlídá test, ne oko."""

    @staticmethod
    def _dicts():
        import re
        src = open('public/i18n.js', encoding='utf-8').read()
        i_cs = src.index('    cs: {')
        i_en = src.index('    en: {')
        i_end = src.index('\n  };', i_en)
        keys = lambda b: set(re.findall(r"^\s+'([a-zA-Z0-9_.]+)':", b, re.M))
        return keys(src[i_cs:i_en]), keys(src[i_en:i_end])

    @staticmethod
    def _used(known_prefixes):
        import re, glob
        used = set()
        # I skripty: markup rezervací se přestěhoval do bookings-panel.js,
        # takže by jeho klíče jinak vypadly z kontroly.
        for f in glob.glob('public/*.html') + glob.glob('public/*.js'):
            if f.endswith('i18n.js'):
                continue
            txt = open(f, encoding='utf-8').read()
            if f.endswith('.js'):
                # Markup v JS je uložený jako řetězec, takže uvozovky
                # atributů jsou odescapované — bez tohohle by regexy níž
                # nenašly ani jeden data-i18n.
                txt = txt.replace('\\"', '"')
            used |= set(re.findall(r'data-i18n(?:-html)?="([^"]+)"', txt))
            for attr in re.findall(r'data-i18n-attr="([^"]+)"', txt):
                for pair in attr.split(','):
                    if ':' in pair:
                        used.add(pair.split(':')[0].strip())
            # Dynamické použití — t(cond ? 'a.b' : 'c.d') — chytáme přes
            # literály, jejichž prefix slovník zná.
            for lit in re.findall(r"'([a-zA-Z][a-zA-Z0-9_]*\.[a-zA-Z0-9_.]+)'", txt):
                if lit.split('.')[0] in known_prefixes:
                    used.add(lit)
        return used

    def test_every_used_key_exists_in_both_languages(self):
        cs, en = self._dicts()
        used = self._used({k.split('.')[0] for k in cs | en})
        self.assertTrue(used, 'nenašel jsem žádné klíče — checker je rozbitý')
        self.assertEqual(sorted(used - cs), [], 'klíče chybí v češtině')
        self.assertEqual(sorted(used - en), [], 'klíče chybí v angličtině')

    def test_no_i18n_on_element_with_children(self):
        """apply() dělá el.textContent = t(key), takže data-i18n na prvku,
        který má potomky, je smaže. V rezervacích takhle zmizel span
        s počtem rezervací a loadArtist() pak padal na null. Popisek musí mít
        vlastní span."""
        import re, glob
        bad = []
        for f in glob.glob('public/*.html'):
            txt = open(f, encoding='utf-8').read()
            # Otevírací tag s data-i18n, za ním text a hned vnořený element.
            for m in re.finditer(r'<([a-z0-9]+)[^>]*\sdata-i18n="[^"]*"[^>]*>([^<]*)<([a-z])', txt):
                # Prázdné elementy potomky mít nemůžou — tam regex jen
                # přeskočil na následující tag.
                if m.group(1) in ('meta', 'link', 'br', 'hr', 'img', 'input'):
                    continue
                bad.append(f'{f}: <{m.group(1)} data-i18n=…>{m.group(2).strip()[:30]}<{m.group(3)}…')
        self.assertEqual(bad, [])

    def test_no_duplicate_keys(self):
        """Dvakrát stejný klíč v jednom objektu je tichá chyba: platí
        poslední a rozdíl nikdo nevidí. Vzniklo to při vkládání klíče,
        který ve slovníku už byl."""
        import re
        src = open('public/i18n.js', encoding='utf-8').read()
        i_cs, i_en = src.index('    cs: {'), src.index('    en: {')
        i_end = src.index('\n  };', i_en)
        for name, block in (('cs', src[i_cs:i_en]), ('en', src[i_en:i_end])):
            keys = re.findall(r"^ +'([\w.]+)':", block, re.M)
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            self.assertEqual(dupes, [], f'duplicitní klíče v {name}')

    def test_dictionaries_are_symmetric(self):
        cs, en = self._dicts()
        self.assertEqual(sorted(cs - en), [], 'klíč jen v češtině')
        self.assertEqual(sorted(en - cs), [], 'klíč jen v angličtině')



class OwnProfileAffordanceTests(unittest.TestCase):
    """Vlastní práce nesmí nabízet akce mířené na cizího tatéra.

    Server posílá `is_own` správně, ale tři místa ho nečetla: lightbox ve
    feedu nabízel „Napsat" sám sobě, detail skici totéž a veřejná stránka
    tatéra nabízela rezervaci u sebe sama. Jsou to čistě frontendové
    podmínky, takže je hlídá čtení zdroje — jinak je refaktor tiše smaže."""

    @staticmethod
    def _src(name):
        return open('public/' + name, encoding='utf-8').read()

    def test_feed_lightbox_checks_owner(self):
        src = self._src('index.html')
        self.assertIn("me.username === username", src,
                      'lightbox nezná rozdíl mezi mojí a cizí prací')
        # Rezervovat vlastní skicu nedává smysl.
        self.assertIn('if (isSketch && !isMine)', src)

    def test_sketch_detail_checks_owner(self):
        src = self._src('sketch.html')
        self.assertIn('me && me.username === u.username', src)
        # Tlačítko bylo natvrdo česky, přestože zdrojový jazyk je angličtina.
        self.assertNotIn('> Zpráva</a>', src)

    def test_public_artist_page_checks_owner(self):
        src = self._src('book.html')
        self.assertIn('if (p.is_own)', src,
                      'sdílený odkaz nabízí tatérovi rezervaci u sebe sama')

    def test_profile_hides_follow_on_own_profile(self):
        src = self._src('profile.html')
        i = src.find('id="followBtn"')
        self.assertNotEqual(i, -1, 'followBtn ve zdroji není')
        # Tlačítko musí vzniknout ve větvi pro cizí profil. Kdyby se
        # přesunulo nad ni, tatér by mohl sledovat sám sebe.
        self.assertGreater(src.rfind('} else if (me) {', 0, i),
                           src.rfind('if (profile.is_own) {', 0, i),
                           'follow tlačítko není ve větvi pro cizí profil')



class BookingActionsLiveInCalendarTests(unittest.TestCase):
    """Rezervaci tatér odbaví na jednom místě — v kalendáři.

    Dřív byly stejné akce i v Mých rezervacích, takže každá oprava se
    musela udělat dvakrát a karta měla pět tlačítek pod sebou. Tenhle test
    hlídá, že se nevrátí — a hlavně že se s nimi neztratilo účtování
    doplatku, které v Mých rezervacích jako jediné existovalo."""

    @staticmethod
    def _src(name):
        with open('public/' + name, encoding='utf-8') as f:
            return f.read()

    def test_artist_card_has_single_action(self):
        src = self._src('bookings-panel.js')
        self.assertIn("bk.openInCalendar", src)
        # Zeď tlačítek u tatéra: dokončení a další sezení patří do kalendáře.
        for gone in ('openFollowUp', 'openComplete', 'confirmComplete', 'completeModal'):
            self.assertNotIn(gone, src, f'{gone} zůstalo v Mých rezervacích')

    def test_calendar_sheet_covers_what_was_removed(self):
        src = self._src('calendar.html')
        # Další sezení jede přes stejný výběr termínu jako přesun.
        self.assertIn('submitFollowUp', src)
        self.assertIn('/follow-up', src)
        # Doplatek a hotovost na místě — bez nich by se peníze nedaly zapsat.
        self.assertIn("num('bsOnsite')", src)
        self.assertIn("num('bsBalance')", src)
        # Selhání doplatku se nesmí spolknout: rezervace je dokončená,
        # ale klientovi žádný odkaz nedorazil.
        self.assertIn('j.balance_charge.error', src)

    def test_calendar_deep_link_is_parsed(self):
        src = self._src('calendar.html')
        self.assertIn('pendingDeepLink', src)
        # Bez data v odkazu bychom nevěděli, který týden načíst.
        self.assertIn('weekStart = startOfWeek(new Date(deep.date', src)

    def test_client_keeps_its_own_actions(self):
        """Klient kalendář nemá — jemu se tlačítka brát nesmí."""
        src = self._src('bookings-panel.js')
        for kept in ('openReschedule', 'cancelBooking', 'openEditBook', 'openRefund'):
            self.assertIn(kept, src)



class BookingsPanelTests(unittest.TestCase):
    """Rezervace se ze samostatné stránky přestěhovaly na profil.

    URL /my-bookings musí přežít jako přesměrování: míří na ni odkazy
    v už odeslaných e-mailech, in-app notifikace i zástupce v manifestu."""

    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    @staticmethod
    def _src(name):
        with open('public/' + name, encoding='utf-8') as f:
            return f.read()

    def test_old_url_redirects_to_the_profile_tab(self):
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO users (username, display_name, password_hash, email) '
                     "VALUES ('inker','Inker',?,'i@t.cz')",
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'inker', 'password': 'pass1234'})
        r = self.client.get('/my-bookings')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/profile/inker#bookings', r.headers['Location'])

    def test_anonymous_is_sent_to_login(self):
        r = self.client.get('/my-bookings')
        self.assertEqual(r.status_code, 302)
        self.assertIn('/login', r.headers['Location'])

    def test_panel_styles_are_scoped(self):
        """Profil má vlastní .tabs, .tab i .empty. Nezaprefixovaný styl
        panelu by je přebarvil — proto se hlídá, že prefix nezmizel."""
        src = self._src('bookings-panel.js')
        i = src.index('const CSS = ')
        css = src[i:src.index('\n', i)]
        for sel in ('.tabs{', '.tab{', '.empty{', '.b-row{'):
            self.assertNotIn('\\n' + sel, css, f'{sel} není zakotvené v .il-bookings')
        self.assertIn('.il-bookings .b-row{', css)

    def test_panel_does_not_touch_page_globals(self):
        """Profil deklaruje vlastní `me`, `fmtDate` i `escapeHtml`. Druhá
        deklarace v globálu shodí celou stránku, proto je panel v IIFE."""
        src = self._src('bookings-panel.js')
        self.assertIn('window.InkLinkBookings = (function () {', src)
        self.assertTrue(src.rstrip().endswith('})();'))

    def test_profile_hosts_the_tab(self):
        src = self._src('profile.html')
        self.assertIn('id="tab-bookings"', src)
        self.assertIn('InkLinkBookings.mount', src)
        # Cizí rezervace na profil nepatří.
        self.assertIn("if (profile.is_own) {\n    document.getElementById('bookingsTab')", src)

    def test_house_icon_is_the_feed(self):
        """Domeček znamená feed. Tatérovi pod ním chvíli byly rezervace."""
        src = self._src('mobile-nav.js')
        i = src.index("ico: 'i-home'")
        line_start = src.rindex('\n', 0, i)
        self.assertIn("href: '/'", src[line_start:i])
        self.assertNotIn("{ href: '/my-bookings'", src)



class SketchSizesTests(_Sprint2Base):
    """Tatér může u jednoho návrhu nabídnout tři velikosti, každou za svou cenu.

    Klíčové je, že se vybraná varianta propíše do rezervace. Kdyby se cena
    tiše brala z položky, klient by za velké tetování zaplatil cenu malého —
    a naopak by tatér přišel o peníze."""

    def _mk_item(self, artist_id=1, kind='sketch'):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO portfolio_items (user_id, image, caption, kind) "
                     "VALUES (?, 'x.png', 'Vlk', ?)", (artist_id, kind))
        conn.commit()
        iid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.close()
        return iid

    def _login_artist(self):
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    SIZES = [{'size_label': 'small',  'price_kc': 2500, 'estimated_hours': 2},
             {'size_label': 'medium', 'price_kc': 4000, 'estimated_hours': 3},
             {'size_label': 'large',  'price_kc': 6500, 'estimated_hours': 5}]

    def test_price_list_sets_the_from_price(self):
        """price_kc na položce je "od" cena — čte ji řazení feedu i OG
        obrázky, takže se nesmí rozejít s ceníkem."""
        iid = self._mk_item()
        self._login_artist()
        r = self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        self.assertEqual(r.status_code, 200)
        item = r.get_json()['item']
        self.assertEqual(len(item['sizes']), 3)
        self.assertEqual(item['price_kc'], 2500)
        self.assertEqual(item['estimated_hours'], 2)
        # Pořadí je vždy S→M→L, ne pořadí vložení.
        self.assertEqual([v['size_label'] for v in item['sizes']],
                         ['small', 'medium', 'large'])

    def test_rewrite_replaces_the_whole_list(self):
        iid = self._mk_item()
        self._login_artist()
        self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        r = self.client.patch(f'/api/portfolio/{iid}',
                              json={'sizes': [self.SIZES[2]]})
        item = r.get_json()['item']
        self.assertEqual([v['size_label'] for v in item['sizes']], ['large'])
        self.assertEqual(item['price_kc'], 6500)

    def test_half_filled_row_is_rejected(self):
        iid = self._mk_item()
        self._login_artist()
        r = self.client.patch(f'/api/portfolio/{iid}', json={
            'sizes': [{'size_label': 'small', 'price_kc': 2500}]})
        self.assertEqual(r.status_code, 400)

    def test_unknown_size_is_rejected(self):
        iid = self._mk_item()
        self._login_artist()
        r = self.client.patch(f'/api/portfolio/{iid}', json={
            'sizes': [{'size_label': 'obri', 'price_kc': 1, 'estimated_hours': 1}]})
        self.assertEqual(r.status_code, 400)

    def test_stranger_cannot_touch_the_list(self):
        iid = self._mk_item()   # patří tatérovi, přihlášený je klient
        r = self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        self.assertEqual(r.status_code, 404)

    def test_booking_takes_price_and_length_from_the_chosen_size(self):
        iid = self._mk_item()
        self._login_artist()
        self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        start = self._day_at(10, 9)
        sid = self._mk_slot(start, start + timedelta(hours=8))
        _register(self.client, 'client2')
        r = self.client.post('/api/bookings', json={
            'slot_id': sid, 'design_note': 'vlk na predlokti',
            'portfolio_item_id': iid, 'size_label': 'large'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        b = r.get_json()
        self.assertEqual(b['total_price_kc'], 6500)
        self.assertEqual(b['duration_hours'], 5)

    def test_booking_without_a_size_is_refused(self):
        """Bez zvolené velikosti cena neexistuje. Tiché spadnutí na "od"
        cenu by klientovi naúčtovalo malé tetování za velké."""
        iid = self._mk_item()
        self._login_artist()
        self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        start = self._day_at(11, 9)
        sid = self._mk_slot(start, start + timedelta(hours=8))
        _register(self.client, 'client3')
        r = self.client.post('/api/bookings', json={
            'slot_id': sid, 'design_note': 'vlk na predlokti',
            'portfolio_item_id': iid})
        self.assertEqual(r.status_code, 400)
        # Chyba nese ceník, ať ho klient nemusí dohledávat.
        self.assertEqual(len(r.get_json()['sizes']), 3)

    def test_item_without_a_list_keeps_its_single_price(self):
        """Starší položky mají jen price_kc. Ty musí fungovat dál beze změny."""
        import sqlite3
        iid = self._mk_item()
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE portfolio_items SET price_kc=3000, estimated_hours=2 WHERE id=?',
                     (iid,))
        conn.commit(); conn.close()
        start = self._day_at(12, 9)
        sid = self._mk_slot(start, start + timedelta(hours=8))
        r = self.client.post('/api/bookings', json={
            'slot_id': sid, 'design_note': 'vlk na predlokti',
            'portfolio_item_id': iid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['total_price_kc'], 3000)

    def test_deleting_the_item_takes_the_list_with_it(self):
        """SQLite bez PRAGMA foreign_keys FK nevynucuje, takže potomky
        musí uklidit kód — jinak ceník přežije svou položku."""
        import sqlite3
        iid = self._mk_item()
        self._login_artist()
        self.client.patch(f'/api/portfolio/{iid}', json={'sizes': self.SIZES})
        self.client.delete(f'/api/portfolio/{iid}')
        conn = sqlite3.connect(self.db)
        left = conn.execute('SELECT COUNT(*) FROM portfolio_item_sizes WHERE item_id=?',
                            (iid,)).fetchone()[0]
        conn.close()
        self.assertEqual(left, 0)


class DesignRequestTests(_Sprint2Base):
    """Poptávka vlastního návrhu. Nezakládá frontu ani rezervaci —
    složí strukturovanou zprávu a pošle tatérovi e-mail."""

    def _ask(self, **over):
        body = {'artist': 'artist1', 'motif': 'Geometricky vlk, fineline',
                'placement': 'predlokti', 'size_label': 'medium',
                'budget_kc': 5000, 'timing': 'na jare'}
        body.update(over)
        return self.client.post('/api/design-requests', json=body)

    def test_request_lands_as_a_message(self):
        import sqlite3
        r = self._ask()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['thread'], '/messages?user=artist1')
        conn = sqlite3.connect(self.db)
        m = conn.execute('SELECT sender_id, receiver_id, content FROM messages '
                         'ORDER BY id DESC LIMIT 1').fetchone()
        conn.close()
        self.assertEqual((m[0], m[1]), (2, 1))
        for part in ('Custom design request', 'Motif:', 'Placement:', 'Size:', 'Budget:'):
            self.assertIn(part, m[2])

    def test_optional_fields_are_left_out(self):
        import sqlite3
        self._ask(placement='', budget_kc=None, timing='', size_label='')
        conn = sqlite3.connect(self.db)
        content = conn.execute('SELECT content FROM messages ORDER BY id DESC LIMIT 1').fetchone()[0]
        conn.close()
        self.assertIn('Motif:', content)
        for gone in ('Placement:', 'Size:', 'Budget:', 'Timing:'):
            self.assertNotIn(gone, content)

    def test_artist_gets_a_notification(self):
        import sqlite3
        self._ask()
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT user_id, type FROM notifications "
                         "WHERE type='design_request'").fetchone()
        conn.close()
        self.assertEqual(n[0], 1)

    def test_empty_motif_is_refused(self):
        self.assertEqual(self._ask(motif='vlk').status_code, 400)

    def test_unknown_artist_is_404(self):
        self.assertEqual(self._ask(artist='nikdo').status_code, 404)

    def test_unknown_size_is_refused(self):
        self.assertEqual(self._ask(size_label='obri').status_code, 400)

    def test_cannot_ask_yourself(self):
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})
        self.assertEqual(self._ask().status_code, 400)

    def test_anonymous_is_refused(self):
        self.client.post('/api/logout')
        self.assertEqual(self._ask().status_code, 401)

    # — referenční fotky —

    @staticmethod
    def _png(name='ref.png'):
        import io
        return (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), name)

    def _ask_multipart(self, files, **over):
        data = {'artist': 'artist1', 'motif': 'Geometricky vlk, fineline',
                'placement': 'predlokti', 'size_label': 'medium'}
        data.update(over)
        data['photos'] = files
        return self.client.post('/api/design-requests', data=data,
                                content_type='multipart/form-data')

    def test_references_land_as_image_messages(self):
        """Reference jdou stejnou cestou jako obrázková zpráva — vlastní
        úložiště by znamenalo druhou cestu k témuž a v konverzaci by
        chyběly. Pořadí je text → fotky, ať vlákno čte 'co chci' a pak
        'jak to má vypadat'."""
        import sqlite3
        r = self._ask_multipart([self._png('a.png'), self._png('b.png')])
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conn = sqlite3.connect(self.db)
        rows = conn.execute('SELECT content_type, image FROM messages ORDER BY id').fetchall()
        conn.close()
        self.assertEqual([x[0] for x in rows], ['text', 'image', 'image'])
        self.assertTrue(all(x[1] for x in rows[1:]))

    def test_more_than_three_references_is_refused(self):
        r = self._ask_multipart([self._png(f'{i}.png') for i in range(4)])
        self.assertEqual(r.status_code, 400)

    def test_non_image_reference_is_refused(self):
        import io
        r = self._ask_multipart([(io.BytesIO(b'not an image'), 'virus.exe')])
        self.assertEqual(r.status_code, 400)

    def test_request_without_references_still_works(self):
        """Bez fotek se posílá JSON — ta cesta nesmí přestat fungovat."""
        self.assertEqual(self._ask().status_code, 200)


class BookingOfferTests(_Sprint2Base):
    """Tatér se domluví v chatu na custom práci a pošle konkrétní termín
    s cenou. Klient přijme → vznikne běžná rezervace se zálohou.

    Slot zabere až přijetí. Držet ho od odeslání by z každé zapomenuté
    nabídky udělalo díru v kalendáři a nikdo by ji neuklidil."""

    def setUp(self):
        super().setUp()
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.start = self._day_at(9, 10)
        self.slot  = self._mk_slot(self.start, self.start + timedelta(hours=8))

    def _as_artist(self):
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    def _as_client(self):
        self.client.post('/api/login', json={'username': 'client1', 'password': 'pass1234'})

    def _offer(self, **over):
        body = {'client_id': 2, 'slot_id': self.slot,
                'booking_start_at': self.start.isoformat(),
                'duration_hours': 3, 'price_kc': 7500, 'note': 'Custom vlk'}
        body.update(over)
        return self.client.post('/api/booking-offers', json=body)

    def test_offer_reaches_the_thread(self):
        import sqlite3
        self._as_artist()
        r = self._offer()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        conn = sqlite3.connect(self.db)
        m = conn.execute('SELECT sender_id, receiver_id, content_type, offer_id '
                         'FROM messages ORDER BY id DESC LIMIT 1').fetchone()
        conn.close()
        self.assertEqual((m[0], m[1], m[2]), (1, 2, 'offer'))
        self.assertEqual(m[3], r.get_json()['offer_id'])

    def test_offer_does_not_block_the_slot(self):
        """Nabídka je nabídka. Kdyby držela čas, každá zapomenutá by ubrala
        z kalendáře a nikdo by ji neuklidil."""
        self._as_artist()
        self._offer()
        self._as_client()
        # Někdo jiný si stejný čas pořád může vzít běžnou cestou.
        r = self.client.post('/api/bookings', json={
            'slot_id': self.slot, 'design_note': 'jiny motiv',
            'booking_start_at': self.start.isoformat(), 'duration_hours': 3})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_accepting_creates_a_booking_at_the_agreed_price(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        self._as_client()
        r = self.client.post('/api/bookings', json={'offer_id': oid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        b = r.get_json()
        self.assertEqual(b['total_price_kc'], 7500)
        self.assertEqual(b['duration_hours'], 3)

    def test_accepted_offer_records_the_booking(self):
        import sqlite3
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        self._as_client()
        bid = self.client.post('/api/bookings', json={'offer_id': oid}).get_json()['id']
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT status, booking_id FROM booking_offers WHERE id=?',
                           (oid,)).fetchone()
        conn.close()
        self.assertEqual(row, ('accepted', bid))

    def test_offer_cannot_be_accepted_twice(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        self._as_client()
        self.client.post('/api/bookings', json={'offer_id': oid})
        r = self.client.post('/api/bookings', json={'offer_id': oid})
        self.assertEqual(r.status_code, 409)

    def test_only_the_addressee_can_accept(self):
        """404, ne 403 — jinak by se dalo hádáním id zjistit, že nabídka
        existuje."""
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        r = self.client.post('/api/bookings', json={'offer_id': oid})   # pořád tatér
        self.assertEqual(r.status_code, 404)

    def test_client_declines(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        self._as_client()
        r = self.client.post(f'/api/booking-offers/{oid}/decline')
        self.assertEqual(r.get_json()['status'], 'declined')
        self.assertEqual(self.client.post('/api/bookings', json={'offer_id': oid}).status_code, 409)

    def test_artist_cancels(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        r = self.client.post(f'/api/booking-offers/{oid}/decline')
        self.assertEqual(r.get_json()['status'], 'cancelled')

    def test_stranger_sees_nothing(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        _register(self.client, 'kolemjdouci')
        self.assertEqual(self.client.post(f'/api/booking-offers/{oid}/decline').status_code, 404)

    def test_new_offer_cancels_the_previous_one(self):
        """Platí ta, na které jste se domluvili naposled. Jinak by klient
        mohl přijmout tu starou a levnější."""
        self._as_artist()
        first = self._offer().get_json()['offer_id']
        self._offer(price_kc=9000)
        self._as_client()
        self.assertEqual(self.client.post('/api/bookings', json={'offer_id': first}).status_code, 409)

    def test_offer_must_fit_the_block(self):
        self._as_artist()
        self.assertEqual(self._offer(duration_hours=12).status_code, 400)

    def test_offer_cannot_collide(self):
        """Nabízet obsazený čas znamená slíbit něco, co při přijetí spadne."""
        self._as_client()
        self.client.post('/api/bookings', json={
            'slot_id': self.slot, 'design_note': 'uz zabrano',
            'booking_start_at': self.start.isoformat(), 'duration_hours': 3})
        self._as_artist()
        self.assertEqual(self._offer().status_code, 409)

    def test_offer_needs_a_price(self):
        self._as_artist()
        self.assertEqual(self._offer(price_kc=0).status_code, 400)

    def test_offer_only_from_own_slot(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO users (username, display_name, password_hash, email, is_artist) "
                     "VALUES ('artist2','Artist Two','x','a2@t.cz',1)")
        conn.commit(); conn.close()
        other_slot = self._mk_slot(self.start, self.start + timedelta(hours=8), artist_id=3)
        self._as_artist()
        self.assertEqual(self._offer(slot_id=other_slot).status_code, 404)

    def test_thread_carries_the_offer(self):
        self._as_artist()
        oid = self._offer().get_json()['offer_id']
        self._as_client()
        msgs = self.client.get('/api/messages/1').get_json()['messages']
        offer = next(m['offer'] for m in msgs if m['offer'])
        self.assertEqual(offer['id'], oid)
        self.assertEqual(offer['status'], 'pending')
        self.assertFalse(offer['mine'])
        self.assertEqual(offer['price_kc'], 7500)

    def test_anonymous_cannot_offer(self):
        self.client.post('/api/logout')
        self.assertEqual(self._offer().status_code, 401)

    # — termín vytvořený tatérem —

    def _offer_new(self, **over):
        """Nabídka bez slot_id — termín vznikne rovnou z ní."""
        body = {'client_id': 2,
                'booking_start_at': self._day_at(20, 11).isoformat(),
                'duration_hours': 5, 'price_kc': 12000, 'note': 'Celodenní custom'}
        body.update(over)
        return self.client.post('/api/booking-offers', json=body)

    def _slots(self, **where):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        sql = 'SELECT * FROM slots'
        if where:
            sql += ' WHERE ' + ' AND '.join(f'{k}=?' for k in where)
        rows = conn.execute(sql, tuple(where.values())).fetchall()
        conn.close()
        return rows

    def test_artist_can_create_the_date_with_the_offer(self):
        self._as_artist()
        r = self._offer_new()
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        made = self._slots(is_private=1)
        self.assertEqual(len(made), 1)
        # Blok je přesně na délku sezení, ne na celý den.
        self.assertEqual(made[0]['start_at'], self._day_at(20, 11).isoformat())
        self.assertEqual(made[0]['end_at'], self._day_at(20, 16).isoformat())

    def test_created_date_is_not_offered_publicly(self):
        """Kdyby se soukromý termín objevil na profilu, vzal by ho mezitím
        někdo jiný a tatér by slíbil čas, který už nemá."""
        self._as_artist()
        self._offer_new()
        self.client.post('/api/logout')
        public = self.client.get('/api/profile/artist1').get_json()['slots']
        self.assertNotIn(self._day_at(20, 11).isoformat(), [s['start_at'] for s in public])

    def test_declined_offer_takes_its_date_with_it(self):
        """Termín vyrobený kvůli nabídce nemá bez ní důvod existovat."""
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._as_client()
        self.client.post(f'/api/booking-offers/{oid}/decline')
        self.assertEqual(self._slots(is_private=1), [])

    def test_accepted_offer_keeps_its_date(self):
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._as_client()
        r = self.client.post('/api/bookings', json={'offer_id': oid})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['total_price_kc'], 12000)
        self.assertEqual(len(self._slots(is_private=1)), 1)

    def test_created_date_respects_blocked_time(self):
        self._as_artist()
        day = self._day_at(20, 8)
        self.client.post('/api/blocked-time', json={
            'start_at': day.isoformat(),
            'end_at': (day + timedelta(hours=12)).isoformat(),
            'reason': 'dovolená'})
        self.assertEqual(self._offer_new().status_code, 409)

    def test_created_date_cannot_collide_with_a_booking(self):
        self._as_client()
        self.client.post('/api/bookings', json={
            'slot_id': self.slot, 'design_note': 'uz zabrano',
            'booking_start_at': self.start.isoformat(), 'duration_hours': 3})
        self._as_artist()
        r = self._offer_new(booking_start_at=self.start.isoformat(), duration_hours=2)
        self.assertEqual(r.status_code, 409)

    def test_created_date_cannot_be_in_the_past(self):
        self._as_artist()
        past = (self._now() - timedelta(days=1)).replace(microsecond=0)
        self.assertEqual(self._offer_new(booking_start_at=past.isoformat()).status_code, 400)

    def test_offer_needs_a_slot_or_a_date(self):
        self._as_artist()
        r = self.client.post('/api/booking-offers', json={
            'client_id': 2, 'duration_hours': 3, 'price_kc': 5000})
        self.assertEqual(r.status_code, 400)

    def test_bad_client_leaves_no_orphan_date(self):
        """Klienta ověřujeme dřív, než vyrobíme termín — jinak by po
        neplatném id zůstal v kalendáři osiřelý blok."""
        self._as_artist()
        self.assertEqual(self._offer_new(client_id=9999).status_code, 404)
        self.assertEqual(self._slots(is_private=1), [])

    # — platnost nabídky —

    def _age_offer(self, offer_id, days):
        """Posune platnost do minulosti, jako by nabídka ležela `days` dní."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE booking_offers SET expires_at=? WHERE id=?',
                     ((self._now() - timedelta(days=days)).isoformat(), offer_id))
        conn.commit(); conn.close()

    def test_private_date_does_not_tick_the_onboarding_step(self):
        """Soukromý termín není "vypsaný termín". Jinak by si tatér
        odškrtl krok onboardingu, aniž by veřejně cokoliv nabídl."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM slots')
        conn.commit(); conn.close()
        self._as_artist()
        self._offer_new()
        steps = {it['key']: it for it in self.client.get('/api/me/checklist').get_json()['items']}
        self.assertFalse(steps['slot']['done'])

    def test_offer_is_valid_for_a_week(self):
        import sqlite3
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        conn = sqlite3.connect(self.db)
        exp = conn.execute('SELECT expires_at FROM booking_offers WHERE id=?', (oid,)).fetchone()[0]
        conn.close()
        from datetime import datetime as _dt
        delta = (_dt.fromisoformat(exp) - self._now()).days
        self.assertEqual(delta, 7 - 1)   # 6 celých dní + zbytek dneška

    def test_validity_never_outlives_the_date_itself(self):
        """Nabídka na termín za tři dny nemůže platit týden."""
        import sqlite3
        self._as_artist()
        soon = self._day_at(3, 12)
        oid = self._offer_new(booking_start_at=soon.isoformat()).get_json()['offer_id']
        conn = sqlite3.connect(self.db)
        exp = conn.execute('SELECT expires_at FROM booking_offers WHERE id=?', (oid,)).fetchone()[0]
        conn.close()
        self.assertEqual(exp, soon.isoformat())

    def test_expired_offer_cannot_be_accepted(self):
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._age_offer(oid, 1)
        self._as_client()
        r = self.client.post('/api/bookings', json={'offer_id': oid})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()['status'], 'expired')

    def test_expired_offer_releases_its_date(self):
        """Mrtvý blok má zmizet přesně ve chvíli, kdy se na kalendář někdo
        dívá — proto úklid běží při čtení, ne cronem."""
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._age_offer(oid, 1)
        self.assertEqual(len(self._slots(is_private=1)), 1)
        self.client.get('/api/me/slots')          # tatér otevře kalendář
        self.assertEqual(self._slots(is_private=1), [])
        import sqlite3
        conn = sqlite3.connect(self.db)
        st = conn.execute('SELECT status FROM booking_offers WHERE id=?', (oid,)).fetchone()[0]
        conn.close()
        self.assertEqual(st, 'expired')

    def test_expired_offer_keeps_a_booked_date(self):
        """Když už na termínu sedí rezervace, propadlá nabídka ho nesmí vzít."""
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._as_client()
        self.client.post('/api/bookings', json={'offer_id': oid})
        self._age_offer(oid, 1)
        self._as_artist()
        self.client.get('/api/me/slots')
        self.assertEqual(len(self._slots(is_private=1)), 1)

    def test_thread_shows_the_offer_as_expired(self):
        self._as_artist()
        oid = self._offer_new().get_json()['offer_id']
        self._age_offer(oid, 1)
        self._as_client()
        msgs = self.client.get('/api/messages/1').get_json()['messages']
        offer = next(m['offer'] for m in msgs if m['offer'])
        self.assertEqual(offer['status'], 'expired')




class MessageValidationTests(_Sprint2Base):
    """Zpráva se ukládala komukoliv, i neexistujícímu id.

    Vrátila ok:true, řádek v databázi vznikl a pak zmizel — výpis
    konverzací ho odfiltruje JOINem na users. Monolog sám se sebou se
    v tom výpisu naopak objevil jako plnohodnotná konverzace."""

    def test_recipient_must_exist(self):
        import sqlite3
        r = self.client.post('/api/messages/999999', json={'content': 'do prazdna'})
        self.assertEqual(r.status_code, 404)
        conn = sqlite3.connect(self.db)
        n = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
        conn.close()
        self.assertEqual(n, 0, 'zpráva se uložila i tak')

    def test_cannot_message_yourself(self):
        r = self.client.post('/api/messages/2', json={'content': 'monolog'})
        self.assertEqual(r.status_code, 400)

    def test_normal_message_still_works(self):
        r = self.client.post('/api/messages/1', json={'content': 'ahoj'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_photo_has_a_size_limit(self):
        """Bez stropu projde cokoliv až do MAX_CONTENT_LENGTH (500 MB) —
        rovnou do úložiště a rovnou do vlákna, které se pak nenačte."""
        import io, server
        big = io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * (server.MESSAGE_IMAGE_MAX_BYTES + 1))
        r = self.client.post('/api/messages/1', data={'image': (big, 'huge.png')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

    def test_small_photo_passes(self):
        import io
        small = io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 512)
        r = self.client.post('/api/messages/1', data={'image': (small, 'ok.png')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_photo_to_a_stranger_is_refused(self):
        import io
        img = io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 64)
        r = self.client.post('/api/messages/999999', data={'image': (img, 'a.png')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 404)


class SharedScriptVersionTests(unittest.TestCase):
    """Sdílené skripty musí mít všude stejnou verzi.

    Verze se udržovaly ručně a rozešly se: mobile-nav.js měl na osmi
    stránkách v=14, na dvou v=15 a na jedné žádnou; i18n.js byl na osmi
    stránkách bez verze. Obojí se přitom měnilo, takže vracející se
    uživatel dostal na většině stránek starou lištu a starý slovník —
    a na kterých, to záviselo na tom, kudy chodil."""

    SHARED = ('i18n.js', 'mobile-nav.js', 'notifs.js', 'icons.js', 'nav-avatar.js',
              'ink-trail.js', 'cookie-consent.js', 'native.js', 'legal.js',
              'bookings-panel.js')

    def _refs(self):
        import re, glob
        found = {}
        for f in glob.glob('public/*.html'):
            src = open(f, encoding='utf-8').read()
            for name, ver in re.findall(r'src="/([a-z0-9-]+\.js)(\?v=\d+)?"', src):
                if name in self.SHARED:
                    found.setdefault(name, set()).add(ver or '(bez verze)')
        return found

    def test_one_version_per_script(self):
        for name, versions in self._refs().items():
            self.assertEqual(len(versions), 1,
                             f'{name} má napříč stránkami víc verzí: {sorted(versions)}')

    def test_nothing_is_unversioned(self):
        """Bez verze prohlížeč drží starou kopii, dokud si ji sám nezahodí."""
        for name, versions in self._refs().items():
            self.assertNotIn('(bez verze)', versions, f'{name} je někde bez verze')


class DynamicTranslationTests(unittest.TestCase):
    """Půlka UI vzniká až po načtení dat (profil, seznamy, modaly).

    apply() při startu ty uzly ještě nevidí, takže 54 klíčů napříč devíti
    stránkami zůstávalo anglicky, dokud uživatel ručně nepřepnul jazyk.
    Řeší to observer v i18n.js — volat apply() po každém renderu je
    křehké, na desátý render se zapomene."""

    def test_observer_translates_new_nodes(self):
        src = open('public/i18n.js', encoding='utf-8').read()
        self.assertIn('MutationObserver', src)
        self.assertIn('translateTree', src)
        # Bez téhle pojistky by nastavení textContent spustilo observer
        # znovu na vlastní textový uzel — nekonečná smyčka.
        self.assertIn("node.nodeType !== 1", src)

    def test_apply_and_observer_share_one_path(self):
        """Dvě různé implementace překladu by se rozešly."""
        src = open('public/i18n.js', encoding='utf-8').read()
        self.assertEqual(src.count('function applyToEl'), 1)
        self.assertIn('document.querySelectorAll(I18N_SEL).forEach(applyToEl)', src)


class PremiumGateTests(_Sprint2Base):
    """Premium přidává, nikdy neubírá.

    Denní práce tatéra — kalendář, rezervace, zprávy, nabídky — musí
    zůstat celá zdarma. Za peníze je jen to, co otevře jednou za měsíc."""

    PAID = ('/api/me/accounting/export', '/api/me/stats')
    FREE = ('/api/me/slots', '/api/me/bookings/artist', '/api/me/calendar',
            '/api/messages/conversations', '/api/me/earnings', '/api/clients')

    def _as_artist(self, premium=False):
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        if premium:
            conn.execute('UPDATE users SET premium_until=? WHERE id=1',
                         ((self._now() + timedelta(days=30)).isoformat(),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    def test_daily_work_stays_free(self):
        self._as_artist(premium=False)
        for path in self.FREE:
            r = self.client.get(path)
            self.assertNotEqual(r.status_code, 402, f'{path} se zamklo za paywall')

    def test_premium_features_are_gated(self):
        """402, ne 403: 403 znamená 'nemáš právo', tohle znamená
        'ještě nezaplaceno' a frontend na to umí nabídnout předplatné."""
        self._as_artist(premium=False)
        for path in self.PAID:
            r = self.client.get(path)
            self.assertEqual(r.status_code, 402, path)
            self.assertTrue(r.get_json().get('premium_required'), path)

    def test_premium_opens_them(self):
        self._as_artist(premium=True)
        for path in self.PAID:
            self.assertEqual(self.client.get(path).status_code, 200, path)

    def test_expired_premium_closes_again(self):
        import sqlite3
        self._as_artist(premium=True)
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET premium_until=? WHERE id=1',
                     ((self._now() - timedelta(days=1)).isoformat(),))
        conn.commit(); conn.close()
        self.assertEqual(self.client.get('/api/me/stats').status_code, 402)

    def test_anonymous_gets_401_not_402(self):
        """Nepřihlášenému nemá smysl nabízet předplatné."""
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/me/stats').status_code, 401)

    def test_me_reports_premium(self):
        self._as_artist(premium=True)
        me = self.client.get('/api/me').get_json()
        self.assertTrue(me['premium'])
        self.assertTrue(me['premium_until'])


class AccountingExportTests(_Sprint2Base):
    """Export pro účetní. Čísla musí sedět s tím, co vidí tatér ve
    Výdělcích — dvě různá čísla o stejných penězích jsou horší než žádná."""

    def setUp(self):
        super().setUp()
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=?, premium_until=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),
                      (self._now() + timedelta(days=30)).isoformat()))
        conn.commit(); conn.close()
        start = self._day_at(5, 10)
        slot = self._mk_slot(start, start + timedelta(hours=8))
        self.client.post('/api/bookings', json={
            'slot_id': slot, 'design_note': 'vlk na predlokti',
            'booking_start_at': start.isoformat(), 'duration_hours': 3})
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})
        self.day = start.date().isoformat()

    def test_csv_has_bom_and_semicolons(self):
        """Český Excel jinak rozhodí sloupce i diakritiku."""
        r = self.client.get(f'/api/me/accounting/export?from={self.day}&to={self.day}')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith('﻿'))
        self.assertIn(';', body.splitlines()[0])
        self.assertIn('Čistý příjem', body)

    def test_row_and_total_are_present(self):
        r = self.client.get(f'/api/me/accounting/export?from={self.day}&to={self.day}&format=json')
        rows = r.get_json()['rows']
        self.assertEqual(len(rows), 1)
        row = rows[0]
        # Čistý příjem = co reálně zůstalo po provizi a vrácení.
        self.assertAlmostEqual(
            row['net'],
            row['deposit'] + row['balance_inklink'] + row['onsite']
            - row['refunded'] - row['commission'], places=2)

    def test_range_outside_returns_nothing(self):
        r = self.client.get('/api/me/accounting/export?from=2020-01-01&to=2020-01-31&format=json')
        self.assertEqual(r.get_json()['rows'], [])

    def test_backwards_range_is_refused(self):
        r = self.client.get('/api/me/accounting/export?from=2026-12-01&to=2026-01-01')
        self.assertEqual(r.status_code, 400)

    def test_only_own_bookings(self):
        """Export cizích peněz je to nejhorší, co může účetní sestava udělat."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO users (username, display_name, password_hash, email, is_artist) "
                     "VALUES ('artist2','Artist Two','x','a2@t.cz',1)")
        conn.execute('UPDATE bookings SET artist_id=3')
        conn.commit(); conn.close()
        r = self.client.get(f'/api/me/accounting/export?from={self.day}&to={self.day}&format=json')
        self.assertEqual(r.get_json()['rows'], [])


class PremiumStatsTests(_Sprint2Base):
    def setUp(self):
        super().setUp()
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=?, premium_until=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),
                      (self._now() + timedelta(days=30)).isoformat()))
        conn.commit(); conn.close()
        start = self._day_at(6, 14)
        slot = self._mk_slot(start, start + timedelta(hours=8))
        self.client.post('/api/bookings', json={
            'slot_id': slot, 'design_note': 'test', 'booking_start_at': start.isoformat(),
            'duration_hours': 2})
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    def test_shape(self):
        d = self.client.get('/api/me/stats').get_json()
        for key in ('totals', 'by_month', 'weekday', 'by_hour', 'sketches'):
            self.assertIn(key, d)
        self.assertEqual(len(d['weekday']), 7)
        self.assertEqual(d['totals']['bookings'], 1)

    def test_cancelled_share_needs_a_denominator(self):
        """Absolutní počet zrušených nic neříká; podíl ano."""
        d = self.client.get('/api/me/stats').get_json()
        self.assertIn('cancelled_pct', d['totals'])
        self.assertEqual(d['totals']['cancelled_pct'], 0.0)


class SchemaOrderTests(unittest.TestCase):
    """add_col na tabulku, která ještě neexistuje, tiše propadne.

    Chyba se pak ukáže až za běhu, na produkci, u prvního uživatele, co
    ten sloupec potřebuje — a v logu bude "no such column" bez náznaku,
    že migrace vůbec proběhla."""

    def test_columns_are_added_after_their_table(self):
        import re
        src = open('server.py', encoding='utf-8').read().splitlines()
        created = {}
        for i, line in enumerate(src):
            m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", line)
            if m and m.group(1) not in created:
                created[m.group(1)] = i
        late = []
        for i, line in enumerate(src):
            m = re.search(r"add_col\('(\w+)'", line)
            if not m:
                continue
            table = m.group(1)
            if table in created and i < created[table]:
                late.append(f'{table} (řádek {i + 1}, tabulka až na {created[table] + 1})')
        self.assertEqual(late, [], 'add_col před CREATE TABLE')


class CampaignTests(_Sprint2Base):
    """Rozesílání klientům. Právní základ je oprávněný zájem — obchodní
    sdělení vlastním zákazníkům o obdobné službě. Drží jen při třech
    podmínkách a všechny tři musí vynutit kód, ne dobrá vůle odesílatele."""

    def setUp(self):
        super().setUp()
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=?, premium_until=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),
                      (self._now() + timedelta(days=30)).isoformat()))
        conn.commit(); conn.close()
        # Klient s proběhlou rezervací = zákaznický vztah.
        start = self._day_at(4, 10)
        slot = self._mk_slot(start, start + timedelta(hours=6))
        self.client.post('/api/bookings', json={
            'slot_id': slot, 'design_note': 'test', 'booking_start_at': start.isoformat(),
            'duration_hours': 2})
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    def _count(self):
        return self.client.get('/api/me/campaigns/recipients').get_json()['count']

    def test_client_with_a_booking_is_a_recipient(self):
        self.assertEqual(self._count(), 1)

    def test_recipients_never_expose_addresses(self):
        """Výpis by z klientely udělal exportovatelný adresář."""
        d = self.client.get('/api/me/campaigns/recipients').get_json()
        self.assertNotIn('emails', d)
        self.assertNotIn('@', json.dumps(d))

    def test_mere_enquiry_is_not_a_customer(self):
        """Oprávněný zájem stojí na tom, že u tatéra opravdu byli. Kdo si
        jen napsal, zákazník není."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('DELETE FROM bookings')
        conn.commit(); conn.close()
        self.assertEqual(self._count(), 0)

    def test_unsubscribe_needs_a_valid_token(self):
        import sqlite3, server
        conn = sqlite3.connect(self.db)
        cid = conn.execute('SELECT id FROM clients LIMIT 1').fetchone()[0]
        conn.close()
        self.assertIn('neplatný',
                      self.client.get(f'/unsubscribe?c={cid}&t=spatny').get_data(as_text=True))
        self.assertEqual(self._count(), 1, 'špatný token přesto odhlásil')

    def test_unsubscribe_works_without_login_and_is_immediate(self):
        """Odhlašovací odkaz, který po někom chce heslo, není odhlašovací
        odkaz."""
        import sqlite3, server
        conn = sqlite3.connect(self.db)
        cid = conn.execute('SELECT id FROM clients LIMIT 1').fetchone()[0]
        conn.close()
        token = server._campaign_token(cid)
        fresh = server.app.test_client()          # bez přihlášení
        r = fresh.get(f'/unsubscribe?c={cid}&t={token}')
        self.assertIn('Odhlášeno', r.get_data(as_text=True))
        self.assertEqual(self._count(), 0)

    def test_token_is_not_guessable_from_another_id(self):
        import server
        self.assertNotEqual(server._campaign_token(1), server._campaign_token(2))

    def test_short_body_is_refused(self):
        r = self.client.post('/api/me/campaigns', json={'subject': 'Flash day', 'body': 'ahoj'})
        self.assertEqual(r.status_code, 400)

    def test_free_artist_cannot_send(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET premium_until=NULL WHERE id=1')
        conn.commit(); conn.close()
        self.assertEqual(self.client.get('/api/me/campaigns/recipients').status_code, 402)

    def test_only_own_clients(self):
        """Souhlas dal klient konkrétnímu tatérovi, ne studiu."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO users (username, display_name, password_hash, email, is_artist) "
                     "VALUES ('artist2','Artist Two','x','a2@t.cz',1)")
        conn.execute('UPDATE clients SET artist_id=3')
        conn.commit(); conn.close()
        self.assertEqual(self._count(), 0)


class CompletionBalanceTests(_Sprint2Base):
    """Součet záloha + doplatek + hotovost musí dát konečnou cenu.

    Dřív šlo dokončit s nulami: rezervace za 3 500 se zálohou 1 050 se
    uzavřela a 2 450 Kč zůstalo navždy nedoplacených. Tatér to zjistil
    až od účetní, nebo vůbec."""

    def setUp(self):
        super().setUp()
        import sqlite3
        from werkzeug.security import generate_password_hash
        start = self._day_at(3, 10)
        self.slot = self._mk_slot(start, start + timedelta(hours=8))
        r = self.client.post('/api/bookings', json={
            'slot_id': self.slot, 'design_note': 'vlk', 'duration_hours': 3,
            'booking_start_at': start.isoformat()})
        self.bid = r.get_json()['id']
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})
        self.info = self.client.get(f'/api/bookings/{self.bid}/completion-info').get_json()

    def test_outstanding_is_reported(self):
        """Bez tohohle čísla tatér při dokončování hádá."""
        self.assertEqual(self.info['outstanding_kc'],
                         self.info['total_kc'] - self.info['deposit_kc'])
        self.assertGreater(self.info['outstanding_kc'], 0)

    def test_completing_with_zero_is_refused(self):
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'onsite_kc': 0, 'balance_kc': 0})
        self.assertEqual(r.status_code, 400)
        j = r.get_json()
        self.assertEqual(j['outstanding_kc'], self.info['outstanding_kc'])

    def test_completing_with_too_little_is_refused(self):
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'onsite_kc': self.info['outstanding_kc'] - 100,
                                   'balance_kc': 0})
        self.assertEqual(r.status_code, 400)

    def test_full_amount_on_site_completes(self):
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'onsite_kc': self.info['outstanding_kc'], 'balance_kc': 0})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.client.get(f'/api/bookings/{self.bid}/completion-info').get_json()
        self.assertEqual(after['outstanding_kc'], 0)

    def test_split_between_cash_and_inklink(self):
        owed = self.info['outstanding_kc']
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'onsite_kc': owed - 500, 'balance_kc': 500})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_discount_lowers_the_price_and_balances(self):
        """Konečná cena se od domluvené může lišit — sleva, kratší práce."""
        lower = self.info['deposit_kc'] + 300
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'final_price_kc': lower, 'onsite_kc': 300, 'balance_kc': 0})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.client.get(f'/api/bookings/{self.bid}/completion-info').get_json()
        self.assertEqual(after['total_kc'], lower)
        self.assertEqual(after['outstanding_kc'], 0)

    def test_price_below_deposit_is_refused(self):
        """Přeplatek se vrací refundem, ne snížením ceny pod zálohu —
        jinak by v účetnictví vznikla záporná pohledávka."""
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'final_price_kc': 1, 'onsite_kc': 0, 'balance_kc': 0})
        self.assertEqual(r.status_code, 400)

    def test_legacy_booking_without_price_still_completes(self):
        """Starší rezervace mají total_price_cents = 0; není z čeho počítat,
        takže se nic nevymáhá."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE bookings SET total_price_cents=0 WHERE id=?', (self.bid,))
        conn.commit(); conn.close()
        r = self.client.post(f'/api/bookings/{self.bid}/complete',
                             json={'onsite_kc': 0, 'balance_kc': 0})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

    def test_completion_info_is_artist_only(self):
        self.client.post('/api/login', json={'username': 'client1', 'password': 'pass1234'})
        self.assertEqual(
            self.client.get(f'/api/bookings/{self.bid}/completion-info').status_code, 403)

    def test_export_shows_what_is_still_owed(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET premium_until=? WHERE id=1',
                     ((self._now() + timedelta(days=30)).isoformat(),))
        conn.commit(); conn.close()
        day = self._day_at(3, 10).date().isoformat()
        rows = self.client.get(
            f'/api/me/accounting/export?from={day}&to={day}&format=json').get_json()['rows']
        self.assertEqual(rows[0]['outstanding'], float(self.info['outstanding_kc']))


class AftercareTests(_Sprint2Base):
    """Sekvence po sezení. Běží, když tatér spí — a právě proto musí být
    přesná: mail navíc je horší než mail chybějící."""

    def setUp(self):
        super().setUp()
        import sqlite3, server
        from werkzeug.security import generate_password_hash
        server.CRON_SECRET = 'testsecret'
        start = self._day_at(2, 10)
        slot = self._mk_slot(start, start + timedelta(hours=6))
        r = self.client.post('/api/bookings', json={
            'slot_id': slot, 'design_note': 'vlk', 'duration_hours': 2,
            'booking_start_at': start.isoformat()})
        self.bid = r.get_json()['id']
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET password_hash=?, premium_until=?, '
                     'aftercare_text=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),
                      (self._now() + timedelta(days=30)).isoformat(),
                      'Folii nech 24 h.'))
        conn.commit(); conn.close()

    def _complete_days_ago(self, days):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE bookings SET status='completed', completed_at=? WHERE id=?",
                     ((self._now() - timedelta(days=days, hours=1)).isoformat(), self.bid))
        conn.commit(); conn.close()

    def _run(self):
        return self.client.get('/api/cron/aftercare?key=testsecret').get_json()

    def _sent_steps(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        rows = [r[0] for r in conn.execute(
            'SELECT step FROM aftercare_sent WHERE booking_id=?', (self.bid,))]
        conn.close()
        return sorted(rows)

    def test_cron_needs_a_secret(self):
        self.assertEqual(self.client.get('/api/cron/aftercare').status_code, 401)

    def test_instructions_go_out_on_completion_not_by_cron(self):
        """Klient je má mít, než odejde ze studia — ne až druhý den."""
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})
        info = self.client.get(f'/api/bookings/{self.bid}/completion-info').get_json()
        self.client.post(f'/api/bookings/{self.bid}/complete',
                         json={'onsite_kc': info['outstanding_kc'], 'balance_kc': 0})
        self.assertEqual(self._sent_steps(), ['day0'])

    def test_day7_fires_a_week_after(self):
        self._complete_days_ago(7)
        self._run()
        self.assertEqual(self._sent_steps(), ['day7'])

    def test_nothing_fires_the_same_day(self):
        self._complete_days_ago(0)
        self._run()
        self.assertEqual(self._sent_steps(), [])

    def test_each_step_fires_once(self):
        self._complete_days_ago(7)
        self._run(); self._run(); self._run()
        self.assertEqual(self._sent_steps(), ['day7'])

    def test_old_bookings_do_not_get_the_backlog(self):
        """Zapnutí premia nesmí vyslat celou historii — klient by dostal
        tři maily o tetování z loňska."""
        self._complete_days_ago(200)
        self._run()
        self.assertEqual(self._sent_steps(), [])

    def test_free_artist_sends_nothing(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET premium_until=NULL WHERE id=1')
        conn.commit(); conn.close()
        self._complete_days_ago(7)
        d = self._run()
        self.assertEqual(self._sent_steps(), [])
        self.assertEqual(d['skipped_not_premium'], 1)

    def test_artist_can_switch_it_off(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET aftercare_enabled=0 WHERE id=1')
        conn.commit(); conn.close()
        self._complete_days_ago(7)
        self._run()
        self.assertEqual(self._sent_steps(), [])

    def test_client_can_stop_it_without_logging_in(self):
        """Odkaz, který po klientovi chce heslo, není zastavovací odkaz."""
        import server
        self._complete_days_ago(7)
        fresh = server.app.test_client()
        token = server._aftercare_token(self.bid)
        r = fresh.get(f'/aftercare/stop?b={self.bid}&t={token}')
        self.assertIn('Hotovo', r.get_data(as_text=True))
        self._run()
        self.assertEqual(self._sent_steps(), [])

    def test_stop_needs_a_valid_token(self):
        self._complete_days_ago(7)
        r = self.client.get(f'/aftercare/stop?b={self.bid}&t=spatny')
        self.assertIn('neplatný', r.get_data(as_text=True))
        self._run()
        self.assertEqual(self._sent_steps(), ['day7'])

    def test_healed_photo_lands_in_the_thread(self):
        """Fotka má přijít tam, kde spolu mluví — ne do tiché složky."""
        import io, sqlite3, server
        token = server._aftercare_token(self.bid)
        fresh = server.app.test_client()          # klient bez přihlášení
        img = io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 64)
        r = fresh.post('/aftercare/photo',
                       data={'b': str(self.bid), 't': token, 'photo': (img, 'healed.png')},
                       content_type='multipart/form-data')
        self.assertIn('Díky', r.get_data(as_text=True))
        conn = sqlite3.connect(self.db)
        m = conn.execute('SELECT sender_id, receiver_id, content_type FROM messages '
                         'ORDER BY id DESC LIMIT 1').fetchone()
        n = conn.execute("SELECT COUNT(*) FROM notifications WHERE type='healed_photo'").fetchone()[0]
        conn.close()
        self.assertEqual(m, (2, 1, 'image'))
        self.assertEqual(n, 1)

    def test_photo_upload_needs_a_valid_token(self):
        import io, server
        fresh = server.app.test_client()
        img = io.BytesIO(b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 64)
        r = fresh.post('/aftercare/photo',
                       data={'b': str(self.bid), 't': 'spatny', 'photo': (img, 'x.png')},
                       content_type='multipart/form-data')
        self.assertIn('neplatný', r.get_data(as_text=True))

    def test_instructions_come_from_the_artist(self):
        """Platforma nemá co radit v něčem zdravotním."""
        import server
        _, html = server._aftercare_email('day0', {
            'artist_name': 'Artist One',
            'care_text': 'MUJ VLASTNI POSTUP', 'stop_url': '#'})
        self.assertIn('MUJ VLASTNI POSTUP', html)

    def test_without_instructions_it_defers_to_the_artist(self):
        import server
        _, html = server._aftercare_email('day0', {
            'artist_name': 'Artist One', 'care_text': '', 'stop_url': '#'})
        self.assertIn('můj postup', html)

    def test_review_ask_is_skipped_when_one_exists(self):
        """Nenaléhat na recenzi, kterou už klient napsal."""
        import server
        _, with_ask = server._aftercare_email('day30', {
            'artist_name': 'A', 'stop_url': '#',
            'photo_url': '#', 'review_url': '/rev', 'ask_review': True})
        _, without = server._aftercare_email('day30', {
            'artist_name': 'A', 'stop_url': '#',
            'photo_url': '#', 'review_url': '/rev', 'ask_review': False})
        self.assertIn('recenze', with_ask)
        self.assertNotIn('recenze', without)

    def test_settings_are_artist_only(self):
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/me/aftercare').status_code, 401)


class CreditLedgerTests(_Sprint2Base):
    """Kredit jsou cizí peníze, které držíme my. Musíme umět kdykoliv
    doložit, odkud přišly a kam šly — číslo na uživateli se dá přepsat,
    kniha ne."""

    def _bal(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        b = conn.execute('SELECT COALESCE(account_credit_cents,0) FROM users WHERE id=2').fetchone()[0]
        led = conn.execute('SELECT COALESCE(SUM(delta_cents),0) FROM credit_ledger '
                           'WHERE user_id=2').fetchone()[0]
        conn.close()
        return b, led

    def _move(self, delta, reason='admin_adjust'):
        import server
        conn = server.get_db()
        out = server._credit_move(conn, 2, delta, reason)
        conn.commit(); conn.close()
        return out

    def test_balance_always_matches_the_ledger(self):
        """Rozejít se nesmí ani o korunu — pak už nevíme, komu co dlužíme."""
        self._move(50000)
        self._move(-20000, 'booking_spend')
        self._move(3000, 'referral_bonus')
        bal, led = self._bal()
        self.assertEqual(bal, led)
        self.assertEqual(bal, 33000)

    def test_cannot_go_negative(self):
        """Utratit se dá jen to, co tam je."""
        self._move(1000)
        self.assertIsNone(self._move(-5000, 'booking_spend'))
        bal, led = self._bal()
        self.assertEqual((bal, led), (1000, 1000))

    def test_unknown_reason_is_refused(self):
        """Pohyb bez důvodu se v knize dohledat nedá."""
        import server
        conn = server.get_db()
        with self.assertRaises(ValueError):
            server._credit_move(conn, 2, 100, 'protoze_ano')
        conn.close()

    def test_history_is_visible_to_the_owner(self):
        self._move(2500, 'voucher_redeem')
        d = self.client.get('/api/me/credit').get_json()
        self.assertEqual(d['balance_kc'], 25.0)
        self.assertEqual(d['history'][0]['reason'], 'voucher_redeem')

    def test_credit_requires_login(self):
        self.client.post('/api/logout')
        self.assertEqual(self.client.get('/api/me/credit').status_code, 401)


class VoucherTests(_Sprint2Base):
    """Dárkový poukaz je koupený kredit. Neuplatněné poukazy jsou závazek,
    ne tržba — dokud je někdo neutratí, dlužíme jejich hodnotu."""

    def _buy(self, **over):
        body = {'amount_kc': 3000, 'recipient_name': 'Jan Novák', 'message': 'Hodně štěstí'}
        body.update(over)
        return self.client.post('/api/vouchers', json=body)

    def test_buy_and_redeem(self):
        code = self._buy().get_json()['code']
        r = self.client.post('/api/vouchers/redeem', json={'code': code})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertEqual(r.get_json()['balance_kc'], 3000.0)

    def test_code_is_case_and_space_tolerant(self):
        """Kód se opisuje z papíru — velikost písmen ani mezery řešit nemá."""
        code = self._buy().get_json()['code']
        r = self.client.post('/api/vouchers/redeem',
                             json={'code': f'  {code.lower()} '})
        self.assertEqual(r.status_code, 200)

    def test_code_avoids_confusable_characters(self):
        """Nula a O, jednička a I se z papíru opsat nedají."""
        import server
        for _ in range(30):
            code = server._voucher_code().replace('-', '')
            for ch in '01OI':
                self.assertNotIn(ch, code)

    def test_cannot_be_redeemed_twice(self):
        code = self._buy().get_json()['code']
        self.client.post('/api/vouchers/redeem', json={'code': code})
        r = self.client.post('/api/vouchers/redeem', json={'code': code})
        self.assertEqual(r.status_code, 409)

    def test_double_redeem_does_not_double_the_credit(self):
        """Závod na dvou zařízeních nesmí udělat kredit ze vzduchu."""
        import sqlite3
        code = self._buy().get_json()['code']
        self.client.post('/api/vouchers/redeem', json={'code': code})
        self.client.post('/api/vouchers/redeem', json={'code': code})
        conn = sqlite3.connect(self.db)
        total = conn.execute('SELECT COALESCE(SUM(delta_cents),0) FROM credit_ledger '
                             "WHERE reason='voucher_redeem'").fetchone()[0]
        conn.close()
        self.assertEqual(total, 300000)

    def test_expired_voucher_is_refused(self):
        import sqlite3
        code = self._buy().get_json()['code']
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE vouchers SET expires_at=? WHERE code=?',
                     ((self._now() - timedelta(days=1)).isoformat(), code))
        conn.commit(); conn.close()
        r = self.client.post('/api/vouchers/redeem', json={'code': code})
        self.assertEqual(r.status_code, 409)

    def test_unknown_code_is_404(self):
        r = self.client.post('/api/vouchers/redeem', json={'code': 'AAAA-BBBB-CCCC'})
        self.assertEqual(r.status_code, 404)

    def test_amount_limits(self):
        self.assertEqual(self._buy(amount_kc=100).status_code, 400)
        self.assertEqual(self._buy(amount_kc=999999).status_code, 400)

    def test_unpaid_voucher_cannot_be_redeemed(self):
        """Poukaz, za který nikdo nezaplatil, by byl kredit z ničeho."""
        import sqlite3
        code = self._buy().get_json()['code']
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE vouchers SET status='awaiting_payment' WHERE code=?", (code,))
        conn.commit(); conn.close()
        r = self.client.post('/api/vouchers/redeem', json={'code': code})
        self.assertEqual(r.status_code, 409)

    def test_printable_page_works_without_login(self):
        """Dárce ho posílá dál; obdarovaný účet mít nemusí, dokud
        kód neuplatní."""
        import server
        code = self._buy().get_json()['code']
        fresh = server.app.test_client()
        body = fresh.get(f'/vouchers/{code}').get_data(as_text=True)
        self.assertIn(code, body)
        self.assertIn('3 000', body)
        self.assertIn('Jan Novák', body)

    def test_unpaid_voucher_is_not_printable(self):
        import sqlite3, server
        code = self._buy().get_json()['code']
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE vouchers SET status='awaiting_payment' WHERE code=?", (code,))
        conn.commit(); conn.close()
        self.assertEqual(server.app.test_client().get(f'/vouchers/{code}').status_code, 404)

    def test_redeemed_voucher_is_marked_on_the_print(self):
        code = self._buy().get_json()['code']
        self.client.post('/api/vouchers/redeem', json={'code': code})
        self.assertIn('UPLATNĚNO', self.client.get(f'/vouchers/{code}').get_data(as_text=True))


class CreditSpendTests(_Sprint2Base):
    """Kredit snižuje, co platí klient — ne to, co dostane tatér.
    Rozdíl doplácíme my z peněz, které za kredit držíme."""

    def setUp(self):
        super().setUp()
        import server
        conn = server.get_db()
        server._credit_move(conn, 2, 200000, 'voucher_redeem')
        conn.commit(); conn.close()
        self.start = self._day_at(4, 10)
        self.slot = self._mk_slot(self.start, self.start + timedelta(hours=8))

    def _book(self, use_credit):
        return self.client.post('/api/bookings', json={
            'slot_id': self.slot, 'design_note': 'vlk', 'duration_hours': 3,
            'booking_start_at': self.start.isoformat(), 'use_credit': use_credit})

    def test_credit_is_not_spent_unless_asked(self):
        """Kredit je klientův; utratit ho bez jeho vědomí nesmíme."""
        r = self._book(False)
        self.assertEqual(r.get_json()['credit_used_kc'], 0)

    def test_credit_covers_part_of_the_deposit(self):
        j = self._book(True).get_json()
        self.assertGreater(j['credit_used_kc'], 0)
        self.assertLessEqual(j['credit_used_kc'] * 100, j['deposit_cents'])

    def test_artist_is_kept_whole(self):
        """Kdybychom tatérovi poslali míň, zaplatil by cizí poukaz on."""
        import sqlite3
        bid = self._book(True).get_json()['id']
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
        conn.close()
        self.assertEqual(b['platform_owes_artist_cents'], b['credit_used_cents'])
        self.assertGreater(b['platform_owes_artist_cents'], 0)

    def test_spend_is_recorded_in_the_ledger(self):
        import sqlite3
        bid = self._book(True).get_json()['id']
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT delta_cents, ref_id FROM credit_ledger "
                           "WHERE reason='booking_spend'").fetchone()
        conn.close()
        self.assertLess(row[0], 0)
        self.assertEqual(row[1], bid)

    def test_spends_at_most_what_is_there(self):
        """Kredit menší než záloha se utratí celý, ale ani o korunu víc —
        jinak by zůstatek šel do minusu a my bychom dlužili sami sobě."""
        import sqlite3, server
        conn = server.get_db()
        conn.execute('UPDATE users SET account_credit_cents=5000 WHERE id=2')
        conn.execute('DELETE FROM credit_ledger')
        conn.execute('INSERT INTO credit_ledger (user_id, delta_cents, reason) '
                     "VALUES (2, 5000, 'admin_adjust')")
        conn.commit(); conn.close()
        j = self._book(True).get_json()
        self.assertEqual(j['credit_used_kc'], 50)
        self.assertLess(j['credit_used_kc'] * 100, j['deposit_cents'])
        conn = sqlite3.connect(self.db)
        bal = conn.execute('SELECT account_credit_cents FROM users WHERE id=2').fetchone()[0]
        led = conn.execute('SELECT SUM(delta_cents) FROM credit_ledger WHERE user_id=2').fetchone()[0]
        conn.close()
        self.assertEqual(bal, 0)
        self.assertEqual(bal, led)


class VoucherTemplateTests(_Sprint2Base):
    """Grafiku dělá člověk v Canvě, ne my v CSS. Poukaz ale nesmí zůstat
    prázdný ani bez šablony, ani po jejím rozbití."""

    def _as_admin(self):
        import sqlite3
        from werkzeug.security import generate_password_hash
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET is_admin=1, password_hash=? WHERE id=1',
                     (generate_password_hash('pass1234', method='pbkdf2:sha256'),))
        conn.commit(); conn.close()
        self.client.post('/api/login', json={'username': 'artist1', 'password': 'pass1234'})

    def _voucher(self):
        r = self.client.post('/api/vouchers', json={'amount_kc': 3000,
                                                    'recipient_name': 'Jan Novák'})
        return r.get_json()['code']

    def test_renders_without_a_template(self):
        """Výchozí karta musí stačit — poukaz bez grafiky je pořád poukaz."""
        code = self._voucher()
        body = self.client.get(f'/vouchers/{code}').get_data(as_text=True)
        self.assertIn(code, body)
        self.assertIn('3 000', body)

    def test_only_admin_can_change_it(self):
        self.assertEqual(self.client.get('/api/admin/voucher-template').status_code, 403)

    def test_layout_is_clamped(self):
        """Souřadnice mimo plochu by text vystrčily z poukazu."""
        self._as_admin()
        r = self.client.post('/api/admin/voucher-template', json={'layout': {
            'code': {'x': 500, 'y': -80, 'size': 999, 'color': '#fff', 'align': 'nahoru'}}})
        L = r.get_json()['layout']['code']
        self.assertEqual(L['x'], 100.0)
        self.assertEqual(L['y'], 0.0)
        self.assertLessEqual(L['size'], 30.0)
        self.assertIn(L['align'], ('left', 'center', 'right'))

    def test_missing_fields_fall_back_to_defaults(self):
        """Po přidání dalšího pole nesmí render starých šablon spadnout."""
        import server
        conn = server.get_db()
        server._setting_set(conn, 'voucher_template_layout', '{"code": {"x": 20}}')
        conn.commit()
        tpl = server._voucher_template(conn)
        conn.close()
        self.assertEqual(tpl['layout']['code']['x'], 20)
        self.assertIn('amount', tpl['layout'])
        self.assertIn('color', tpl['layout']['code'])

    def test_broken_layout_does_not_break_the_voucher(self):
        import server
        conn = server.get_db()
        server._setting_set(conn, 'voucher_template_layout', 'tohle není JSON')
        conn.commit(); conn.close()
        code = self._voucher()
        r = self.client.get(f'/vouchers/{code}')
        self.assertEqual(r.status_code, 200)
        self.assertIn(code, r.get_data(as_text=True))

    def test_reset_removes_the_template(self):
        import server
        self._as_admin()
        conn = server.get_db()
        server._setting_set(conn, 'voucher_template_image', 'x.png')
        conn.commit(); conn.close()
        self.client.delete('/api/admin/voucher-template')
        conn = server.get_db()
        tpl = server._voucher_template(conn)
        conn.close()
        self.assertIsNone(tpl['image'])

    def test_preview_needs_no_real_voucher(self):
        """Ladit šablonu se musí dát bez toho, aby se kvůli tomu kupoval
        poukaz."""
        self._as_admin()
        r = self.client.get('/api/admin/voucher-preview')
        self.assertEqual(r.status_code, 200)
        self.assertIn('ABCD-2K5X-QW74', r.get_data(as_text=True))

    def test_bad_image_type_is_refused(self):
        import io
        self._as_admin()
        r = self.client.post('/api/admin/voucher-template',
                             data={'image': (io.BytesIO(b'x' * 32), 'sablona.svg')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)


class CurrencyTests(_Sprint2Base):
    """Měna patří tatérovi, ne divákovi: Stripe strhává v jedné měně a
    výplatní účet má jednu měnu. Neptáme se na ni — odvozuje se ze země."""

    def _set_city(self, city):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET city=? WHERE id=1', (city,))
        conn.commit(); conn.close()

    def _derive(self, stripe_country=None):
        import server
        conn = server.get_db()
        out = server._derive_currency(conn, 1, stripe_country)
        conn.close()
        return out

    def test_city_decides(self):
        for city, cur in [('Praha', 'CZK'), ('Bratislava', 'EUR'), ('Košice', 'EUR'),
                          ('Kraków', 'PLN'), ('Berlin', 'EUR'), ('London', 'GBP')]:
            self._set_city(city)
            self.assertEqual(self._derive(), cur, city)

    def test_slovak_artist_is_not_billed_in_crowns(self):
        """Slovensko je v eurozóně. Bez mapy měst by tatér z Bratislavy
        účtoval v korunách, které mu Stripe nepošle."""
        self._set_city('Bratislava')
        self.assertEqual(self._derive(), 'EUR')

    def test_stripe_country_beats_the_city(self):
        """Země Stripe účtu určuje, v čem přijdou peníze — proti tomu
        nemá vlastní odhad co dělat."""
        self._set_city('Praha')
        self.assertEqual(self._derive('US'), 'USD')

    def test_unknown_place_falls_back(self):
        self._set_city('Někde jinde')
        self.assertEqual(self._derive(), 'CZK')

    def test_unsupported_currency_is_normalised(self):
        """Neznámý kód nesmí projít do Stripe volání."""
        import server
        self.assertEqual(server._norm_currency('JPY'), 'CZK')
        self.assertEqual(server._norm_currency(''), 'CZK')
        self.assertEqual(server._norm_currency('eur'), 'EUR')

    def test_all_supported_currencies_use_minor_units(self):
        """Kód všude počítá v setinách. Jen a Won mají nulová desetinná
        místa a rozbily by každou částku stokrát."""
        import server
        self.assertNotIn('JPY', server.CURRENCIES)
        self.assertNotIn('KRW', server.CURRENCIES)

    def test_slot_inherits_the_artist_currency(self):
        import sqlite3, server
        self._set_city('Bratislava')
        conn = server.get_db(); server._sync_currency(conn, 1); conn.commit(); conn.close()
        start = self._day_at(3, 10)
        sid = self._mk_slot(start, start + timedelta(hours=4))
        # _mk_slot obchází endpoint, tak měnu doplníme jako by ji vložil
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE slots SET currency=(SELECT currency FROM users WHERE id=1) '
                     'WHERE id=?', (sid,))
        conn.commit()
        cur = conn.execute('SELECT currency FROM slots WHERE id=?', (sid,)).fetchone()[0]
        conn.close()
        self.assertEqual(cur, 'EUR')

    def test_booking_inherits_the_slot_currency(self):
        import sqlite3
        start = self._day_at(4, 10)
        sid = self._mk_slot(start, start + timedelta(hours=4))
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE slots SET currency='EUR' WHERE id=?", (sid,))
        conn.commit(); conn.close()
        r = self.client.post('/api/bookings', json={
            'slot_id': sid, 'design_note': 'vlk', 'duration_hours': 2,
            'booking_start_at': start.isoformat()})
        bid = r.get_json()['id']
        conn = sqlite3.connect(self.db)
        cur = conn.execute('SELECT currency FROM bookings WHERE id=?', (bid,)).fetchone()[0]
        conn.close()
        self.assertEqual(cur, 'EUR')

    def test_profile_exposes_the_currency(self):
        """Klient musí vidět, v čem tatér účtuje, dřív než klikne."""
        self._set_city('Bratislava')
        import server
        conn = server.get_db(); server._sync_currency(conn, 1); conn.commit(); conn.close()
        self.assertEqual(self.client.get('/api/profile/artist1').get_json()['currency'], 'EUR')

    def test_currency_list_is_shared(self):
        d = self.client.get('/api/currencies').get_json()
        codes = [c['code'] for c in d['currencies']]
        self.assertIn('EUR', codes)
        self.assertEqual(d['default'], 'CZK')


class MoneyFormatTests(unittest.TestCase):
    """Formátování je na jednom místě — jinak by každá stránka psala
    částky jinak."""

    def test_one_formatter_for_everything(self):
        src = open('public/i18n.js', encoding='utf-8').read()
        self.assertIn('function money(', src)
        self.assertIn('CURRENCY_LOCALE', src)

    def test_currency_shape_follows_the_currency_not_the_ui(self):
        """3 000 Kč se píše stejně Čechovi i Němci. Jazyk rozhraní by
        z korun udělal 'CZK 3,000'."""
        src = open('public/i18n.js', encoding='utf-8').read()
        i = src.index('function money(')
        body = src[i:i + 700]
        self.assertIn('CURRENCY_LOCALE[cur]', body)
        self.assertNotIn("lang === 'cs' ? 'cs-CZ'", body)

    def test_no_page_hardcodes_the_currency_in_prices(self):
        """Natvrdo připsané ' CZK' by eurovému tatérovi ukázalo koruny."""
        import glob, re
        bad = []
        for f in glob.glob('public/*.html') + glob.glob('public/*.js'):
            if f.endswith(('i18n.js', 'admin.html')):
                continue                      # slovník a admin jsou v korunách záměrně
            src = open(f, encoding='utf-8').read()
            for m in re.finditer(r"toLocaleString\([^)]*\)\s*\+?\s*['\"` ]*CZK", src):
                bad.append(f'{f}: {m.group(0)[:40]}')
        self.assertEqual(bad, [])


class LoginIdentifierTests(unittest.TestCase):
    """Přihlášení párovalo prázdný identifikátor na prázdné sloupce.

    Telefon je nepovinný a defaultně prázdný řetězec, takže dotaz
    `phone = ''` se napároval na PRVNÍHO uživatele bez telefonu. Odeslání
    prázdného jména se správně uhodnutým heslem tedy přihlásilo k cizímu
    účtu. Našlo se to náhodou, když testovací volání posílalo špatný název
    pole a server přesto vrátil ok:true."""

    def setUp(self):
        self.client, self.db = _fresh_client()
        import sqlite3
        from werkzeug.security import generate_password_hash
        pw = generate_password_hash('pass1234', method='pbkdf2:sha256')
        conn = sqlite3.connect(self.db)
        # Ani jeden nemá telefon — přesně stav, který chybu spouštěl.
        conn.execute('INSERT INTO users (username, display_name, password_hash, email, phone) '
                     "VALUES ('artist','Artist',?, 'a@t.cz', '')", (pw,))
        conn.execute('INSERT INTO users (username, display_name, password_hash, email, phone) '
                     "VALUES ('client','Client',?, 'c@t.cz', '')", (pw,))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)

    def test_empty_identifier_is_rejected(self):
        r = self.client.post('/api/login', json={'username': '', 'password': 'pass1234'})
        self.assertEqual(r.status_code, 401)
        self.assertIsNone(self.client.get('/api/me').get_json())

    def test_missing_identifier_field_is_rejected(self):
        r = self.client.post('/api/login', json={'password': 'pass1234'})
        self.assertEqual(r.status_code, 401)

    def test_empty_password_is_rejected(self):
        r = self.client.post('/api/login', json={'username': 'artist', 'password': ''})
        self.assertEqual(r.status_code, 401)

    def test_empty_email_column_does_not_match(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET email='' WHERE username='artist'")
        conn.commit()
        conn.close()
        r = self.client.post('/api/login', json={'username': '', 'password': 'pass1234'})
        self.assertEqual(r.status_code, 401)

    def test_login_still_works_by_username_email_and_phone(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE users SET phone='777123456' WHERE username='client'")
        conn.commit()
        conn.close()
        for ident in ('artist', 'a@t.cz', '777123456'):
            self.client.post('/api/logout')
            r = self.client.post('/api/login', json={'username': ident, 'password': 'pass1234'})
            self.assertEqual(r.status_code, 200, ident)
            self.assertIsNotNone(self.client.get('/api/me').get_json(), ident)

    def test_logging_in_as_someone_else_switches_the_session(self):
        self.client.post('/api/login', json={'username': 'artist', 'password': 'pass1234'})
        self.assertEqual(self.client.get('/api/me').get_json()['username'], 'artist')
        self.client.post('/api/login', json={'username': 'client', 'password': 'pass1234'})
        self.assertEqual(self.client.get('/api/me').get_json()['username'], 'client')


class InternalNoteTests(_Sprint2Base):
    """Soukromá poznámka tatéra u rezervace.

    PATCH ji dřív tiše zahodil — neznámé pole prostě přeskočil a vrátil ok,
    takže UI hlásilo "uloženo" a v databázi nebylo nic. Chyba, kterou by
    žádná chybová hláška neprozradila."""

    def _booking(self):
        start = self._day_at(3, 10)
        sid = self._mk_slot(self._day_at(3, 9), self._day_at(3, 18))
        self._as_client()
        r = self._book(sid, start)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()['id']

    def test_artist_can_save_internal_note(self):
        bid = self._booking()
        self._as_artist()
        r = self.client.patch(f'/api/bookings/{bid}',
                              json={'internal_note': 'alergie na latex'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._booking_row(bid)['internal_note'], 'alergie na latex')

    def test_client_cannot_touch_the_internal_note(self):
        bid = self._booking()
        self._as_artist()
        self.client.patch(f'/api/bookings/{bid}', json={'internal_note': 'jen pro mě'})
        self._as_client()
        r = self.client.patch(f'/api/bookings/{bid}', json={'internal_note': 'hacknuto'})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self._booking_row(bid)['internal_note'], 'jen pro mě')

    def test_design_note_stays_editable_by_both(self):
        bid = self._booking()
        self._as_artist()
        self.assertEqual(self.client.patch(
            f'/api/bookings/{bid}', json={'design_note': 'od tatéra'}).status_code, 200)
        self._as_client()
        self.assertEqual(self.client.patch(
            f'/api/bookings/{bid}', json={'design_note': 'od klienta'}).status_code, 200)


class RescheduleEmailTests(_Sprint2Base):
    """Žádost o přesun čeká na tatéra a dokud ji nevyřídí, termín se nehne.
    In-app notifikace na to nestačí — kdyby ji uviděl až při příštím otevření
    appky, může to být po původním termínu."""

    def setUp(self):
        super().setUp()
        from unittest.mock import patch
        import server
        self.patch, self.server = patch, server

    def _late_booking(self):
        """Rezervace do 48 h → přesun klientem vytvoří žádost, neaplikuje se."""
        sid = self._mk_slot(self._day_at(1, 9), self._day_at(1, 18))
        self._as_client()
        r = self._book(sid, self._day_at(1, 10))
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        return r.get_json()['id'], sid

    def test_request_sends_email_to_artist(self):
        bid, sid = self._late_booking()
        with self.patch.object(self.server, 'send_booking_email') as mail:
            r = self.client.patch(f'/api/bookings/{bid}/reschedule',
                                  json={'new_slot_id': sid,
                                        'booking_start_at': self._day_at(1, 14).isoformat()})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.get_json()['applied'])   # žádost, ne přesun
        self.assertTrue(mail.called, 'tatérovi se neodeslal mail')
        args = mail.call_args[0]
        self.assertEqual(args[1], 1)                             # artist_id
        self.assertEqual(args[2], 'reschedule_requested_for_artist')
        # Mail musí říct, odkud kam — jinak musí tatér otevřít appku,
        # aby vůbec věděl, o čem rozhoduje.
        ctx = mail.call_args[0][3]
        self.assertTrue(ctx['current_when'])
        self.assertTrue(ctx['when'])
        self.assertNotEqual(ctx['current_when'], ctx['when'])
        self.assertIn('/calendar', ctx['booking_url'])

    def test_failing_email_does_not_lose_the_request(self):
        """Resend může být dole. Žádost je v tu chvíli uložená a commitnutá,
        takže výpadek mailu ji nesmí zahodit."""
        bid, sid = self._late_booking()
        with self.patch.object(self.server, 'send_booking_email', side_effect=RuntimeError('resend down')):
            try:
                self.client.patch(f'/api/bookings/{bid}/reschedule',
                                  json={'new_slot_id': sid,
                                        'booking_start_at': self._day_at(1, 14).isoformat()})
            except RuntimeError:
                pass
        self._as_artist()
        pending = [r for r in self.client.get('/api/reschedule-requests').get_json()
                   if r['status'] == 'pending']
        self.assertEqual(len(pending), 1, 'žádost se ztratila při výpadku mailu')

    def test_artist_moving_directly_sends_no_request_email(self):
        """Tatér přesouvá rovnou, takže není co schvalovat a mail
        o žádosti by byl nesmysl."""
        sid = self._mk_slot(self._day_at(9, 9), self._day_at(9, 18))
        self._as_client()
        bid = self._book(sid, self._day_at(9, 10)).get_json()['id']
        self._as_artist()
        with self.patch.object(self.server, 'send_booking_email') as mail:
            r = self.client.patch(f'/api/bookings/{bid}/reschedule',
                                  json={'new_slot_id': sid,
                                        'booking_start_at': self._day_at(9, 14).isoformat()})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['applied'])
        sent = [c for c in mail.call_args_list
                if c[0][2] == 'reschedule_requested_for_artist']
        self.assertEqual(sent, [])


class InstagramConnectTests(unittest.TestCase):
    """OAuth je jediné místo, kde do appky vstupuje cizí identita, takže
    kontroly kolem `state` a nakládání s tokenem jsou tu podstatnější než
    samotné volání Instagramu (to je při testu stejně zaslepené)."""

    def setUp(self):
        os.environ['INSTAGRAM_APP_ID'] = 'test-app-id'
        os.environ['INSTAGRAM_APP_SECRET'] = 'test-secret'
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, is_artist) '
            "VALUES ('inker','Inker','x','i@t.cz',1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)
        os.environ.pop('INSTAGRAM_APP_ID', None)
        os.environ.pop('INSTAGRAM_APP_SECRET', None)

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1

    def _connect_row(self, token='tok-123'):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('INSERT INTO instagram_accounts '
                     '(user_id, ig_user_id, username, access_token) VALUES (1,?,?,?)',
                     ('99', 'inker_ig', token))
        conn.commit()
        conn.close()

    def test_connect_requires_login(self):
        self.assertEqual(self.client.get('/api/instagram/connect').status_code, 401)

    def test_connect_redirects_with_state(self):
        self._login()
        r = self.client.get('/api/instagram/connect')
        self.assertEqual(r.status_code, 302)
        self.assertIn('instagram.com/oauth/authorize', r.headers['Location'])
        self.assertIn('state=', r.headers['Location'])
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('ig_oauth_state'))

    def test_callback_rejects_wrong_state(self):
        """Bez téhle kontroly jde uživateli podstrčit callback a připojit
        mu cizí Instagram účet."""
        self._login()
        self.client.get('/api/instagram/connect')
        r = self.client.get('/api/instagram/callback?code=abc&state=podvrzeny')
        self.assertEqual(r.status_code, 302)
        self.assertIn('ig=state', r.headers['Location'])

    def test_callback_rejects_missing_state(self):
        self._login()
        r = self.client.get('/api/instagram/callback?code=abc&state=cokoli')
        self.assertIn('ig=state', r.headers['Location'])

    def test_state_is_single_use(self):
        self._login()
        r1 = self.client.get('/api/instagram/connect')
        from urllib.parse import urlparse, parse_qs
        st = parse_qs(urlparse(r1.headers['Location']).query)['state'][0]
        # První callback state spotřebuje (i když pak selže na výměně tokenu),
        # druhý už neprojde.
        self.client.get(f'/api/instagram/callback?error=denied&state={st}')
        r2 = self.client.get(f'/api/instagram/callback?code=abc&state={st}')
        self.assertIn('ig=state', r2.headers['Location'])

    def test_status_never_returns_the_token(self):
        self._login()
        self._connect_row(token='SUPER-TAJNY-TOKEN')
        r = self.client.get('/api/instagram/status')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['connected'])
        self.assertNotIn(b'SUPER-TAJNY-TOKEN', r.get_data())

    def test_status_when_not_connected(self):
        self._login()
        d = self.client.get('/api/instagram/status').get_json()
        self.assertFalse(d['connected'])
        self.assertTrue(d['available'])

    def test_disconnect_removes_account_but_keeps_import_history(self):
        self._login()
        self._connect_row()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO instagram_imports (user_id, ig_media_id) VALUES (1,'m1')")
        conn.commit()
        conn.close()

        self.assertEqual(self.client.post('/api/instagram/disconnect').status_code, 200)
        conn = sqlite3.connect(self.db)
        accounts = conn.execute('SELECT COUNT(*) FROM instagram_accounts').fetchone()[0]
        imports  = conn.execute('SELECT COUNT(*) FROM instagram_imports').fetchone()[0]
        conn.close()
        self.assertEqual(accounts, 0)
        # Historie zůstává: po znovupropojení nemá import znovu natáhnout
        # fotky, které si tatér mezitím z portfolia smazal.
        self.assertEqual(imports, 1)

    def test_media_and_import_need_a_connected_account(self):
        self._login()
        self.assertEqual(self.client.get('/api/instagram/media').status_code, 404)
        self.assertEqual(self.client.post(
            '/api/instagram/import', json={'ids': ['m1']}).status_code, 404)

    def test_import_rejects_empty_selection(self):
        self._login()
        self._connect_row()
        r = self.client.post('/api/instagram/import', json={'ids': []})
        self.assertEqual(r.status_code, 400)


class InstagramDisabledTests(unittest.TestCase):
    """Bez vyplněných proměnných se propojení nesmí nabízet — a nesmí
    ani spadnout."""

    def setUp(self):
        os.environ.pop('INSTAGRAM_APP_ID', None)
        os.environ.pop('INSTAGRAM_APP_SECRET', None)
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_connect_returns_503(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
        self.assertEqual(self.client.get('/api/instagram/connect').status_code, 503)

    def test_status_reports_unavailable(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
        d = self.client.get('/api/instagram/status').get_json()
        self.assertFalse(d['available'])
        self.assertFalse(d['connected'])


class ComingSoonGateTests(unittest.TestCase):
    """Brána stojí před veřejnou doménou, takže její chyby jsou drahé:
    zablokovaný webhook = ztracené platby, zablokovaný health check =
    Railway prohlásí deploy za mrtvý a vrátí předchozí verzi."""

    def setUp(self):
        os.environ['COMING_SOON'] = '1'
        os.environ['COMING_SOON_TOKEN'] = 'letmein'
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, is_artist) '
            "VALUES ('inker','Inker','x','i@t.cz',1)")
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)
        os.environ.pop('COMING_SOON', None)
        os.environ.pop('COMING_SOON_TOKEN', None)

    def _as_user(self):
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1

    def test_anonymous_sees_the_gate_not_the_app(self):
        for path in ('/', '/feed', '/events', '/my-bookings'):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertIn(b'inklink', r.data.lower())
            # Skutečná aplikace se nesmí prosypat ven.
            self.assertIn(b'wlForm', r.data, path)          # waitlist = brána

    def test_health_is_never_gated(self):
        # Bez tohohle Railway prohlásí deploy za mrtvý.
        self.assertEqual(self.client.get('/__health').status_code, 200)

    def test_webhooks_and_assets_bypass_the_gate(self):
        # Testujeme rozhodnutí brány, ne chování handleru za ní — poslat sem
        # skutečný request znamená stavět Stripe payload jen kvůli routingu.
        import server
        for path in ('/api/stripe/webhook', '/uploads/x.jpg', '/theme.css',
                     '/i18n.js', '/robots.txt', '/api/waitlist'):
            self.assertTrue(server._gate_is_open_path(path), path)
        for path in ('/', '/my-bookings', '/api/feed', '/profile/inker'):
            self.assertFalse(server._gate_is_open_path(path), path)

    def test_login_stays_reachable(self):
        r = self.client.get('/login')
        self.assertEqual(r.status_code, 200)
        self.assertIn(b'password', r.data.lower())

    def test_logged_in_user_passes_through(self):
        self._as_user()
        r = self.client.get('/calendar')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'wlForm', r.data)

    def test_preview_token_opens_the_gate_and_sticks(self):
        r = self.client.get('/?preview=letmein')
        self.assertEqual(r.status_code, 302)
        # Cookie drží, takže další prokliky už token v URL nepotřebují.
        r2 = self.client.get('/calendar')
        self.assertNotIn(b'wlForm', r2.data)

    def test_wrong_preview_token_stays_out(self):
        self.client.get('/?preview=nope')
        r = self.client.get('/calendar')
        self.assertIn(b'wlForm', r.data)

    def test_api_gets_json_not_html(self):
        r = self.client.get('/api/feed')
        self.assertEqual(r.status_code, 503)
        self.assertIsNotNone(r.get_json())

    def test_robots_and_sitemap_hide_the_rest(self):
        robots = self.client.get('/robots.txt').get_data(as_text=True)
        self.assertIn('Disallow: /', robots)
        sitemap = self.client.get('/sitemap.xml').get_data(as_text=True)
        self.assertEqual(sitemap.count('<url>'), 1)


class ComingSoonOffTests(unittest.TestCase):
    """Bez zapnuté proměnné se nesmí změnit vůbec nic."""

    def setUp(self):
        os.environ.pop('COMING_SOON', None)
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_app_is_open(self):
        r = self.client.get('/calendar')
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(b'wlForm', r.data)

    def test_sitemap_is_full(self):
        self.assertIn('/events', self.client.get('/sitemap.xml').get_data(as_text=True))


class WaitlistTests(unittest.TestCase):
    """Veřejný nepřihlášený zápis — proto rate limit a proto se odpověď
    nesmí lišit podle toho, jestli e-mail na seznamu už je."""

    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def _count(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        n = conn.execute('SELECT COUNT(*) FROM waitlist').fetchone()[0]
        conn.close()
        return n

    def test_signup_stores_entry(self):
        r = self.client.post('/api/waitlist',
                             json={'email': 'Tereza@Studio.CZ', 'role': 'artist'})
        self.assertEqual(r.status_code, 200)
        import sqlite3
        conn = sqlite3.connect(self.db)
        row = conn.execute('SELECT email, role FROM waitlist').fetchone()
        conn.close()
        self.assertEqual(row[0], 'tereza@studio.cz')  # normalizované
        self.assertEqual(row[1], 'artist')

    def test_duplicate_is_idempotent_and_indistinguishable(self):
        first  = self.client.post('/api/waitlist', json={'email': 'a@b.cz'})
        second = self.client.post('/api/waitlist', json={'email': 'a@b.cz'})
        self.assertEqual(first.status_code, second.status_code)
        # Celé tělo, ne jen status. Původní verze porovnávala jen kód a 'ok',
        # takže jí uniklo pole 'already', kterým šlo ověřit, jestli je adresa
        # na seznamu — přesně ten únik, kterému má tenhle test bránit.
        self.assertEqual(first.get_json(), second.get_json())
        self.assertEqual(first.get_data(), second.get_data())
        self.assertEqual(self._count(), 1)

    def test_invalid_email_rejected(self):
        for bad in ('', 'nope', 'a@b', 'a b@c.cz'):
            self.assertEqual(
                self.client.post('/api/waitlist', json={'email': bad}).status_code, 400, bad)
        self.assertEqual(self._count(), 0)

    def test_bogus_role_is_dropped_not_stored(self):
        self.client.post('/api/waitlist', json={'email': 'x@y.cz', 'role': '<script>'})
        import sqlite3
        conn = sqlite3.connect(self.db)
        role = conn.execute('SELECT role FROM waitlist').fetchone()[0]
        conn.close()
        self.assertEqual(role, '')

    def test_export_requires_admin(self):
        self.client.post('/api/waitlist', json={'email': 'a@b.cz'})
        self.assertEqual(self.client.get('/api/admin/waitlist').status_code, 401)
        with self.client.session_transaction() as sess:
            sess['user_id'] = 1
        self.assertEqual(self.client.get('/api/admin/waitlist').status_code, 404)


class PublicEventsTests(unittest.TestCase):
    """/events je veřejná SEO plocha v sitemapě. Endpointy pod ní musí
    odpovídat i nepřihlášenému — dřív vracely 401, nebo se lámaly na
    přelomu měsíce, protože filtrovaly `date LIKE '2026-09%'`."""

    def setUp(self):
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute(
            'INSERT INTO users (username, display_name, password_hash, email, is_artist) '
            "VALUES ('inker','Inker','x','i@t.cz',1)")
        # Konec září a začátek října — jeden týden, dva měsíce.
        for title, date in (('Guest spot Praha', '2027-09-29'),
                            ('Flash day Brno',   '2027-10-02')):
            conn.execute(
                'INSERT INTO events (user_id, title, date, time, city, genre) '
                "VALUES (1, ?, ?, '18:00', 'Praha', 'Guest spot')", (title, date))
        conn.commit()
        conn.close()

    def tearDown(self):
        os.unlink(self.db)

    def test_profile_exposes_the_artists_own_cancel_policy(self):
        """Profil dřív storno lhůty vůbec neposílal, takže je frontend
        vypisoval natvrdo jako 96/48 h. Tatér, který si je od Sprintu 2
        změnil, tak klientovi ukazoval cizí podmínky — a vracelo se pak
        podle jiných čísel, než jaká slíbil."""
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute('UPDATE users SET cancel_refund_full_hours=120, '
                     'cancel_refund_half_hours=72 WHERE id=1')
        conn.commit()
        conn.close()
        d = self.client.get('/api/profile/inker').get_json()
        self.assertEqual(d['cancel_full_hours'], 120)
        self.assertEqual(d['cancel_half_hours'], 72)

    def test_profile_falls_back_to_platform_policy(self):
        d = self.client.get('/api/profile/inker').get_json()
        import server
        self.assertEqual(d['cancel_full_hours'], server.CANCEL_REFUND_FULL_HOURS)
        self.assertEqual(d['cancel_half_hours'], server.CANCEL_REFUND_HALF_HOURS)

    def test_events_list_is_public(self):
        r = self.client.get('/api/events?year=2027&month=9')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 1)

    def test_range_spans_month_boundary(self):
        # Přesně ten případ, který měsíční LIKE utne v půlce týdne.
        r = self.client.get('/api/events?from=2027-09-27&to=2027-10-03')
        self.assertEqual(r.status_code, 200)
        titles = {e['title'] for e in r.get_json()}
        self.assertEqual(titles, {'Guest spot Praha', 'Flash day Brno'})

    def test_artist_id_filter(self):
        r = self.client.get('/api/events?from=2027-09-01&to=2027-10-31&artist_id=999')
        self.assertEqual(r.get_json(), [])
        r = self.client.get('/api/events?from=2027-09-01&to=2027-10-31&artist_id=1')
        self.assertEqual(len(r.get_json()), 2)

    def test_anonymous_gets_no_ownership_flags(self):
        # is_own u nepřihlášeného nesmí vyjít True kvůli uid == 0.
        r = self.client.get('/api/events?from=2027-09-01&to=2027-10-31')
        self.assertTrue(all(e['is_own'] is False for e in r.get_json()))
        self.assertTrue(all(e['is_saved'] is False for e in r.get_json()))

    def test_profile_events_is_public(self):
        r = self.client.get('/api/profile/inker/events')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()), 2)

    def test_creating_an_event_still_requires_login(self):
        r = self.client.post('/api/events', data={'title': 'X', 'date': '2027-11-01'})
        self.assertEqual(r.status_code, 401)


class PragueTimeTests(unittest.TestCase):
    """Časy se v DB drží jako pražský wall-clock — porovnávat je proti
    datetime.utcnow() posouvalo všechny 'kolik hodin před' kontroly."""

    def test_prague_now_is_ahead_of_utc(self):
        import server
        from datetime import datetime as _dt
        delta_h = (server._prague_now_naive() - _dt.utcnow()).total_seconds() / 3600.0
        self.assertIn(round(delta_h), (1, 2))  # CET / CEST

    def test_naive_dt_normalizes_offset_aware_input(self):
        import server
        naive = server._naive_dt('2027-06-01T12:00:00')
        aware = server._naive_dt('2027-06-01T12:00:00Z')
        self.assertIsNone(naive.tzinfo)
        self.assertIsNone(aware.tzinfo)
        self.assertEqual(naive, aware)


if __name__ == '__main__':
    unittest.main(verbosity=2)
