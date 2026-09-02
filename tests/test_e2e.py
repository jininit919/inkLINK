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
import os
import sys
import tempfile
import unittest

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


class StudioIdBackfillTests(unittest.TestCase):
    """P1 item 7: bookings.studio_id denormalization + backfill."""

    def setUp(self):
        self.client, self.db = _fresh_client()

    def tearDown(self):
        os.unlink(self.db)

    def test_backfill_populates_studio_id_from_studio_members(self):
        import sqlite3, server
        from datetime import datetime, timedelta
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, email, is_artist) "
            "VALUES ('artist1','Artist','x','a@t.cz',1)"
        )
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, email, is_artist) "
            "VALUES ('client1','Client','x','c@t.cz',0)"
        )
        conn.execute("INSERT INTO studios (slug, name) VALUES ('studio1', 'Studio One')")
        studio_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        conn.execute('INSERT INTO studio_members (studio_id, artist_id) VALUES (?, 1)', (studio_id,))
        start = (datetime.utcnow() + timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max, "
            "price_unit, min_duration_hours) VALUES (1, ?, ?, 'free', 1500, 1500, 'hour', 1)",
            (start, start)
        )
        conn.execute(
            "INSERT INTO bookings (slot_id, artist_id, client_id, status, deposit_cents) "
            "VALUES (1, 1, 2, 'confirmed', 15000)"
        )
        conn.commit()
        conn.close()

        server.init_db()  # backfill runs here, same as on process startup

        conn = sqlite3.connect(self.db)
        got = conn.execute('SELECT studio_id FROM bookings WHERE id=1').fetchone()[0]
        conn.close()
        self.assertEqual(got, studio_id)

    def test_solo_artist_booking_studio_id_stays_null(self):
        import sqlite3, server
        from datetime import datetime, timedelta
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, email, is_artist) "
            "VALUES ('solo1','Solo','x','s@t.cz',1)"
        )
        conn.execute(
            "INSERT INTO users (username, display_name, password_hash, email, is_artist) "
            "VALUES ('client1','Client','x','c@t.cz',0)"
        )
        start = (datetime.utcnow() + timedelta(days=1)).isoformat()
        conn.execute(
            "INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max, "
            "price_unit, min_duration_hours) VALUES (1, ?, ?, 'free', 1500, 1500, 'hour', 1)",
            (start, start)
        )
        conn.execute(
            "INSERT INTO bookings (slot_id, artist_id, client_id, status, deposit_cents) "
            "VALUES (1, 1, 2, 'confirmed', 15000)"
        )
        conn.commit()
        conn.close()

        server.init_db()

        conn = sqlite3.connect(self.db)
        got = conn.execute('SELECT studio_id FROM bookings WHERE id=1').fetchone()[0]
        conn.close()
        self.assertIsNone(got)


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
