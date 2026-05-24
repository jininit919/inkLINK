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
