from flask import Flask, request, jsonify, session, send_from_directory, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import sqlite3
import os
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None
import time
import math
from html import escape as html_escape
import uuid
import random
import resend
from datetime import datetime, timedelta, timezone
import stripe
import boto3
from botocore.client import Config

# ── Sentry error monitoring ──────────────────────────────────────────────────
# Init proběhne jen pokud je SENTRY_DSN nastavený (env var v Railway). Bez něj
# se nic neimportuje a app jede dál.
SENTRY_DSN = os.environ.get('SENTRY_DSN', '').strip()
SENTRY_ENV = os.environ.get('SENTRY_ENVIRONMENT', 'production' if SENTRY_DSN else 'development')
if SENTRY_DSN:
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration

        def _scrub(event, hint):
            # Strip request bodies & cookies — můžou obsahovat hesla, verify
            # kódy, message texty. Necháme jen request URL + status code.
            try:
                req = event.get('request') or {}
                req.pop('cookies', None)
                req.pop('data', None)
                if 'headers' in req:
                    for h in ('cookie', 'authorization', 'x-api-key'):
                        req['headers'].pop(h, None)
                        req['headers'].pop(h.title(), None)
            except Exception:
                pass
            return event

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=SENTRY_ENV,
            traces_sample_rate=0.1,   # 10 % requestů — performance monitoring
            profiles_sample_rate=0.0, # profiling vypnuté (šetří kvótu)
            send_default_pii=False,   # neposílat IP, cookies, user agent
            integrations=[FlaskIntegration()],
            before_send=_scrub,
            ignore_errors=[KeyboardInterrupt, SystemExit],
        )
    except Exception as _e:
        print(f'[SENTRY] init failed: {_e}')

app = Flask(__name__, static_folder='public', static_url_path='')

# Session secret — načti z env nebo vygeneruj a ulož
_secret_file = os.path.join(os.path.dirname(__file__), '.session_secret')
if os.environ.get('SECRET_KEY'):
    app.secret_key = os.environ['SECRET_KEY']
elif os.path.exists(_secret_file):
    app.secret_key = open(_secret_file).read().strip()
else:
    app.secret_key = uuid.uuid4().hex + uuid.uuid4().hex
    open(_secret_file, 'w').write(app.secret_key)

# Trust proxy headers (Railway / Heroku / nginx) — aby Flask viděl skutečné
# scheme=https a remote IP přes X-Forwarded-* hlavičky.
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Session cookie security — Secure se aktivuje automaticky když request je https
# (díky ProxyFix), takže lokálně přes http session funguje a v produkci je Secure.
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
@app.before_request
def _set_secure_cookie():
    app.config['SESSION_COOKIE_SECURE'] = (request.scheme == 'https')

# Rate limiter
limiter = Limiter(
    key_func=get_remote_address,
    app=app,
    default_limits=[],
    storage_uri='memory://',
)

# Security headers
@app.after_request
def security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['X-XSS-Protection'] = '1; mode=block'
    resp.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return resp

STRIPE_SECRET_KEY     = os.environ.get('STRIPE_SECRET_KEY', '')
STRIPE_WEBHOOK_SECRET = os.environ.get('STRIPE_WEBHOOK_SECRET', '')
STRIPE_PUBLIC_KEY     = os.environ.get('STRIPE_PUBLIC_KEY', '')
STRIPE_PRO_PRICE_ID   = os.environ.get('STRIPE_PRO_PRICE_ID', '')
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

RESEND_API_KEY    = os.environ.get('RESEND_API_KEY', '')
VAPID_PUBLIC_KEY  = os.environ.get('PUSH_PUBLIC', '')
VAPID_PRIVATE_KEY = os.environ.get('PUSH_PRIVATE', '')

# Cloudflare R2 (cloud storage pro nahrané soubory)
R2_ACCOUNT_ID = os.environ.get('R2_ACCOUNT_ID', '')
R2_ACCESS_KEY = os.environ.get('R2_ACCESS_KEY', '')
R2_SECRET_KEY = os.environ.get('R2_SECRET_KEY', '')
R2_BUCKET     = os.environ.get('R2_BUCKET', '')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL', '').rstrip('/')

_s3 = None
if R2_BUCKET and R2_ACCESS_KEY and R2_ACCOUNT_ID:
    _s3 = boto3.client(
        's3',
        endpoint_url=f'https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com',
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )

def save_upload(file_storage, filename):
    """Uloží soubor buď do R2 (produkce) nebo na disk (lokální dev)."""
    file_storage.seek(0)
    if _s3 and R2_BUCKET:
        _s3.upload_fileobj(file_storage, R2_BUCKET, filename)
    else:
        file_storage.save(os.path.join(UPLOAD_FOLDER, filename))

# Platform commission rates
PLATFORM_FEE_TICKET  = 0.10   # 10 % z ceny vstupenky (poplatek za zpracování)
PLATFORM_FEE_LISTING = 0.08   # 8 % z ceny inzerátu (provize platformy)


RESEND_FROM = os.environ.get('RESEND_FROM', 'InkLink <onboarding@resend.dev>')
APP_BASE_URL = os.environ.get('APP_BASE_URL', 'https://www.inklink.club').rstrip('/')


def send_email(to, subject, html_body):
    """Pošle e-mail přes Resend. Vrací True při úspěchu, False při chybě
    (nedostupný API key, chyba Resend, ověřená doména v sandboxu, atd.)."""
    if not RESEND_API_KEY:
        print(f'[EMAIL] {to} | {subject} | (RESEND_API_KEY not set — kód jen v terminálu)')
        return False
    resend.api_key = RESEND_API_KEY
    try:
        resend.Emails.send({'from': RESEND_FROM, 'to': to, 'subject': subject, 'html': html_body})
        return True
    except Exception as e:
        try:
            app.logger.error(f'[EMAIL] failed: to={to} from={RESEND_FROM} subject={subject!r} err={e}')
        except Exception:
            print(f'[EMAIL ERROR] to={to}: {e}')
        return False


# ── Booking emails ───────────────────────────────────────────────────────────
# Posílají se ze 4 booking endpointů (create / confirm / cancel / complete).
# Vše v angličtině, dark theme inline styly. Pokud RESEND_API_KEY chybí,
# send_email() vrátí False a zalogu se, ale endpoint pokračuje (booking
# nesmí selhat kvůli mailu).

def _fmt_booking_when(start_iso, duration_h=None):
    """Returns 'May 25, 2026 · 14:00–16:00' or falls back to raw ISO."""
    try:
        s = datetime.fromisoformat((start_iso or '').replace('Z', '+00:00'))
        date_s = s.strftime('%b %d, %Y')
        start_s = s.strftime('%H:%M')
        if duration_h:
            e = s + timedelta(hours=float(duration_h))
            return f'{date_s} · {start_s}–{e.strftime("%H:%M")}'
        return f'{date_s} · {start_s}'
    except Exception:
        return start_iso or ''


def _booking_email_html(event, ctx):
    """Render (subject, html) for the given booking event."""
    from html import escape as _h
    name  = _h(ctx.get('recipient_name') or 'there')
    other = _h(ctx.get('other_name') or '')
    when  = _h(ctx.get('when') or '')
    note  = _h(ctx.get('design_note') or '')
    url   = _h(ctx.get('booking_url') or APP_BASE_URL + '/my-bookings')

    header = (
        '<div style="background:#000;color:#ccc;font-family:monospace;'
        'padding:40px;max-width:520px;margin:0 auto">'
        '<div style="font-size:28px;letter-spacing:0.2em;color:#e8e8e8;margin-bottom:6px">INKLINK</div>'
        '<div style="font-size:10px;color:#777;letter-spacing:0.15em;margin-bottom:28px">'
        'TATTOO BOOKING NETWORK</div>'
    )
    footer = (
        f'<p style="color:#555;font-size:11px;margin-top:36px;line-height:1.7">'
        f'You\'re getting this email because of activity on your InkLink account. '
        f'<br><a href="{_h(APP_BASE_URL)}" style="color:#888">{_h(APP_BASE_URL)}</a></p>'
        f'</div>'
    )

    def cta(label):
        return (f'<p style="margin-top:22px"><a href="{url}" '
                f'style="display:inline-block;background:#e8e8e8;color:#000;'
                f'padding:13px 26px;text-decoration:none;letter-spacing:0.1em;'
                f'text-transform:uppercase;font-size:12px">{label}</a></p>')

    if event == 'new_booking_for_artist':
        subject = f'InkLink — New booking from {ctx.get("other_name") or "a client"}'
        rows = [
            ('Client', other),
            ('When', when),
        ]
        if note:
            rows.append(('Notes', note))
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p>You have a new booking request:</p>'
            + '<table style="margin:14px 0;font-size:13px;line-height:1.9">'
            + ''.join(f'<tr><td style="color:#888;padding-right:14px;vertical-align:top">{k}:</td>'
                      f'<td>{v}</td></tr>' for k, v in rows)
            + '</table>'
            + cta('View booking')
        )
        return subject, header + body + footer

    if event == 'booking_confirmed_for_client':
        subject = f'InkLink — Booking confirmed with {ctx.get("other_name") or "your tattooer"}'
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p><strong>{other}</strong> confirmed your booking:</p>'
            f'<p style="font-size:18px;color:#fff;margin:18px 0">{when}</p>'
            + cta('View booking')
        )
        return subject, header + body + footer

    if event == 'booking_cancelled':
        actor_role = _h(ctx.get('actor_role') or 'The other party')
        refund_pct = ctx.get('refund_pct')
        refund_line = ''
        if refund_pct is not None:
            refund_line = (f'<p style="color:#aaa;font-size:13px">'
                           f'Deposit refund: <strong style="color:#fff">{int(refund_pct)} %</strong>.</p>')
        subject = 'InkLink — Booking cancelled'
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p>{actor_role} cancelled the booking scheduled for '
            f'<strong style="color:#fff">{when}</strong>.</p>'
            + refund_line
            + cta('View details')
        )
        return subject, header + body + footer

    if event == 'review_request_for_client':
        subject = f'InkLink — How was your session with {ctx.get("other_name") or "your tattooer"}?'
        review_url = _h(ctx.get('review_url') or url)
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p>Your session with <strong>{other}</strong> on {when} is marked complete.</p>'
            f'<p>Could you take a minute to leave a review? It helps other clients '
            f'find great tattooers.</p>'
            f'<p style="margin-top:22px"><a href="{review_url}" '
            f'style="display:inline-block;background:#e8e8e8;color:#000;'
            f'padding:13px 26px;text-decoration:none;letter-spacing:0.1em;'
            f'text-transform:uppercase;font-size:12px">Leave a review</a></p>'
        )
        return subject, header + body + footer

    if event == 'reminder_for_client':
        subject = f'InkLink — Tomorrow you\'ve got a tattoo session'
        studio = _h(ctx.get('studio') or '')
        city = _h(ctx.get('city') or '')
        location_parts = [x for x in (studio, city) if x]
        location_line = ''
        if location_parts:
            location_line = (f'<p style="color:#aaa;font-size:13px;margin-top:8px">'
                             f'Where: <strong style="color:#fff">{", ".join(location_parts)}</strong></p>')
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p>Quick reminder — tomorrow you\'ve got a tattoo session with '
            f'<strong>{other}</strong>:</p>'
            f'<p style="font-size:20px;color:#fff;margin:18px 0">{when}</p>'
            + location_line
            + cta('View booking')
        )
        return subject, header + body + footer

    if event == 'reminder_for_artist':
        subject = f'InkLink — Tomorrow {ctx.get("other_name") or "a client"} is coming in'
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p>Quick reminder — <strong>{other}</strong> is booked in tomorrow:</p>'
            f'<p style="font-size:20px;color:#fff;margin:18px 0">{when}</p>'
            + cta('View booking')
        )
        return subject, header + body + footer

    return None, None


def send_booking_email(conn, user_id, event, ctx):
    """Load user.email and dispatch the booking email. Never raises;
    returns True on success, False otherwise. Booking endpoints must
    not depend on this succeeding."""
    if not RESEND_API_KEY:
        return False
    try:
        u = conn.execute('SELECT email, display_name FROM users WHERE id=?',
                         (user_id,)).fetchone()
        if not u or not u['email']:
            return False
        c = dict(ctx)
        c.setdefault('recipient_name', u['display_name'] or 'there')
        c.setdefault('booking_url', APP_BASE_URL + '/my-bookings')
        subject, html = _booking_email_html(event, c)
        if not subject or not html:
            return False
        return send_email(u['email'], subject, html)
    except Exception as e:
        try:
            app.logger.error(f'[booking_email] {event} for user {user_id}: {e}')
        except Exception:
            print(f'[booking_email ERROR] {e}')
        return False


app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB (pro video)

UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER', 'uploads')
ALLOWED_EXTENSIONS = {'mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac'}
DB_PATH = os.environ.get('DB_PATH', 'inklink.db')

# Railway DATABASE_URL (PostgreSQL) or local SQLite
DATABASE_URL = os.environ.get('DATABASE_URL', '')
if DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = 'postgresql://' + DATABASE_URL[len('postgres://'):]

os.makedirs(UPLOAD_FOLDER, exist_ok=True)


# ── Database wrapper (SQLite locally, PostgreSQL in production) ───────────────

class DBCursor:
    """Unified cursor for sqlite3 and psycopg2."""
    __slots__ = ('_cur', '_pg')

    def __init__(self, cur, pg: bool):
        self._cur = cur
        self._pg  = pg

    @staticmethod
    def _adapt(sql: str, pg: bool) -> str:
        if not pg:
            return sql
        sql = sql.replace('?', '%s')
        sql = sql.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'SERIAL PRIMARY KEY')
        sql = sql.replace('last_insert_rowid()', 'lastval()')
        sql = sql.replace('DEFAULT ""', "DEFAULT ''")
        return sql

    def execute(self, sql, params=()):
        self._cur.execute(self._adapt(sql, self._pg), params)
        return self

    def fetchone(self):  return self._cur.fetchone()
    def fetchall(self):  return self._cur.fetchall()
    def __iter__(self):  return iter(self._cur)

    @property
    def lastrowid(self): return self._cur.lastrowid


class DBConn:
    """Unified connection wrapper — SQLite for local dev, PostgreSQL in prod."""

    def __init__(self):
        if DATABASE_URL and psycopg2:
            self._conn = psycopg2.connect(
                DATABASE_URL,
                cursor_factory=psycopg2.extras.DictCursor,
            )
            self._pg = True
        else:
            self._conn = sqlite3.connect(DB_PATH)
            self._conn.row_factory = sqlite3.Row
            self._pg = False

    def cursor(self) -> DBCursor:
        if self._pg:
            return DBCursor(
                self._conn.cursor(cursor_factory=psycopg2.extras.DictCursor), True
            )
        return DBCursor(self._conn.cursor(), False)

    def execute(self, sql, params=()):
        c = self.cursor()
        c.execute(sql, params)
        return c

    def commit(self):    self._conn.commit()
    def close(self):     self._conn.close()
    def rollback(self):  self._conn.rollback()


def get_db() -> DBConn:
    return DBConn()


def init_db():
    conn = get_db()
    c = conn.cursor()

    def add_col(table: str, col_def: str):
        """Safely add a column to an existing table (idempotent)."""
        if conn._pg:
            c.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col_def}')
        else:
            try:
                c.execute(f'ALTER TABLE {table} ADD COLUMN {col_def}')
                conn.commit()
            except Exception:
                pass

    # ── users ───────────────────────────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        display_name  TEXT NOT NULL,
        city          TEXT DEFAULT '',
        bio           TEXT DEFAULT '',
        avatar        TEXT DEFAULT '',
        emoji         TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    for col in ('lat REAL DEFAULT NULL', 'lng REAL DEFAULT NULL',
                'email TEXT DEFAULT ""', 'phone TEXT DEFAULT ""',
                'verified INTEGER DEFAULT 1',
                'verify_code TEXT DEFAULT NULL',
                'verify_expires TEXT DEFAULT NULL',
                # Legacy from hear-me-out fork — kept so old SELECTs don't break
                'genres TEXT DEFAULT ""',
                'photo1 TEXT DEFAULT ""', 'photo2 TEXT DEFAULT ""',
                'photo3 TEXT DEFAULT ""', 'photo4 TEXT DEFAULT ""',
                'pro INTEGER DEFAULT 0',
                # InkLink: artist-specific fields on users
                'is_artist INTEGER DEFAULT 0',
                'artist_slug TEXT DEFAULT NULL',
                'studio TEXT DEFAULT ""',
                'instagram TEXT DEFAULT ""',
                'styles TEXT DEFAULT ""',
                'deposit_pct_default INTEGER DEFAULT 30',
                'stripe_account_id TEXT DEFAULT NULL',
                'stripe_charges_enabled INTEGER DEFAULT 0',
                'stripe_payouts_enabled INTEGER DEFAULT 0',
                'stripe_details_submitted INTEGER DEFAULT 0',
                'verified_artist_at TEXT DEFAULT NULL',
                # Default hodinová sazba (předvyplní se v každém novém bloku)
                'hourly_rate_min INTEGER DEFAULT NULL',
                'hourly_rate_max INTEGER DEFAULT NULL',
                # M7: defaultní platební režim tatéra
                # 'deposit' (jen záloha + doplatek na místě), 'full' (vše předem),
                # 'client_choice' (klient si vybere v modalu)
                "default_payment_mode TEXT DEFAULT 'deposit'",
                # iCal feed token — opaque slug pro Apple/Google Calendar subscription
                'calendar_token TEXT DEFAULT NULL',
                # Admin flag — moderace, dashboard. Lze taky přes ADMIN_USERNAME env.
                'is_admin INTEGER DEFAULT 0'):
        add_col('users', col)
    conn.commit()

    # ── follows / messages / favorite_cities (kept from hear-me-out) ────────
    c.execute('''CREATE TABLE IF NOT EXISTS follows (
        follower_id  INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        PRIMARY KEY (follower_id, following_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS messages (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id    INTEGER NOT NULL,
        receiver_id  INTEGER NOT NULL,
        content      TEXT NOT NULL,
        content_type TEXT DEFAULT 'text',
        image        TEXT DEFAULT '',
        read         INTEGER DEFAULT 0,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (sender_id)   REFERENCES users(id),
        FOREIGN KEY (receiver_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS favorite_cities (
        user_id  INTEGER NOT NULL,
        name     TEXT NOT NULL,
        lat      REAL NOT NULL,
        lng      REAL NOT NULL,
        PRIMARY KEY (user_id, name),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # ── events / event_saves (kept) ─────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        title       TEXT NOT NULL,
        date        TEXT NOT NULL,
        time        TEXT DEFAULT '',
        venue       TEXT DEFAULT '',
        city        TEXT DEFAULT '',
        genre       TEXT DEFAULT '',
        description TEXT DEFAULT '',
        link        TEXT DEFAULT '',
        lat         REAL DEFAULT NULL,
        lng         REAL DEFAULT NULL,
        photo1      TEXT DEFAULT '',
        photo2      TEXT DEFAULT '',
        photo3      TEXT DEFAULT '',
        photo4      TEXT DEFAULT '',
        photo5      TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS event_saves (
        user_id  INTEGER NOT NULL,
        event_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, event_id)
    )''')

    # ── notifications / push_subscriptions / password_reset_tokens (kept) ───
    c.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        actor_id   INTEGER,
        type       TEXT    NOT NULL,
        ref_id     INTEGER,
        ref_type   TEXT,
        message    TEXT    NOT NULL,
        read       INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS push_subscriptions (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        endpoint   TEXT    NOT NULL UNIQUE,
        p256dh     TEXT    NOT NULL,
        auth       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        token      TEXT    NOT NULL UNIQUE,
        expires_at TEXT    NOT NULL,
        used       INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ── InkLink-specific: portfolio_items ───────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_items (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        image      TEXT NOT NULL,
        caption    TEXT DEFAULT '',
        kind       TEXT DEFAULT 'done',
        styles     TEXT DEFAULT '',
        like_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    # Tatér může u sketche / návrhu navrhnout fixní celkovou cenu + odhad délky
    add_col('portfolio_items', 'price_kc INTEGER DEFAULT NULL')
    add_col('portfolio_items', 'estimated_hours REAL DEFAULT NULL')
    # Multi-photo support (1-4 fotek). image je primary, image2-4 jsou volitelné.
    add_col('portfolio_items', 'image2 TEXT DEFAULT NULL')
    add_col('portfolio_items', 'image3 TEXT DEFAULT NULL')
    add_col('portfolio_items', 'image4 TEXT DEFAULT NULL')

    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_likes (
        user_id     INTEGER NOT NULL,
        item_id     INTEGER NOT NULL,
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, item_id)
    )''')

    # ── InkLink-specific: slots (bloky dostupnosti tatérů) ──────────────────
    # price_unit='hour' (default) → price_min/max je sazba ZA HODINU; klient
    # si rezervuje sub-range a deposit se počítá z duration*hourly*pct.
    # price_unit='flat' → price_min/max je TOTAL pro celý slot (legacy);
    # rezervace zabere celý blok najednou.
    c.execute('''CREATE TABLE IF NOT EXISTS slots (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL,
        start_at            TEXT NOT NULL,
        end_at              TEXT NOT NULL,
        status              TEXT DEFAULT 'free',
        price_min           INTEGER DEFAULT 0,
        price_max           INTEGER DEFAULT 0,
        deposit_pct         INTEGER DEFAULT NULL,
        note                TEXT DEFAULT '',
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')
    add_col('slots', "price_unit TEXT DEFAULT 'hour'")
    add_col('slots', 'min_duration_hours INTEGER DEFAULT 1')

    # ── InkLink-specific: bookings ──────────────────────────────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS bookings (
        id                       INTEGER PRIMARY KEY AUTOINCREMENT,
        slot_id                  INTEGER NOT NULL,
        artist_id                INTEGER NOT NULL,
        client_id                INTEGER NOT NULL,
        status                   TEXT DEFAULT 'pending_payment',
        deposit_cents            INTEGER NOT NULL DEFAULT 0,
        platform_fee_cents       INTEGER NOT NULL DEFAULT 0,
        currency                 TEXT DEFAULT 'CZK',
        stripe_payment_intent_id TEXT DEFAULT NULL,
        stripe_charge_id         TEXT DEFAULT NULL,
        refund_cents             INTEGER NOT NULL DEFAULT 0,
        onsite_amount_cents      INTEGER NOT NULL DEFAULT 0,
        design_note              TEXT DEFAULT '',
        cancellation_actor       TEXT DEFAULT NULL,
        created_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        confirmed_at             TIMESTAMP DEFAULT NULL,
        cancelled_at             TIMESTAMP DEFAULT NULL,
        completed_at             TIMESTAMP DEFAULT NULL,
        FOREIGN KEY (slot_id)    REFERENCES slots(id),
        FOREIGN KEY (artist_id)  REFERENCES users(id),
        FOREIGN KEY (client_id)  REFERENCES users(id)
    )''')
    # Sub-range a velikost rezervace v rámci slot bloku
    add_col('bookings', 'booking_start_at TEXT DEFAULT NULL')
    add_col('bookings', 'booking_end_at TEXT DEFAULT NULL')
    add_col('bookings', 'duration_hours REAL DEFAULT NULL')
    add_col('bookings', "size_label TEXT DEFAULT ''")
    # Klient se může rezervovat na konkrétní portfolio sketch — pak se cena fixuje
    add_col('bookings', 'portfolio_item_id INTEGER DEFAULT NULL')
    # 24h reminder — kdy byl reminder email/push odeslán, aby se neopakoval
    add_col('bookings', 'reminder_sent_at TEXT DEFAULT NULL')
    # M7: Plná platba předem + doplatek přes platformu
    add_col('bookings', "payment_mode TEXT DEFAULT 'deposit'")          # 'deposit'|'full'
    add_col('bookings', 'total_price_cents INTEGER NOT NULL DEFAULT 0') # celková cena při bookingu
    add_col('bookings', 'balance_due_cents INTEGER NOT NULL DEFAULT 0') # zbývá doplatit
    add_col('bookings', 'balance_paid_cents INTEGER NOT NULL DEFAULT 0')# kolik už doplaceno přes platformu
    add_col('bookings', 'balance_payment_intent_id TEXT DEFAULT NULL')
    # Aktuální vystavený doplatek (čeká na zaplacení); v demo se naplní hned,
    # v live se napárlujeme se Stripe PaymentIntent.amount
    add_col('bookings', 'balance_charge_cents INTEGER NOT NULL DEFAULT 0')
    add_col('bookings', 'balance_charge_fee_cents INTEGER NOT NULL DEFAULT 0')

    # ── InkLink: reviews (klient hodnotí dokončené sezení) ──────────────────
    c.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id   INTEGER NOT NULL UNIQUE,
        client_id    INTEGER NOT NULL,
        artist_id    INTEGER NOT NULL,
        rating       INTEGER NOT NULL,
        text         TEXT DEFAULT '',
        response     TEXT DEFAULT '',
        response_at  TIMESTAMP DEFAULT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (booking_id) REFERENCES bookings(id) ON DELETE CASCADE,
        FOREIGN KEY (client_id)  REFERENCES users(id),
        FOREIGN KEY (artist_id)  REFERENCES users(id)
    )''')

    # Reportace nevhodných recenzí — moderace
    c.execute('''CREATE TABLE IF NOT EXISTS review_reports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        review_id   INTEGER NOT NULL,
        reporter_id INTEGER NOT NULL,
        reason      TEXT NOT NULL,
        note        TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved    INTEGER DEFAULT 0,
        FOREIGN KEY (review_id)   REFERENCES reviews(id) ON DELETE CASCADE,
        FOREIGN KEY (reporter_id) REFERENCES users(id)
    )''')

    # ── studios / studio_members / studio_invites ───────────────────────────
    # Studio je organizační + brand entita. Tatéři jsou členové; jeden je
    # admin. Stripe/payouts/rezervace zůstávají per-artist — studio jen
    # seskupuje portfolio a má veřejnou stránku /studio/<slug>.
    c.execute('''CREATE TABLE IF NOT EXISTS studios (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        slug         TEXT UNIQUE NOT NULL,
        name         TEXT NOT NULL,
        description  TEXT DEFAULT '',
        address      TEXT DEFAULT '',
        city         TEXT DEFAULT '',
        country      TEXT DEFAULT '',
        lat          REAL DEFAULT NULL,
        lng          REAL DEFAULT NULL,
        logo         TEXT DEFAULT '',
        photos       TEXT DEFAULT '[]',
        instagram    TEXT DEFAULT '',
        website      TEXT DEFAULT '',
        phone        TEXT DEFAULT '',
        email        TEXT DEFAULT '',
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_studios_city ON studios(city)')

    # studio_members: vazba tatér ↔ studio. UNIQUE artist_id znamená,
    # že tatér může být jen v jednom studiu zároveň (MVP).
    c.execute('''CREATE TABLE IF NOT EXISTS studio_members (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id   INTEGER NOT NULL,
        artist_id   INTEGER NOT NULL UNIQUE,
        role        TEXT NOT NULL DEFAULT 'member',
        joined_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (studio_id) REFERENCES studios(id) ON DELETE CASCADE,
        FOREIGN KEY (artist_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_studio_members_studio ON studio_members(studio_id)')

    c.execute('''CREATE TABLE IF NOT EXISTS studio_invites (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        studio_id    INTEGER NOT NULL,
        email        TEXT NOT NULL,
        token        TEXT UNIQUE NOT NULL,
        invited_by   INTEGER NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        expires_at   TIMESTAMP NOT NULL,
        accepted_at  TIMESTAMP DEFAULT NULL,
        declined_at  TIMESTAMP DEFAULT NULL,
        FOREIGN KEY (studio_id)  REFERENCES studios(id) ON DELETE CASCADE,
        FOREIGN KEY (invited_by) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_studio_invites_studio ON studio_invites(studio_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_studio_invites_email ON studio_invites(email)')

    # ── native_push_tokens (iOS APNs / Android FCM tokeny z Capacitor app) ──
    # Web VAPID push žije v push_subscriptions (endpoint/p256dh/auth schéma).
    # Native push má jiný formát — token string + provider.
    c.execute('''CREATE TABLE IF NOT EXISTS native_push_tokens (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        token      TEXT    NOT NULL UNIQUE,
        provider   TEXT    NOT NULL,
        platform   TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_native_push_user ON native_push_tokens(user_id)')

    conn.commit()
    conn.close()


# ── Helpers ───────────────────────────────────────────────────────────────────

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# Magic bytes for audio/video formats
AUDIO_MAGIC = [
    b'ID3',               # MP3
    b'\xff\xfb', b'\xff\xf3', b'\xff\xf2',  # MP3 frames
    b'OggS',              # OGG
    b'fLaC',              # FLAC
    b'RIFF',              # WAV
]
VIDEO_MAGIC = [
    b'\x00\x00\x00\x18ftyp', b'\x00\x00\x00\x20ftyp',  # MP4/M4V
    b'\x1aE\xdf\xa3',    # MKV/WebM
]
IMAGE_MAGIC = [
    b'\xff\xd8\xff',      # JPEG
    b'\x89PNG',           # PNG
    b'GIF8',              # GIF
    b'RIFF',              # WebP (starts with RIFF)
]

def check_magic(stream, magic_list):
    header = stream.read(32)
    stream.seek(0)
    return any(header.startswith(m) for m in magic_list)

def allowed_image(file_storage):
    name = (file_storage.filename or '').lower()
    if not any(name.endswith(e) for e in ('.jpg', '.jpeg', '.png', '.gif', '.webp')):
        return False
    return check_magic(file_storage.stream, IMAGE_MAGIC)

def allowed_audio(file_storage):
    if not allowed_file(file_storage.filename):
        return False
    return check_magic(file_storage.stream, AUDIO_MAGIC)

def allowed_video(file_storage):
    name = (file_storage.filename or '').lower()
    if not any(name.endswith(e) for e in ('.mp4', '.mov', '.webm', '.mkv')):
        return False
    return check_magic(file_storage.stream, AUDIO_MAGIC + VIDEO_MAGIC)


def time_ago(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        diff = datetime.now() - dt
        secs = int(diff.total_seconds())
        if secs < 60:
            return 'právě teď'
        if secs < 3600:
            return f'{secs // 60}m ago'
        if diff.days == 0:
            return f'{secs // 3600}h ago'
        if diff.days == 1:
            return '1d ago'
        return f'{diff.days}d ago'
    except Exception:
        return 'recently'


def haversine(lat1, lng1, lat2, lng2):
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def initials(name):
    parts = name.split()
    return ''.join(p[0] for p in parts[:2]).upper() if parts else '?'


def send_web_push(user_id: int, title: str, body: str, url: str = '/'):
    """Sends a browser push notification to all subscriptions of a user."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        return
    try:
        from pywebpush import webpush, WebPushException
        import json, base64
        pem = base64.urlsafe_b64decode(VAPID_PRIVATE_KEY + '==')
        conn = get_db()
        subs = conn.execute(
            'SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?',
            (user_id,)
        ).fetchall()
        conn.close()
        payload = json.dumps({'title': title, 'body': body, 'url': url})
        dead = []
        for sub in subs:
            try:
                webpush(
                    subscription_info={'endpoint': sub['endpoint'],
                                       'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                    data=payload,
                    vapid_private_key=pem,
                    vapid_claims={'sub': 'mailto:admin@inklink.app',
                                  'aud': sub['endpoint'].split('/', 3)[:3][2] if '/' in sub['endpoint'] else sub['endpoint']},
                )
            except WebPushException as ex:
                if ex.response and ex.response.status_code in (404, 410):
                    dead.append(sub['endpoint'])
            except Exception:
                pass
        if dead:
            c2 = get_db()
            for ep in dead:
                c2.execute('DELETE FROM push_subscriptions WHERE endpoint = ?', (ep,))
            c2.commit()
            c2.close()
    except Exception as e:
        print(f'[PUSH] {e}')


def push_notif(conn, user_id, actor_id, notif_type, ref_id, ref_type, message):
    if user_id == actor_id:
        return
    conn.execute(
        'INSERT INTO notifications (user_id, actor_id, type, ref_id, ref_type, message) VALUES (?,?,?,?,?,?)',
        (user_id, actor_id, notif_type, ref_id, ref_type, message)
    )
    send_web_push(user_id, 'InkLink', message, '/')


def require_login():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    return None


ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip().lower()


def is_admin_user(user_id):
    """Vrátí True pokud user má is_admin=1 v DB nebo username matchuje
    ADMIN_USERNAME env var (bootstrap fallback)."""
    if not user_id:
        return False
    conn = get_db()
    row = conn.execute('SELECT username, is_admin FROM users WHERE id=?', (user_id,)).fetchone()
    conn.close()
    if not row:
        return False
    if row['is_admin']:
        return True
    if ADMIN_USERNAME and (row['username'] or '').lower() == ADMIN_USERNAME:
        return True
    return False


def require_admin():
    err = require_login()
    if err: return err
    if not is_admin_user(session.get('user_id')):
        return jsonify({'error': 'Admin only'}), 403
    return None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    return send_from_directory('public', 'sw.js', mimetype='application/javascript')

@app.route('/robots.txt')
def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /uploads/\n"
        "Disallow: /verify\n"
        "Disallow: /artist-setup\n"
        "Disallow: /my-bookings\n"
        "Disallow: /messages\n"
        "Disallow: /earnings\n"
        "Disallow: /liked\n"
        "Disallow: /balance-pay/\n"
        "Disallow: /forgot-password\n"
        "Disallow: /reset-password\n"
        f"\nSitemap: {APP_BASE_URL}/sitemap.xml\n"
    )
    return Response(body, mimetype='text/plain')


@app.route('/sitemap.xml')
def sitemap():
    """Dynamicky vygeneruje sitemap z DB — homepage, statické stránky,
    profily tatérů a detail stránky skic/prací."""
    base = APP_BASE_URL.rstrip('/')
    conn = get_db()
    # Tatéři s public profile
    artists = conn.execute('''
        SELECT username, COALESCE(
            (SELECT MAX(created_at) FROM portfolio_items WHERE user_id = users.id),
            users.created_at
        ) AS updated
        FROM users WHERE is_artist = 1 AND username IS NOT NULL
        ORDER BY updated DESC
        LIMIT 5000
    ''').fetchall()
    items = conn.execute('''
        SELECT id, created_at FROM portfolio_items
        ORDER BY created_at DESC
        LIMIT 5000
    ''').fetchall()
    conn.close()

    def _lastmod(iso):
        if not iso: return ''
        try:
            return iso[:10]  # YYYY-MM-DD
        except Exception:
            return ''

    urls = []
    # Static / klíčové stránky
    urls.append((f'{base}/', 'hourly', '1.0', ''))
    urls.append((f'{base}/feed', 'hourly', '0.9', ''))
    urls.append((f'{base}/map', 'weekly', '0.8', ''))
    urls.append((f'{base}/events', 'daily', '0.7', ''))
    urls.append((f'{base}/login', 'monthly', '0.5', ''))
    urls.append((f'{base}/terms', 'yearly', '0.3', ''))
    urls.append((f'{base}/privacy', 'yearly', '0.3', ''))
    # Tatéři
    for a in artists:
        urls.append((f'{base}/profile/{a["username"]}', 'weekly', '0.7', _lastmod(a['updated'])))
    # Portfolio items (sketches + done)
    for it in items:
        urls.append((f'{base}/sketch/{it["id"]}', 'weekly', '0.6', _lastmod(it['created_at'])))

    parts = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, freq, prio, mod in urls:
        parts.append('  <url>')
        parts.append(f'    <loc>{html_escape(loc)}</loc>')
        if mod:
            parts.append(f'    <lastmod>{mod}</lastmod>')
        parts.append(f'    <changefreq>{freq}</changefreq>')
        parts.append(f'    <priority>{prio}</priority>')
        parts.append('  </url>')
    parts.append('</urlset>')
    return Response('\n'.join(parts), mimetype='application/xml')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('public', 'manifest.json', mimetype='application/manifest+json')

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory('public/icons', filename)


@app.route('/fonts/<path:filename>')
def fonts(filename):
    # Explicit MIME types pro web fonty (některé deploy targety nemají defaults)
    mime = None
    fl = filename.lower()
    if fl.endswith('.otf'):
        mime = 'font/otf'
    elif fl.endswith('.ttf'):
        mime = 'font/ttf'
    elif fl.endswith('.woff'):
        mime = 'font/woff'
    elif fl.endswith('.woff2'):
        mime = 'font/woff2'
    return send_from_directory('public/fonts', filename, mimetype=mime)

@app.route('/logo-1080.svg')
def logo_download():
    return send_from_directory('public', 'logo-1080.svg', mimetype='image/svg+xml')

@app.route('/')
def index():
    # Přihlášený uživatel → feed; host → landing
    if 'user_id' in session:
        return send_from_directory('public', 'index.html')
    return send_from_directory('public', 'landing.html')


@app.route('/feed')
def feed_page():
    return send_from_directory('public', 'index.html')


@app.route('/landing')
def landing_page():
    return send_from_directory('public', 'landing.html')


@app.route('/style-guide')
def style_guide_page():
    return send_from_directory('public', 'style-guide.html')


@app.route('/icons.svg')
def icons_sprite():
    return send_from_directory('public', 'icons.svg', mimetype='image/svg+xml')


@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/')
    return send_from_directory('public', 'login.html')

@app.route('/verify')
def verify_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'verify.html')


@app.route('/artist-setup')
def artist_setup_page():
    return send_from_directory('public', 'artist-setup.html')


@app.route('/my-bookings')
def my_bookings_page():
    return send_from_directory('public', 'my-bookings.html')


@app.route('/calendar')
def calendar_page():
    return send_from_directory('public', 'calendar.html')


@app.route('/liked')
def liked_page():
    return send_from_directory('public', 'liked.html')


@app.route('/map')
def map_page():
    return send_from_directory('public', 'map.html')


@app.route('/earnings')
def earnings_page():
    return send_from_directory('public', 'earnings.html')


@app.route('/admin')
def admin_page():
    return send_from_directory('public', 'admin.html')


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    if _s3 and R2_PUBLIC_URL:
        return redirect(f'{R2_PUBLIC_URL}/{filename}')
    return send_from_directory(os.path.abspath(UPLOAD_FOLDER), filename)


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
@limiter.limit('30 per hour')
def register():
    data         = request.get_json()
    username     = data.get('username', '').strip().lower()
    display_name = data.get('display_name', '').strip()
    city         = data.get('city', '').strip()
    password     = data.get('password', '')
    email        = data.get('email', '').strip().lower()
    phone        = data.get('phone', '').strip()

    if not username or not display_name or not password or not email:
        return jsonify({'error': 'Vyplň uživatelské jméno, jméno, e-mail a heslo'}), 400
    if len(username) > 30:
        return jsonify({'error': 'Username je příliš dlouhý (max 30 znaků)'}), 400
    if len(display_name) > 60:
        return jsonify({'error': 'Jméno je příliš dlouhé (max 60 znaků)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Heslo musí mít aspoň 6 znaků'}), 400
    if len(password) > 128:
        return jsonify({'error': 'Heslo je příliš dlouhé (max 128 znaků)'}), 400
    if len(email) > 254:
        return jsonify({'error': 'E-mail je příliš dlouhý'}), 400
    if not username.replace('_', '').isalnum():
        return jsonify({'error': 'Username může obsahovat jen písmena, čísla a podtržítka'}), 400
    if '@' not in email or '.' not in email:
        return jsonify({'error': 'Zadej platný e-mail'}), 400

    # Pokud Resend není nakonfigurovaný, ověření e-mailem přeskočíme (dev fallback).
    # Email verify flow vypnutý — sandbox Resend bez ověřené domény blokuje
    # mail. Zapne se nastavením VERIFY_EMAIL=1 v Railway (až bude doména
    # ověřená v Resend) a samozřejmě platným RESEND_API_KEY.
    require_verify = bool(RESEND_API_KEY) and os.environ.get('VERIFY_EMAIL', '0') == '1'
    code    = str(random.randint(100000, 999999)) if require_verify else None
    expires = (datetime.utcnow() + timedelta(minutes=15)).isoformat() if require_verify else None
    verified_flag = 0 if require_verify else 1

    conn = get_db()
    existing_email = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing_email:
        conn.close()
        return jsonify({'error': 'Tento e-mail je již zaregistrován'}), 400
    try:
        conn.execute(
            'INSERT INTO users (username, display_name, city, password_hash, email, phone, verified, verify_code, verify_expires) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (username, display_name, city,
             generate_password_hash(password, method='pbkdf2:sha256'),
             email, phone, verified_flag, code, expires)
        )
        conn.commit()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        session['user_id']      = user['id']
        session['username']     = user['username']
        session['display_name'] = user['display_name']
        if require_verify:
            session['pending_verify'] = True
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Username je už obsazený'}), 400
    finally:
        conn.close()

    email_sent = True
    if require_verify:
        email_sent = send_email(email, 'InkLink — ověření účtu', f'''
        <div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px;margin:0 auto">
          <div style="font-size:28px;letter-spacing:0.2em;color:#b20000;margin-bottom:8px">INKLINK</div>
          <div style="font-size:12px;color:#555;margin-bottom:32px;letter-spacing:0.1em">Tattoo Booking Network</div>
          <p style="margin-bottom:16px">Ahoj <strong>{display_name}</strong>, použij tento kód pro ověření účtu:</p>
          <div style="font-size:40px;letter-spacing:0.3em;color:#c62828;background:#0e0e0e;padding:20px;text-align:center;border:1px solid #1a1a1a;margin:24px 0">{code}</div>
          <p style="color:#555;font-size:12px">Platnost 15 minut. Pokud ses neregistroval(a), e-mail ignoruj.</p>
        </div>''')

    return jsonify({'ok': True, 'verify': require_verify, 'email_sent': email_sent})


@app.route('/api/verify-email', methods=['POST'])
@limiter.limit('10 per hour')
def verify_email():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.get_json()
    code = (data.get('code') or '').strip()
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user:
        conn.close(); return jsonify({'error': 'User not found'}), 404
    if user['verified']:
        conn.close(); return jsonify({'ok': True})
    if not user['verify_code'] or user['verify_code'] != code:
        conn.close(); return jsonify({'error': 'Incorrect code'}), 400
    if datetime.utcnow().isoformat() > user['verify_expires']:
        conn.close(); return jsonify({'error': 'Code expired — request a new one'}), 400
    conn.execute('UPDATE users SET verified = 1, verify_code = NULL, verify_expires = NULL WHERE id = ?', (session['user_id'],))
    conn.commit(); conn.close()
    session.pop('pending_verify', None)
    return jsonify({'ok': True})


@app.route('/api/resend-verify', methods=['POST'])
@limiter.limit('5 per hour')
def resend_verify():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user or user['verified']:
        conn.close(); return jsonify({'ok': True})
    code    = str(random.randint(100000, 999999))
    expires = (datetime.utcnow() + timedelta(minutes=15)).isoformat()
    conn.execute('UPDATE users SET verify_code = ?, verify_expires = ? WHERE id = ?', (code, expires, user['id']))
    conn.commit(); conn.close()
    sent = send_email(user['email'], 'InkLink — nový ověřovací kód', f'''
    <div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px;margin:0 auto">
      <div style="font-size:28px;letter-spacing:0.2em;color:#b20000;margin-bottom:32px">INKLINK</div>
      <p style="margin-bottom:16px">Nový ověřovací kód:</p>
      <div style="font-size:40px;letter-spacing:0.3em;color:#c62828;background:#0e0e0e;padding:20px;text-align:center;border:1px solid #1a1a1a;margin:24px 0">{code}</div>
      <p style="color:#555;font-size:12px">Platnost 15 minut.</p>
    </div>''')
    if not sent:
        return jsonify({'ok': False, 'error': 'E-mail se teď nepodařilo odeslat. Zkus to za chvíli.'}), 500
    return jsonify({'ok': True})


@app.route('/api/forgot-password', methods=['POST'])
@limiter.limit('5 per hour')
def forgot_password():
    data  = request.get_json()
    email = (data.get('email') or '').strip().lower()
    if not email:
        return jsonify({'error': 'Please enter your email address'}), 400
    conn = get_db()
    user = conn.execute('SELECT id, display_name FROM users WHERE email = ?', (email,)).fetchone()
    if not user:
        conn.close()
        # Don't reveal whether email exists
        return jsonify({'ok': True})
    token   = uuid.uuid4().hex + uuid.uuid4().hex
    expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    conn.execute(
        'INSERT INTO password_reset_tokens (user_id, token, expires_at) VALUES (?, ?, ?)',
        (user['id'], token, expires)
    )
    conn.commit()
    conn.close()
    reset_url = request.host_url.rstrip('/') + f'/reset-password?token={token}'
    send_email(email, 'InkLink — password reset', f'''
    <div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px;margin:0 auto">
      <div style="font-size:28px;letter-spacing:0.2em;color:#b20000;margin-bottom:8px">INKLINK</div>
      <div style="font-size:12px;color:#555;margin-bottom:32px;letter-spacing:0.1em">Tattoo Booking Network</div>
      <p style="margin-bottom:24px">Hi <strong>{user['display_name']}</strong>, we received a password reset request for your account.</p>
      <a href="{reset_url}" style="display:block;background:#b20000;color:#fff;text-align:center;padding:16px;font-family:sans-serif;font-size:14px;letter-spacing:0.1em;text-decoration:none;margin-bottom:24px">RESET YOUR PASSWORD</a>
      <p style="color:#555;font-size:12px">Link valid for 1 hour. If you didn't request this, ignore this email.</p>
    </div>''')
    return jsonify({'ok': True})


@app.route('/api/reset-password', methods=['POST'])
@limiter.limit('10 per hour')
def reset_password():
    data     = request.get_json()
    token    = (data.get('token') or '').strip()
    password = data.get('password', '')
    if not token or len(password) < 6:
        return jsonify({'error': 'Invalid request'}), 400
    conn = get_db()
    row = conn.execute(
        'SELECT * FROM password_reset_tokens WHERE token = ? AND used = 0',
        (token,)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Invalid or already used link'}), 400
    if datetime.utcnow().isoformat() > row['expires_at']:
        conn.close()
        return jsonify({'error': 'Link expired — please request a new one'}), 400
    new_hash = generate_password_hash(password)
    conn.execute('UPDATE users SET password_hash = ? WHERE id = ?', (new_hash, row['user_id']))
    conn.execute('UPDATE password_reset_tokens SET used = 1 WHERE token = ?', (token,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/forgot-password')
def forgot_password_page():
    return send_from_directory('public', 'forgot-password.html')

@app.route('/reset-password')
def reset_password_page():
    return send_from_directory('public', 'forgot-password.html')


@app.route('/api/login', methods=['POST'])
@limiter.limit('10 per minute; 30 per hour')
def login():
    data     = request.get_json()
    username = data.get('username', '').strip().lower()
    password = data.get('password', '')

    conn = get_db()
    user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401

    session['user_id']      = user['id']
    session['username']     = user['username']
    session['display_name'] = user['display_name']
    return jsonify({'ok': True})


@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/artists')
def browse_artists():
    q      = request.args.get('q', '').strip()
    offset = max(0, int(request.args.get('offset', 0)))
    limit  = min(24, int(request.args.get('limit', 24)))
    conn   = get_db()
    if q:
        like = f'%{q}%'
        rows = conn.execute('''
            SELECT id, username, display_name, city, genres, avatar, emoji
            FROM users WHERE display_name LIKE ? OR username LIKE ? OR city LIKE ? OR genres LIKE ?
            ORDER BY display_name ASC LIMIT ? OFFSET ?
        ''', (like, like, like, like, limit, offset)).fetchall()
    else:
        rows = conn.execute('''
            SELECT id, username, display_name, city, genres, avatar, emoji
            FROM users ORDER BY id DESC LIMIT ? OFFSET ?
        ''', (limit, offset)).fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'username': r['username'], 'display_name': r['display_name'],
        'city': r['city'] or '', 'genres': r['genres'] or '',
        'emoji': r['emoji'] or '',
        'avatar': f'/uploads/{r["avatar"]}' if r['avatar'] else '',
    } for r in rows])


@app.route('/api/search')
def global_search():
    """Globální vyhledávání pro nav search bar — tatéři, portfolio, eventy."""
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'artists': [], 'portfolio': [], 'events': []})
    conn = get_db()
    like = f'%{q}%'

    artists = conn.execute('''
        SELECT id, username, display_name, city, studio, avatar, styles,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = users.id) AS rating_avg,
               (SELECT COUNT(*)   FROM reviews WHERE artist_id = users.id) AS rating_count
        FROM users
        WHERE is_artist = 1
          AND (display_name LIKE ? OR username LIKE ? OR city LIKE ?
               OR studio LIKE ? OR styles LIKE ?)
        ORDER BY rating_count DESC, display_name ASC
        LIMIT 8
    ''', (like, like, like, like, like)).fetchall()

    portfolio = conn.execute('''
        SELECT p.id, p.image, p.caption, p.kind, p.price_kc, p.estimated_hours,
               u.username, u.display_name, u.avatar
        FROM portfolio_items p
        JOIN users u ON u.id = p.user_id
        WHERE u.is_artist = 1
          AND (p.caption LIKE ? OR p.styles LIKE ? OR u.display_name LIKE ? OR u.styles LIKE ?)
        ORDER BY p.created_at DESC
        LIMIT 8
    ''', (like, like, like, like)).fetchall()

    events = conn.execute('''
        SELECT e.id, e.title, e.date, e.city, e.genre
        FROM events e
        WHERE e.title LIKE ? OR e.city LIKE ? OR e.genre LIKE ?
        ORDER BY e.date ASC
        LIMIT 6
    ''', (like, like, like)).fetchall()

    conn.close()
    return jsonify({
        'artists': [{
            'id': r['id'], 'username': r['username'], 'display_name': r['display_name'],
            'city': r['city'] or '', 'studio': r['studio'] or '',
            'styles': r['styles'] or '',
            'avatar_url': f'/uploads/{r["avatar"]}' if r['avatar'] else None,
            'rating_avg':   round(r['rating_avg'], 2) if r['rating_avg'] else None,
            'rating_count': r['rating_count'] or 0,
        } for r in artists],
        'portfolio': [{
            'id': r['id'], 'image': r['image'], 'caption': r['caption'] or '',
            'kind': r['kind'] or 'done',
            'price_kc': r['price_kc'], 'estimated_hours': r['estimated_hours'],
            'username': r['username'], 'display_name': r['display_name'],
        } for r in portfolio],
        'events':   [{
            'id': r['id'], 'title': r['title'], 'date': r['date'],
            'city': r['city'] or '', 'genre': r['genre'] or '',
        } for r in events],
    })


@app.route('/api/push/vapid-key')
def push_vapid_key():
    return jsonify({'publicKey': VAPID_PUBLIC_KEY})


@app.route('/api/push/subscribe', methods=['POST'])
def push_subscribe():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.json or {}
    endpoint = data.get('endpoint', '')
    p256dh   = (data.get('keys') or {}).get('p256dh', '')
    auth     = (data.get('keys') or {}).get('auth', '')
    if not endpoint or not p256dh or not auth:
        return jsonify({'error': 'Invalid subscription'}), 400
    conn = get_db()
    if conn._pg:
        conn.execute(
            'INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?,?,?,?) ON CONFLICT (endpoint) DO UPDATE SET user_id=EXCLUDED.user_id, p256dh=EXCLUDED.p256dh, auth=EXCLUDED.auth',
            (session['user_id'], endpoint, p256dh, auth)
        )
    else:
        conn.execute(
            'INSERT OR REPLACE INTO push_subscriptions (user_id, endpoint, p256dh, auth) VALUES (?,?,?,?)',
            (session['user_id'], endpoint, p256dh, auth)
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/push/unsubscribe', methods=['POST'])
def push_unsubscribe():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.json or {}
    endpoint = data.get('endpoint', '')
    if endpoint:
        conn = get_db()
        conn.execute('DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?',
                     (endpoint, session['user_id']))
        conn.commit()
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/me')
def me():
    if 'user_id' not in session:
        return jsonify(None), 200
    conn = get_db()
    user = conn.execute('''SELECT id, username, display_name, city, avatar, emoji,
                                  is_artist, artist_slug, studio, instagram, styles,
                                  deposit_pct_default, hourly_rate_min, hourly_rate_max,
                                  default_payment_mode,
                                  stripe_account_id, stripe_charges_enabled,
                                  stripe_payouts_enabled, stripe_details_submitted
                           FROM users WHERE id = ?''',
                        (session['user_id'],)).fetchone()
    push_n = conn.execute('SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?',
                          (session['user_id'],)).fetchone()[0]
    conn.close()
    d = dict(user)
    d['avatar_url'] = f'/uploads/{d["avatar"]}' if d.get('avatar') else None
    d['is_artist'] = bool(d.get('is_artist'))
    d['can_accept_bookings'] = bool(d.get('stripe_charges_enabled'))
    d['push_subscriptions'] = push_n
    d['push_available'] = bool(VAPID_PUBLIC_KEY)
    return jsonify(d)


# ── Feed API ──────────────────────────────────────────────────────────────────

@app.route('/api/feed')
def feed():
    """Vrací nejnovější portfolio_items od ověřených tatérů jako InkLink feed."""
    uid = session.get('user_id', 0)

    city_filter   = request.args.get('city', '').strip()
    style_filter  = request.args.get('style', '').strip()
    kind_filter   = request.args.get('kind', '').strip().lower()
    search        = request.args.get('q', '').strip()
    sort          = request.args.get('sort', '').strip().lower()
    offset        = int(request.args.get('offset', 0))
    try:
        flat, flng = float(request.args.get('lat', '')), float(request.args.get('lng', ''))
        fradius    = float(request.args.get('radius', 50))
        gps_filter = True
    except (ValueError, TypeError):
        gps_filter = False

    conn   = get_db()
    params = [uid]

    query = '''
        SELECT p.*,
               u.username, u.display_name, u.city AS user_city, u.styles AS user_styles,
               u.emoji, u.avatar, u.lat, u.lng,
               u.is_artist, u.studio, u.stripe_charges_enabled,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = u.id) AS rating_count,
               EXISTS(SELECT 1 FROM portfolio_likes WHERE user_id = ? AND item_id = p.id) AS liked
        FROM portfolio_items p
        JOIN users u ON p.user_id = u.id
        WHERE u.is_artist = 1
    '''

    if city_filter and city_filter != 'All':
        query += ' AND u.city = ?'
        params.append(city_filter)
    if style_filter:
        query += ' AND (p.styles LIKE ? OR u.styles LIKE ?)'
        params += [f'%{style_filter}%', f'%{style_filter}%']
    if kind_filter in ('sketch', 'done'):
        query += ' AND p.kind = ?'
        params.append(kind_filter)
    if search:
        query += ' AND (p.caption LIKE ? OR u.display_name LIKE ? OR u.styles LIKE ?)'
        params += [f'%{search}%', f'%{search}%', f'%{search}%']

    # Sort order — všechny mají DESC fallback na created_at pro tie-break
    if sort == 'rating':
        # Top rated artists first (null ratings na konec)
        query += ' ORDER BY (SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) IS NULL, ' \
                 '(SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) DESC, p.created_at DESC'
    elif sort == 'price':
        # Nejlevnější skicy / práce (NULL ceny na konec)
        query += ' ORDER BY p.price_kc IS NULL, p.price_kc ASC, p.created_at DESC'
    elif sort == 'price_desc':
        # Nejdražší (NULL ceny na konec)
        query += ' ORDER BY p.price_kc IS NULL, p.price_kc DESC, p.created_at DESC'
    elif sort == 'popular':
        # Nejvíc lajknuté
        query += ' ORDER BY p.like_count DESC, p.created_at DESC'
    else:
        # 'newest' (default)
        query += ' ORDER BY p.created_at DESC'
    query += ' LIMIT 200 OFFSET ?'
    params.append(offset)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    if gps_filter:
        rows = [r for r in rows if r['lat'] is not None and r['lng'] is not None
                and haversine(flat, flng, r['lat'], r['lng']) <= fradius]
    rows = rows[:20]

    result = []
    for p in rows:
        result.append({
            'id':            p['id'],
            'image':         p['image'],
            'images':        _portfolio_images(p),
            'caption':       p['caption'] or '',
            'kind':          p['kind'] or 'done',
            'styles':        p['styles'] or '',
            'like_count':    p['like_count'],
            'liked':         bool(p['liked']),
            'price_kc':        p['price_kc'],
            'estimated_hours': p['estimated_hours'],
            'created_at':    time_ago(p['created_at']),
            'user': {
                'username':     p['username'],
                'display_name': p['display_name'],
                'city':         p['user_city'],
                'studio':       p['studio'] or '',
                'styles':       p['user_styles'] or '',
                'emoji':        p['emoji'] or '',
                'initials':     initials(p['display_name']),
                'avatar':       f'/uploads/{p["avatar"]}' if p['avatar'] else None,
                'lat':          p['lat'],
                'lng':          p['lng'],
                'is_artist':    bool(p['is_artist']),
                'can_book':     bool(p['stripe_charges_enabled']),
                'rating_avg':   round(p['rating_avg'], 1) if p['rating_avg'] else None,
                'rating_count': p['rating_count'] or 0,
            }
        })
    return jsonify(result)


@app.route('/api/liked')
def liked_feed():
    """Vrací portfolio_items, které přihlášený user lajknul. Stejný tvar jako /api/feed."""
    err = require_login()
    if err: return err
    uid = session['user_id']

    kind_filter = request.args.get('kind', '').strip().lower()

    conn = get_db()
    query = '''
        SELECT p.*,
               u.username, u.display_name, u.city AS user_city, u.styles AS user_styles,
               u.emoji, u.avatar, u.lat, u.lng,
               u.is_artist, u.studio, u.stripe_charges_enabled,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = u.id) AS rating_count,
               l.created_at AS liked_at
        FROM portfolio_likes l
        JOIN portfolio_items p ON p.id = l.item_id
        JOIN users u ON u.id = p.user_id
        WHERE l.user_id = ?
    '''
    params = [uid]
    if kind_filter in ('sketch', 'done'):
        query += ' AND p.kind = ?'
        params.append(kind_filter)
    query += ' ORDER BY l.created_at DESC LIMIT 200'

    rows = conn.execute(query, params).fetchall()
    conn.close()

    result = []
    for p in rows:
        result.append({
            'id':            p['id'],
            'image':         p['image'],
            'images':        _portfolio_images(p),
            'caption':       p['caption'] or '',
            'kind':          p['kind'] or 'done',
            'styles':        p['styles'] or '',
            'like_count':    p['like_count'],
            'liked':         True,
            'price_kc':        p['price_kc'],
            'estimated_hours': p['estimated_hours'],
            'created_at':    time_ago(p['created_at']),
            'user': {
                'username':     p['username'],
                'display_name': p['display_name'],
                'city':         p['user_city'],
                'studio':       p['studio'] or '',
                'styles':       p['user_styles'] or '',
                'emoji':        p['emoji'] or '',
                'initials':     initials(p['display_name']),
                'avatar':       f'/uploads/{p["avatar"]}' if p['avatar'] else None,
                'lat':          p['lat'],
                'lng':          p['lng'],
                'is_artist':    bool(p['is_artist']),
                'can_book':     bool(p['stripe_charges_enabled']),
                'rating_avg':   round(p['rating_avg'], 1) if p['rating_avg'] else None,
                'rating_count': p['rating_count'] or 0,
            }
        })
    return jsonify(result)


def _portfolio_images(row):
    """Vrátí list filenames v pořadí image, image2, image3, image4 (jen ne-null)."""
    out = []
    if row['image']: out.append(row['image'])
    for col in ('image2', 'image3', 'image4'):
        try:
            v = row[col]
            if v: out.append(v)
        except (KeyError, IndexError):
            pass
    return out


def _sketch_image_url(filename):
    """Vrátí absolutní URL pro OG image / sdílení (R2 nebo vlastní host)."""
    if R2_PUBLIC_URL:
        return f'{R2_PUBLIC_URL}/{filename}'
    return request.host_url.rstrip('/') + f'/uploads/{filename}'


@app.route('/api/sketch/<int:item_id>')
def sketch_detail(item_id):
    """JSON detail jedné skicy / práce — shape jako item v /api/feed."""
    uid = session.get('user_id', 0)
    conn = get_db()
    p = conn.execute('''
        SELECT p.*,
               u.username, u.display_name, u.city AS user_city, u.styles AS user_styles,
               u.emoji, u.avatar, u.lat, u.lng,
               u.is_artist, u.studio, u.stripe_charges_enabled,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = u.id) AS rating_count,
               EXISTS(SELECT 1 FROM portfolio_likes WHERE user_id = ? AND item_id = p.id) AS liked
        FROM portfolio_items p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
    ''', (uid, item_id)).fetchone()
    conn.close()
    if not p:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'id':            p['id'],
        'image':         p['image'],
        'caption':       p['caption'] or '',
        'kind':          p['kind'] or 'done',
        'styles':        p['styles'] or '',
        'like_count':    p['like_count'],
        'liked':         bool(p['liked']),
        'price_kc':        p['price_kc'],
        'estimated_hours': p['estimated_hours'],
        'created_at':    time_ago(p['created_at']),
        'user': {
            'username':     p['username'],
            'display_name': p['display_name'],
            'city':         p['user_city'],
            'studio':       p['studio'] or '',
            'styles':       p['user_styles'] or '',
            'emoji':        p['emoji'] or '',
            'initials':     initials(p['display_name']),
            'avatar':       f'/uploads/{p["avatar"]}' if p['avatar'] else None,
            'lat':          p['lat'],
            'lng':          p['lng'],
            'is_artist':    bool(p['is_artist']),
            'can_book':     bool(p['stripe_charges_enabled']),
            'rating_avg':   round(p['rating_avg'], 1) if p['rating_avg'] else None,
            'rating_count': p['rating_count'] or 0,
        }
    })


@app.route('/sketch/<int:item_id>')
def sketch_page(item_id):
    """Veřejná detail stránka skicy s OG meta tagy pro sdílení."""
    conn = get_db()
    p = conn.execute('''
        SELECT p.id, p.image, p.caption, p.kind, p.price_kc,
               u.display_name, u.studio
        FROM portfolio_items p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
    ''', (item_id,)).fetchone()
    conn.close()
    if not p:
        return send_from_directory('public', '404.html'), 404

    is_sketch = (p['kind'] or 'done') == 'sketch'
    artist = p['display_name'] or 'tatér'
    if is_sketch and p['price_kc']:
        try:
            price_s = f"{int(p['price_kc']):,}".replace(',', ' ') + ' Kč'
            og_title = f'Skica od {artist} — {price_s}'
        except (TypeError, ValueError):
            og_title = f'Skica od {artist}'
    elif is_sketch:
        og_title = f'Skica od {artist}'
    else:
        og_title = f'Tetování od {artist}'

    caption = (p['caption'] or '').strip()
    if caption:
        og_desc = caption[:180]
    else:
        og_desc = ('Skica k rezervaci na InkLinku' if is_sketch
                   else 'Hotová práce na InkLinku')

    og_image = _sketch_image_url(p['image'])
    og_url = request.host_url.rstrip('/') + f'/sketch/{p["id"]}'

    # JSON-LD structured data (schema.org ImageObject) — pomáhá Google
    # rendrovat rich preview se skicou v search results.
    import json as _json
    ldd = {
        "@context": "https://schema.org",
        "@type": "ImageObject",
        "contentUrl": og_image,
        "thumbnailUrl": og_image,
        "name": og_title,
        "description": og_desc,
        "url": og_url,
        "creator": {
            "@type": "Person",
            "name": artist,
        },
    }
    if is_sketch and p['price_kc']:
        ldd["offers"] = {
            "@type": "Offer",
            "price": int(p['price_kc']),
            "priceCurrency": "CZK",
        }
    json_ld = _json.dumps(ldd, ensure_ascii=False)

    with open(os.path.join('public', 'sketch.html'), 'r', encoding='utf-8') as f:
        page_html = f.read()
    page_html = (page_html
            .replace('{{OG_TITLE}}', html_escape(og_title))
            .replace('{{OG_DESC}}',  html_escape(og_desc))
            .replace('{{OG_IMAGE}}', html_escape(og_image))
            .replace('{{OG_URL}}',   html_escape(og_url))
            .replace('{{ITEM_ID}}',  str(p['id']))
            .replace('{{JSON_LD}}',  json_ld))
    return page_html


_STORY_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'public', 'fonts')


def _draw_il_symbol(draw, x, y, height, fill, bg=(0, 0, 0)):
    """Vykreslí IL monogram (pen-rotary I + plné L coil) jako solid filled
    na pozici x,y s danou výškou. ViewBox 240x180 proporčně přepočítaný.
    `bg` je barva pozadí — používá se pro cut-out cívek uvnitř L."""
    s = height / 180.0
    def sx(v): return int(x + v * s)
    def sy(v): return int(y + v * s)
    # I = pen-style rotary
    draw.rounded_rectangle([sx(44), sy(4),  sx(56), sy(12)],  radius=int(s*2), fill=fill)
    draw.rounded_rectangle([sx(30), sy(12), sx(70), sy(122)], radius=int(s*8), fill=fill)
    draw.polygon([(sx(38), sy(122)), (sx(62), sy(122)),
                  (sx(54), sy(148)), (sx(46), sy(148))], fill=fill)
    draw.rounded_rectangle([sx(49), sy(148), sx(51), sy(166)], radius=max(1, int(s*1)), fill=fill)
    # L = solid filled (continuous L tvar)
    draw.polygon([(sx(86), sy(10)),  (sx(134), sy(10)),  (sx(134), sy(130)),
                  (sx(196), sy(130)), (sx(196), sy(158)), (sx(86), sy(158))], fill=fill)
    # 2 cívky jako cut-outs v bg barvě
    draw.rounded_rectangle([sx(93),  sy(20), sx(107), sy(120)], radius=max(1, int(s*3)), fill=bg)
    draw.rounded_rectangle([sx(113), sy(20), sx(127), sy(120)], radius=max(1, int(s*3)), fill=bg)
    # Needle z konce L
    draw.rounded_rectangle([sx(196), sy(143), sx(218), sy(145)], radius=max(1, int(s*1)), fill=fill)


def _load_story_font(family, size):
    """Načti bundled font (Bebas / DMMono-Regular / DMMono-Medium).
    Fallback: pokud bundled chybí, použij systémový DejaVuSans a nakonec PIL default."""
    from PIL import ImageFont
    bundled = {
        'bebas':  'BebasNeue-Regular.ttf',
        'mono':   'DMMono-Regular.ttf',
        'monoB':  'DMMono-Medium.ttf',
    }
    path = os.path.join(_STORY_FONT_DIR, bundled.get(family, 'DMMono-Regular.ttf'))
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        for sys_path in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                         '/System/Library/Fonts/Helvetica.ttc'):
            try:
                return ImageFont.truetype(sys_path, size)
            except Exception:
                continue
        return ImageFont.load_default()


@app.route('/sketch/<int:item_id>/story.png')
def sketch_story_image(item_id):
    """1080×1920 PNG vhodný na Instagram Story: skica + branding + URL."""
    from PIL import Image, ImageDraw
    import io as _io
    import traceback as _tb

    conn = get_db()
    p = conn.execute('''
        SELECT p.id, p.image, p.kind, p.price_kc, p.estimated_hours,
               u.display_name, u.studio, u.city
        FROM portfolio_items p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
    ''', (item_id,)).fetchone()
    conn.close()
    if not p:
        return jsonify({'error': 'not found'}), 404

    # Načti původní obrázek (R2 přes boto3, jinak z disku)
    try:
        if _s3 and R2_BUCKET:
            obj = _s3.get_object(Bucket=R2_BUCKET, Key=p['image'])
            src_bytes = obj['Body'].read()
            src = Image.open(_io.BytesIO(src_bytes))
        else:
            src = Image.open(os.path.join(UPLOAD_FOLDER, p['image']))
        src = src.convert('RGB')
    except Exception as e:
        app.logger.error(f'story image fetch failed for sketch {item_id}: {e}\n{_tb.format_exc()}')
        return jsonify({'error': 'image not available', 'detail': str(e)}), 500

    W, H = 1080, 1920
    TOP_H = 1280  # ~67 % na skicu, dole jen info + CTA
    canvas = Image.new('RGB', (W, H), (0, 0, 0))

    # object-fit: cover do horní zóny
    sw, sh = src.size
    src_ratio = sw / sh
    top_ratio = W / TOP_H
    if src_ratio > top_ratio:
        nh = TOP_H
        nw = int(TOP_H * src_ratio)
        src_r = src.resize((nw, nh), Image.LANCZOS)
        x = (nw - W) // 2
        src_r = src_r.crop((x, 0, x + W, TOP_H))
    else:
        nw = W
        nh = int(W / src_ratio)
        src_r = src.resize((nw, nh), Image.LANCZOS)
        y = (nh - TOP_H) // 2
        src_r = src_r.crop((0, y, W, y + TOP_H))
    canvas.paste(src_r, (0, 0))

    draw = ImageDraw.Draw(canvas)

    # Fonts
    f_brand   = _load_story_font('bebas', 56)
    f_name    = _load_story_font('bebas', 130)
    f_meta    = _load_story_font('mono',  34)
    f_price   = _load_story_font('bebas', 90)
    f_cta     = _load_story_font('bebas', 72)
    f_ctaArr  = _load_story_font('monoB', 56)

    PAD = 70
    BOT_Y = TOP_H

    # IL symbol + INKLINK wordmark (Bebas Neue, jemně tlumený)
    SYM_H = 70
    _draw_il_symbol(draw, PAD, BOT_Y + 30, SYM_H, (180, 180, 180))
    SYM_W = int(SYM_H * 240 / 180)
    draw.text((PAD + SYM_W + 20, BOT_Y + 40), 'INKLINK',
              font=f_brand, fill=(180, 180, 180))

    # Jméno tatéra (dominantní) — auto-shrink pokud nesedí na řádek
    name_text = (p['display_name'] or 'tatér').upper()
    name_size = 130
    f_name_fit = f_name
    while name_size > 70:
        bbox = draw.textbbox((0, 0), name_text, font=f_name_fit)
        if bbox[2] - bbox[0] <= W - 2 * PAD:
            break
        name_size -= 10
        f_name_fit = _load_story_font('bebas', name_size)
    draw.text((PAD, BOT_Y + 110), name_text,
              font=f_name_fit, fill=(255, 255, 255))

    # Studio · město (DM Mono, jemné)
    meta = ' · '.join([x for x in [p['studio'], p['city']] if x])
    if meta:
        draw.text((PAD, BOT_Y + 260), meta,
                  font=f_meta, fill=(160, 160, 160))

    # Cena (sketch) nebo "HOTOVÁ PRÁCE" (done) — Bebas Neue, světlá
    is_sketch = (p['kind'] or 'done') == 'sketch'
    if is_sketch and p['price_kc']:
        try:
            price_s = f"{int(p['price_kc']):,}".replace(',', ' ') + ' Kč'
        except (TypeError, ValueError):
            price_s = ''
        if p['estimated_hours']:
            try:
                hrs = float(p['estimated_hours'])
                hrs_s = (f'{hrs:.1f}'.rstrip('0').rstrip('.'))
                price_s += f'  ·  {hrs_s}h'
            except (TypeError, ValueError):
                pass
        if price_s:
            draw.text((PAD, BOT_Y + 360), price_s,
                      font=f_price, fill=(232, 232, 232))
    elif not is_sketch:
        draw.text((PAD, BOT_Y + 360), 'HOTOVÁ PRÁCE',
                  font=f_price, fill=(232, 232, 232))

    # Spodní CTA pásek (přes celou šířku) — bílé pozadí, černý text
    # URL se nepíše, tatér si ji ve Story doplní link stickerem.
    CTA_H = 140
    CTA_Y = H - CTA_H
    draw.rectangle([(0, CTA_Y), (W, H)], fill=(232, 232, 232))

    # CTA renderujeme ve dvou kusech, protože Bebas Neue nemá šipkový glyph
    cta_word = 'REZERVUJ'
    cta_arr  = ' »'
    bbox_w = draw.textbbox((0, 0), cta_word, font=f_cta)
    bbox_a = draw.textbbox((0, 0), cta_arr,  font=f_ctaArr)
    word_w = bbox_w[2] - bbox_w[0]
    arr_w  = bbox_a[2] - bbox_a[0]
    word_h = bbox_w[3] - bbox_w[1]
    total_w = word_w + arr_w
    x0 = (W - total_w) // 2
    y0 = CTA_Y + (CTA_H - word_h) // 2 - 6
    draw.text((x0, y0), cta_word, font=f_cta, fill=(0, 0, 0))
    # šipka jemně níž kvůli optickému zarovnání mezi Bebas a Mono baseline
    draw.text((x0 + word_w, y0 + 8), cta_arr, font=f_ctaArr, fill=(0, 0, 0))

    buf = _io.BytesIO()
    canvas.save(buf, format='PNG', optimize=True)
    buf.seek(0)
    return Response(buf.getvalue(), mimetype='image/png', headers={
        'Cache-Control': 'public, max-age=300',
        'Content-Disposition': f'inline; filename="inklink-sketch-{p["id"]}.png"',
    })


# ── Upload API ────────────────────────────────────────────────────────────────


# ── Like API ──────────────────────────────────────────────────────────────────


# ── Repost ───────────────────────────────────────────────────────────────────


# ── Comments ─────────────────────────────────────────────────────────────────


# ── Notifications ────────────────────────────────────────────────────────────

NOTIF_ICONS = {
    'like_track':   '♥',
    'comment_track':'◎',
    'new_follower': '◉',
    'new_track':    '♪',
    'listing_like': '🛒',
    'event_save':   '◷',
    # InkLink specific
    'booking':           '◷',  # nová rezervace tatérovi
    'booking_cancelled': '✕',
    'balance_charge':    '€',  # vystaven doplatek klientovi
    'balance_paid':      '✓',  # klient zaplatil doplatek
}

@app.route('/api/notifications')
def get_notifications():
    if 'user_id' not in session:
        return jsonify([])
    conn = get_db()
    rows = conn.execute('''
        SELECT n.id, n.type, n.ref_id, n.ref_type, n.message, n.read, n.created_at,
               n.actor_id, u.display_name AS actor_name, u.avatar AS actor_avatar,
               u.username AS actor_username
        FROM notifications n
        LEFT JOIN users u ON n.actor_id = u.id
        WHERE n.user_id = ?
        ORDER BY n.created_at DESC
        LIMIT 50
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{
        'id':             r['id'],
        'type':           r['type'],
        'icon':           NOTIF_ICONS.get(r['type'], '●'),
        'ref_id':         r['ref_id'],
        'ref_type':       r['ref_type'],
        'message':        r['message'],
        'read':           bool(r['read']),
        'created_at':     time_ago(r['created_at']),
        'actor_username': r['actor_username'],
        'actor_avatar':   f'/uploads/{r["actor_avatar"]}' if r['actor_avatar'] else None,
        'actor_initials': initials(r['actor_name'] or '?'),
    } for r in rows])


@app.route('/api/notifications/count')
def notifications_count():
    if 'user_id' not in session:
        return jsonify({'count': 0})
    conn = get_db()
    count = conn.execute(
        'SELECT COUNT(*) FROM notifications WHERE user_id = ? AND read = 0',
        (session['user_id'],)
    ).fetchone()[0]
    conn.close()
    return jsonify({'count': count})


@app.route('/api/notifications/read-all', methods=['POST'])
def read_all_notifications():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    conn.execute('UPDATE notifications SET read = 1 WHERE user_id = ?', (session['user_id'],))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/notifications/<int:nid>', methods=['DELETE'])
def delete_notification(nid):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    conn.execute('DELETE FROM notifications WHERE id = ? AND user_id = ?', (nid, session['user_id']))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── News ─────────────────────────────────────────────────────────────────────


# ── Play count ────────────────────────────────────────────────────────────────


# ── Follow API ────────────────────────────────────────────────────────────────

@app.route('/api/follow/<int:user_id>', methods=['POST'])
def toggle_follow(user_id):
    err = require_login()
    if err:
        return err
    if user_id == session['user_id']:
        return jsonify({'error': "You can't follow yourself"}), 400

    conn     = get_db()
    existing = conn.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?',
                            (session['user_id'], user_id)).fetchone()
    if existing:
        conn.execute('DELETE FROM follows WHERE follower_id = ? AND following_id = ?',
                     (session['user_id'], user_id))
        following = False
    else:
        conn.execute('INSERT INTO follows (follower_id, following_id) VALUES (?, ?)',
                     (session['user_id'], user_id))
        following = True
        actor = conn.execute('SELECT display_name FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        push_notif(conn, user_id, session['user_id'], 'new_follower', session['user_id'], 'user',
                   f"{actor['display_name']} tě začal(a) sledovat")

    conn.commit()
    conn.close()
    return jsonify({'following': following})


# ── Trending ──────────────────────────────────────────────────────────────────


# ── Suggested users ───────────────────────────────────────────────────────────

@app.route('/api/suggested')
def suggested():
    if 'user_id' not in session:
        return jsonify([])

    conn  = get_db()
    rows  = conn.execute('''
        SELECT u.id, u.username, u.display_name, u.city, u.genres, u.avatar,
               EXISTS(SELECT 1 FROM follows WHERE follower_id = ? AND following_id = u.id) AS following
        FROM users u
        WHERE u.id != ?
        ORDER BY RANDOM()
        LIMIT 5
    ''', (session['user_id'], session['user_id'])).fetchall()
    conn.close()

    return jsonify([{
        'id':           r['id'],
        'username':     r['username'],
        'display_name': r['display_name'],
        'city':         r['city'],
        'genres':       r['genres'],
        'following':    bool(r['following']),
        'initials':     initials(r['display_name']),
        'avatar':       f'/uploads/{r["avatar"]}' if r['avatar'] else None,
    } for r in rows])


# ── Genres list ──────────────────────────────────────────────────────────────


# ── Cities list ───────────────────────────────────────────────────────────────

@app.route('/api/cities')
def cities():
    conn  = get_db()
    rows  = conn.execute('''
        SELECT DISTINCT city FROM (
            SELECT city FROM tracks WHERE city != ''
            UNION
            SELECT city FROM users WHERE city != ''
        ) ORDER BY city
    ''').fetchall()
    conn.close()
    return jsonify([r['city'] for r in rows])


# ── Profile pages ─────────────────────────────────────────────────────────────

@app.route('/profile/<username>')
def profile_page(username):
    """Server-render profile.html s OG meta tagy a JSON-LD pro SEO."""
    import json as _json
    conn = get_db()
    u = conn.execute('''SELECT id, display_name, username, city, studio, bio, avatar, styles,
                               is_artist, created_at
                        FROM users WHERE username = ?''', (username,)).fetchone()
    review_agg = None
    if u:
        review_agg = conn.execute(
            'SELECT AVG(rating) AS avg, COUNT(*) AS cnt FROM reviews WHERE artist_id=?',
            (u['id'],)).fetchone()
    conn.close()

    page_url = APP_BASE_URL.rstrip('/') + f'/profile/{username}'
    if not u:
        og_title = 'Profil — InkLink'
        og_desc  = 'Tetování na rezervaci na InkLinku.'
        og_image = APP_BASE_URL.rstrip('/') + '/icons/icon-512.png'
        json_ld  = '{}'
    else:
        name = u['display_name'] or u['username']
        loc  = u['city'] or u['studio'] or ''
        if u['is_artist']:
            og_title = f'{name} — tatér' + (f' · {loc}' if loc else '')
        else:
            og_title = f'{name} — InkLink'
        og_desc = (u['bio'] or '').strip()[:180] or f'Profil {name} na InkLinku.'
        og_image = (f'{APP_BASE_URL.rstrip("/")}/uploads/{u["avatar"]}'
                    if u['avatar'] else APP_BASE_URL.rstrip('/') + '/icons/icon-512.png')

        ldd = {
            "@context": "https://schema.org",
            "@type": "Person",
            "name": name,
            "url": page_url,
        }
        if u['avatar']:
            ldd["image"] = og_image
        if u['is_artist']:
            ldd["jobTitle"] = "Tattoo artist"
            if loc:
                ldd["address"] = {"@type": "PostalAddress", "addressLocality": loc}
            if u['styles']:
                ldd["knowsAbout"] = [s.strip() for s in u['styles'].split(',') if s.strip()]
            if review_agg and review_agg['cnt']:
                ldd["aggregateRating"] = {
                    "@type": "AggregateRating",
                    "ratingValue": round(review_agg['avg'], 1),
                    "ratingCount": review_agg['cnt'],
                    "bestRating": 5,
                    "worstRating": 1,
                }
        json_ld = _json.dumps(ldd, ensure_ascii=False)

    with open(os.path.join('public', 'profile.html'), 'r', encoding='utf-8') as f:
        page_html = f.read()
    page_html = (page_html
            .replace('{{OG_TITLE}}', html_escape(og_title))
            .replace('{{OG_DESC}}',  html_escape(og_desc))
            .replace('{{OG_IMAGE}}', html_escape(og_image))
            .replace('{{OG_URL}}',   html_escape(page_url))
            .replace('{{JSON_LD}}',  json_ld))
    return page_html


@app.route('/@<username>')
def short_artist_alias(username):
    """Krátký vanity URL — `inklink.club/@username` → 301 na /book/<username>.
    Pro IG bio, vizitky, marketing share."""
    return redirect(f'/book/{username}', code=301)


@app.route('/book/<username>')
def book_showcase_page(username):
    """Booking showcase — landing pro klienta. Hero artist + portfolio
    carousel + volné termíny + velký rezervovat CTA. Server-renderuje
    OG meta tagy pro IG/Twitter preview."""
    import json as _json
    conn = get_db()
    u = conn.execute('''SELECT id, display_name, username, city, studio, bio, avatar, styles,
                               is_artist, instagram, hourly_rate_min, hourly_rate_max, created_at
                        FROM users WHERE username = ?''', (username,)).fetchone()
    if not u or not u['is_artist']:
        conn.close()
        # Fallback: client profil → redirect na regular /profile/
        return redirect(f'/profile/{username}', code=302)

    # Top sketch pro OG image
    top = conn.execute(
        "SELECT image FROM portfolio_items WHERE user_id=? AND kind='sketch' ORDER BY created_at DESC LIMIT 1",
        (u['id'],)
    ).fetchone()
    review_agg = conn.execute(
        'SELECT AVG(rating) AS avg, COUNT(*) AS cnt FROM reviews WHERE artist_id=?',
        (u['id'],)
    ).fetchone()
    conn.close()

    name = u['display_name'] or u['username']
    loc  = u['city'] or u['studio'] or ''
    styles_list = [s.strip() for s in (u['styles'] or '').split(',') if s.strip()]

    page_url = APP_BASE_URL.rstrip('/') + f'/book/{username}'
    og_title = f'Rezervuj tetování u {name}' + (f' · {loc}' if loc else '')
    og_desc_parts = []
    if styles_list:
        og_desc_parts.append(' · '.join(styles_list[:3]))
    if u['hourly_rate_min']:
        rate = f'{int(u["hourly_rate_min"])}'
        if u['hourly_rate_max'] and u['hourly_rate_max'] != u['hourly_rate_min']:
            rate += f'–{int(u["hourly_rate_max"])}'
        og_desc_parts.append(f'{rate} Kč/h')
    og_desc_parts.append('Bezpečná rezervace přes Stripe.')
    og_desc = ' · '.join(og_desc_parts)

    if top and top['image']:
        og_image = f'{APP_BASE_URL.rstrip("/")}/uploads/{top["image"]}'
    elif u['avatar']:
        og_image = f'{APP_BASE_URL.rstrip("/")}/uploads/{u["avatar"]}'
    else:
        og_image = APP_BASE_URL.rstrip('/') + '/icons/icon-512.png'

    ldd = {
        '@context': 'https://schema.org',
        '@type': 'Person',
        'name': name,
        'url': page_url,
        'jobTitle': 'Tattoo artist',
    }
    if u['avatar']:
        ldd['image'] = f'{APP_BASE_URL.rstrip("/")}/uploads/{u["avatar"]}'
    if loc:
        ldd['address'] = {'@type': 'PostalAddress', 'addressLocality': loc}
    if styles_list:
        ldd['knowsAbout'] = styles_list
    if review_agg and review_agg['cnt']:
        ldd['aggregateRating'] = {
            '@type': 'AggregateRating',
            'ratingValue': round(review_agg['avg'], 1),
            'ratingCount': review_agg['cnt'],
            'bestRating': 5,
            'worstRating': 1,
        }
    json_ld = _json.dumps(ldd, ensure_ascii=False)

    with open(os.path.join('public', 'book.html'), 'r', encoding='utf-8') as f:
        page_html = f.read()
    return (page_html
            .replace('{{OG_TITLE}}', html_escape(og_title))
            .replace('{{OG_DESC}}',  html_escape(og_desc))
            .replace('{{OG_IMAGE}}', html_escape(og_image))
            .replace('{{OG_URL}}',   html_escape(page_url))
            .replace('{{USERNAME}}', html_escape(username))
            .replace('{{JSON_LD}}',  json_ld))


@app.route('/events')
def events_page():
    return send_from_directory('public', 'events.html')


@app.route('/messages')
def messages_page():
    return send_from_directory('public', 'messages.html')


@app.route('/privacy')
def privacy_page():
    return send_from_directory('public', 'privacy.html')

@app.route('/terms')
def terms_page():
    return send_from_directory('public', 'terms.html')


# ── Profile API ───────────────────────────────────────────────────────────────

@app.route('/api/profile/<username>')
def get_profile(username):
    uid  = session.get('user_id', 0)
    conn = get_db()
    u = conn.execute('''SELECT id, username, display_name, city, bio, avatar, emoji,
                               lat, lng, created_at,
                               is_artist, artist_slug, studio, instagram, styles,
                               deposit_pct_default, hourly_rate_min, hourly_rate_max,
                               default_payment_mode,
                               stripe_charges_enabled
                        FROM users WHERE username = ?''', (username,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    portfolio = conn.execute('''
        SELECT p.*, EXISTS(SELECT 1 FROM portfolio_likes WHERE user_id = ? AND item_id = p.id) AS liked
        FROM portfolio_items p WHERE p.user_id = ?
        ORDER BY p.created_at DESC
    ''', (uid, u['id'])).fetchall()

    now_iso = datetime.utcnow().isoformat()
    slots = conn.execute('''
        SELECT * FROM slots
        WHERE user_id = ? AND status IN ('free','held') AND start_at >= ?
        ORDER BY start_at ASC
        LIMIT 60
    ''', (u['id'], now_iso)).fetchall()

    # k slotům dotáhni obsazené sub-rangy (jen pro 'hour' bloky to dává smysl)
    slot_ids = [s['id'] for s in slots]
    occupied_by_slot = {sid: [] for sid in slot_ids}
    if slot_ids:
        placeholders = ','.join(['?'] * len(slot_ids))
        rows = conn.execute(f'''SELECT slot_id, booking_start_at, booking_end_at
                                FROM bookings
                                WHERE slot_id IN ({placeholders})
                                  AND status IN ('pending_payment','confirmed')
                                  AND booking_start_at IS NOT NULL''', slot_ids).fetchall()
        for r in rows:
            occupied_by_slot.setdefault(r['slot_id'], []).append(
                {'start_at': r['booking_start_at'], 'end_at': r['booking_end_at']}
            )

    followers = conn.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (u['id'],)).fetchone()[0]
    following_count = conn.execute('SELECT COUNT(*) FROM follows WHERE follower_id = ?', (u['id'],)).fetchone()[0]
    is_following = bool(conn.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?', (uid, u['id'])).fetchone())
    review_agg = conn.execute('SELECT AVG(rating) AS avg, COUNT(*) AS cnt FROM reviews WHERE artist_id=?',
                              (u['id'],)).fetchone()
    rating_avg = round(review_agg['avg'], 2) if review_agg['avg'] else None
    rating_count = review_agg['cnt'] or 0
    # Trust signals: počet dokončených tetování, čas registrace
    completed_count = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE artist_id=? AND status='completed'",
        (u['id'],)).fetchone()[0]

    # Studio membership (structured) — pokud je tatér členem registrovaného studia
    studio_link = None
    sm = conn.execute('''
        SELECT s.slug, s.name, sm.role
        FROM studio_members sm
        JOIN studios s ON s.id = sm.studio_id
        WHERE sm.artist_id = ?
    ''', (u['id'],)).fetchone()
    if sm:
        studio_link = {'slug': sm['slug'], 'name': sm['name'], 'role': sm['role']}
    conn.close()

    return jsonify({
        'id':              u['id'],
        'username':        u['username'],
        'display_name':    u['display_name'],
        'city':            u['city'],
        'bio':             u['bio'],
        'avatar':          u['avatar'],
        'avatar_url':      f'/uploads/{u["avatar"]}' if u['avatar'] else None,
        'emoji':           u['emoji'],
        'lat':             u['lat'],
        'lng':             u['lng'],
        'initials':        initials(u['display_name']),
        'is_artist':       bool(u['is_artist']),
        'artist_slug':     u['artist_slug'],
        'studio':          u['studio'] or '',
        'studio_link':     studio_link,
        'instagram':       u['instagram'] or '',
        'styles':          u['styles'] or '',
        'deposit_pct':     u['deposit_pct_default'] or 30,
        'hourly_rate_min': u['hourly_rate_min'],
        'hourly_rate_max': u['hourly_rate_max'],
        'default_payment_mode': u['default_payment_mode'] or 'deposit',
        'can_book':        bool(u['stripe_charges_enabled']),
        'is_own':          uid != 0 and u['id'] == uid,
        'is_following':    is_following,
        'followers':       followers,
        'following_count': following_count,
        'rating_avg':      rating_avg,
        'rating_count':    rating_count,
        'completed_count': completed_count,
        'member_since':    u['created_at'],
        'portfolio_count': len(portfolio),
        'portfolio': [{
            'id':              p['id'],
            'image':           p['image'],
            'images':          _portfolio_images(p),
            'caption':         p['caption'] or '',
            'kind':            p['kind'] or 'done',
            'styles':          p['styles'] or '',
            'like_count':      p['like_count'],
            'liked':           bool(p['liked']),
            'price_kc':        p['price_kc'],
            'estimated_hours': p['estimated_hours'],
            'created_at':      time_ago(p['created_at']),
        } for p in portfolio],
        'slots': [{
            'id':          s['id'],
            'start_at':    s['start_at'],
            'end_at':      s['end_at'],
            'status':      s['status'],
            'price_min':   s['price_min'],
            'price_max':   s['price_max'],
            'price_unit':  (s['price_unit'] if 'price_unit' in s.keys() else 'hour') or 'hour',
            'min_duration_hours': (s['min_duration_hours'] if 'min_duration_hours' in s.keys() else 1) or 1,
            'deposit_pct': s['deposit_pct'] if s['deposit_pct'] is not None else (u['deposit_pct_default'] or 30),
            'note':        s['note'] or '',
            'occupied':    occupied_by_slot.get(s['id'], []),
        } for s in slots],
    })


@app.route('/api/profile/emoji', methods=['POST'])
def update_profile_emoji():
    err = require_login()
    if err: return err
    emoji = request.json.get('emoji', '').strip() if request.is_json else request.form.get('emoji', '').strip()
    allowed = ('🎙️', '🎛️', '🎹', '📹', '📸', '')
    if emoji not in allowed:
        return jsonify({'error': 'invalid emoji'}), 400
    conn = get_db()
    conn.execute('UPDATE users SET emoji=? WHERE id=?', (emoji, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'emoji': emoji})


ALLOWED_TATTOO_STYLES = (
    'Realism', 'Traditional', 'Neo-Traditional', 'Black & Grey',
    'Blackwork', 'Minimalist', 'Geometric', 'Dotwork', 'Watercolor',
    'Japanese', 'Tribal', 'Lettering', 'Anime', 'Fineline', 'Surreal',
)


@app.route('/api/profile/update', methods=['POST'])
def update_profile():
    err = require_login()
    if err: return err

    display_name = request.form.get('display_name', '').strip()
    city         = request.form.get('city', '').strip()
    bio          = request.form.get('bio', '').strip()
    studio       = request.form.get('studio', '').strip()
    instagram    = request.form.get('instagram', '').strip().lstrip('@')
    styles_raw   = request.form.get('styles', '').strip()
    try:
        deposit_pct = int(request.form.get('deposit_pct_default', '30'))
    except (ValueError, TypeError):
        deposit_pct = 30
    deposit_pct = max(0, min(100, deposit_pct))

    def _opt_int(name):
        v = request.form.get(name, '').strip()
        if not v:
            return None
        try:
            return max(0, int(v))
        except (ValueError, TypeError):
            return None
    hourly_min = _opt_int('hourly_rate_min')
    hourly_max = _opt_int('hourly_rate_max')
    pay_mode = (request.form.get('default_payment_mode', '') or 'deposit').strip().lower()
    if pay_mode not in ('deposit', 'full', 'client_choice'):
        pay_mode = 'deposit'

    try:
        lat = float(request.form.get('lat', ''))
    except (ValueError, TypeError):
        lat = None
    try:
        lng = float(request.form.get('lng', ''))
    except (ValueError, TypeError):
        lng = None

    if not display_name:
        return jsonify({'error': 'Jméno nemůže být prázdné'}), 400
    if len(display_name) > 60:
        return jsonify({'error': 'Jméno je příliš dlouhé (max 60)'}), 400
    if len(bio) > 500:
        return jsonify({'error': 'Bio je příliš dlouhé (max 500)'}), 400
    if len(city) > 80 or len(studio) > 100 or len(instagram) > 50:
        return jsonify({'error': 'Některé pole je příliš dlouhé'}), 400

    # validate styles against allowed list
    chosen = [s.strip() for s in styles_raw.split(',') if s.strip()]
    chosen = [s for s in chosen if s in ALLOWED_TATTOO_STYLES][:6]
    styles = ','.join(chosen)

    conn = get_db()
    if lat is not None and lng is not None:
        conn.execute('''UPDATE users SET display_name=?, city=?, bio=?, studio=?, instagram=?,
                                          styles=?, deposit_pct_default=?,
                                          hourly_rate_min=?, hourly_rate_max=?,
                                          default_payment_mode=?,
                                          lat=?, lng=?
                        WHERE id=?''',
                     (display_name, city, bio, studio, instagram, styles, deposit_pct,
                      hourly_min, hourly_max, pay_mode, lat, lng,
                      session['user_id']))
    else:
        conn.execute('''UPDATE users SET display_name=?, city=?, bio=?, studio=?, instagram=?,
                                          styles=?, deposit_pct_default=?,
                                          hourly_rate_min=?, hourly_rate_max=?,
                                          default_payment_mode=?
                        WHERE id=?''',
                     (display_name, city, bio, studio, instagram, styles, deposit_pct,
                      hourly_min, hourly_max, pay_mode,
                      session['user_id']))

    f = request.files.get('avatar')
    if f and f.filename:
        ext = secure_filename(f.filename).rsplit('.', 1)[-1].lower()
        if ext in ('jpg', 'jpeg', 'png', 'webp') and allowed_image(f):
            name = f'avatar_{session["user_id"]}_{int(time.time())}.{ext}'
            save_upload(f, name)
            conn.execute('UPDATE users SET avatar=? WHERE id=?', (name, session['user_id']))

    conn.commit()
    u = conn.execute('SELECT display_name FROM users WHERE id=?', (session['user_id'],)).fetchone()
    conn.close()

    session['display_name'] = u['display_name']
    return jsonify({'ok': True})


@app.route('/api/profile/avatar', methods=['POST'])
@limiter.limit('20 per hour')
def update_avatar_only():
    """Upload pouze avatar — bez nutnosti vyplnit zbytek profilu.
    Použito z profile.html (klik na vlastní avatar → file picker)."""
    err = require_login()
    if err: return err
    f = request.files.get('avatar')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400
    ext = secure_filename(f.filename).rsplit('.', 1)[-1].lower()
    if ext not in ('jpg', 'jpeg', 'png', 'webp'):
        return jsonify({'error': 'Unsupported format'}), 400
    if not allowed_image(f):
        return jsonify({'error': 'Not a valid image'}), 400
    name = f'avatar_{session["user_id"]}_{int(time.time())}.{ext}'
    save_upload(f, name)
    conn = get_db()
    conn.execute('UPDATE users SET avatar=? WHERE id=?', (name, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'avatar': name, 'avatar_url': f'/uploads/{name}'})


# ── InkLink: artist setup, portfolio, slots ───────────────────────────────────

def _slugify(s: str) -> str:
    import re, unicodedata
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    s = re.sub(r'[^a-zA-Z0-9]+', '-', s).strip('-').lower()
    return s[:60] or 'artist'


@app.route('/api/me/become-artist', methods=['POST'])
def become_artist():
    err = require_login()
    if err: return err
    conn = get_db()
    u = conn.execute('SELECT username, display_name, is_artist, artist_slug FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    if u['is_artist']:
        conn.close()
        return jsonify({'ok': True, 'artist_slug': u['artist_slug']})
    base = _slugify(u['display_name'] or u['username'])
    slug = base
    n = 1
    while conn.execute('SELECT 1 FROM users WHERE artist_slug=?', (slug,)).fetchone():
        n += 1
        slug = f'{base}-{n}'
    conn.execute('UPDATE users SET is_artist=1, artist_slug=? WHERE id=?', (slug, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'artist_slug': slug})


@app.route('/api/portfolio', methods=['POST'])
@limiter.limit('30 per hour')
def create_portfolio_item():
    err = require_login()
    if err: return err

    # Primary image — povinný. Plus volitelné image2/image3/image4.
    f = request.files.get('image')
    if not f or not f.filename:
        return jsonify({'error': 'Obrázek je povinný'}), 400

    def _validate_image(file_storage):
        if not file_storage or not file_storage.filename:
            return None, None
        ext = secure_filename(file_storage.filename).rsplit('.', 1)[-1].lower()
        if ext not in ('jpg', 'jpeg', 'png', 'webp') or not allowed_image(file_storage):
            return None, 'invalid'
        return ext, None

    primary_ext, err = _validate_image(f)
    if err:
        return jsonify({'error': 'Povolené formáty: JPG, PNG, WEBP'}), 400

    extra_files = []
    for slot in ('image2', 'image3', 'image4'):
        ff = request.files.get(slot)
        if not ff or not ff.filename:
            continue
        ext, err = _validate_image(ff)
        if err:
            return jsonify({'error': f'{slot}: povolené JPG/PNG/WEBP'}), 400
        extra_files.append((slot, ff, ext))

    caption    = (request.form.get('caption') or '').strip()[:500]
    kind       = (request.form.get('kind') or 'done').strip()
    if kind not in ('sketch', 'done'):
        kind = 'done'
    styles_raw = (request.form.get('styles') or '').strip()
    chosen = [s.strip() for s in styles_raw.split(',') if s.strip() in ALLOWED_TATTOO_STYLES][:4]
    styles = ','.join(chosen)

    # volitelná navrhovaná cena + odhad délky (pro sketche / fixed-price návrhy)
    def _opt_pos_int(name):
        v = (request.form.get(name) or '').strip()
        if not v:
            return None
        try: return max(0, int(v))
        except: return None
    def _opt_pos_float(name):
        v = (request.form.get(name) or '').strip()
        if not v:
            return None
        try: return max(0.5, float(v))
        except: return None
    price_kc        = _opt_pos_int('price_kc')
    estimated_hours = _opt_pos_float('estimated_hours')

    base_ts = int(time.time() * 1000)
    primary_name = f'portfolio_{session["user_id"]}_{base_ts}.{primary_ext}'
    save_upload(f, primary_name)

    extra_names = {'image2': None, 'image3': None, 'image4': None}
    for i, (slot, ff, ext) in enumerate(extra_files, start=1):
        nm = f'portfolio_{session["user_id"]}_{base_ts}_{i}.{ext}'
        save_upload(ff, nm)
        extra_names[slot] = nm

    conn = get_db()
    # auto-promote to artist if not yet
    u = conn.execute('SELECT is_artist, artist_slug, display_name, username FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    if not u['is_artist']:
        base = _slugify(u['display_name'] or u['username'])
        slug = base
        n = 1
        while conn.execute('SELECT 1 FROM users WHERE artist_slug=?', (slug,)).fetchone():
            n += 1
            slug = f'{base}-{n}'
        conn.execute('UPDATE users SET is_artist=1, artist_slug=? WHERE id=?', (slug, session['user_id']))
    conn.execute('''INSERT INTO portfolio_items (user_id, image, image2, image3, image4,
                                                 caption, kind, styles, price_kc, estimated_hours)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''',
                 (session['user_id'], primary_name,
                  extra_names['image2'], extra_names['image3'], extra_names['image4'],
                  caption, kind, styles, price_kc, estimated_hours))
    conn.commit()
    item_id = conn.execute('SELECT id FROM portfolio_items WHERE user_id=? ORDER BY id DESC LIMIT 1',
                           (session['user_id'],)).fetchone()['id']
    conn.close()
    return jsonify({'ok': True, 'id': item_id, 'image': primary_name,
                    'images': [primary_name] + [n for n in extra_names.values() if n]})


@app.route('/api/portfolio/<int:item_id>', methods=['DELETE'])
def delete_portfolio_item(item_id):
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT user_id FROM portfolio_items WHERE id=?', (item_id,)).fetchone()
    if not row or row['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.execute('DELETE FROM portfolio_likes WHERE item_id=?', (item_id,))
    conn.execute('DELETE FROM portfolio_items WHERE id=?', (item_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/portfolio/<int:item_id>', methods=['PATCH'])
def update_portfolio_item(item_id):
    """Tatér mění popis, cenu a odhad délky u svého portfolio sketche."""
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT user_id FROM portfolio_items WHERE id=?', (item_id,)).fetchone()
    if not row or row['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    data = request.get_json(silent=True) or {}
    sets, params = [], []

    if 'caption' in data:
        sets.append('caption=?'); params.append((data['caption'] or '').strip()[:500])
    if 'kind' in data:
        kind = (data['kind'] or 'done').strip()
        if kind not in ('sketch', 'done'): kind = 'done'
        sets.append('kind=?'); params.append(kind)
    if 'price_kc' in data:
        v = data['price_kc']
        if v in (None, '', 0):
            sets.append('price_kc=NULL')
        else:
            try: pv = max(0, int(v))
            except (ValueError, TypeError):
                conn.close(); return jsonify({'error': 'Špatná cena'}), 400
            sets.append('price_kc=?'); params.append(pv)
    if 'estimated_hours' in data:
        v = data['estimated_hours']
        if v in (None, '', 0):
            sets.append('estimated_hours=NULL')
        else:
            try: hv = max(0.5, float(v))
            except (ValueError, TypeError):
                conn.close(); return jsonify({'error': 'Špatná délka'}), 400
            sets.append('estimated_hours=?'); params.append(hv)

    if not sets:
        conn.close()
        return jsonify({'ok': True})

    params.append(item_id)
    conn.execute(f'UPDATE portfolio_items SET {", ".join(sets)} WHERE id=?', tuple(params))
    conn.commit()
    updated = conn.execute('SELECT * FROM portfolio_items WHERE id=?', (item_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'item': dict(updated)})


@app.route('/api/portfolio/<int:item_id>/like', methods=['POST'])
def toggle_portfolio_like(item_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    row = conn.execute('SELECT 1 FROM portfolio_likes WHERE user_id=? AND item_id=?',
                       (uid, item_id)).fetchone()
    if row:
        conn.execute('DELETE FROM portfolio_likes WHERE user_id=? AND item_id=?', (uid, item_id))
        # CASE místo MAX/GREATEST — funguje v SQLite i Postgresu
        conn.execute('''UPDATE portfolio_items
                        SET like_count = CASE WHEN like_count > 0 THEN like_count - 1 ELSE 0 END
                        WHERE id=?''', (item_id,))
        liked = False
    else:
        conn.execute('INSERT INTO portfolio_likes (user_id, item_id) VALUES (?,?)', (uid, item_id))
        conn.execute('UPDATE portfolio_items SET like_count = like_count + 1 WHERE id=?', (item_id,))
        liked = True
    conn.commit()
    cnt = conn.execute('SELECT like_count FROM portfolio_items WHERE id=?', (item_id,)).fetchone()
    conn.close()
    return jsonify({'liked': liked, 'like_count': cnt['like_count'] if cnt else 0})


@app.route('/api/slots', methods=['POST'])
def create_slot():
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    start_at = (data.get('start_at') or '').strip()
    end_at   = (data.get('end_at') or '').strip()
    if not start_at or not end_at:
        return jsonify({'error': 'Vyplň start a konec termínu (ISO 8601)'}), 400
    try:
        s_dt = datetime.fromisoformat(start_at.replace('Z', '+00:00'))
        e_dt = datetime.fromisoformat(end_at.replace('Z', '+00:00'))
    except ValueError:
        return jsonify({'error': 'Špatný formát datumu (použij ISO 8601)'}), 400
    if e_dt <= s_dt:
        return jsonify({'error': 'Konec termínu musí být po startu'}), 400
    if s_dt < datetime.utcnow() - timedelta(minutes=5):
        return jsonify({'error': 'Termín nemůže být v minulosti'}), 400

    try:
        price_min = int(data.get('price_min') or 0)
        price_max = int(data.get('price_max') or 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Špatná cena'}), 400
    deposit_pct = data.get('deposit_pct')
    try:
        deposit_pct = int(deposit_pct) if deposit_pct not in (None, '') else None
    except (ValueError, TypeError):
        deposit_pct = None
    if deposit_pct is not None:
        deposit_pct = max(0, min(100, deposit_pct))
    note = (data.get('note') or '').strip()[:200]

    price_unit = (data.get('price_unit') or 'hour').strip().lower()
    if price_unit not in ('hour', 'flat'):
        price_unit = 'hour'
    try:
        min_dur = int(data.get('min_duration_hours') or 1)
    except (ValueError, TypeError):
        min_dur = 1
    min_dur = max(1, min(24, min_dur))

    # ── Recurrence ────────────────────────────────────────────────────
    # Volitelný shape: {"days": [0..6, ...], "until": "YYYY-MM-DD"}
    # 0=pondělí, 6=neděle (ISO weekday: Monday=0 to align s JS getDay()-1 adjust).
    # Pokud chybí 'days' nebo je prázdný → vytvoříme jen jeden slot (jednorázový).
    recur = data.get('recur') if isinstance(data.get('recur'), dict) else None
    occurrences = [(s_dt, e_dt)]  # default: jen základní termín

    if recur:
        days_raw = recur.get('days') or []
        try:
            days = sorted(set(int(d) for d in days_raw if 0 <= int(d) <= 6))
        except (TypeError, ValueError):
            days = []
        until_str = (recur.get('until') or '').strip()
        if days and until_str:
            try:
                until_date = datetime.fromisoformat(until_str).date()
            except ValueError:
                until_date = None
            if until_date:
                # rozsah délky bloku (timedelta) zachováme stejný
                duration = e_dt - s_dt
                # iteruj od dne base+1 do until_date včetně
                from datetime import date as _date, time as _time
                base_date = s_dt.date()
                # pondělí=0 v Pythonu weekday()
                cur = base_date + timedelta(days=1)
                start_time = s_dt.time()
                # bezpečnostní limit — max 200 výskytů
                while cur <= until_date and len(occurrences) < 200:
                    if cur.weekday() in days:
                        ns = datetime.combine(cur, start_time)
                        ne = ns + duration
                        occurrences.append((ns, ne))
                    cur += timedelta(days=1)
                # přidej i základní den pouze pokud jeho weekday je mezi vybranými dny
                # (jinak je v occurrences už od inicializace — necháme tak ať uživatel
                #  vidí, že jeho výchozí termín se vytvořil)

    conn = get_db()
    created_ids = []
    for ns, ne in occurrences:
        conn.execute('''INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max,
                                           deposit_pct, note, price_unit, min_duration_hours)
                        VALUES (?,?,?,'free',?,?,?,?,?,?)''',
                     (session['user_id'], ns.isoformat(), ne.isoformat(),
                      price_min, price_max, deposit_pct, note, price_unit, min_dur))
        if not conn._pg:
            sid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
        else:
            sid = conn.execute('SELECT lastval()').fetchone()[0]
        created_ids.append(sid)

    # auto-promote to artist
    u = conn.execute('SELECT is_artist, artist_slug, display_name, username FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    if not u['is_artist']:
        base = _slugify(u['display_name'] or u['username'])
        slug = base
        n = 1
        while conn.execute('SELECT 1 FROM users WHERE artist_slug=?', (slug,)).fetchone():
            n += 1
            slug = f'{base}-{n}'
        conn.execute('UPDATE users SET is_artist=1, artist_slug=? WHERE id=?', (slug, session['user_id']))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': created_ids[0] if created_ids else None,
                    'created': len(created_ids), 'ids': created_ids})


@app.route('/api/slots/<int:slot_id>', methods=['DELETE'])
def delete_slot(slot_id):
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT user_id, status FROM slots WHERE id=?', (slot_id,)).fetchone()
    if not row or row['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if row['status'] == 'booked':
        conn.close()
        return jsonify({'error': 'Nelze smazat termín s aktivní rezervací — nejdřív rezervaci zruš'}), 409
    # bezpečnostní check: žádná aktivní booking ve slotu (i pro hour bloky kde status zůstává 'free')
    active = conn.execute('''SELECT 1 FROM bookings
                             WHERE slot_id=? AND status IN ('pending_payment','confirmed')
                             LIMIT 1''', (slot_id,)).fetchone()
    if active:
        conn.close()
        return jsonify({'error': 'Blok má aktivní rezervace — nejprve je vyřeš.'}), 409
    conn.execute('DELETE FROM slots WHERE id=?', (slot_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/slots/<int:slot_id>', methods=['PATCH'])
def update_slot(slot_id):
    """Tatér edituje sazbu, zálohu, min délku, poznámku u existujícího bloku.
    Časy (start_at/end_at) lze měnit jen když ve slotu nejsou aktivní rezervace."""
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT * FROM slots WHERE id=?', (slot_id,)).fetchone()
    if not row or row['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    data = request.get_json(silent=True) or {}
    sets, params = [], []

    if 'price_min' in data:
        try: pv = max(0, int(data['price_min'] or 0))
        except (ValueError, TypeError):
            conn.close(); return jsonify({'error': 'Špatná cena'}), 400
        sets.append('price_min=?'); params.append(pv)
    if 'price_max' in data:
        try: pv = max(0, int(data['price_max'] or 0))
        except (ValueError, TypeError):
            conn.close(); return jsonify({'error': 'Špatná cena'}), 400
        sets.append('price_max=?'); params.append(pv)
    if 'deposit_pct' in data:
        v = data['deposit_pct']
        if v in (None, ''):
            sets.append('deposit_pct=NULL')
        else:
            try: dv = max(0, min(100, int(v)))
            except (ValueError, TypeError):
                conn.close(); return jsonify({'error': 'Špatná záloha'}), 400
            sets.append('deposit_pct=?'); params.append(dv)
    if 'min_duration_hours' in data:
        try: mv = max(1, min(24, int(data['min_duration_hours'] or 1)))
        except (ValueError, TypeError):
            conn.close(); return jsonify({'error': 'Špatná délka'}), 400
        sets.append('min_duration_hours=?'); params.append(mv)
    if 'note' in data:
        sets.append('note=?'); params.append((data['note'] or '').strip()[:200])
    if 'price_unit' in data:
        unit = (data['price_unit'] or 'hour').strip().lower()
        if unit not in ('hour', 'flat'): unit = 'hour'
        sets.append('price_unit=?'); params.append(unit)

    # Změna časů — jen pokud nejsou aktivní rezervace
    if 'start_at' in data or 'end_at' in data:
        active = conn.execute('''SELECT 1 FROM bookings
                                 WHERE slot_id=? AND status IN ('pending_payment','confirmed')
                                 LIMIT 1''', (slot_id,)).fetchone()
        if active:
            conn.close()
            return jsonify({'error': 'Časy nelze měnit — ve slotu jsou aktivní rezervace.'}), 409
        if 'start_at' in data:
            try:
                s_dt = datetime.fromisoformat((data['start_at'] or '').replace('Z', '+00:00'))
                if s_dt.tzinfo is not None:
                    s_dt = s_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                conn.close(); return jsonify({'error': 'Špatný formát start_at'}), 400
            sets.append('start_at=?'); params.append(s_dt.isoformat())
        if 'end_at' in data:
            try:
                e_dt = datetime.fromisoformat((data['end_at'] or '').replace('Z', '+00:00'))
                if e_dt.tzinfo is not None:
                    e_dt = e_dt.astimezone(timezone.utc).replace(tzinfo=None)
            except ValueError:
                conn.close(); return jsonify({'error': 'Špatný formát end_at'}), 400
            sets.append('end_at=?'); params.append(e_dt.isoformat())

    if not sets:
        conn.close()
        return jsonify({'ok': True, 'no_changes': True})

    params.append(slot_id)
    conn.execute(f'UPDATE slots SET {", ".join(sets)} WHERE id=?', tuple(params))
    conn.commit()
    updated = conn.execute('SELECT * FROM slots WHERE id=?', (slot_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'slot': dict(updated)})


@app.route('/api/me/slots')
def list_my_slots():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute('''SELECT * FROM slots WHERE user_id=?
                           ORDER BY start_at DESC LIMIT 200''',
                        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/me/calendar')
def my_calendar():
    """Vrací sloty + jejich bookings v daném týdenním okně.
    Query: ?from=YYYY-MM-DD (pondělí) — vrátí sloty od pondělí 00:00 do neděle 23:59.
           Pokud chybí, vrátí aktuální týden v lokálním čase serveru.
    """
    err = require_login()
    if err: return err

    from_str = (request.args.get('from') or '').strip()
    if from_str:
        try:
            week_start = datetime.fromisoformat(from_str)
        except ValueError:
            return jsonify({'error': 'Špatný formát from (YYYY-MM-DD)'}), 400
    else:
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())  # pondělí

    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end   = week_start + timedelta(days=7)

    conn = get_db()
    slots = conn.execute('''SELECT * FROM slots
                            WHERE user_id=? AND start_at < ? AND end_at > ?
                            ORDER BY start_at ASC''',
                         (session['user_id'], week_end.isoformat(), week_start.isoformat())).fetchall()
    slot_ids = [s['id'] for s in slots]

    bookings_by_slot = {sid: [] for sid in slot_ids}
    if slot_ids:
        placeholders = ','.join(['?'] * len(slot_ids))
        b_rows = conn.execute(f'''
            SELECT b.id, b.slot_id, b.booking_start_at, b.booking_end_at, b.duration_hours,
                   b.size_label, b.status, b.deposit_cents, b.payment_mode,
                   b.client_id, uc.username AS c_username, uc.display_name AS c_display_name,
                   uc.avatar AS c_avatar
            FROM bookings b
            JOIN users uc ON uc.id = b.client_id
            WHERE b.slot_id IN ({placeholders}) AND b.status NOT IN ('cancelled_client','cancelled_artist')
            ORDER BY b.booking_start_at ASC
        ''', slot_ids).fetchall()
        for b in b_rows:
            bookings_by_slot.setdefault(b['slot_id'], []).append({
                'id':          b['id'],
                'start_at':    b['booking_start_at'],
                'end_at':      b['booking_end_at'],
                'duration_h':  b['duration_hours'],
                'size_label':  b['size_label'] or '',
                'status':      b['status'],
                'payment_mode': b['payment_mode'] or 'deposit',
                'deposit_cents': b['deposit_cents'],
                'client': {
                    'username':     b['c_username'],
                    'display_name': b['c_display_name'],
                    'avatar_url':   f'/uploads/{b["c_avatar"]}' if b['c_avatar'] else None,
                },
            })
    conn.close()

    return jsonify({
        'from': week_start.isoformat(),
        'to':   week_end.isoformat(),
        'slots': [{
            **dict(s),
            'bookings': bookings_by_slot.get(s['id'], []),
        } for s in slots],
    })


# ── Calendar (.ics) export ──────────────────────────────────────────────────
def _ics_escape(s):
    """Escape special chars dle RFC5545."""
    if not s: return ''
    return (str(s).replace('\\', '\\\\')
                  .replace(',', '\\,')
                  .replace(';', '\\;')
                  .replace('\n', '\\n')
                  .replace('\r', ''))


def _ics_fold(line):
    """RFC5545 line folding — 75 oktetů max, pokračování s úvodní mezerou."""
    if len(line) <= 75:
        return line
    out = [line[:75]]
    rest = line[75:]
    while rest:
        out.append(' ' + rest[:74])
        rest = rest[74:]
    return '\r\n'.join(out)


def _build_ics_for_user(user_id):
    """Vrátí .ics text pro daného usera — všechny aktivní bookings
    (jako tatér i klient) plus volné sloty (jen pro tatéra)."""
    conn = get_db()
    me = conn.execute('SELECT display_name, is_artist FROM users WHERE id=?', (user_id,)).fetchone()
    if not me:
        conn.close()
        return None
    bookings = conn.execute('''
        SELECT b.id, b.artist_id, b.client_id, b.booking_start_at, b.booking_end_at,
               b.status, b.design_note, b.size_label,
               ua.display_name AS artist_name, ua.studio AS artist_studio, ua.city AS artist_city,
               uc.display_name AS client_name
        FROM bookings b
        JOIN users ua ON ua.id = b.artist_id
        JOIN users uc ON uc.id = b.client_id
        WHERE (b.artist_id = ? OR b.client_id = ?)
          AND b.status NOT IN ('cancelled_client', 'cancelled_artist')
          AND b.booking_start_at IS NOT NULL
        ORDER BY b.booking_start_at ASC
    ''', (user_id, user_id)).fetchall()
    free_slots = []
    if me['is_artist']:
        now_iso = datetime.utcnow().isoformat()
        free_slots = conn.execute('''
            SELECT id, start_at, end_at FROM slots
            WHERE user_id=? AND status='free' AND start_at >= ?
            ORDER BY start_at ASC
        ''', (user_id, now_iso)).fetchall()
    conn.close()

    def _ical_dt(iso):
        try:
            d = datetime.fromisoformat((iso or '').replace('Z', '+00:00'))
            if d.tzinfo is None:
                # Naive — assume UTC (DB stores UTC ISO)
                return d.strftime('%Y%m%dT%H%M%SZ')
            return d.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        except Exception:
            return None

    now_stamp = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    lines = [
        'BEGIN:VCALENDAR',
        'VERSION:2.0',
        'PRODID:-//InkLink//Calendar//EN',
        'CALSCALE:GREGORIAN',
        'METHOD:PUBLISH',
        'X-WR-CALNAME:InkLink',
        'X-WR-TIMEZONE:Europe/Prague',
    ]
    status_map = {'confirmed': 'CONFIRMED', 'completed': 'CONFIRMED',
                  'pending_payment': 'TENTATIVE'}
    for b in bookings:
        start_s = _ical_dt(b['booking_start_at'])
        end_s   = _ical_dt(b['booking_end_at'])
        if not start_s or not end_s:
            continue
        is_artist_for_this = (user_id == b['artist_id'])
        if is_artist_for_this:
            partner = b['client_name'] or 'klient'
            summary = f'Tetování — {partner}'
        else:
            partner = b['artist_name'] or 'tatér'
            summary = f'Tetování u {partner}'
        location_parts = [x for x in (b['artist_studio'], b['artist_city']) if x]
        location = ', '.join(location_parts) if location_parts else 'InkLink'
        desc_parts = [f'InkLink booking #{b["id"]}']
        if b['size_label']:
            desc_parts.append(f'Velikost: {b["size_label"]}')
        if b['design_note']:
            desc_parts.append(b['design_note'][:200])
        lines += [
            'BEGIN:VEVENT',
            _ics_fold(f'UID:booking-{b["id"]}@inklink.club'),
            f'DTSTAMP:{now_stamp}',
            f'DTSTART:{start_s}',
            f'DTEND:{end_s}',
            _ics_fold(f'SUMMARY:{_ics_escape(summary)}'),
            _ics_fold(f'DESCRIPTION:{_ics_escape(chr(10).join(desc_parts))}'),
            _ics_fold(f'LOCATION:{_ics_escape(location)}'),
            f'STATUS:{status_map.get(b["status"], "CONFIRMED")}',
            'END:VEVENT',
        ]
    for s in free_slots:
        start_s = _ical_dt(s['start_at'])
        end_s   = _ical_dt(s['end_at'])
        if not start_s or not end_s:
            continue
        lines += [
            'BEGIN:VEVENT',
            f'UID:slot-{s["id"]}@inklink.club',
            f'DTSTAMP:{now_stamp}',
            f'DTSTART:{start_s}',
            f'DTEND:{end_s}',
            'SUMMARY:Volný termín (InkLink)',
            'TRANSP:TRANSPARENT',
            'STATUS:TENTATIVE',
            'END:VEVENT',
        ]
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'


@app.route('/api/me/calendar.ics')
def my_calendar_ics():
    """Session-protected jednorázové stažení .ics souboru."""
    err = require_login()
    if err: return err
    body = _build_ics_for_user(session['user_id'])
    if body is None:
        return jsonify({'error': 'not found'}), 404
    return Response(body, mimetype='text/calendar',
                    headers={'Content-Disposition': 'attachment; filename="inklink.ics"'})


@app.route('/api/me/calendar-token', methods=['GET', 'POST'])
def my_calendar_token():
    """Vrátí (či vygeneruje) iCal token. POST = regenerate, GET = read."""
    err = require_login()
    if err: return err
    import secrets as _secrets
    uid = session['user_id']
    conn = get_db()
    row = conn.execute('SELECT calendar_token FROM users WHERE id=?', (uid,)).fetchone()
    token = row['calendar_token'] if row else None
    if request.method == 'POST' or not token:
        token = _secrets.token_urlsafe(24)
        conn.execute('UPDATE users SET calendar_token=? WHERE id=?', (token, uid))
        conn.commit()
    conn.close()
    return jsonify({
        'token': token,
        'subscribe_url': f'{APP_BASE_URL}/calendar/{token}.ics',
    })


@app.route('/calendar/<token>.ics')
def public_calendar_ics(token):
    """Token-based feed pro Apple/Google Calendar subscription (no session)."""
    if not token or len(token) < 10:
        return jsonify({'error': 'invalid token'}), 404
    conn = get_db()
    row = conn.execute('SELECT id FROM users WHERE calendar_token=?', (token,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'invalid token'}), 404
    body = _build_ics_for_user(row['id'])
    if body is None:
        return jsonify({'error': 'not found'}), 404
    return Response(body, mimetype='text/calendar',
                    headers={'Cache-Control': 'private, max-age=300'})


@app.route('/api/me/portfolio')
def list_my_portfolio():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute('''SELECT * FROM portfolio_items WHERE user_id=?
                           ORDER BY created_at DESC''',
                        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/me/checklist')
def my_checklist():
    """Vrátí completeness checklist pro tatérský onboarding.
    UI v artist-setup zobrazí banner s progress dokud není vše hotové."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    u = conn.execute('''SELECT display_name, city, studio, bio, styles,
                               hourly_rate_min, hourly_rate_max,
                               stripe_charges_enabled, is_artist
                        FROM users WHERE id=?''', (uid,)).fetchone()
    portfolio_count = conn.execute('SELECT COUNT(*) FROM portfolio_items WHERE user_id=?',
                                    (uid,)).fetchone()[0]
    now_iso = datetime.utcnow().isoformat()
    upcoming_slots = conn.execute('''SELECT COUNT(*) FROM slots
                                     WHERE user_id=? AND end_at >= ?''',
                                  (uid, now_iso)).fetchone()[0]
    conn.close()

    items = [
        {
            'key':  'profile',
            'label':'Vyplnit profil (jméno + město nebo studio + bio)',
            'done': bool(u['display_name'] and (u['city'] or u['studio']) and u['bio']),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'rate',
            'label':'Nastavit hodinovou sazbu',
            'done': bool(u['hourly_rate_min'] or u['hourly_rate_max']),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'styles',
            'label':'Vybrat styly tetování',
            'done': bool((u['styles'] or '').strip()),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'portfolio',
            'label':'Přidat aspoň jednu položku do portfolia',
            'done': portfolio_count > 0,
            'href': '/artist-setup#portfolio',
            'count': portfolio_count,
        },
        {
            'key':  'slot',
            'label':'Přidat aspoň jeden volný blok do kalendáře',
            'done': upcoming_slots > 0,
            'href': '/artist-setup#slots',
            'count': upcoming_slots,
        },
        {
            'key':  'stripe',
            'label':'Propojit Stripe Connect (přijímat zálohy)',
            'done': bool(u['stripe_charges_enabled']),
            'href': '/artist-setup#payments',
        },
    ]
    done_n = sum(1 for it in items if it['done'])
    return jsonify({
        'items':       items,
        'completed':   done_n,
        'total':       len(items),
        'percent':     round(done_n * 100 / len(items)),
        'all_done':    done_n == len(items),
        'is_artist':   bool(u['is_artist']),
    })


@app.route('/api/me/earnings')
def my_earnings():
    """Tatérův earnings dashboard — KPIs + posledních 30 transakcí."""
    err = require_login()
    if err: return err
    uid = session['user_id']

    conn = get_db()
    u = conn.execute('SELECT is_artist FROM users WHERE id=?', (uid,)).fetchone()
    if not u or not u['is_artist']:
        conn.close()
        return jsonify({'error': 'Pouze pro tatéry'}), 403

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = month_start - timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    # Net revenue = (deposit + balance) - fees - refunds. Onsite je mimo platformu
    # (cash/karta) → vedeme ho odděleně, nepočítáme do KPI.
    base_sql = '''
        SELECT
          COALESCE(SUM(deposit_cents - platform_fee_cents - refund_cents), 0) AS deposit_net,
          COALESCE(SUM(balance_paid_cents - balance_charge_fee_cents), 0) AS balance_net,
          COALESCE(SUM(onsite_amount_cents), 0) AS onsite,
          COUNT(*) AS n
        FROM bookings
        WHERE artist_id = ? AND status IN ('confirmed', 'completed')
    '''

    def kpi(row):
        return {
            'net_cents':         (row['deposit_net'] or 0) + (row['balance_net'] or 0),
            'deposit_net_cents': row['deposit_net'] or 0,
            'balance_net_cents': row['balance_net'] or 0,
            'onsite_cents':      row['onsite'] or 0,
            'bookings_count':    row['n'] or 0,
        }

    total       = kpi(conn.execute(base_sql, (uid,)).fetchone())
    this_month  = kpi(conn.execute(base_sql + ' AND created_at >= ?',
                                   (uid, month_start.isoformat())).fetchone())
    last_month  = kpi(conn.execute(base_sql + ' AND created_at >= ? AND created_at < ?',
                                   (uid, last_month_start.isoformat(),
                                    month_start.isoformat())).fetchone())

    pending = conn.execute('''
        SELECT COALESCE(SUM(deposit_cents - platform_fee_cents), 0) AS net,
               COUNT(*) AS n
        FROM bookings
        WHERE artist_id = ? AND status = 'pending_payment'
    ''', (uid,)).fetchone()

    rows = conn.execute('''
        SELECT b.id, b.created_at, b.status,
               b.deposit_cents, b.platform_fee_cents, b.refund_cents,
               b.balance_paid_cents, b.balance_charge_cents, b.balance_charge_fee_cents,
               b.onsite_amount_cents,
               uc.display_name AS client_name, uc.username AS client_username,
               uc.avatar AS client_avatar,
               s.start_at AS session_at
        FROM bookings b
        JOIN users uc ON uc.id = b.client_id
        LEFT JOIN slots s ON s.id = b.slot_id
        WHERE b.artist_id = ?
        ORDER BY b.created_at DESC
        LIMIT 30
    ''', (uid,)).fetchall()
    conn.close()

    transactions = []
    for r in rows:
        net = ((r['deposit_cents'] or 0) - (r['platform_fee_cents'] or 0)
               - (r['refund_cents'] or 0)
               + (r['balance_paid_cents'] or 0) - (r['balance_charge_fee_cents'] or 0))
        transactions.append({
            'id':                 r['id'],
            'created_at':         r['created_at'],
            'session_at':         r['session_at'],
            'status':             r['status'],
            'client_name':        r['client_name'],
            'client_username':    r['client_username'],
            'client_avatar_url':  f'/uploads/{r["client_avatar"]}' if r['client_avatar'] else None,
            'deposit_cents':         r['deposit_cents'] or 0,
            'platform_fee_cents':    r['platform_fee_cents'] or 0,
            'refund_cents':          r['refund_cents'] or 0,
            'balance_paid_cents':    r['balance_paid_cents'] or 0,
            'balance_charge_cents':  r['balance_charge_cents'] or 0,
            'balance_charge_fee_cents': r['balance_charge_fee_cents'] or 0,
            'onsite_cents':       r['onsite_amount_cents'] or 0,
            'net_cents':          net,
        })

    return jsonify({
        'this_month':   this_month,
        'last_month':   last_month,
        'total':        total,
        'pending':      {'net_cents': pending['net'] or 0, 'count': pending['n'] or 0},
        'transactions': transactions,
    })


# ── Admin API ────────────────────────────────────────────────────────────────
@app.route('/api/admin/stats')
def admin_stats():
    err = require_admin()
    if err: return err

    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    conn = get_db()
    users_total   = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
    users_artists = conn.execute('SELECT COUNT(*) FROM users WHERE is_artist=1').fetchone()[0]
    users_new     = conn.execute('SELECT COUNT(*) FROM users WHERE created_at >= ?',
                                  (month_start.isoformat(),)).fetchone()[0]

    bookings_total      = conn.execute('SELECT COUNT(*) FROM bookings').fetchone()[0]
    bookings_confirmed  = conn.execute(
        "SELECT COUNT(*) FROM bookings WHERE status IN ('confirmed','completed')").fetchone()[0]
    bookings_this_month = conn.execute(
        'SELECT COUNT(*) FROM bookings WHERE created_at >= ?',
        (month_start.isoformat(),)).fetchone()[0]

    # Platforma revenue z fees
    fee_total = conn.execute('''
        SELECT COALESCE(SUM(platform_fee_cents), 0)
          + COALESCE(SUM(balance_charge_fee_cents), 0)
        FROM bookings
        WHERE status IN ('confirmed','completed')
    ''').fetchone()[0]
    fee_month = conn.execute('''
        SELECT COALESCE(SUM(platform_fee_cents), 0)
          + COALESCE(SUM(balance_charge_fee_cents), 0)
        FROM bookings
        WHERE status IN ('confirmed','completed') AND created_at >= ?
    ''', (month_start.isoformat(),)).fetchone()[0]

    portfolio_total = conn.execute('SELECT COUNT(*) FROM portfolio_items').fetchone()[0]
    reviews_total   = conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
    reports_open    = conn.execute(
        'SELECT COUNT(*) FROM review_reports WHERE resolved=0').fetchone()[0]

    # Top 5 tatérů podle revenue
    top_artists = conn.execute('''
        SELECT u.username, u.display_name,
               COUNT(b.id) AS bookings_n,
               COALESCE(SUM(b.deposit_cents - b.platform_fee_cents - b.refund_cents
                            + b.balance_paid_cents - b.balance_charge_fee_cents), 0) AS revenue
        FROM users u
        LEFT JOIN bookings b ON b.artist_id = u.id AND b.status IN ('confirmed','completed')
        WHERE u.is_artist = 1
        GROUP BY u.id
        ORDER BY revenue DESC
        LIMIT 5
    ''').fetchall()

    # Recent bookings — 10 nejnovějších
    recent = conn.execute('''
        SELECT b.id, b.created_at, b.status, b.deposit_cents,
               ua.username AS artist_username, ua.display_name AS artist_name,
               uc.username AS client_username, uc.display_name AS client_name
        FROM bookings b
        JOIN users ua ON ua.id = b.artist_id
        JOIN users uc ON uc.id = b.client_id
        ORDER BY b.created_at DESC
        LIMIT 10
    ''').fetchall()
    conn.close()

    return jsonify({
        'users':    {'total': users_total, 'artists': users_artists, 'new_this_month': users_new},
        'bookings': {'total': bookings_total, 'confirmed': bookings_confirmed,
                     'this_month': bookings_this_month},
        'platform_fee_cents': {'total': fee_total, 'this_month': fee_month},
        'portfolio_total': portfolio_total,
        'reviews_total':   reviews_total,
        'reports_open':    reports_open,
        'top_artists': [
            {'username': r['username'], 'display_name': r['display_name'],
             'bookings_n': r['bookings_n'], 'revenue_cents': r['revenue']}
            for r in top_artists
        ],
        'recent_bookings': [
            {'id': r['id'], 'created_at': r['created_at'], 'status': r['status'],
             'deposit_cents': r['deposit_cents'],
             'artist_username': r['artist_username'], 'artist_name': r['artist_name'],
             'client_username': r['client_username'], 'client_name': r['client_name']}
            for r in recent
        ],
    })


@app.route('/api/admin/reports')
def admin_reports():
    err = require_admin()
    if err: return err
    conn = get_db()
    rows = conn.execute('''
        SELECT r.id, r.review_id, r.reason, r.note, r.created_at, r.resolved,
               rep.username AS reporter_username,
               rv.rating, rv.text AS review_text,
               uc.username AS review_client_username, uc.display_name AS review_client_name,
               ua.username AS review_artist_username, ua.display_name AS review_artist_name
        FROM review_reports r
        JOIN users rep ON rep.id = r.reporter_id
        LEFT JOIN reviews rv ON rv.id = r.review_id
        LEFT JOIN users uc ON uc.id = rv.client_id
        LEFT JOIN users ua ON ua.id = rv.artist_id
        ORDER BY r.resolved ASC, r.created_at DESC
        LIMIT 100
    ''').fetchall()
    conn.close()
    return jsonify([{
        'id':           r['id'],
        'review_id':    r['review_id'],
        'reason':       r['reason'],
        'note':         r['note'] or '',
        'created_at':   r['created_at'],
        'resolved':     bool(r['resolved']),
        'reporter_username':      r['reporter_username'],
        'review_rating':          r['rating'],
        'review_text':            r['review_text'] or '',
        'review_client_username': r['review_client_username'],
        'review_client_name':     r['review_client_name'],
        'review_artist_username': r['review_artist_username'],
        'review_artist_name':     r['review_artist_name'],
    } for r in rows])


@app.route('/api/admin/reports/<int:rid>/resolve', methods=['POST'])
def admin_resolve_report(rid):
    err = require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    delete_review = bool(data.get('delete_review'))
    conn = get_db()
    row = conn.execute('SELECT review_id FROM review_reports WHERE id=?', (rid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'not found'}), 404
    conn.execute('UPDATE review_reports SET resolved=1 WHERE id=?', (rid,))
    if delete_review and row['review_id']:
        conn.execute('DELETE FROM reviews WHERE id=?', (row['review_id'],))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted_review': delete_review})


CRON_SECRET = os.environ.get('CRON_SECRET', '').strip()


def _check_cron_auth():
    """Authorize cron request. Expects Bearer header nebo ?key=...
    Vrátí None pokud OK, Response pokud not authorized."""
    if not CRON_SECRET:
        return jsonify({'error': 'CRON_SECRET not configured on server'}), 503
    provided = (request.headers.get('Authorization', '')
                .replace('Bearer ', '').strip())
    if not provided:
        provided = (request.args.get('key', '') or '').strip()
    if provided != CRON_SECRET:
        return jsonify({'error': 'unauthorized'}), 401
    return None


@app.route('/api/cron/booking-reminders', methods=['GET', 'POST'])
def cron_booking_reminders():
    """Posílá 24h reminder pro bookings, které začínají za 22-26h
    a ještě nedostaly reminder. Voláno externím cronem (cron-job.org)
    každou hodinu. Idempotent — duplicate volání nepošlou reminder 2x
    dík reminder_sent_at flagu."""
    err = _check_cron_auth()
    if err: return err

    now = datetime.utcnow()
    win_start = (now + timedelta(hours=22)).isoformat()
    win_end   = (now + timedelta(hours=26)).isoformat()

    conn = get_db()
    rows = conn.execute('''
        SELECT b.id, b.client_id, b.artist_id, b.booking_start_at, b.duration_hours,
               ua.display_name AS artist_name, ua.studio AS artist_studio, ua.city AS artist_city,
               uc.display_name AS client_name
        FROM bookings b
        JOIN users ua ON ua.id = b.artist_id
        JOIN users uc ON uc.id = b.client_id
        WHERE b.status IN ('confirmed', 'pending_payment')
          AND b.booking_start_at >= ?
          AND b.booking_start_at <  ?
          AND b.reminder_sent_at IS NULL
    ''', (win_start, win_end)).fetchall()

    sent = []
    failed = []
    for b in rows:
        when_str = _fmt_booking_when(b['booking_start_at'], b['duration_hours'])
        try:
            # Email + push klientovi
            send_booking_email(conn, b['client_id'], 'reminder_for_client', {
                'other_name': b['artist_name'],
                'when':       when_str,
                'studio':     b['artist_studio'] or '',
                'city':       b['artist_city'] or '',
                'booking_url': f'{APP_BASE_URL}/my-bookings',
            })
            push_notif(conn, b['client_id'], b['artist_id'], 'booking_reminder',
                       b['id'], 'booking',
                       f'Zítra v {b["booking_start_at"][11:16]} máš tetování u {b["artist_name"]}')
            # Email + push tatérovi
            send_booking_email(conn, b['artist_id'], 'reminder_for_artist', {
                'other_name': b['client_name'],
                'when':       when_str,
                'booking_url': f'{APP_BASE_URL}/my-bookings',
            })
            push_notif(conn, b['artist_id'], b['client_id'], 'booking_reminder',
                       b['id'], 'booking',
                       f'Zítra v {b["booking_start_at"][11:16]} máš klienta {b["client_name"]}')
            # Marker, aby se neopakovalo
            conn.execute('UPDATE bookings SET reminder_sent_at=? WHERE id=?',
                         (datetime.utcnow().isoformat(), b['id']))
            sent.append(b['id'])
        except Exception as e:
            failed.append({'id': b['id'], 'err': str(e)})
    conn.commit()
    conn.close()

    return jsonify({
        'ok':     True,
        'window': [win_start, win_end],
        'sent':   sent,
        'failed': failed,
        'count':  len(sent),
    })


@app.route('/api/styles')
def list_styles():
    return jsonify(list(ALLOWED_TATTOO_STYLES))


@app.route('/api/artists/similar/<username>')
def artists_similar(username):
    """Doporučení podobných tatérů — pro 'Lidé také navštívili' sekci na profilu."""
    conn = get_db()
    target = conn.execute('SELECT id, city, styles FROM users WHERE username = ? AND is_artist = 1',
                          (username,)).fetchone()
    if not target:
        conn.close()
        return jsonify([])

    target_styles = [s.strip().lower() for s in (target['styles'] or '').split(',') if s.strip()]
    target_city = target['city'] or ''

    # Sběr kandidátů s váhou (city match = 3, style match = 1 per shared style)
    rows = conn.execute('''
        SELECT id, username, display_name, city, studio, avatar, styles,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = users.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = users.id) AS rating_count
        FROM users
        WHERE is_artist = 1 AND id != ?
        ORDER BY rating_count DESC, rating_avg DESC, id DESC
        LIMIT 200
    ''', (target['id'],)).fetchall()
    conn.close()

    def score(r):
        s = 0
        if target_city and (r['city'] or '') == target_city:
            s += 3
        if target_styles:
            r_styles = [x.strip().lower() for x in (r['styles'] or '').split(',') if x.strip()]
            s += sum(1 for t in target_styles if t in r_styles)
        # rating boost — preferuj vícekrát hodnocené tatéry
        s += min(2, (r['rating_count'] or 0) * 0.1)
        return s

    scored = sorted(rows, key=score, reverse=True)
    top = scored[:6]
    return jsonify([
        {
            'username':      r['username'],
            'display_name':  r['display_name'],
            'city':          r['city'] or '',
            'studio':        r['studio'] or '',
            'avatar_url':    f'/uploads/{r["avatar"]}' if r['avatar'] else None,
            'initials':      initials(r['display_name']),
            'rating_avg':    round(r['rating_avg'], 1) if r['rating_avg'] else None,
            'rating_count':  r['rating_count'] or 0,
        }
        for r in top
    ])


@app.route('/api/artists/map')
def artists_map():
    """Vrací seznam tatérů s GPS souřadnicemi pro mapový view."""
    conn = get_db()
    rows = conn.execute('''
        SELECT id, username, display_name, city, studio, styles, avatar, lat, lng,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = users.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = users.id) AS rating_count
        FROM users
        WHERE is_artist = 1
          AND lat IS NOT NULL AND lng IS NOT NULL
        ORDER BY display_name ASC
    ''').fetchall()
    conn.close()
    return jsonify([
        {
            'id':            r['id'],
            'username':      r['username'],
            'display_name':  r['display_name'],
            'city':          r['city'] or '',
            'studio':        r['studio'] or '',
            'styles':        r['styles'] or '',
            'avatar_url':    f'/uploads/{r["avatar"]}' if r['avatar'] else None,
            'initials':      initials(r['display_name']),
            'lat':           r['lat'],
            'lng':           r['lng'],
            'rating_avg':    round(r['rating_avg'], 1) if r['rating_avg'] else None,
            'rating_count':  r['rating_count'] or 0,
        }
        for r in rows
    ])


@app.route('/api/sizes')
def list_sizes():
    """Velikosti tetování s mapováním na hodiny — sdílené mezi UI a backendem."""
    return jsonify([
        {'key': k, 'hours': h, 'label': lbl}
        for k, (h, lbl) in SIZE_PRESETS.items()
    ])


# ── InkLink: Bookings ─────────────────────────────────────────────────────────

CANCEL_REFUND_FULL_HOURS = 96   # >= 96 h → 100% refund
CANCEL_REFUND_HALF_HOURS = 48   # 48–96 h → 50% refund
                                # < 48 h  → 0% (záloha propadá)


def _slot_avg_price(slot) -> int:
    pmin = slot['price_min'] or 0
    pmax = slot['price_max'] or 0
    if pmin and pmax:
        return (pmin + pmax) // 2
    return pmin or pmax or 0


def _booking_to_dict(row, slot=None, artist=None, client=None):
    d = dict(row)
    if slot:
        d['slot'] = {
            'id':        slot['id'],
            'start_at':  slot['start_at'],
            'end_at':    slot['end_at'],
            'price_min': slot['price_min'],
            'price_max': slot['price_max'],
            'note':      slot['note'] or '',
        }
    if artist:
        d['artist'] = {
            'id':           artist['id'],
            'username':     artist['username'],
            'display_name': artist['display_name'],
            'avatar_url':   f'/uploads/{artist["avatar"]}' if artist['avatar'] else None,
            'studio':       artist['studio'] or '',
            'city':         artist['city'] or '',
        }
    if client:
        d['client'] = {
            'id':           client['id'],
            'username':     client['username'],
            'display_name': client['display_name'],
            'email':        client['email'] or '',
            'phone':        client['phone'] or '',
            'avatar_url':   f'/uploads/{client["avatar"]}' if client['avatar'] else None,
        }
    return d


SIZE_PRESETS = {
    # label: (duration_hours, čj label)
    'mini':     (1, 'Mini (do 5 cm)'),
    'small':    (2, 'Malé (5–10 cm)'),
    'medium':   (3, 'Středně velké (10–20 cm)'),
    'large':    (5, 'Velké (20–30 cm)'),
    'xl':       (8, 'Celý den / sleeve'),
}


def _slot_active_bookings(conn, slot_id):
    """Vrátí seznam (start, end) obsazených sub-rangů (v ISO) pro daný slot.
    Bere v úvahu jen pending_payment + confirmed (zrušené/dokončené nepřekáží)."""
    rows = conn.execute('''SELECT booking_start_at, booking_end_at, status
                           FROM bookings
                           WHERE slot_id = ? AND status IN ('pending_payment','confirmed')
                                 AND booking_start_at IS NOT NULL
                                 AND booking_end_at IS NOT NULL''', (slot_id,)).fetchall()
    return [(r['booking_start_at'], r['booking_end_at']) for r in rows]


def _ranges_overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


@app.route('/api/bookings', methods=['POST'])
def create_booking():
    err = require_login()
    if err: return err

    data        = request.get_json(silent=True) or request.form
    slot_id     = data.get('slot_id')
    design_note = (data.get('design_note') or '').strip()[:1000]
    size_label  = (data.get('size_label') or '').strip().lower()
    booking_start_raw = (data.get('booking_start_at') or '').strip()
    duration_raw      = data.get('duration_hours')
    portfolio_item_id = data.get('portfolio_item_id')
    pay_full          = bool(data.get('pay_full'))

    if not slot_id:
        return jsonify({'error': 'slot_id je povinný'}), 400
    if not design_note:
        return jsonify({'error': 'Popiš tatérovi co chceš (lokace, motiv, velikost…).'}), 400

    conn = get_db()
    slot = conn.execute('SELECT * FROM slots WHERE id=?', (slot_id,)).fetchone()
    if not slot:
        conn.close()
        return jsonify({'error': 'Termín nenalezen.'}), 404
    if slot['user_id'] == session['user_id']:
        conn.close()
        return jsonify({'error': 'Nemůžeš si rezervovat vlastní termín.'}), 400

    price_unit = (slot['price_unit'] if 'price_unit' in slot.keys() else None) or 'hour'
    artist = conn.execute('''SELECT id, deposit_pct_default, stripe_charges_enabled, display_name
                             FROM users WHERE id=?''', (slot['user_id'],)).fetchone()
    deposit_pct = slot['deposit_pct'] if slot['deposit_pct'] is not None else (artist['deposit_pct_default'] or 30)
    avg_price   = _slot_avg_price(slot)
    avg_hourly  = avg_price  # ze sazby — pro 'hour'

    # Pokud klient rezervuje konkrétní portfolio sketch, načti jeho fixní cenu/délku
    portfolio_item = None
    if portfolio_item_id:
        try:
            portfolio_item_id = int(portfolio_item_id)
        except (ValueError, TypeError):
            conn.close(); return jsonify({'error': 'Špatné portfolio_item_id'}), 400
        portfolio_item = conn.execute(
            'SELECT id, user_id, price_kc, estimated_hours, caption FROM portfolio_items WHERE id=?',
            (portfolio_item_id,)).fetchone()
        if not portfolio_item or portfolio_item['user_id'] != slot['user_id']:
            conn.close(); return jsonify({'error': 'Portfolio návrh nepatří k tomuto tatérovi.'}), 400

    def _naive(s):
        """Parse ISO; pokud je tz-aware, převeď na naive UTC (matchuje DB-style naive)."""
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    try:
        slot_start = _naive(slot['start_at'])
        slot_end   = _naive(slot['end_at'])
    except Exception:
        conn.close()
        return jsonify({'error': 'Termín má vadný čas.'}), 500
    slot_total_hours = (slot_end - slot_start).total_seconds() / 3600.0

    # --- větev podle price_unit ----------------------------------------------
    if price_unit == 'flat':
        # Legacy chování: jeden booking zabere celý slot
        if slot['status'] != 'free':
            conn.close()
            return jsonify({'error': 'Termín už není volný.'}), 409
        duration_hours = round(slot_total_hours, 2)
        booking_start  = slot_start
        booking_end    = slot_end
        total_price    = avg_price                    # v Kč (celkem)
        deposit_cents  = int(round(total_price * deposit_pct / 100)) * 100
    else:
        # Hodinový blok — klient si vybírá sub-range
        # 1) Spočti duration — pokud je portfolio sketch, použij jeho odhad
        if portfolio_item and portfolio_item['estimated_hours']:
            duration_hours = float(portfolio_item['estimated_hours'])
        elif size_label and size_label in SIZE_PRESETS:
            duration_hours = float(SIZE_PRESETS[size_label][0])
        else:
            try:
                duration_hours = float(duration_raw or 0)
            except (ValueError, TypeError):
                duration_hours = 0
        if duration_hours <= 0:
            conn.close()
            return jsonify({'error': 'Vyber velikost tetování (počet hodin).'}), 400
        min_dur = slot['min_duration_hours'] if 'min_duration_hours' in slot.keys() else 1
        if duration_hours < (min_dur or 1):
            conn.close()
            return jsonify({'error': f'Tatér přijímá min. {min_dur} h sezení.'}), 400
        if duration_hours > slot_total_hours + 1e-6:
            conn.close()
            return jsonify({'error': 'Délka rezervace přesahuje volný blok.'}), 400

        # 2) Parse booking_start_at; pokud chybí, dej 1. volný okamžik
        if booking_start_raw:
            try:
                booking_start = _naive(booking_start_raw)
            except ValueError:
                conn.close()
                return jsonify({'error': 'Špatný formát začátku rezervace.'}), 400
        else:
            booking_start = slot_start
        booking_end = booking_start + timedelta(hours=duration_hours)

        if booking_start < slot_start - timedelta(minutes=1):
            conn.close()
            return jsonify({'error': 'Začátek rezervace je před začátkem bloku.'}), 400
        if booking_end > slot_end + timedelta(minutes=1):
            conn.close()
            return jsonify({'error': 'Konec rezervace je za koncem bloku.'}), 400

        # 3) Kontrola kolize s existujícími rezervacemi
        existing = _slot_active_bookings(conn, slot_id)
        for s_iso, e_iso in existing:
            if _ranges_overlap(booking_start.isoformat(), booking_end.isoformat(), s_iso, e_iso):
                conn.close()
                return jsonify({'error': 'Tento čas se kryje s jinou rezervací — vyber jiný začátek.'}), 409

        # 4) Cena: fixní z portfolio sketche (pokud je) jinak duration × hourly
        if portfolio_item and portfolio_item['price_kc']:
            total_price = float(portfolio_item['price_kc'])
        else:
            total_price = avg_hourly * duration_hours
        deposit_cents = int(round(total_price * deposit_pct / 100)) * 100

    # Celková cena (pro report a balance) — total_price je v Kč, převést na cents
    total_price_cents = int(round(total_price)) * 100

    # M7: pokud klient zvolil "zaplatit celé předem", deposit = total
    payment_mode = 'full' if pay_full else 'deposit'
    if pay_full:
        deposit_cents = total_price_cents
    balance_due_cents = max(0, total_price_cents - deposit_cents)

    platform_fee_cents = int(round(deposit_cents * PLATFORM_COMMISSION_PCT / 100))
    demo_mode  = not STRIPE_SECRET_KEY or not artist['stripe_charges_enabled']
    init_status = 'confirmed' if demo_mode else 'pending_payment'

    conn.execute('''INSERT INTO bookings
        (slot_id, artist_id, client_id, status, deposit_cents, platform_fee_cents,
         design_note, confirmed_at,
         booking_start_at, booking_end_at, duration_hours, size_label, portfolio_item_id,
         payment_mode, total_price_cents, balance_due_cents)
        VALUES (?,?,?,?,?,?,?, ?, ?,?,?,?,?, ?,?,?)''',
        (slot_id, slot['user_id'], session['user_id'], init_status,
         deposit_cents, platform_fee_cents, design_note,
         datetime.utcnow().isoformat() if init_status == 'confirmed' else None,
         booking_start.isoformat(), booking_end.isoformat(), duration_hours, size_label,
         portfolio_item['id'] if portfolio_item else None,
         payment_mode, total_price_cents, balance_due_cents))

    if price_unit == 'flat':
        # legacy: zablokuj slot
        conn.execute("UPDATE slots SET status='held' WHERE id=?", (slot_id,))
        if init_status == 'confirmed':
            conn.execute("UPDATE slots SET status='booked' WHERE id=?", (slot_id,))
    # 'hour' bloky zůstávají 'free' — kapacitu řešíme přes booking_start/end overlap

    conn.commit()
    bid = conn.execute('SELECT id FROM bookings WHERE client_id=? ORDER BY id DESC LIMIT 1',
                       (session['user_id'],)).fetchone()['id']

    push_notif(conn, slot['user_id'], session['user_id'], 'booking',
               bid, 'booking', f'Nová rezervace ({duration_hours} h): {design_note[:60]}')
    conn.commit()

    # Email to artist
    client_row = conn.execute('SELECT display_name FROM users WHERE id=?',
                              (session['user_id'],)).fetchone()
    send_booking_email(conn, slot['user_id'], 'new_booking_for_artist', {
        'other_name': client_row['display_name'] if client_row else 'a client',
        'when': _fmt_booking_when(slot['start_at'], duration_hours),
        'design_note': design_note,
        'booking_url': f'{APP_BASE_URL}/my-bookings',
    })
    conn.close()

    return jsonify({
        'ok': True,
        'id': bid,
        'status': init_status,
        'demo_mode': demo_mode,
        'payment_mode': payment_mode,
        'deposit_cents': deposit_cents,
        'platform_fee_cents': platform_fee_cents,
        'balance_due_cents': balance_due_cents,
        'total_price_cents': total_price_cents,
        'duration_hours': duration_hours,
        'booking_start_at': booking_start.isoformat(),
        'booking_end_at':   booking_end.isoformat(),
        'total_price_kc':   round(total_price),
    })


@app.route('/api/bookings/<int:bid>')
def get_booking(bid):
    err = require_login()
    if err: return err
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if session['user_id'] not in (b['client_id'], b['artist_id']):
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    slot   = conn.execute('SELECT * FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
    artist = conn.execute('SELECT id, username, display_name, avatar, studio, city FROM users WHERE id=?',
                           (b['artist_id'],)).fetchone()
    client = conn.execute('SELECT id, username, display_name, email, phone, avatar FROM users WHERE id=?',
                           (b['client_id'],)).fetchone()
    conn.close()
    return jsonify(_booking_to_dict(b, slot, artist, client))


@app.route('/api/bookings/<int:bid>', methods=['PATCH'])
def update_booking(bid):
    """Edit existující rezervace.
    - Klient může měnit jen `design_note` (doplnit detaily / poznámku).
    - Tatér může měnit `booking_start_at`, `duration_hours`, `design_note`
      (přesun termínu po dohodě s klientem). Tatér nemůže měnit cenu —
      ta byla zafixovaná při bookingu (viz pay_full / portfolio fixed price).
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if uid not in (b['client_id'], b['artist_id']):
        conn.close(); return jsonify({'error': 'forbidden'}), 403
    if b['status'] in ('completed', 'cancelled_client', 'cancelled_artist', 'no_show'):
        conn.close(); return jsonify({'error': 'Tuto rezervaci už nelze editovat (' + b['status'] + ').'}), 409

    is_artist = (uid == b['artist_id'])
    data = request.get_json(silent=True) or {}

    sets, params = [], []
    changes = []
    new_start = new_end = None

    # design_note — povolen oběma stranám
    if 'design_note' in data:
        note = (data['design_note'] or '').strip()[:1000]
        sets.append('design_note=?'); params.append(note)
        changes.append('popis')

    # booking_start_at + duration_hours — pouze tatér
    wants_time_change = ('booking_start_at' in data) or ('duration_hours' in data) or ('size_label' in data)
    if wants_time_change and not is_artist:
        conn.close()
        return jsonify({'error': 'Termín a délku může měnit jen tatér. Klient může upravit jen popis.'}), 403

    if wants_time_change:
        # Najdi cílovou délku
        size_label = (data.get('size_label') or '').strip().lower()
        if size_label and size_label in SIZE_PRESETS:
            duration_h = float(SIZE_PRESETS[size_label][0])
        else:
            try:
                duration_h = float(data.get('duration_hours') or b['duration_hours'] or 0)
            except (ValueError, TypeError):
                conn.close(); return jsonify({'error': 'Špatná délka'}), 400
        if duration_h <= 0:
            conn.close(); return jsonify({'error': 'Délka musí být kladná'}), 400

        # start_at: pokud zadán, použij; jinak ponech původní
        start_raw = (data.get('booking_start_at') or b['booking_start_at'] or '').strip()
        try:
            new_start = datetime.fromisoformat(start_raw.replace('Z', '+00:00'))
            if new_start.tzinfo is not None:
                new_start = new_start.astimezone(timezone.utc).replace(tzinfo=None)
        except (ValueError, AttributeError):
            conn.close(); return jsonify({'error': 'Špatný formát začátku'}), 400
        new_end = new_start + timedelta(hours=duration_h)

        if new_start < datetime.utcnow() - timedelta(minutes=5):
            conn.close(); return jsonify({'error': 'Nový termín nemůže být v minulosti'}), 400

        # validace vůči slotu
        slot = conn.execute('SELECT * FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
        if not slot:
            conn.close(); return jsonify({'error': 'Slot rezervace neexistuje'}), 500
        try:
            slot_start = datetime.fromisoformat(slot['start_at'].replace('Z', '+00:00'))
            slot_end   = datetime.fromisoformat(slot['end_at'].replace('Z', '+00:00'))
            for d in (slot_start, slot_end):
                pass
        except Exception:
            conn.close(); return jsonify({'error': 'Slot má vadný čas'}), 500
        if new_start < slot_start - timedelta(minutes=1) or new_end > slot_end + timedelta(minutes=1):
            conn.close(); return jsonify({'error': 'Nový termín nesedí do bloku tatéra ('
                                           + slot_start.strftime('%H:%M') + '–'
                                           + slot_end.strftime('%H:%M') + ').'}), 400

        # overlap s jinými aktivními bookings (vyjma self)
        others = conn.execute('''SELECT booking_start_at, booking_end_at FROM bookings
                                 WHERE slot_id=? AND id<>?
                                       AND status IN ('pending_payment','confirmed')
                                       AND booking_start_at IS NOT NULL''',
                              (b['slot_id'], bid)).fetchall()
        for r in others:
            if _ranges_overlap(new_start.isoformat(), new_end.isoformat(),
                               r['booking_start_at'], r['booking_end_at']):
                conn.close()
                return jsonify({'error': 'Nový termín se kryje s jinou rezervací v bloku.'}), 409

        sets.append('booking_start_at=?'); params.append(new_start.isoformat())
        sets.append('booking_end_at=?');   params.append(new_end.isoformat())
        sets.append('duration_hours=?');   params.append(duration_h)
        if size_label:
            sets.append('size_label=?'); params.append(size_label)
        changes.append('termín')

    if not sets:
        conn.close(); return jsonify({'ok': True, 'no_changes': True})

    params.append(bid)
    conn.execute(f'UPDATE bookings SET {", ".join(sets)} WHERE id=?', tuple(params))
    conn.commit()

    # notif druhé straně
    other_id = b['artist_id'] if uid == b['client_id'] else b['client_id']
    actor_role = 'tatér' if is_artist else 'klient'
    push_notif(conn, other_id, uid, 'booking_updated', bid, 'booking',
               f'Rezervace upravena ({actor_role} změnil/a {", ".join(changes)}).')
    conn.commit()
    updated = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'changes': changes, 'booking': dict(updated)})


@app.route('/api/me/bookings/<role>')
def list_my_bookings(role):
    err = require_login()
    if err: return err
    if role not in ('client', 'artist'):
        return jsonify({'error': 'role must be client|artist'}), 400
    col = 'client_id' if role == 'client' else 'artist_id'
    conn = get_db()
    rows = conn.execute(f'''
        SELECT b.*, s.start_at, s.end_at, s.price_min, s.price_max, s.note AS slot_note,
               ua.username AS a_username, ua.display_name AS a_display_name, ua.avatar AS a_avatar,
               ua.studio AS a_studio, ua.city AS a_city,
               uc.username AS c_username, uc.display_name AS c_display_name, uc.avatar AS c_avatar,
               uc.email AS c_email, uc.phone AS c_phone,
               r.id AS review_id, r.rating AS review_rating, r.text AS review_text,
               r.response AS review_response, r.created_at AS review_created_at
        FROM bookings b
        JOIN slots s ON b.slot_id = s.id
        JOIN users ua ON b.artist_id = ua.id
        JOIN users uc ON b.client_id = uc.id
        LEFT JOIN reviews r ON r.booking_id = b.id
        WHERE b.{col} = ?
        ORDER BY s.start_at DESC
        LIMIT 200
    ''', (session['user_id'],)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            'id':                  r['id'],
            'status':              r['status'],
            'design_note':         r['design_note'] or '',
            'deposit_cents':       r['deposit_cents'],
            'platform_fee_cents':  r['platform_fee_cents'],
            'onsite_amount_cents': r['onsite_amount_cents'],
            'refund_cents':        r['refund_cents'],
            'cancellation_actor':  r['cancellation_actor'],
            'currency':            r['currency'],
            'created_at':          r['created_at'],
            'confirmed_at':        r['confirmed_at'],
            'cancelled_at':        r['cancelled_at'],
            'completed_at':        r['completed_at'],
            'booking_start_at':    r['booking_start_at'],
            'booking_end_at':      r['booking_end_at'],
            'duration_hours':      r['duration_hours'],
            'size_label':          r['size_label'] or '',
            'portfolio_item_id':   r['portfolio_item_id'],
            'payment_mode':        r['payment_mode'] or 'deposit',
            'total_price_cents':   r['total_price_cents'] or 0,
            'balance_due_cents':   r['balance_due_cents'] or 0,
            'balance_paid_cents':  r['balance_paid_cents'] or 0,
            'balance_payment_intent_id': r['balance_payment_intent_id'],
            'balance_charge_cents': r['balance_charge_cents'] or 0,
            'review': ({
                'id':         r['review_id'],
                'rating':     r['review_rating'],
                'text':       r['review_text'] or '',
                'response':   r['review_response'] or '',
                'created_at': r['review_created_at'],
            } if r['review_id'] else None),
            'slot': {
                'id':        r['slot_id'], 'start_at': r['start_at'], 'end_at': r['end_at'],
                'price_min': r['price_min'], 'price_max': r['price_max'], 'note': r['slot_note'] or '',
            },
            'artist': {
                'username':     r['a_username'], 'display_name': r['a_display_name'],
                'avatar_url':   f'/uploads/{r["a_avatar"]}' if r['a_avatar'] else None,
                'studio':       r['a_studio'] or '', 'city': r['a_city'] or '',
            },
            'client': {
                'username':     r['c_username'], 'display_name': r['c_display_name'],
                'avatar_url':   f'/uploads/{r["c_avatar"]}' if r['c_avatar'] else None,
                'email':        r['c_email'] or '', 'phone': r['c_phone'] or '',
            },
        })
    return jsonify(out)


def _cancellation_refund_pct(hours_before: float, actor: str) -> int:
    """Vrátí % refundu podle pravidel storna."""
    if actor == 'artist':
        return 100
    if hours_before >= CANCEL_REFUND_FULL_HOURS:
        return 100
    if hours_before >= CANCEL_REFUND_HALF_HOURS:
        return 50
    return 0


@app.route('/api/bookings/<int:bid>/cancel', methods=['POST'])
def cancel_booking(bid):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if uid not in (b['client_id'], b['artist_id']):
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    if b['status'] in ('cancelled_client', 'cancelled_artist', 'completed'):
        conn.close()
        return jsonify({'error': 'Tato rezervace už nelze zrušit.'}), 409

    actor = 'artist' if uid == b['artist_id'] else 'client'
    slot  = conn.execute('SELECT * FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
    try:
        start_dt = datetime.fromisoformat(slot['start_at'].replace('Z', '+00:00'))
    except Exception:
        start_dt = datetime.utcnow() + timedelta(days=7)
    hours_before = (start_dt - datetime.utcnow()).total_seconds() / 3600.0
    refund_pct   = _cancellation_refund_pct(hours_before, actor)
    refund_cents = int(round(b['deposit_cents'] * refund_pct / 100))
    new_status   = 'cancelled_artist' if actor == 'artist' else 'cancelled_client'

    # M4 později: pokud máme stripe_payment_intent_id, zavolat Refund. Teď jen status.
    conn.execute('''UPDATE bookings SET status=?, cancelled_at=?, cancellation_actor=?, refund_cents=?
                    WHERE id=?''',
                 (new_status, datetime.utcnow().isoformat(), actor, refund_cents, bid))
    conn.execute("UPDATE slots SET status='free' WHERE id=?", (b['slot_id'],))
    conn.commit()

    other = b['artist_id'] if actor == 'client' else b['client_id']
    push_notif(conn, other, uid, 'booking_cancelled', bid, 'booking',
               f'Rezervace zrušena ({actor}). Refund {refund_pct}%.')
    conn.commit()

    # Email to the other party
    duration_h = b['duration_hours'] if 'duration_hours' in b.keys() else None
    when_str = _fmt_booking_when(slot['start_at'] if slot else None, duration_h)
    send_booking_email(conn, other, 'booking_cancelled', {
        'actor_role': 'The client' if actor == 'client' else 'The tattooer',
        'when': when_str,
        'refund_pct': refund_pct,
        'booking_url': f'{APP_BASE_URL}/my-bookings',
    })
    conn.close()
    return jsonify({
        'ok': True,
        'status': new_status,
        'actor': actor,
        'hours_before': round(hours_before, 1),
        'refund_pct': refund_pct,
        'refund_cents': refund_cents,
    })


@app.route('/api/bookings/<int:bid>/complete', methods=['POST'])
def complete_booking(bid):
    """Tatér potvrdí, že rezervace proběhla. Přijímá:
       - onsite_kc          → hotovost / karta vybraná na místě (mimo platformu)
       - balance_kc         → částka, kterou si tatér vyžádá od klienta přes InkLink
                              (vytvoří se balance-charge a klient dostane mail s linkem)
    """
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    try:
        onsite_kc  = max(0, int(data.get('onsite_kc')  or 0))
        balance_kc = max(0, int(data.get('balance_kc') or 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Neplatná částka'}), 400
    onsite_cents = onsite_kc * 100

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if session['user_id'] != b['artist_id']:
        conn.close(); return jsonify({'error': 'Pouze tatér může označit rezervaci jako dokončenou.'}), 403
    if b['status'] not in ('confirmed', 'pending_payment'):
        conn.close(); return jsonify({'error': 'Tuto rezervaci nelze dokončit.'}), 409

    # Pokud tatér chce vystavit balance, spusť to PŘED dokončením — když selže
    # (např. částka přesahuje zbytek), zachová se status='confirmed' a tatér může retry.
    balance_result = None
    if balance_kc > 0:
        balance_result = _create_balance_charge(bid, balance_kc, session['user_id'])
        if balance_result.get('error'):
            conn.close()
            return jsonify({'error': f"Doplatek selhal: {balance_result['error']}"}), 400

    conn.execute('''UPDATE bookings SET status='completed', completed_at=?, onsite_amount_cents=?
                    WHERE id=?''',
                 (datetime.utcnow().isoformat(), onsite_cents, bid))
    conn.commit()

    # Email to client — review request
    artist = conn.execute('SELECT display_name, username FROM users WHERE id=?',
                          (b['artist_id'],)).fetchone()
    slot = conn.execute('SELECT start_at FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
    when_str = _fmt_booking_when(slot['start_at'] if slot else None,
                                 b['duration_hours'] if 'duration_hours' in b.keys() else None)
    review_url = (f'{APP_BASE_URL}/profile/{artist["username"]}#book-{bid}'
                  if artist else f'{APP_BASE_URL}/my-bookings')
    send_booking_email(conn, b['client_id'], 'review_request_for_client', {
        'other_name': artist['display_name'] if artist else 'your tattooer',
        'when': when_str,
        'review_url': review_url,
        'booking_url': f'{APP_BASE_URL}/my-bookings',
    })
    conn.close()

    return jsonify({
        'ok': True,
        'onsite_cents': onsite_cents,
        'balance_charge': balance_result,
    })


def _create_balance_charge(booking_id: int, kc: int, requesting_user_id: int) -> dict:
    """Vytvoří doplatkovou platbu (Stripe PaymentIntent v live módu, nebo mark-as-paid v demo).
    Vrátí dict popisující výsledek (vhodné pro JSON response)."""
    cents = kc * 100
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (booking_id,)).fetchone()
    if not b:
        conn.close(); return {'error': 'not found'}
    if requesting_user_id != b['artist_id']:
        conn.close(); return {'error': 'forbidden'}

    # validace: nepřekročit balance_due (legacy bookingy mají total_price_cents=0,
    # tehdy přeskočíme kontrolu — neznáme původní celkovou cenu)
    already_paid = b['balance_paid_cents'] or 0
    total_price  = b['total_price_cents'] or 0
    if total_price > 0:
        remaining = max(0, (b['balance_due_cents'] or 0) - already_paid)
        if cents > remaining + 1:
            conn.close()
            return {'error': f'Doplatek přes InkLink ({kc} Kč) přesahuje zbývající částku ({remaining//100} Kč).'}

    artist = conn.execute('SELECT email, display_name, stripe_account_id, stripe_charges_enabled FROM users WHERE id=?',
                          (b['artist_id'],)).fetchone()
    client = conn.execute('SELECT email, display_name FROM users WHERE id=?',
                          (b['client_id'],)).fetchone()

    fee_cents = int(round(cents * PLATFORM_COMMISSION_PCT / 100))
    pi_id = None
    payment_url = None
    demo_mode = not STRIPE_SECRET_KEY or not artist['stripe_charges_enabled']

    if not demo_mode:
        try:
            pi = stripe.PaymentIntent.create(
                amount=cents,
                currency=(b['currency'] or 'CZK').lower(),
                description=f'InkLink — doplatek za rezervaci #{booking_id}',
                application_fee_amount=fee_cents,
                transfer_data={'destination': artist['stripe_account_id']},
                metadata={
                    'inklink_booking_id': str(booking_id),
                    'inklink_kind': 'balance',
                },
            )
            pi_id = pi.id
            # klient dostane Stripe Payment Link / Hosted page přes /api/balance-pay/<bid>
            payment_url = f"{_origin()}/balance-pay/{booking_id}"
        except stripe.error.StripeError as e:
            conn.close()
            return {'error': f'Stripe: {str(e)}'}
    else:
        # Demo režim: rovnou označíme jako zaplacené
        pi_id = f'demo_pi_{booking_id}_{int(time.time())}'

    # Uložit záměr (v demo i live tytéž tři pole — paid se nastaví až po /balance-pay confirm
    # nebo po payment_intent.succeeded webhooku)
    conn.execute('''UPDATE bookings SET balance_payment_intent_id=?,
                                          balance_charge_cents=?,
                                          balance_charge_fee_cents=?
                    WHERE id=?''', (pi_id, cents, fee_cents, booking_id))
    conn.commit()

    # In-app notifikace klientovi (vždy — viditelná v notif panelu i v /my-bookings)
    msg = (f'Tatér {artist["display_name"]} ti vystavil doplatek {kc:,} Kč. '
           f'Zaplať v sekci „Moje rezervace" nebo přes link.')
    push_notif(conn, b['client_id'], b['artist_id'], 'balance_charge',
               booking_id, 'booking', msg)
    conn.commit()

    # Mail klientovi (jen když Resend nakonfigurovaný a live mód — ať nezasypeme demo)
    if client['email'] and RESEND_API_KEY and not demo_mode:
        in_app_url = f"{_origin()}/my-bookings"
        send_email(client['email'],
                   f'InkLink — doplatek {kc:,} Kč za tetování u {artist["display_name"]}',
                   f'''<div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px">
                     <div style="font-size:28px;letter-spacing:0.2em;color:#e8e8e8">INKLINK</div>
                     <p style="margin:24px 0">Ahoj {client["display_name"]}, tatér <b>{artist["display_name"]}</b>
                       ti vystavil doplatek za sezení.</p>
                     <p style="font-size:32px;color:#fff;margin:24px 0"><b>{kc:,} Kč</b></p>
                     <a href="{payment_url}" style="display:inline-block;padding:14px 28px;background:#fff;color:#000;text-decoration:none;letter-spacing:0.1em;text-transform:uppercase">Zaplatit kartou</a>
                     <p style="color:#888;font-size:12px;margin-top:24px">Nebo si link najdeš v aplikaci v sekci
                       <a href="{in_app_url}" style="color:#aaa">Moje rezervace</a>.</p>
                     <p style="color:#666;font-size:12px;margin-top:16px">Link je platný 7 dní.</p>
                   </div>''')

    conn.close()
    return {
        'ok': True,
        'demo_mode': demo_mode,
        'kc': kc,
        'cents': cents,
        'fee_cents': fee_cents,
        'payment_intent_id': pi_id,
        'payment_url': payment_url or f"{_origin()}/balance-pay/{booking_id}",
        'awaiting_payment': True,  # klient teď musí prokliknout /balance-pay/<id>
    }


@app.route('/balance-pay/<int:bid>')
def balance_pay_page(bid):
    """Veřejná stránka, na kterou klient klikne z e-mailu pro doplacení."""
    return send_from_directory('public', 'balance-pay.html')


@app.route('/api/balance-pay/<int:bid>')
def balance_pay_info(bid):
    """Veřejné info o doplatku — co klient potřebuje vidět na payment page."""
    conn = get_db()
    b = conn.execute('''SELECT b.*, ua.display_name AS a_name, ua.studio AS a_studio,
                               ua.avatar AS a_avatar
                        FROM bookings b
                        JOIN users ua ON ua.id = b.artist_id
                        WHERE b.id = ?''', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'Rezervace neexistuje.'}), 404
    if not b['balance_payment_intent_id']:
        conn.close()
        return jsonify({'error': 'Pro tuto rezervaci nebyl vystaven žádný doplatek.'}), 404

    charge_cents = b['balance_charge_cents'] or 0
    paid_cents   = b['balance_paid_cents']   or 0
    pi           = b['balance_payment_intent_id'] or ''
    is_demo      = pi.startswith('demo_pi_')
    conn.close()

    return jsonify({
        'id':          b['id'],
        'artist_name': b['a_name'],
        'artist_studio': b['a_studio'] or '',
        'artist_avatar_url': f'/uploads/{b["a_avatar"]}' if b['a_avatar'] else None,
        'when':        b['booking_start_at'],
        'design_note': b['design_note'] or '',
        'amount_cents': charge_cents,
        'paid_cents':   paid_cents,
        'is_demo':      is_demo,
        'already_paid_in_full': paid_cents >= charge_cents > 0,
    })


@app.route('/api/balance-pay/<int:bid>/demo-confirm', methods=['POST'])
def balance_pay_demo_confirm(bid):
    """Demo režim — simuluje úspěšnou platbu klienta. Označí jako zaplacené,
    přičte provizi platformě, pošle notif tatérovi.
    Endpoint je veřejný — link už zná pouze klient (přes mail / přes /my-bookings)."""
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    pi = b['balance_payment_intent_id'] or ''
    if not pi.startswith('demo_pi_'):
        conn.close(); return jsonify({'error': 'Tato rezervace je v live módu — platba probíhá přes Stripe.'}), 400
    if (b['balance_paid_cents'] or 0) >= (b['balance_charge_cents'] or 0) > 0:
        conn.close(); return jsonify({'ok': True, 'already_paid': True})

    cents = b['balance_charge_cents'] or 0
    fee   = b['balance_charge_fee_cents'] or 0
    conn.execute('''UPDATE bookings SET balance_paid_cents = balance_paid_cents + ?,
                                          platform_fee_cents = platform_fee_cents + ?
                    WHERE id=?''', (cents, fee, bid))
    # notif tatérovi
    push_notif(conn, b['artist_id'], b['client_id'], 'balance_paid',
               bid, 'booking', f'Klient zaplatil doplatek {cents//100:,} Kč přes InkLink (demo).')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'paid_cents': cents, 'fee_cents': fee})


@app.route('/api/bookings/<int:bid>/balance-charge', methods=['POST'])
def balance_charge(bid):
    """Tatér iniciuje doplatkovou platbu — buď samostatně, nebo z complete_booking."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    try:
        kc = int(data.get('kc') or 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Neplatná částka'}), 400
    if kc <= 0:
        return jsonify({'error': 'Částka musí být kladná'}), 400
    result = _create_balance_charge(bid, kc, session['user_id'])
    if result.get('error'):
        return jsonify(result), (404 if result['error'] == 'not found'
                                  else 403 if result['error'] == 'forbidden' else 400)
    return jsonify(result)


# ── InkLink: Reviews (recenze klientů) ──────────────────────────────────────

def _review_to_dict(r, client=None, booking_when=None):
    d = {
        'id':          r['id'],
        'booking_id':  r['booking_id'],
        'rating':      r['rating'],
        'text':        r['text'] or '',
        'response':    r['response'] or '',
        'response_at': r['response_at'],
        'created_at':  r['created_at'],
        'updated_at':  r['updated_at'],
        'when':        booking_when,
    }
    if client:
        d['client'] = {
            'username':     client['username'],
            'display_name': client['display_name'],
            'avatar_url':   f'/uploads/{client["avatar"]}' if client['avatar'] else None,
            'initials':     initials(client['display_name'] or client['username']),
        }
    return d


@app.route('/api/bookings/<int:bid>/review', methods=['GET'])
def get_review(bid):
    err = require_login()
    if err: return err
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if session['user_id'] not in (b['client_id'], b['artist_id']):
        conn.close(); return jsonify({'error': 'forbidden'}), 403
    row = conn.execute('SELECT * FROM reviews WHERE booking_id=?', (bid,)).fetchone()
    conn.close()
    if not row:
        return jsonify(None)
    return jsonify(_review_to_dict(row, booking_when=b['booking_start_at'] or b['confirmed_at']))


@app.route('/api/bookings/<int:bid>/review', methods=['POST'])
def upsert_review(bid):
    """Klient napíše nebo upraví hodnocení dokončeného bookingu."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    try:
        rating = int(data.get('rating') or 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Hodnocení musí být číslo 1–5'}), 400
    if rating < 1 or rating > 5:
        return jsonify({'error': 'Hodnocení musí být 1–5'}), 400
    text = (data.get('text') or '').strip()[:1000]

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if session['user_id'] != b['client_id']:
        conn.close(); return jsonify({'error': 'Pouze klient může napsat hodnocení.'}), 403
    if b['status'] != 'completed':
        conn.close(); return jsonify({'error': 'Hodnotit lze jen dokončené rezervace.'}), 409

    existing = conn.execute('SELECT id FROM reviews WHERE booking_id=?', (bid,)).fetchone()
    now = datetime.utcnow().isoformat()
    if existing:
        conn.execute('UPDATE reviews SET rating=?, text=?, updated_at=? WHERE id=?',
                     (rating, text, now, existing['id']))
        rid = existing['id']
        is_new = False
    else:
        conn.execute('''INSERT INTO reviews (booking_id, client_id, artist_id, rating, text)
                        VALUES (?,?,?,?,?)''',
                     (bid, b['client_id'], b['artist_id'], rating, text))
        rid = conn.execute('SELECT id FROM reviews WHERE booking_id=?', (bid,)).fetchone()['id']
        is_new = True
        push_notif(conn, b['artist_id'], b['client_id'], 'review_added',
                   rid, 'review',
                   f'Klient ti dal {rating}★{(": " + text[:60]) if text else ""}')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': rid, 'rating': rating, 'is_new': is_new})


@app.route('/api/reviews/<int:rid>', methods=['DELETE'])
def delete_review(rid):
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT client_id FROM reviews WHERE id=?', (rid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if row['client_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Pouze autor může smazat recenzi.'}), 403
    conn.execute('DELETE FROM reviews WHERE id=?', (rid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/reviews/<int:rid>/respond', methods=['POST'])
def respond_to_review(rid):
    """Tatér odpoví na recenzi."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    text = (data.get('response') or '').strip()[:500]

    conn = get_db()
    row = conn.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if row['artist_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Odpovídat může jen tatér z recenze.'}), 403
    conn.execute('UPDATE reviews SET response=?, response_at=? WHERE id=?',
                 (text, datetime.utcnow().isoformat() if text else None, rid))
    push_notif(conn, row['client_id'], row['artist_id'], 'review_response',
               rid, 'review',
               f'Tatér odpověděl na tvou recenzi{(": " + text[:80]) if text else ""}')
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/reviews/<int:rid>/report', methods=['POST'])
@limiter.limit('10 per hour')
def report_review(rid):
    """Klient/tatér může nahlásit nevhodnou recenzi. Moderace ručně z DB."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    reason = (data.get('reason') or '').strip().lower()[:30]
    note   = (data.get('note') or '').strip()[:500]
    ALLOWED = ('spam', 'offensive', 'false', 'private', 'other')
    if reason not in ALLOWED:
        return jsonify({'error': 'Neplatný důvod'}), 400

    conn = get_db()
    rv = conn.execute('SELECT id FROM reviews WHERE id=?', (rid,)).fetchone()
    if not rv:
        conn.close(); return jsonify({'error': 'not found'}), 404
    # Idempotent: stejný user + stejný review → nevadí, jen vrátit ok
    dup = conn.execute(
        'SELECT 1 FROM review_reports WHERE review_id=? AND reporter_id=?',
        (rid, session['user_id'])).fetchone()
    if dup:
        conn.close()
        return jsonify({'ok': True, 'duplicate': True})
    conn.execute(
        'INSERT INTO review_reports (review_id, reporter_id, reason, note) VALUES (?,?,?,?)',
        (rid, session['user_id'], reason, note))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/profile/<username>/reviews')
def get_profile_reviews(username):
    """Veřejný list recenzí tatéra — paginated, default limit 20."""
    conn = get_db()
    u = conn.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        conn.close(); return jsonify({'error': 'not found'}), 404
    try:
        offset = int(request.args.get('offset', 0))
        limit  = min(50, int(request.args.get('limit', 20)))
    except (ValueError, TypeError):
        offset, limit = 0, 20
    rows = conn.execute('''
        SELECT r.*, b.booking_start_at, b.size_label, b.duration_hours,
               uc.username AS c_username, uc.display_name AS c_display_name,
               uc.avatar AS c_avatar
        FROM reviews r
        JOIN bookings b ON b.id = r.booking_id
        JOIN users uc ON uc.id = r.client_id
        WHERE r.artist_id = ?
        ORDER BY r.created_at DESC
        LIMIT ? OFFSET ?
    ''', (u['id'], limit, offset)).fetchall()

    total = conn.execute('SELECT COUNT(*) FROM reviews WHERE artist_id=?', (u['id'],)).fetchone()[0]
    avg_row = conn.execute('SELECT AVG(rating) AS avg, COUNT(*) AS cnt FROM reviews WHERE artist_id=?',
                           (u['id'],)).fetchone()
    conn.close()
    return jsonify({
        'total':  total,
        'avg':    round(avg_row['avg'], 2) if avg_row['avg'] else None,
        'count':  avg_row['cnt'] or 0,
        'reviews': [_review_to_dict(r,
                       client={'username': r['c_username'], 'display_name': r['c_display_name'],
                               'avatar': r['c_avatar']},
                       booking_when=r['booking_start_at']) for r in rows],
    })


# ── InkLink: Měsíční report (PDF) ────────────────────────────────────────────

def _aggregate_artist_month(conn, artist_id: int, year: int, month: int):
    """Sečte rezervace tatéra v daném měsíci do strukturovaného summary.
    Klíč: 'kdy' = booking_start_at (kdy proběhla / měla proběhnout).
    """
    prefix = f'{year:04d}-{month:02d}'
    rows = conn.execute('''
        SELECT b.*, s.start_at AS slot_start, s.end_at AS slot_end,
               uc.username AS c_username, uc.display_name AS c_display_name
        FROM bookings b
        JOIN slots s ON s.id = b.slot_id
        JOIN users uc ON uc.id = b.client_id
        WHERE b.artist_id = ?
          AND COALESCE(b.booking_start_at, s.start_at) LIKE ?
        ORDER BY COALESCE(b.booking_start_at, s.start_at) ASC
    ''', (artist_id, prefix + '%')).fetchall()

    summary = {
        'period': f'{year:04d}-{month:02d}',
        'artist_id': artist_id,
        'count_total': len(rows),
        'count_completed': 0,
        'count_confirmed': 0,
        'count_cancelled_client': 0,
        'count_cancelled_artist': 0,
        'count_no_show': 0,
        # finanční částky v Kč (cents/100)
        'deposits_received_kc':  0,   # zálohy, co tatérovi přišly (po refundech)
        'balance_via_platform_kc': 0, # doplatky přes InkLink po sezení
        'platform_fees_kc':      0,   # provize InkLinku z toho všeho přes platformu
        'forfeited_deposits_kc': 0,   # propadlé zálohy z late storna (subset deposits)
        'onsite_cash_kc':        0,   # hotovost na místě z dokončených (mimo platformu)
        'refunded_to_clients_kc':0,   # refundy klientům
        'gross_revenue_kc':      0,   # celkový obrat (vše, po refundech, před provizí)
        'net_to_artist_kc':      0,   # co reálně tatérovi zůstane (gross − provize)
        'rows': [],
    }

    for r in rows:
        st     = r['status']
        dep_kc = (r['deposit_cents'] or 0) / 100
        ref_kc = (r['refund_cents'] or 0) / 100
        fee_kc = (r['platform_fee_cents'] or 0) / 100
        ons_kc = (r['onsite_amount_cents'] or 0) / 100
        bal_kc = (r['balance_paid_cents'] or 0) / 100

        is_completed = (st == 'completed')
        is_cancel_c  = (st == 'cancelled_client')
        is_cancel_a  = (st == 'cancelled_artist')
        is_no_show   = (st == 'no_show')
        is_confirmed = (st in ('confirmed', 'pending_payment'))

        # Záloha tatérovi (po refundu klientovi): co zůstalo z deposit_cents - refund
        artist_deposit_kc = max(0, dep_kc - ref_kc)
        # Provize InkLinku: poměrově k tomu, co tatérovi zůstalo
        # (pro jednoduchost bereme platform_fee_cents tak, jak je uložené)
        if is_cancel_a:
            # tatér zrušil → klient dostal vše zpět; platforma provize nebere
            artist_deposit_kc = 0
            fee_share_kc = 0
        elif is_cancel_c and ref_kc < dep_kc:
            # klient zrušil pozdě → záloha (částečně) propadá tatérovi
            forfeit = dep_kc - ref_kc
            summary['forfeited_deposits_kc'] += forfeit
            fee_share_kc = fee_kc * (forfeit / dep_kc) if dep_kc else 0
        else:
            fee_share_kc = fee_kc

        summary['deposits_received_kc']    += artist_deposit_kc
        summary['balance_via_platform_kc'] += bal_kc
        summary['platform_fees_kc']        += fee_share_kc
        summary['refunded_to_clients_kc']  += ref_kc
        summary['onsite_cash_kc']          += ons_kc if is_completed else 0
        summary['gross_revenue_kc']        += artist_deposit_kc + bal_kc + (ons_kc if is_completed else 0)
        summary['net_to_artist_kc']        += artist_deposit_kc + bal_kc - fee_share_kc + (ons_kc if is_completed else 0)

        if is_completed:           summary['count_completed']        += 1
        elif is_cancel_c:          summary['count_cancelled_client'] += 1
        elif is_cancel_a:          summary['count_cancelled_artist'] += 1
        elif is_no_show:           summary['count_no_show']          += 1
        elif is_confirmed:         summary['count_confirmed']        += 1

        summary['rows'].append({
            'when':      r['booking_start_at'] or r['slot_start'],
            'duration_h': r['duration_hours'],
            'client':    r['c_display_name'] or r['c_username'],
            'status':    st,
            'mode':      r['payment_mode'] or 'deposit',
            'deposit_kc': dep_kc,
            'balance_kc': bal_kc,
            'refund_kc':  ref_kc,
            'fee_kc':     fee_share_kc,
            'onsite_kc':  ons_kc,
            'artist_kc':  artist_deposit_kc + bal_kc - fee_share_kc + (ons_kc if is_completed else 0),
            'note':       (r['design_note'] or '')[:80],
        })

    # zaokrouhlit
    for k in ('deposits_received_kc','balance_via_platform_kc','platform_fees_kc',
              'forfeited_deposits_kc','onsite_cash_kc','refunded_to_clients_kc',
              'gross_revenue_kc','net_to_artist_kc'):
        summary[k] = round(summary[k])
    return summary


def _format_dt_cs(iso: str) -> str:
    try:
        d = datetime.fromisoformat(iso.replace('Z', '+00:00'))
        return d.strftime('%-d. %-m. %Y %H:%M')
    except Exception:
        return iso or ''


def _build_report_pdf(artist: dict, summary: dict) -> bytes:
    """Vygeneruje PDF report a vrátí bytes."""
    from io import BytesIO
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, PageBreak)

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18*mm, rightMargin=18*mm,
                            topMargin=20*mm, bottomMargin=18*mm,
                            title=f'InkLink report {summary["period"]}')

    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('h1', parent=styles['Title'], fontSize=22, leading=26,
                        textColor=colors.HexColor('#111'))
    h2 = ParagraphStyle('h2', parent=styles['Heading2'], fontSize=13, leading=16,
                        textColor=colors.HexColor('#444'))
    p_normal = ParagraphStyle('p', parent=styles['Normal'], fontSize=10, leading=13)
    p_small  = ParagraphStyle('ps', parent=styles['Normal'], fontSize=8, leading=11,
                              textColor=colors.HexColor('#777'))

    elements = []
    period_label = datetime(int(summary['period'][:4]), int(summary['period'][5:7]), 1).strftime('%B %Y')

    elements.append(Paragraph('INKLINK', ParagraphStyle('brand', parent=styles['Title'],
                              fontSize=14, textColor=colors.HexColor('#999'),
                              spaceAfter=2)))
    elements.append(Paragraph(f'Měsíční přehled · {period_label}', h1))
    elements.append(Spacer(1, 4*mm))
    elements.append(Paragraph(
        f"<b>{artist.get('display_name','')}</b> — {artist.get('studio') or 'samostatný tatér'}"
        f"{' · ' + artist['city'] if artist.get('city') else ''}",
        p_normal))
    elements.append(Spacer(1, 8*mm))

    # Souhrnná tabulka
    summary_data = [
        ['Položka', 'Kč'],
        ['Hrubý obrat (vše po refundech, před provizí)',   f"{summary['gross_revenue_kc']:,}".replace(',', ' ')],
        ['Z toho zálohy (přes InkLink)',                   f"{summary['deposits_received_kc']:,}".replace(',', ' ')],
        ['Z toho doplatky přes InkLink (online)',          f"{summary['balance_via_platform_kc']:,}".replace(',', ' ')],
        ['Z toho hotovost na místě (mimo InkLink)',        f"{summary['onsite_cash_kc']:,}".replace(',', ' ')],
        ['Provize InkLink (8 % z online plateb)',          f"-{summary['platform_fees_kc']:,}".replace(',', ' ')],
        ['Refundováno klientům',                           f"{summary['refunded_to_clients_kc']:,}".replace(',', ' ')],
        ['Propadlé zálohy (late storno)',                  f"{summary['forfeited_deposits_kc']:,}".replace(',', ' ')],
        ['Čistý příjem tatéra',                            f"{summary['net_to_artist_kc']:,}".replace(',', ' ')],
    ]
    t = Table(summary_data, colWidths=[110*mm, 50*mm])
    t.setStyle(TableStyle([
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#111')),
        ('TEXTCOLOR',  (0,0), (-1,0), colors.white),
        ('LINEBELOW', (0,0), (-1,0), 0.6, colors.HexColor('#111')),
        ('LINEBELOW', (0,-2), (-1,-2), 0.4, colors.HexColor('#999')),
        ('FONTNAME', (0,-1), (-1,-1), 'Helvetica-Bold'),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, colors.HexColor('#f7f7f7')]),
        ('LEFTPADDING', (0,0), (-1,-1), 8),
        ('RIGHTPADDING', (0,0), (-1,-1), 8),
        ('TOPPADDING', (0,0), (-1,-1), 6),
        ('BOTTOMPADDING', (0,0), (-1,-1), 6),
    ]))
    elements.append(t)

    elements.append(Spacer(1, 8*mm))

    # Statistika rezervací
    elements.append(Paragraph('Rezervace v období', h2))
    stats = [
        ['Celkem', summary['count_total']],
        ['Dokončeno', summary['count_completed']],
        ['Potvrzené (čekají)', summary['count_confirmed']],
        ['Zrušeno klientem', summary['count_cancelled_client']],
        ['Zrušeno tatérem', summary['count_cancelled_artist']],
        ['No-show', summary['count_no_show']],
    ]
    t2 = Table(stats, colWidths=[110*mm, 50*mm])
    t2.setStyle(TableStyle([
        ('FONTSIZE', (0,0), (-1,-1), 10),
        ('ALIGN', (1,0), (1,-1), 'RIGHT'),
        ('LINEBELOW', (0,0), (-1,-1), 0.2, colors.HexColor('#ddd')),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    elements.append(t2)
    elements.append(Spacer(1, 8*mm))

    # Detailní tabulka rezervací
    if summary['rows']:
        elements.append(Paragraph('Detail rezervací', h2))
        STATUS_LABEL = {
            'completed':       'dokončeno',
            'confirmed':       'potvrzeno',
            'pending_payment': 'čeká na platbu',
            'cancelled_client':'zruš. klientem',
            'cancelled_artist':'zruš. tatérem',
            'no_show':         'no-show',
        }
        head = ['Datum', 'Klient', 'Délka', 'Stav', 'Záloha', 'Doplatek', 'Refund', 'Provize', 'Onsite', 'Tatérovi']
        body = [head]
        for r in summary['rows']:
            body.append([
                _format_dt_cs(r['when']),
                (r['client'] or '')[:24],
                f"{r['duration_h']:.1f}h" if r['duration_h'] else '—',
                STATUS_LABEL.get(r['status'], r['status']),
                f"{r['deposit_kc']:,.0f}".replace(',', ' '),
                f"{r['balance_kc']:,.0f}".replace(',', ' ') if r['balance_kc'] else '—',
                f"{r['refund_kc']:,.0f}".replace(',', ' ') if r['refund_kc'] else '—',
                f"{r['fee_kc']:,.0f}".replace(',', ' ') if r['fee_kc'] else '—',
                f"{r['onsite_kc']:,.0f}".replace(',', ' ') if r['onsite_kc'] else '—',
                f"{r['artist_kc']:,.0f}".replace(',', ' '),
            ])
        t3 = Table(body, colWidths=[26*mm, 24*mm, 11*mm, 20*mm, 14*mm, 13*mm, 12*mm, 12*mm, 12*mm, 16*mm], repeatRows=1)
        t3.setStyle(TableStyle([
            ('FONTSIZE', (0,0), (-1,-1), 7.5),
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#eaeaea')),
            ('LINEBELOW', (0,0), (-1,0), 0.4, colors.HexColor('#999')),
            ('LINEBELOW', (0,1), (-1,-1), 0.2, colors.HexColor('#eee')),
            ('ALIGN', (4,1), (-1,-1), 'RIGHT'),
            ('VALIGN', (0,0), (-1,-1), 'TOP'),
            ('LEFTPADDING', (0,0), (-1,-1), 4),
            ('RIGHTPADDING', (0,0), (-1,-1), 4),
            ('TOPPADDING', (0,0), (-1,-1), 3),
            ('BOTTOMPADDING', (0,0), (-1,-1), 3),
        ]))
        elements.append(t3)
        elements.append(Spacer(1, 6*mm))

    elements.append(Paragraph(
        'Vygenerováno automaticky platformou InkLink. Částky v Kč zaokrouhlené na celé. '
        'Skutečné peníze chodí přímo tatérovi přes Stripe Connect (mínus provize 8 %), '
        'tento přehled je pro tvoji evidenci a daňové účely.',
        p_small
    ))

    doc.build(elements)
    buf.seek(0)
    return buf.read()


@app.route('/api/me/report/<int:year>/<int:month>')
def my_monthly_report(year, month):
    err = require_login()
    if err: return err
    if not (2025 <= year <= 2099 and 1 <= month <= 12):
        return jsonify({'error': 'Neplatný rok/měsíc'}), 400
    conn = get_db()
    artist = conn.execute('''SELECT id, username, display_name, studio, city
                             FROM users WHERE id=?''', (session['user_id'],)).fetchone()
    if not artist:
        conn.close(); return jsonify({'error': 'not found'}), 404
    summary = _aggregate_artist_month(conn, artist['id'], year, month)
    conn.close()

    fmt = (request.args.get('format') or 'pdf').lower()
    if fmt == 'json':
        return jsonify(summary)

    pdf_bytes = _build_report_pdf(dict(artist), summary)
    from flask import Response
    safe_name = (artist['username'] or 'tater').replace('/', '_')
    filename = f'inklink-report-{safe_name}-{year:04d}-{month:02d}.pdf'
    return Response(pdf_bytes, mimetype='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{filename}"',
    })


# ── Messages API ──────────────────────────────────────────────────────────────

@app.route('/api/messages/conversations')
def conversations():
    err = require_login()
    if err: return err

    conn = get_db()
    rows = conn.execute('''
        SELECT
            CASE WHEN m.sender_id = ? THEN m.receiver_id ELSE m.sender_id END AS other_id,
            u.username, u.display_name, u.avatar,
            m.content AS last_msg,
            m.created_at AS last_at,
            SUM(CASE WHEN m.read=0 AND m.receiver_id=? THEN 1 ELSE 0 END) AS unread
        FROM messages m
        JOIN users u ON u.id = CASE WHEN m.sender_id=? THEN m.receiver_id ELSE m.sender_id END
        WHERE m.sender_id=? OR m.receiver_id=?
        GROUP BY other_id
        ORDER BY last_at DESC
    ''', (session['user_id'], session['user_id'], session['user_id'], session['user_id'], session['user_id'])).fetchall()
    conn.close()

    return jsonify([{
        'user_id':      r['other_id'],
        'username':     r['username'],
        'display_name': r['display_name'],
        'avatar':       r['avatar'],
        'initials':     initials(r['display_name']),
        'last_msg':     r['last_msg'] if r['last_msg'] else '📷 Fotka',
        'last_at':      time_ago(r['last_at']),
        'unread':       r['unread'],
    } for r in rows])


@app.route('/api/messages/<int:other_id>')
def get_messages(other_id):
    err = require_login()
    if err: return err

    conn = get_db()
    # mark as read
    conn.execute('UPDATE messages SET read=1 WHERE sender_id=? AND receiver_id=?',
                 (other_id, session['user_id']))
    conn.commit()

    rows = conn.execute('''
        SELECT m.*, u.display_name, u.avatar
        FROM messages m JOIN users u ON u.id = m.sender_id
        WHERE (m.sender_id=? AND m.receiver_id=?) OR (m.sender_id=? AND m.receiver_id=?)
        ORDER BY m.created_at ASC
        LIMIT 100
    ''', (session['user_id'], other_id, other_id, session['user_id'])).fetchall()

    other = conn.execute('SELECT id, username, display_name, avatar FROM users WHERE id=?', (other_id,)).fetchone()
    conn.close()

    if not other:
        return jsonify({'error': 'User not found'}), 404

    return jsonify({
        'other': {
            'id':           other['id'],
            'username':     other['username'],
            'display_name': other['display_name'],
            'avatar':       other['avatar'],
            'initials':     initials(other['display_name']),
        },
        'messages': [{
            'id':           m['id'],
            'content':      m['content'],
            'content_type': m['content_type'] or 'text',
            'image':        m['image'] or '',
            'mine':         m['sender_id'] == session['user_id'],
            'created_at':   time_ago(m['created_at']),
        } for m in rows]
    })


@app.route('/api/messages/<int:other_id>', methods=['POST'])
@limiter.limit('60 per minute')
def send_message(other_id):
    err = require_login()
    if err: return err

    # image message
    if 'image' in request.files:
        img = request.files['image']
        ext = img.filename.rsplit('.', 1)[-1].lower() if img.filename else ''
        if ext not in ('jpg', 'jpeg', 'png', 'webp', 'gif'):
            return jsonify({'error': 'Unsupported image format'}), 400
        safe   = secure_filename(img.filename)
        unique = f"msg_{session['user_id']}_{int(time.time())}_{safe}"
        save_upload(img, unique)
        conn = get_db()
        conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type, image) VALUES (?, ?, ?, ?, ?)',
                     (session['user_id'], other_id, '', 'image', unique))
        conn.commit(); conn.close()
        return jsonify({'ok': True})

    data    = request.get_json(silent=True) or {}
    content = data.get('content', '').strip()
    if not content:
        return jsonify({'error': 'Message cannot be empty'}), 400
    if len(content) > 2000:
        return jsonify({'error': 'Message is too long'}), 400

    conn = get_db()
    conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type) VALUES (?, ?, ?, ?)',
                 (session['user_id'], other_id, content, 'text'))
    conn.commit()
    conn.close()

    return jsonify({'ok': True})


@app.route('/api/messages/unread')
def unread_count():
    if 'user_id' not in session:
        return jsonify({'count': 0})
    conn   = get_db()
    count  = conn.execute('SELECT COUNT(*) FROM messages WHERE receiver_id=? AND read=0',
                          (session['user_id'],)).fetchone()[0]
    conn.close()
    return jsonify({'count': count})


@app.route('/api/users/search')
def users_search():
    """Vyhledávání uživatelů pro start nové konverzace (artists i clients)."""
    err = require_login()
    if err: return err
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])
    like = f'%{q}%'
    conn = get_db()
    rows = conn.execute('''
        SELECT id, username, display_name, avatar, is_artist
        FROM users
        WHERE id != ?
          AND username IS NOT NULL
          AND (LOWER(username) LIKE LOWER(?) OR LOWER(display_name) LIKE LOWER(?))
        ORDER BY is_artist DESC, display_name ASC
        LIMIT 12
    ''', (session['user_id'], like, like)).fetchall()
    conn.close()
    def _initials(name):
        if not name: return '??'
        parts = [p for p in name.strip().split() if p]
        return ''.join(p[0] for p in parts[:2]).upper() or '??'
    return jsonify([{
        'id': r['id'],
        'username': r['username'],
        'display_name': r['display_name'] or r['username'],
        'avatar': r['avatar'] or '',
        'initials': _initials(r['display_name'] or r['username']),
        'is_artist': bool(r['is_artist']),
    } for r in rows])


# ── Favorite cities API ───────────────────────────────────────────────────────

@app.route('/api/favorites/cities')
def get_favorite_cities():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute('SELECT name, lat, lng FROM favorite_cities WHERE user_id = ? ORDER BY name',
                        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{'name': r['name'], 'lat': r['lat'], 'lng': r['lng']} for r in rows])


@app.route('/api/favorites/cities', methods=['POST'])
def add_favorite_city():
    err = require_login()
    if err: return err
    data = request.get_json()
    name = data.get('name', '').strip()
    try:
        lat = float(data.get('lat'))
        lng = float(data.get('lng'))
    except (TypeError, ValueError):
        return jsonify({'error': 'Invalid coordinates'}), 400
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    conn = get_db()
    try:
        conn.execute('INSERT OR IGNORE INTO favorite_cities (user_id, name, lat, lng) VALUES (?, ?, ?, ?)',
                     (session['user_id'], name, lat, lng))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/favorites/cities/<name>', methods=['DELETE'])
def remove_favorite_city(name):
    err = require_login()
    if err: return err
    conn = get_db()
    conn.execute('DELETE FROM favorite_cities WHERE user_id = ? AND name = ?',
                 (session['user_id'], name))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Events API ────────────────────────────────────────────────────────────────

@app.route('/api/events')
def get_events():
    uid   = session.get('user_id', 0)
    year  = int(request.args.get('year',  datetime.now().year))
    month = int(request.args.get('month', datetime.now().month))
    genre = request.args.get('genre', '').strip()

    city  = request.args.get('city', '').strip()
    try:
        flat    = float(request.args.get('lat', ''))
        flng    = float(request.args.get('lng', ''))
        fradius = float(request.args.get('radius', 50))
        gps_filter = True
    except (ValueError, TypeError):
        gps_filter = False

    month_str = f'{year}-{month:02d}'
    conn   = get_db()
    params = [f'{month_str}%']
    query  = '''
        SELECT e.*, u.username, u.display_name, u.emoji, u.lat AS user_lat, u.lng AS user_lng
        FROM events e
        JOIN users u ON e.user_id = u.id
        WHERE e.date LIKE ?
    '''
    if genre:
        query += ' AND e.genre LIKE ?'
        params.append(f'%{genre}%')
    if city and not gps_filter:
        query += ' AND e.city LIKE ?'
        params.append(f'%{city}%')
    query += ' ORDER BY e.date ASC, e.time ASC'

    rows = conn.execute(query, params).fetchall()
    saved_ids = {r['event_id'] for r in conn.execute(
        'SELECT event_id FROM event_saves WHERE user_id = ?', (uid,)
    ).fetchall()}
    conn.close()

    if gps_filter:
        rows = [r for r in rows if r['user_lat'] is not None and r['user_lng'] is not None
                and haversine(flat, flng, r['user_lat'], r['user_lng']) <= fradius]

    def photo_url(name):
        return f'/uploads/{name}' if name else ''

    return jsonify([{
        'id':          r['id'],
        'title':       r['title'],
        'date':        r['date'],
        'time':        r['time'],
        'venue':       r['venue'],
        'city':        r['city'],
        'genre':       r['genre'],
        'description': r['description'],
        'link':        r['link'],
        'lat':         r['lat'],
        'lng':         r['lng'],
        'photos':      [photo_url(r[f'photo{i}']) for i in range(1, 6) if r[f'photo{i}']],
        'is_own':      uid != 0 and r['user_id'] == uid,
        'is_saved':    r['id'] in saved_ids,
        'user': {
            'username':     r['username'],
            'display_name': r['display_name'],
            'emoji':        r['emoji'] or '',
            'initials':     initials(r['display_name']),
        }
    } for r in rows])


@app.route('/api/events', methods=['POST'])
def create_event():
    err = require_login()
    if err: return err

    title       = request.form.get('title', '').strip()
    date        = request.form.get('date', '').strip()
    time_str    = request.form.get('time', '').strip()
    venue       = request.form.get('venue', '').strip()
    city        = request.form.get('city', '').strip()
    genre       = request.form.get('genre', '').strip()
    description = request.form.get('description', '').strip()
    link        = request.form.get('link', '').strip()
    try: lat = float(request.form.get('lat', ''))
    except (TypeError, ValueError): lat = None
    try: lng = float(request.form.get('lng', ''))
    except (TypeError, ValueError): lng = None

    if not title or not date:
        return jsonify({'error': 'Title and date are required'}), 400
    if len(title) > 120:
        return jsonify({'error': 'Title is too long (max 120 characters)'}), 400
    if len(description) > 2000:
        return jsonify({'error': 'Description is too long (max 2000 characters)'}), 400
    if len(venue) > 120:
        return jsonify({'error': 'Venue name is too long'}), 400

    photos = []
    for i in range(1, 6):
        f = request.files.get(f'photo{i}')
        if f and f.filename and allowed_file(f.filename) and allowed_image(f):
            ext    = secure_filename(f.filename).rsplit('.', 1)[1].lower()
            name   = f'ev_{session["user_id"]}_{int(datetime.now().timestamp()*1000)}_{i}.{ext}'
            save_upload(f, name)
            photos.append(name)
        else:
            photos.append('')

    conn = get_db()
    conn.execute(
        'INSERT INTO events (user_id, title, date, time, venue, city, genre, description, link, lat, lng, photo1, photo2, photo3, photo4, photo5) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (session['user_id'], title, date, time_str, venue, city, genre, description, link, lat, lng, *photos)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/my-events')
def get_my_events():
    err = require_login()
    if err: return err
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    rows = conn.execute(
        'SELECT id, title, date, time, city, genre FROM events WHERE user_id = ? ORDER BY date ASC, time ASC',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify([{
        'id':    r['id'],
        'title': r['title'],
        'date':  r['date'],
        'time':  r['time'],
        'city':  r['city'],
        'genre': r['genre'],
        'past':  r['date'] < today,
    } for r in rows])


@app.route('/api/events/<int:event_id>/save', methods=['POST'])
def save_event(event_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    existing = conn.execute('SELECT 1 FROM event_saves WHERE user_id=? AND event_id=?',
                            (session['user_id'], event_id)).fetchone()
    if existing:
        conn.execute('DELETE FROM event_saves WHERE user_id=? AND event_id=?',
                     (session['user_id'], event_id))
        saved = False
    else:
        conn.execute('INSERT INTO event_saves (user_id, event_id) VALUES (?,?)',
                     (session['user_id'], event_id))
        saved = True
        ev = conn.execute('SELECT user_id, title FROM events WHERE id = ?', (event_id,)).fetchone()
        if ev:
            actor = conn.execute('SELECT display_name FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            push_notif(conn, ev['user_id'], session['user_id'], 'event_save', event_id, 'event',
                       f"{actor['display_name']} ulozil(a) tvou akci \"{ev['title']}\"")
    conn.commit(); conn.close()
    return jsonify({'saved': saved})


@app.route('/api/calendar')
def get_calendar():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    today = datetime.now().strftime('%Y-%m-%d')
    rows = conn.execute('''
        SELECT e.id, e.title, e.date, e.time, e.city, e.genre
        FROM event_saves es
        JOIN events e ON es.event_id = e.id
        WHERE es.user_id = ?
        ORDER BY e.date ASC, e.time ASC
    ''', (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'title': r['title'], 'date': r['date'],
        'time': r['time'], 'city': r['city'], 'genre': r['genre'],
        'past': r['date'] < today,
    } for r in rows])


@app.route('/api/profile/<username>/events')
def get_profile_events(username):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not user:
        conn.close(); return jsonify([])
    today = datetime.now().strftime('%Y-%m-%d')
    rows = conn.execute(
        'SELECT id, title, date, time, venue, city, genre, description, link, photo1 FROM events WHERE user_id = ? ORDER BY date ASC, time ASC',
        (user['id'],)
    ).fetchall()
    conn.close()
    def photo_url(name):
        return f'/uploads/{name}' if name else ''
    return jsonify([{
        'id': r['id'], 'title': r['title'], 'date': r['date'], 'time': r['time'],
        'venue': r['venue'], 'city': r['city'], 'genre': r['genre'],
        'description': r['description'], 'link': r['link'],
        'photo': photo_url(r['photo1']),
        'past': r['date'] < today,
    } for r in rows])


@app.route('/api/events/<int:event_id>', methods=['DELETE'])
def delete_event(event_id):
    err = require_login()
    if err: return err

    conn  = get_db()
    event = conn.execute('SELECT user_id FROM events WHERE id = ?', (event_id,)).fetchone()
    if not event:
        conn.close()
        return jsonify({'error': 'Event not found'}), 404
    if event['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Not authorized'}), 403

    conn.execute('DELETE FROM events WHERE id = ?', (event_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Playlists ────────────────────────────────────────────────────────────────


# ── Bazar ─────────────────────────────────────────────────────────────────────

CATEGORIES = ['Kytary & Baskytary','Bicí & Perkuse','Klávesy & Syntezátory',
               'DJ & Elektronika','Studiové vybavení','Zesilovače & Aparáty',
               'Efekty & Pedály','Sluchátka & Mikrofony','Vinylové desky','Ostatní']


SKILL_CATEGORIES = {
    'Sound & Production': ['Mixing', 'Mastering', 'Beatmaking', 'Music production', 'Audio editing & Cleanup', 'Sound design'],
    'Composition & Writing': ['Ghostwriting', 'Toplining', 'Arranging', 'Jingle & sample creation'],
    'Session musicians': ['Vocals (Lead / Backing)', 'Instrument recording', 'Voiceover / Spoken word', 'DJing'],
    'Visual & Promo': ['Cover Art', 'Music video production', 'Music photography', 'Merch design', 'Animation & Visualizers'],
    'Management & Live': ['Live Sound Engineer', 'Booking', 'PR & Social Media', 'Lighting design'],
}


# ── Analytics (PRO) ──────────────────────────────────────────────────────────


# ── PRO Subscription ─────────────────────────────────────────────────────────


# ── Payments ─────────────────────────────────────────────────────────────────

@app.route('/api/stripe/public-key')
def stripe_public_key():
    return jsonify({'key': STRIPE_PUBLIC_KEY})


# ── InkLink: Stripe Connect Express ──────────────────────────────────────────
#
# Tatéři dělají Connect onboarding přes Express účet. Po dokončení Stripe
# pošle account.updated webhook, kterým si zaktualizujeme charges_enabled
# / payouts_enabled. Bez těchto flagů tatér nemůže přijímat rezervace.

PLATFORM_COMMISSION_PCT = 8  # % z každé zálohy zůstává platformě


def _stripe_required():
    if not STRIPE_SECRET_KEY:
        return jsonify({'error': 'Stripe není nakonfigurovaný (chybí STRIPE_SECRET_KEY).'}), 503
    return None


def _origin():
    return request.headers.get('Origin') or request.host_url.rstrip('/')


@app.route('/api/artist/connect/onboard', methods=['POST'])
def connect_onboard():
    err = require_login()
    if err: return err
    err = _stripe_required()
    if err: return err

    conn = get_db()
    u = conn.execute('SELECT id, email, display_name, username, stripe_account_id FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()

    acct_id = u['stripe_account_id']
    try:
        if not acct_id:
            acct = stripe.Account.create(
                type='express',
                country='CZ',
                email=u['email'] or None,
                capabilities={
                    'card_payments': {'requested': True},
                    'transfers':     {'requested': True},
                },
                business_type='individual',
                business_profile={
                    'name': u['display_name'] or u['username'],
                    'product_description': 'Tetování — služby',
                    'mcc': '7299',  # personal services
                },
                metadata={'inklink_user_id': str(u['id']), 'inklink_username': u['username']},
            )
            acct_id = acct.id
            conn.execute('UPDATE users SET stripe_account_id=? WHERE id=?', (acct_id, u['id']))
            conn.commit()

        link = stripe.AccountLink.create(
            account=acct_id,
            refresh_url=f'{_origin()}/api/artist/connect/refresh',
            return_url=f'{_origin()}/api/artist/connect/return',
            type='account_onboarding',
        )
    except stripe.error.StripeError as e:
        conn.close()
        return jsonify({'error': f'Stripe: {str(e)}'}), 400
    conn.close()
    return jsonify({'url': link.url, 'account_id': acct_id})


@app.route('/api/artist/connect/refresh')
def connect_refresh():
    """Stripe redirectne sem, pokud AccountLink expiroval — vygeneruj nový a redirect zpět."""
    if 'user_id' not in session:
        return redirect('/login')
    if not STRIPE_SECRET_KEY:
        return redirect('/artist-setup?stripe=missing-key')
    conn = get_db()
    u = conn.execute('SELECT stripe_account_id FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    conn.close()
    if not u or not u['stripe_account_id']:
        return redirect('/artist-setup?stripe=no-account')
    try:
        link = stripe.AccountLink.create(
            account=u['stripe_account_id'],
            refresh_url=f'{_origin()}/api/artist/connect/refresh',
            return_url=f'{_origin()}/api/artist/connect/return',
            type='account_onboarding',
        )
        return redirect(link.url)
    except stripe.error.StripeError:
        return redirect('/artist-setup?stripe=error')


@app.route('/api/artist/connect/return')
def connect_return():
    """Stripe sem redirectne po dokončení onboardingu — sync stavu a redirect na setup."""
    if 'user_id' not in session:
        return redirect('/login')
    _sync_connect_status(session['user_id'])
    return redirect('/artist-setup?stripe=ok#payments')


@app.route('/api/me/connect-status')
def connect_status():
    """On-demand sync stavu Connect účtu (užitečné v dev, kdy webhook nepřijde)."""
    err = require_login()
    if err: return err
    info = _sync_connect_status(session['user_id'])
    return jsonify(info)


@app.route('/api/artist/connect/dashboard', methods=['POST'])
def connect_dashboard():
    """Vrátí jednorázový login link do Stripe Express dashboardu."""
    err = require_login()
    if err: return err
    err = _stripe_required()
    if err: return err
    conn = get_db()
    u = conn.execute('SELECT stripe_account_id FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    conn.close()
    if not u or not u['stripe_account_id']:
        return jsonify({'error': 'Stripe účet ještě nemáš.'}), 400
    try:
        link = stripe.Account.create_login_link(u['stripe_account_id'])
    except stripe.error.StripeError as e:
        return jsonify({'error': str(e)}), 400
    return jsonify({'url': link.url})


def _sync_connect_status(user_id: int) -> dict:
    """Načte stav účtu ze Stripe a uloží do users."""
    conn = get_db()
    u = conn.execute('SELECT stripe_account_id FROM users WHERE id=?', (user_id,)).fetchone()
    if not u or not u['stripe_account_id'] or not STRIPE_SECRET_KEY:
        conn.close()
        return {'connected': False}
    try:
        acct = stripe.Account.retrieve(u['stripe_account_id'])
        charges  = 1 if acct.charges_enabled else 0
        payouts  = 1 if acct.payouts_enabled else 0
        details  = 1 if acct.details_submitted else 0
        conn.execute('''UPDATE users SET stripe_charges_enabled=?, stripe_payouts_enabled=?,
                                          stripe_details_submitted=?
                        WHERE id=?''',
                     (charges, payouts, details, user_id))
        conn.commit()
        conn.close()
        return {
            'connected': True,
            'account_id': acct.id,
            'charges_enabled': bool(charges),
            'payouts_enabled': bool(payouts),
            'details_submitted': bool(details),
            'requirements': {
                'currently_due':  list(getattr(acct.requirements, 'currently_due', []) or []),
                'past_due':       list(getattr(acct.requirements, 'past_due', []) or []),
                'eventually_due': list(getattr(acct.requirements, 'eventually_due', []) or []),
            },
        }
    except stripe.error.StripeError as e:
        conn.close()
        return {'connected': True, 'error': str(e)}

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Webhook handler — především account.updated z Connect účtů.
    M3 přidá payment_intent.succeeded / charge.refunded pro bookings."""
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    try:
        if STRIPE_WEBHOOK_SECRET:
            event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
        else:
            # dev fallback: parsuj bez podpisu (NIKDY v produkci)
            import json as _json
            event = _json.loads(payload.decode('utf-8'))
    except Exception:
        return '', 400

    etype = event['type'] if isinstance(event, dict) else event.type
    obj   = event['data']['object'] if isinstance(event, dict) else event.data.object

    if etype == 'account.updated':
        acct_id = obj['id'] if isinstance(obj, dict) else obj.id
        charges = 1 if (obj.get('charges_enabled') if isinstance(obj, dict) else obj.charges_enabled) else 0
        payouts = 1 if (obj.get('payouts_enabled') if isinstance(obj, dict) else obj.payouts_enabled) else 0
        details = 1 if (obj.get('details_submitted') if isinstance(obj, dict) else obj.details_submitted) else 0
        conn = get_db()
        conn.execute('''UPDATE users SET stripe_charges_enabled=?, stripe_payouts_enabled=?,
                                          stripe_details_submitted=?,
                                          verified_artist_at = CASE WHEN ?=1 AND verified_artist_at IS NULL
                                                                    THEN ? ELSE verified_artist_at END
                        WHERE stripe_account_id=?''',
                     (charges, payouts, details, charges, datetime.utcnow().isoformat(), acct_id))
        conn.commit()
        conn.close()

    elif etype == 'account.application.deauthorized':
        acct_id = obj['id'] if isinstance(obj, dict) else obj.id
        conn = get_db()
        conn.execute('''UPDATE users SET stripe_account_id=NULL, stripe_charges_enabled=0,
                                          stripe_payouts_enabled=0, stripe_details_submitted=0
                        WHERE stripe_account_id=?''', (acct_id,))
        conn.commit()
        conn.close()

    # M3 hooks (payment_intent.succeeded, charge.refunded) přijdou s booking flow.

    return '', 200

@app.route('/payment/success')
def payment_success():
    return send_from_directory('public', 'payment_success.html')


# ── Tickets (QR) ──────────────────────────────────────────────────────────────


# ── InkLink: Studios (team / crew) ───────────────────────────────────────────
# Studio je brand + org slupka pro tatéry. Stripe, payouts a rezervace zůstávají
# per-artist; studio jen seskupuje portfolia a má veřejnou stránku.
# Jeden tatér = jeden studio (MVP). Admin invituje přes e-mail s tokenem.

STUDIO_INVITE_TTL_DAYS = 7
STUDIO_MAX_MEMBERS = 50  # soft cap


def _studio_row_dict(row):
    """sqlite3.Row → dict s parsovaným photos JSON."""
    import json as _json
    if not row:
        return None
    d = dict(row)
    try:
        d['photos'] = _json.loads(d.get('photos') or '[]')
    except Exception:
        d['photos'] = []
    return d


def _get_my_studio_membership(conn, user_id):
    """Vrátí (studio_row_dict, role) nebo (None, None)."""
    m = conn.execute(
        'SELECT studio_id, role FROM studio_members WHERE artist_id=?',
        (user_id,)
    ).fetchone()
    if not m:
        return (None, None)
    s = conn.execute('SELECT * FROM studios WHERE id=?', (m['studio_id'],)).fetchone()
    return (_studio_row_dict(s), m['role'])


def _studio_admin_check(conn, user_id, studio_id):
    """True pokud user je admin daného studia."""
    m = conn.execute(
        "SELECT 1 FROM studio_members WHERE artist_id=? AND studio_id=? AND role='admin'",
        (user_id, studio_id)
    ).fetchone()
    return bool(m)


def _studio_members_list(conn, studio_id):
    """Seznam členů studia s detaily pro public profile."""
    rows = conn.execute('''
        SELECT u.id, u.username, u.display_name, u.avatar, u.artist_slug,
               u.city, u.styles, u.instagram, sm.role, sm.joined_at,
               (SELECT AVG(rating) FROM reviews WHERE artist_id=u.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id=u.id) AS rating_count
        FROM studio_members sm
        JOIN users u ON u.id = sm.artist_id
        WHERE sm.studio_id = ?
        ORDER BY (sm.role='admin') DESC, sm.joined_at ASC
    ''', (studio_id,)).fetchall()
    return [dict(r) for r in rows]


@app.route('/api/studios', methods=['POST'])
@limiter.limit('5 per hour')
def create_studio():
    """Vytvoří studio; current logged-in artist se stane adminem.
    Body: name, slug? (jinak generujeme), city, description, address, instagram, website."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name or len(name) < 2:
        return jsonify({'error': 'Name is required'}), 400

    conn = get_db()
    user_id = session['user_id']

    # Tatér už nemůže být ve dvou studiích zároveň
    existing = conn.execute(
        'SELECT studio_id FROM studio_members WHERE artist_id=?', (user_id,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'You are already in a studio. Leave it first.'}), 409

    # Auto-promote na artista, pokud ještě není
    u = conn.execute('SELECT is_artist, artist_slug FROM users WHERE id=?', (user_id,)).fetchone()
    if not u['is_artist']:
        # Studio může založit jen tatér — jemně vyzveme uživatele
        conn.close()
        return jsonify({'error': 'Only artists can create a studio. Become an artist first.'}), 403

    # Slug
    base = _slugify(data.get('slug') or name)
    slug = base
    n = 1
    while conn.execute('SELECT 1 FROM studios WHERE slug=?', (slug,)).fetchone():
        n += 1
        slug = f'{base}-{n}'

    cur = conn.execute('''
        INSERT INTO studios (slug, name, description, address, city, country,
                             instagram, website, logo, photos, phone, email)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
    ''', (
        slug, name,
        (data.get('description') or '').strip()[:2000],
        (data.get('address') or '').strip()[:200],
        (data.get('city') or '').strip()[:80],
        (data.get('country') or '').strip()[:80],
        (data.get('instagram') or '').strip()[:80],
        (data.get('website') or '').strip()[:200],
        (data.get('logo') or '').strip()[:300],
        (data.get('phone') or '').strip()[:40],
        (data.get('email') or '').strip()[:120],
    ))
    studio_id = cur.lastrowid

    conn.execute(
        "INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?, ?, 'admin')",
        (studio_id, user_id)
    )
    conn.commit()
    s = conn.execute('SELECT * FROM studios WHERE id=?', (studio_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'studio': _studio_row_dict(s)})


@app.route('/api/studios/<slug>', methods=['GET'])
def get_studio(slug):
    """Veřejné info o studiu + seznam členů."""
    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404
    members = _studio_members_list(conn, s['id'])
    conn.close()
    out = _studio_row_dict(s)
    out['members'] = members
    return jsonify(out)


@app.route('/api/studios/<slug>', methods=['PATCH'])
@limiter.limit('30 per hour')
def update_studio(slug):
    """Admin edituje studio profil."""
    err = require_login()
    if err: return err
    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404
    if not _studio_admin_check(conn, session['user_id'], s['id']):
        conn.close()
        return jsonify({'error': 'Admin only'}), 403

    data = request.get_json(silent=True) or {}
    fields = {
        'name':        (data.get('name'),        80),
        'description': (data.get('description'), 2000),
        'address':     (data.get('address'),     200),
        'city':        (data.get('city'),        80),
        'country':     (data.get('country'),     80),
        'instagram':   (data.get('instagram'),   80),
        'website':     (data.get('website'),     200),
        'logo':        (data.get('logo'),        300),
        'phone':       (data.get('phone'),       40),
        'email':       (data.get('email'),       120),
    }
    sets = []
    vals = []
    for col, (val, maxlen) in fields.items():
        if val is None:
            continue
        sets.append(f'{col}=?')
        vals.append(str(val).strip()[:maxlen])

    # photos: list of URLs
    if isinstance(data.get('photos'), list):
        import json as _json
        photos = [str(p).strip()[:300] for p in data['photos'] if p][:12]
        sets.append('photos=?')
        vals.append(_json.dumps(photos))

    if sets:
        vals.append(s['id'])
        conn.execute(f'UPDATE studios SET {", ".join(sets)} WHERE id=?', vals)
        conn.commit()
    s = conn.execute('SELECT * FROM studios WHERE id=?', (s['id'],)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'studio': _studio_row_dict(s)})


@app.route('/api/studios/<slug>/invite', methods=['POST'])
@limiter.limit('20 per hour')
def invite_to_studio(slug):
    """Admin pošle e-mailovou pozvánku."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    email = (data.get('email') or '').strip().lower()
    if not email or '@' not in email or len(email) > 200:
        return jsonify({'error': 'Valid email required'}), 400

    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404
    if not _studio_admin_check(conn, session['user_id'], s['id']):
        conn.close()
        return jsonify({'error': 'Admin only'}), 403

    # Soft cap
    count = conn.execute('SELECT COUNT(*) AS c FROM studio_members WHERE studio_id=?', (s['id'],)).fetchone()['c']
    if count >= STUDIO_MAX_MEMBERS:
        conn.close()
        return jsonify({'error': f'Member limit reached ({STUDIO_MAX_MEMBERS})'}), 409

    # Pokud má účet a už je členem
    existing_user = conn.execute('SELECT id FROM users WHERE LOWER(email)=?', (email,)).fetchone()
    if existing_user:
        already = conn.execute(
            'SELECT studio_id FROM studio_members WHERE artist_id=?',
            (existing_user['id'],)
        ).fetchone()
        if already and already['studio_id'] == s['id']:
            conn.close()
            return jsonify({'error': 'Already a member'}), 409
        if already:
            conn.close()
            return jsonify({'error': 'User is already in another studio'}), 409

    # Smaž starou pending invite pro stejný email + studio
    conn.execute('''
        DELETE FROM studio_invites
        WHERE studio_id=? AND LOWER(email)=? AND accepted_at IS NULL AND declined_at IS NULL
    ''', (s['id'], email))

    import secrets as _secrets
    token = _secrets.token_urlsafe(24)
    expires_at = (datetime.utcnow() + timedelta(days=STUDIO_INVITE_TTL_DAYS)).isoformat(timespec='seconds')

    conn.execute('''
        INSERT INTO studio_invites (studio_id, email, token, invited_by, expires_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (s['id'], email, token, session['user_id'], expires_at))
    conn.commit()

    # Pošli email
    site_url = request.host_url.rstrip('/')
    invite_url = f'{site_url}/invite/{token}'
    inviter = conn.execute(
        'SELECT display_name, username FROM users WHERE id=?', (session['user_id'],)
    ).fetchone()
    inviter_name = html_escape((inviter['display_name'] if inviter else '') or (inviter['username'] if inviter else ''))
    studio_name = html_escape(s['name'])

    html_body = f'''
    <div style="background:#0a0a0a;padding:48px 24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e8e8e8;">
      <div style="max-width:480px;margin:0 auto;background:#111;border:1px solid #222;border-radius:14px;padding:36px;">
        <div style="font-size:11px;letter-spacing:0.2em;color:#888;margin-bottom:24px;">INKLINK</div>
        <h1 style="font-size:24px;font-weight:600;line-height:1.3;margin:0 0 16px;color:#fff;">
          Pozvánka do studia {studio_name}
        </h1>
        <p style="font-size:15px;line-height:1.6;color:#bbb;margin:0 0 28px;">
          {inviter_name} tě zve, ať se přidáš do tetovacího studia
          <strong style="color:#fff;">{studio_name}</strong> na InkLinku.
        </p>
        <p style="font-size:14px;line-height:1.6;color:#999;margin:0 0 28px;">
          Pokud pozvánku přijmeš, budeš v profilu studia uveden jako člen
          a tvé portfolio se zobrazí na jeho veřejné stránce. Tvé platby
          a Stripe účet zůstávají tvé — nic se nesdílí.
        </p>
        <a href="{invite_url}" style="display:inline-block;background:#fff;color:#000;text-decoration:none;padding:14px 28px;border-radius:8px;font-weight:600;font-size:14px;letter-spacing:0.04em;">
          Přijmout pozvánku
        </a>
        <p style="font-size:12px;color:#666;margin:28px 0 0;line-height:1.5;">
          Odkaz vyprší za {STUDIO_INVITE_TTL_DAYS} dní. Pokud jsi tuhle pozvánku
          nečekal(a), prostě ji ignoruj.
        </p>
      </div>
    </div>
    '''
    try:
        send_email(email, f'Pozvánka do studia {s["name"]} — InkLink', html_body)
    except Exception as e:
        print(f'[STUDIO INVITE] email send failed: {e}')

    conn.close()
    return jsonify({'ok': True, 'invite_url': invite_url})


@app.route('/api/studios/invites/<token>', methods=['GET'])
def view_studio_invite(token):
    """Veřejný preview pozvánky — pro /invite/<token> landing."""
    conn = get_db()
    inv = conn.execute('SELECT * FROM studio_invites WHERE token=?', (token,)).fetchone()
    if not inv:
        conn.close()
        return jsonify({'error': 'Invite not found'}), 404
    if inv['accepted_at']:
        conn.close()
        return jsonify({'error': 'Invite already accepted', 'status': 'accepted'}), 410
    if inv['declined_at']:
        conn.close()
        return jsonify({'error': 'Invite was declined', 'status': 'declined'}), 410
    try:
        if datetime.fromisoformat(inv['expires_at']) < datetime.utcnow():
            conn.close()
            return jsonify({'error': 'Invite expired', 'status': 'expired'}), 410
    except Exception:
        pass
    s = conn.execute('SELECT * FROM studios WHERE id=?', (inv['studio_id'],)).fetchone()
    inviter = conn.execute(
        'SELECT display_name, username FROM users WHERE id=?', (inv['invited_by'],)
    ).fetchone()
    conn.close()
    return jsonify({
        'studio':       _studio_row_dict(s),
        'email':        inv['email'],
        'inviter_name': (inviter['display_name'] if inviter else '') or (inviter['username'] if inviter else ''),
        'expires_at':   inv['expires_at'],
    })


@app.route('/api/studios/invites/<token>/accept', methods=['POST'])
@limiter.limit('10 per hour')
def accept_studio_invite(token):
    err = require_login()
    if err: return err
    conn = get_db()
    inv = conn.execute('SELECT * FROM studio_invites WHERE token=?', (token,)).fetchone()
    if not inv:
        conn.close()
        return jsonify({'error': 'Invite not found'}), 404
    if inv['accepted_at'] or inv['declined_at']:
        conn.close()
        return jsonify({'error': 'Invite no longer active'}), 410
    try:
        if datetime.fromisoformat(inv['expires_at']) < datetime.utcnow():
            conn.close()
            return jsonify({'error': 'Invite expired'}), 410
    except Exception:
        pass

    user_id = session['user_id']
    u = conn.execute('SELECT is_artist, email FROM users WHERE id=?', (user_id,)).fetchone()
    if not u or not u['is_artist']:
        conn.close()
        return jsonify({'error': 'Only artists can join a studio'}), 403

    # Existing membership?
    existing = conn.execute('SELECT studio_id FROM studio_members WHERE artist_id=?', (user_id,)).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'You are already in a studio. Leave it first.'}), 409

    # Cap
    count = conn.execute(
        'SELECT COUNT(*) AS c FROM studio_members WHERE studio_id=?', (inv['studio_id'],)
    ).fetchone()['c']
    if count >= STUDIO_MAX_MEMBERS:
        conn.close()
        return jsonify({'error': 'Studio is full'}), 409

    conn.execute(
        "INSERT INTO studio_members (studio_id, artist_id, role) VALUES (?, ?, 'member')",
        (inv['studio_id'], user_id)
    )
    conn.execute(
        'UPDATE studio_invites SET accepted_at=? WHERE id=?',
        (datetime.utcnow().isoformat(timespec='seconds'), inv['id'])
    )
    conn.commit()
    s = conn.execute('SELECT slug FROM studios WHERE id=?', (inv['studio_id'],)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'slug': s['slug']})


@app.route('/api/studios/invites/<token>/decline', methods=['POST'])
def decline_studio_invite(token):
    conn = get_db()
    inv = conn.execute('SELECT * FROM studio_invites WHERE token=?', (token,)).fetchone()
    if not inv:
        conn.close()
        return jsonify({'error': 'Invite not found'}), 404
    if inv['accepted_at'] or inv['declined_at']:
        conn.close()
        return jsonify({'ok': True})  # idempotent
    conn.execute(
        'UPDATE studio_invites SET declined_at=? WHERE id=?',
        (datetime.utcnow().isoformat(timespec='seconds'), inv['id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/studios/<slug>/members/<int:artist_id>', methods=['DELETE'])
def remove_studio_member(slug, artist_id):
    """Admin odebere člena, nebo člen sám sebe (self-leave).
    Admin nemůže odebrat sám sebe, dokud převede admin práva."""
    err = require_login()
    if err: return err
    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404

    user_id = session['user_id']
    is_admin = _studio_admin_check(conn, user_id, s['id'])
    is_self = (artist_id == user_id)
    if not is_admin and not is_self:
        conn.close()
        return jsonify({'error': 'Not allowed'}), 403

    target = conn.execute(
        'SELECT role FROM studio_members WHERE studio_id=? AND artist_id=?',
        (s['id'], artist_id)
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({'error': 'Not a member'}), 404

    # Admin si nemůže odejít, pokud je jediný admin a studio má ostatní členy
    if target['role'] == 'admin':
        other_admins = conn.execute(
            "SELECT COUNT(*) AS c FROM studio_members WHERE studio_id=? AND role='admin' AND artist_id!=?",
            (s['id'], artist_id)
        ).fetchone()['c']
        total = conn.execute(
            'SELECT COUNT(*) AS c FROM studio_members WHERE studio_id=?',
            (s['id'],)
        ).fetchone()['c']
        if other_admins == 0 and total > 1:
            conn.close()
            return jsonify({'error': 'Transfer admin role first'}), 409

    conn.execute(
        'DELETE FROM studio_members WHERE studio_id=? AND artist_id=?',
        (s['id'], artist_id)
    )
    # Pokud byl poslední, smaž celé studio
    rest = conn.execute(
        'SELECT COUNT(*) AS c FROM studio_members WHERE studio_id=?', (s['id'],)
    ).fetchone()['c']
    if rest == 0:
        conn.execute('DELETE FROM studios WHERE id=?', (s['id'],))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'deleted_studio': rest == 0})


@app.route('/api/studios/<slug>/transfer-admin', methods=['POST'])
def transfer_studio_admin(slug):
    """Current admin předá admin práva jinému členovi."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    target_id = data.get('artist_id')
    if not target_id:
        return jsonify({'error': 'artist_id required'}), 400

    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404
    if not _studio_admin_check(conn, session['user_id'], s['id']):
        conn.close()
        return jsonify({'error': 'Admin only'}), 403
    target = conn.execute(
        'SELECT 1 FROM studio_members WHERE studio_id=? AND artist_id=?',
        (s['id'], target_id)
    ).fetchone()
    if not target:
        conn.close()
        return jsonify({'error': 'Target is not a member'}), 404

    conn.execute("UPDATE studio_members SET role='member' WHERE studio_id=? AND artist_id=?",
                 (s['id'], session['user_id']))
    conn.execute("UPDATE studio_members SET role='admin' WHERE studio_id=? AND artist_id=?",
                 (s['id'], target_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/me/studio', methods=['GET'])
def me_studio():
    """Context pro logged-in tatéra: jeho studio (pokud má) + role."""
    err = require_login()
    if err: return err
    conn = get_db()
    studio, role = _get_my_studio_membership(conn, session['user_id'])
    if studio:
        studio['members'] = _studio_members_list(conn, studio['id'])
        # Pending invites pro adminy
        if role == 'admin':
            invites = conn.execute('''
                SELECT id, email, created_at, expires_at, token
                FROM studio_invites
                WHERE studio_id=? AND accepted_at IS NULL AND declined_at IS NULL
                ORDER BY created_at DESC
            ''', (studio['id'],)).fetchall()
            studio['pending_invites'] = [dict(i) for i in invites]
    conn.close()
    return jsonify({'studio': studio, 'role': role})


@app.route('/api/studios/<slug>/invites/<int:invite_id>', methods=['DELETE'])
def cancel_studio_invite(slug, invite_id):
    """Admin zruší pending pozvánku."""
    err = require_login()
    if err: return err
    conn = get_db()
    s = conn.execute('SELECT * FROM studios WHERE slug=?', (slug,)).fetchone()
    if not s:
        conn.close()
        return jsonify({'error': 'Studio not found'}), 404
    if not _studio_admin_check(conn, session['user_id'], s['id']):
        conn.close()
        return jsonify({'error': 'Admin only'}), 403
    conn.execute(
        'DELETE FROM studio_invites WHERE id=? AND studio_id=? AND accepted_at IS NULL',
        (invite_id, s['id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# Static page routes
@app.route('/studio-create')
def studio_create_page():
    return send_from_directory('public', 'studio-create.html')

@app.route('/studio-admin')
def studio_admin_page():
    return send_from_directory('public', 'studio-admin.html')

@app.route('/studio/<slug>')
def studio_public_page(slug):
    return send_from_directory('public', 'studio.html')

@app.route('/invite/<token>')
def studio_invite_page(token):
    return send_from_directory('public', 'invite.html')


@app.errorhandler(404)
def not_found(e):
    return send_from_directory('public', '404.html'), 404

@app.route('/api/native/register-push', methods=['POST'])
@limiter.limit('60 per hour')
def register_native_push():
    """Capacitor app (iOS APNs nebo Android FCM) registruje push token.
    Schema validace: provider ∈ {apns, fcm}, platform ∈ {ios, android}."""
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    provider = (data.get('provider') or '').strip().lower()
    platform = (data.get('platform') or '').strip().lower()
    if not token or len(token) > 500:
        return jsonify({'error': 'Invalid token'}), 400
    if provider not in ('apns', 'fcm'):
        return jsonify({'error': 'Invalid provider'}), 400
    if platform not in ('ios', 'android'):
        return jsonify({'error': 'Invalid platform'}), 400

    conn = get_db()
    # Upsert — jeden token → jeden user (poslední přihlášený vlastní token)
    try:
        # Postgres
        conn.execute('''
            INSERT INTO native_push_tokens (user_id, token, provider, platform)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (token) DO UPDATE SET
              user_id = EXCLUDED.user_id,
              provider = EXCLUDED.provider,
              platform = EXCLUDED.platform,
              last_seen_at = CURRENT_TIMESTAMP
        ''', (session['user_id'], token, provider, platform))
    except sqlite3.OperationalError:
        # SQLite fallback
        conn.execute('''
            INSERT OR REPLACE INTO native_push_tokens
              (user_id, token, provider, platform, last_seen_at)
            VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ''', (session['user_id'], token, provider, platform))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/__health')
def __health():
    """Health/version endpoint — pro ověření že Railway nasadil čerstvou verzi.
    curl https://www.inklink.club/__health"""
    return jsonify({
        'ok': True,
        'commit': os.environ.get('RAILWAY_GIT_COMMIT_SHA', 'unknown'),
        'commit_short': (os.environ.get('RAILWAY_GIT_COMMIT_SHA', 'unknown') or 'unknown')[:7],
        'branch': os.environ.get('RAILWAY_GIT_BRANCH', 'unknown'),
        'build_marker': 'paper-mode-wave-3',
    })


@app.errorhandler(429)
def rate_limited(e):
    return jsonify({'error': 'Too many requests — please try again later'}), 429

# Inicializace DB při startu (funguje i pro gunicorn)
init_db()

# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5002))
    print(f'\n◉  InkLink běží na  http://localhost:{port}\n')
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'
    app.run(debug=debug, port=port)
