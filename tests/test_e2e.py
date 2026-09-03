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

    MEDICAL_KEY = 'ZmFrZS1rZXktZm9yLXRlc3RzLTMyLWJ5dGVzLWxvbmc9'  # placeholder, viz setUp

    def setUp(self):
        from cryptography.fernet import Fernet
        os.environ['MEDICAL_NOTES_KEY'] = Fernet.generate_key().decode()
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
        os.environ.pop('MEDICAL_NOTES_KEY', None)

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
            ('GET',    f'/api/clients/{self.cid}/medical', None),
            ('PUT',    f'/api/clients/{self.cid}/medical', {'notes': 'x'}),
            ('DELETE', f'/api/clients/{self.cid}/medical', None),
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


class MedicalNotesTests(_CrmBase):

    def setUp(self):
        super().setUp()
        self.cid = self._mk_client_for(2)
        self._as(2)

    def _log_rows(self):
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM medical_notes_access_log ORDER BY id').fetchall()]
        conn.close()
        return rows

    def test_encrypt_decrypt_round_trip(self):
        secret = 'Alergie na latex, epilepsie'
        self.assertEqual(self.client.put(f'/api/clients/{self.cid}/medical',
                                         json={'notes': secret}).status_code, 200)
        r = self.client.get(f'/api/clients/{self.cid}/medical')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['notes'], secret)

    def test_stored_value_is_ciphertext_not_plaintext(self):
        import sqlite3
        secret = 'Alergie na latex'
        self.client.put(f'/api/clients/{self.cid}/medical', json={'notes': secret})
        conn = sqlite3.connect(self.db)
        stored = conn.execute('SELECT ciphertext, key_version FROM client_medical_notes').fetchone()
        conn.close()
        self.assertNotIn(secret, stored[0])
        self.assertEqual(stored[1], 1)

    def test_access_is_logged(self):
        self.client.put(f'/api/clients/{self.cid}/medical', json={'notes': 'x'})
        before = len(self._log_rows())
        self.client.get(f'/api/clients/{self.cid}/medical')
        rows = self._log_rows()
        self.assertGreater(len(rows), before)
        self.assertTrue(any(r['action'] == 'read' and r['outcome'] == 'ok' for r in rows))

    def test_audit_failure_refuses_the_read(self):
        from unittest.mock import patch
        import server
        self.client.put(f'/api/clients/{self.cid}/medical', json={'notes': 'tajné'})
        with patch.object(server, '_medical_audit', side_effect=RuntimeError('disk full')):
            r = self.client.get(f'/api/clients/{self.cid}/medical')
        self.assertEqual(r.status_code, 503)
        self.assertNotIn(b'tajn', r.data)

    def test_client_detail_never_returns_plaintext(self):
        self.client.put(f'/api/clients/{self.cid}/medical', json={'notes': 'tajné'})
        d = self.client.get(f'/api/clients/{self.cid}').get_json()
        self.assertTrue(d['has_medical_notes'])
        self.assertNotIn('tajné', json.dumps(d, ensure_ascii=False))


class MedicalNotesNoKeyTests(_CrmBase):
    """Chybějící klíč smí shodit JEN zdravotní endpoint, ne celé CRM."""

    def setUp(self):
        # Schválně bez MEDICAL_NOTES_KEY — _fresh_client reimportuje server.
        os.environ.pop('MEDICAL_NOTES_KEY', None)
        self.client, self.db = _fresh_client()
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO users (username, display_name, password_hash, email, is_artist) "
                     "VALUES ('artist1','A','x','a@t.cz',1)")
        conn.execute('INSERT INTO clients (artist_id, user_id, name, created_by) '
                     "VALUES (1,NULL,'Jana',1)")
        conn.commit()
        conn.close()
        self.cid = 1
        self._as(1)

    def test_medical_endpoint_503(self):
        self.assertEqual(self.client.get(f'/api/clients/{self.cid}/medical').status_code, 503)
        self.assertEqual(self.client.put(f'/api/clients/{self.cid}/medical',
                                         json={'notes': 'x'}).status_code, 503)

    def test_client_detail_still_works(self):
        r = self.client.get(f'/api/clients/{self.cid}')
        self.assertEqual(r.status_code, 200, r.data[:300])
        self.assertFalse(r.get_json()['medical_notes_available'])

    def test_write_creates_no_row(self):
        import sqlite3
        self.client.put(f'/api/clients/{self.cid}/medical', json={'notes': 'x'})
        conn = sqlite3.connect(self.db)
        n = conn.execute('SELECT COUNT(*) FROM client_medical_notes').fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)


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
