from flask import Flask, request, jsonify, session, send_from_directory, redirect
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
import uuid
import random
import resend
from datetime import datetime, timedelta
import stripe
import boto3
from botocore.client import Config

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

# Session cookie security
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_SECURE']   = os.environ.get('HTTPS', '') == '1'

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


def send_email(to, subject, html_body):
    if not RESEND_API_KEY:
        print(f'[EMAIL] {to} | {subject} | (RESEND_API_KEY not set — kód jen v terminálu)')
        return
    resend.api_key = RESEND_API_KEY
    try:
        resend.Emails.send({'from': 'InkLink <onboarding@resend.dev>', 'to': to, 'subject': subject, 'html': html_body})
    except Exception as e:
        print(f'[EMAIL ERROR] {e}')
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

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        username      TEXT UNIQUE NOT NULL,
        display_name  TEXT NOT NULL,
        city          TEXT DEFAULT '',
        genres        TEXT DEFAULT '',
        bio           TEXT DEFAULT '',
        avatar        TEXT DEFAULT '',
        photo1        TEXT DEFAULT '',
        photo2        TEXT DEFAULT '',
        photo3        TEXT DEFAULT '',
        photo4        TEXT DEFAULT '',
        emoji         TEXT DEFAULT '',
        password_hash TEXT NOT NULL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # migrate existing users table
    for col in ('bio TEXT DEFAULT ""', 'avatar TEXT DEFAULT ""',
                'photo1 TEXT DEFAULT ""', 'photo2 TEXT DEFAULT ""',
                'photo3 TEXT DEFAULT ""', 'photo4 TEXT DEFAULT ""',
                'emoji TEXT DEFAULT ""',
                'lat  REAL DEFAULT NULL',
                'lng  REAL DEFAULT NULL',
                'email TEXT DEFAULT ""',
                'phone TEXT DEFAULT ""',
                'verified INTEGER DEFAULT 1',
                'verify_code TEXT DEFAULT NULL',
                'verify_expires TEXT DEFAULT NULL',
                'pro INTEGER DEFAULT 0',
                'pro_expires TEXT DEFAULT NULL',
                'stripe_customer_id TEXT DEFAULT NULL'):
        add_col('users', col)
    conn.commit()

    c.execute('''CREATE TABLE IF NOT EXISTS tracks (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        title      TEXT NOT NULL,
        genre      TEXT DEFAULT '',
        city       TEXT DEFAULT '',
        filename   TEXT NOT NULL,
        cover      TEXT DEFAULT '',
        duration   TEXT DEFAULT '',
        caption    TEXT DEFAULT '',
        like_count INTEGER DEFAULT 0,
        play_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    for col in ('cover TEXT DEFAULT ""', 'video TEXT DEFAULT ""'):
        add_col('tracks', col)
    conn.commit()

    c.execute('''CREATE TABLE IF NOT EXISTS play_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id   INTEGER NOT NULL,
        user_id    INTEGER DEFAULT NULL,
        city       TEXT    DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS likes (
        user_id  INTEGER NOT NULL,
        track_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, track_id)
    )''')
    add_col('likes', 'created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP')
    conn.commit()

    c.execute('''CREATE TABLE IF NOT EXISTS follows (
        follower_id  INTEGER NOT NULL,
        following_id INTEGER NOT NULL,
        PRIMARY KEY (follower_id, following_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS reposts (
        user_id    INTEGER NOT NULL,
        track_id   INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, track_id)
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
    for col in ('content_type TEXT DEFAULT "text"', 'image TEXT DEFAULT ""'):
        add_col('messages', col)
    conn.commit()

    c.execute('''CREATE TABLE IF NOT EXISTS favorite_cities (
        user_id  INTEGER NOT NULL,
        name     TEXT NOT NULL,
        lat      REAL NOT NULL,
        lng      REAL NOT NULL,
        PRIMARY KEY (user_id, name),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS playlists (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        name       TEXT NOT NULL,
        is_public  INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS playlist_tracks (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        playlist_id INTEGER NOT NULL,
        track_id    INTEGER NOT NULL,
        position    INTEGER DEFAULT 0,
        added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(playlist_id, track_id),
        FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE,
        FOREIGN KEY (track_id)    REFERENCES tracks(id)    ON DELETE CASCADE
    )''')

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
    for col in ('lat REAL DEFAULT NULL', 'lng REAL DEFAULT NULL',
                'photo1 TEXT DEFAULT ""', 'photo2 TEXT DEFAULT ""',
                'photo3 TEXT DEFAULT ""', 'photo4 TEXT DEFAULT ""',
                'photo5 TEXT DEFAULT ""'):
        add_col('events', col)
    conn.commit()

    c.execute('''CREATE TABLE IF NOT EXISTS listings (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        title       TEXT NOT NULL,
        description TEXT DEFAULT '',
        price       INTEGER NOT NULL,
        currency    TEXT DEFAULT 'CZK',
        condition   TEXT DEFAULT 'used',
        category    TEXT DEFAULT '',
        city        TEXT DEFAULT '',
        status      TEXT DEFAULT 'active',
        boosted     INTEGER DEFAULT 0,
        photo1      TEXT DEFAULT '',
        photo2      TEXT DEFAULT '',
        photo3      TEXT DEFAULT '',
        photo4      TEXT DEFAULT '',
        photo5      TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS listing_likes (
        user_id    INTEGER NOT NULL,
        listing_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, listing_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS event_saves (
        user_id  INTEGER NOT NULL,
        event_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, event_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS skill_listings (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        title        TEXT NOT NULL,
        description  TEXT DEFAULT '',
        category     TEXT NOT NULL,
        subcategory  TEXT DEFAULT '',
        price_from   INTEGER DEFAULT 0,
        price_to     INTEGER DEFAULT 0,
        currency     TEXT DEFAULT 'CZK',
        delivery_days INTEGER DEFAULT 7,
        city         TEXT DEFAULT '',
        remote       INTEGER DEFAULT 1,
        status       TEXT DEFAULT 'active',
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS skill_likes (
        user_id  INTEGER NOT NULL,
        skill_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, skill_id)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS comments (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        track_id   INTEGER NOT NULL,
        user_id    INTEGER NOT NULL,
        text       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

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

    c.execute('''CREATE TABLE IF NOT EXISTS news (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        text       TEXT    NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS ticket_types (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id   INTEGER NOT NULL,
        name       TEXT    NOT NULL,
        price      INTEGER NOT NULL,
        currency   TEXT    DEFAULT 'CZK',
        capacity   INTEGER DEFAULT 0,
        sold       INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS orders (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id             INTEGER NOT NULL,
        item_type           TEXT    NOT NULL,
        item_id             INTEGER NOT NULL,
        stripe_session_id   TEXT,
        amount              INTEGER NOT NULL,
        platform_fee        INTEGER NOT NULL DEFAULT 0,
        currency            TEXT    DEFAULT 'CZK',
        status              TEXT    DEFAULT 'pending',
        ticket_code         TEXT,
        created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    add_col('orders', 'platform_fee INTEGER NOT NULL DEFAULT 0')

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


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    return send_from_directory('public', 'sw.js', mimetype='application/javascript')

@app.route('/robots.txt')
def robots():
    return send_from_directory('public', 'robots.txt', mimetype='text/plain')

@app.route('/sitemap.xml')
def sitemap():
    return send_from_directory('public', 'sitemap.xml', mimetype='application/xml')

@app.route('/manifest.json')
def manifest():
    return send_from_directory('public', 'manifest.json', mimetype='application/manifest+json')

@app.route('/icons/<path:filename>')
def icons(filename):
    return send_from_directory('public/icons', filename)

@app.route('/logo-1080.svg')
def logo_download():
    return send_from_directory('public', 'logo-1080.svg', mimetype='image/svg+xml')

@app.route('/')
def index():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'index.html')


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


@app.route('/upload')
def upload_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'upload.html')


@app.route('/uploads/<path:filename>')
def serve_upload(filename):
    if _s3 and R2_PUBLIC_URL:
        return redirect(f'{R2_PUBLIC_URL}/{filename}')
    return send_from_directory(os.path.abspath(UPLOAD_FOLDER), filename)


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.route('/api/register', methods=['POST'])
@limiter.limit('5 per hour')
def register():
    data         = request.get_json()
    username     = data.get('username', '').strip().lower()
    display_name = data.get('display_name', '').strip()
    city         = data.get('city', '').strip()
    genres       = data.get('genres', '').strip()
    password     = data.get('password', '')
    email        = data.get('email', '').strip().lower()
    phone        = data.get('phone', '').strip()

    if not username or not display_name or not password or not email:
        return jsonify({'error': 'Please enter a username, display name, email and password'}), 400
    if len(username) > 30:
        return jsonify({'error': 'Username is too long (max 30 characters)'}), 400
    if len(display_name) > 60:
        return jsonify({'error': 'Display name is too long (max 60 characters)'}), 400
    if len(password) < 6:
        return jsonify({'error': 'Password must be at least 6 characters'}), 400
    if len(password) > 128:
        return jsonify({'error': 'Password is too long (max 128 characters)'}), 400
    if len(email) > 254:
        return jsonify({'error': 'Email is too long'}), 400
    if not username.replace('_', '').isalnum():
        return jsonify({'error': 'Username may only contain letters, numbers and underscores'}), 400
    if '@' not in email or '.' not in email:
        return jsonify({'error': 'Please enter a valid email address'}), 400

    code    = str(random.randint(100000, 999999))
    expires = (datetime.utcnow() + timedelta(minutes=15)).isoformat()

    conn = get_db()
    existing_email = conn.execute('SELECT id FROM users WHERE email = ?', (email,)).fetchone()
    if existing_email:
        conn.close()
        return jsonify({'error': 'This email is already registered'}), 400
    try:
        conn.execute(
            'INSERT INTO users (username, display_name, city, genres, password_hash, email, phone, verified, verify_code, verify_expires) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)',
            (username, display_name, city, genres,
             generate_password_hash(password, method='pbkdf2:sha256'),
             email, phone, code, expires)
        )
        conn.commit()
        user = conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        session['user_id']           = user['id']
        session['username']          = user['username']
        session['display_name']      = user['display_name']
        session['pending_verify']    = True
    except sqlite3.IntegrityError:
        conn.close()
        return jsonify({'error': 'Username already taken'}), 400
    finally:
        conn.close()

    send_email(email, 'InkLink — verify your account', f'''
    <div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px;margin:0 auto">
      <div style="font-size:28px;letter-spacing:0.2em;color:#b20000;margin-bottom:8px">INKLINK</div>
      <div style="font-size:12px;color:#555;margin-bottom:32px;letter-spacing:0.1em">Tattoo Booking Network</div>
      <p style="margin-bottom:16px">Hi <strong>{display_name}</strong>, enter this code to verify your account:</p>
      <div style="font-size:40px;letter-spacing:0.3em;color:#c62828;background:#0e0e0e;padding:20px;text-align:center;border:1px solid #1a1a1a;margin:24px 0">{code}</div>
      <p style="color:#555;font-size:12px">Code valid for 15 minutes. If you didn't sign up, ignore this email.</p>
    </div>''')

    return jsonify({'ok': True, 'verify': True})


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
    send_email(user['email'], 'InkLink — new verification code', f'''
    <div style="background:#000;color:#ccc;font-family:monospace;padding:40px;max-width:480px;margin:0 auto">
      <div style="font-size:28px;letter-spacing:0.2em;color:#b20000;margin-bottom:32px">INKLINK</div>
      <p style="margin-bottom:16px">Your new verification code:</p>
      <div style="font-size:40px;letter-spacing:0.3em;color:#c62828;background:#0e0e0e;padding:20px;text-align:center;border:1px solid #1a1a1a;margin:24px 0">{code}</div>
      <p style="color:#555;font-size:12px">Code valid for 15 minutes.</p>
    </div>''')
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
    q = request.args.get('q', '').strip()
    if len(q) < 2:
        return jsonify({'tracks': [], 'artists': [], 'events': [], 'listings': []})
    conn = get_db()
    like = f'%{q}%'

    tracks = conn.execute('''
        SELECT t.id, t.title, t.genre, u.username, u.display_name, t.cover
        FROM tracks t JOIN users u ON u.id = t.user_id
        WHERE t.title LIKE ? OR t.genre LIKE ? OR u.display_name LIKE ? OR u.username LIKE ?
        ORDER BY t.created_at DESC LIMIT 6
    ''', (like, like, like, like)).fetchall()

    artists = conn.execute('''
        SELECT id, username, display_name, city, avatar
        FROM users
        WHERE display_name LIKE ? OR username LIKE ? OR city LIKE ?
        LIMIT 6
    ''', (like, like, like)).fetchall()

    events = conn.execute('''
        SELECT e.id, e.title, e.date, e.city, e.genre
        FROM events e
        WHERE e.title LIKE ? OR e.city LIKE ? OR e.genre LIKE ?
        ORDER BY e.date ASC LIMIT 6
    ''', (like, like, like)).fetchall()

    listings = conn.execute('''
        SELECT id, title, price, currency, city
        FROM listings
        WHERE (title LIKE ? OR city LIKE ?) AND status = "active"
        LIMIT 6
    ''', (like, like)).fetchall()

    conn.close()
    return jsonify({
        'tracks':   [{'id': r['id'], 'title': r['title'], 'genre': r['genre'] or '',
                      'username': r['username'], 'display_name': r['display_name'],
                      'cover': f'/uploads/{r["cover"]}' if r['cover'] else ''} for r in tracks],
        'artists':  [{'id': r['id'], 'username': r['username'], 'display_name': r['display_name'],
                      'city': r['city'] or '',
                      'avatar': f'/uploads/{r["avatar"]}' if r['avatar'] else ''} for r in artists],
        'events':   [{'id': r['id'], 'title': r['title'], 'date': r['date'],
                      'city': r['city'] or '', 'genre': r['genre'] or ''} for r in events],
        'listings': [{'id': r['id'], 'title': r['title'], 'price': r['price'],
                      'currency': r['currency'], 'city': r['city'] or ''} for r in listings],
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
    user = conn.execute('SELECT id, username, display_name, city, genres, avatar, pro, pro_expires FROM users WHERE id = ?',
                        (session['user_id'],)).fetchone()
    conn.close()
    d = dict(user)
    if d.get('avatar'):
        d['avatar_url'] = f'/uploads/{d["avatar"]}'
    else:
        d['avatar_url'] = None
    # check if PRO has expired
    if d.get('pro') and d.get('pro_expires'):
        if d['pro_expires'] < datetime.utcnow().isoformat():
            d['pro'] = 0
    return jsonify(d)


# ── Feed API ──────────────────────────────────────────────────────────────────

@app.route('/api/feed')
def feed():
    uid = session.get('user_id', 0)

    city_filter = request.args.get('city', '').strip()
    genre_filter = request.args.get('genre', '').strip()
    search       = request.args.get('q', '').strip()
    offset       = int(request.args.get('offset', 0))
    # GPS radius filter
    try:
        flat    = float(request.args.get('lat', ''))
        flng    = float(request.args.get('lng', ''))
        fradius = float(request.args.get('radius', 50))
        gps_filter = True
    except (ValueError, TypeError):
        gps_filter = False

    conn   = get_db()
    params = [uid, uid]

    query = '''
        SELECT t.*,
               u.username, u.display_name, u.city AS user_city, u.genres, u.emoji,
               u.avatar, u.lat, u.lng, u.pro AS user_pro,
               EXISTS(SELECT 1 FROM likes WHERE user_id = ? AND track_id = t.id) AS liked,
               EXISTS(SELECT 1 FROM reposts WHERE user_id = ? AND track_id = t.id) AS reposted,
               (SELECT COUNT(*) FROM comments WHERE track_id = t.id) AS comment_count,
               (SELECT COUNT(*) FROM reposts WHERE track_id = t.id) AS repost_count
        FROM tracks t
        JOIN users u ON t.user_id = u.id
    '''

    conditions = []
    if city_filter and city_filter != 'All':
        conditions.append('(t.city = ? OR u.city = ?)')
        params += [city_filter, city_filter]
    if genre_filter:
        conditions.append('(t.genre LIKE ? OR u.genres LIKE ?)')
        params += [f'%{genre_filter}%', f'%{genre_filter}%']
    if search:
        conditions.append('(t.title LIKE ? OR u.display_name LIKE ? OR t.genre LIKE ?)')
        params += [f'%{search}%', f'%{search}%', f'%{search}%']

    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)

    query += ' ORDER BY t.created_at DESC LIMIT 200 OFFSET ?'
    params.append(offset)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    # apply GPS radius filter in Python (SQLite has no trig functions by default)
    if gps_filter:
        rows = [r for r in rows if r['lat'] is not None and r['lng'] is not None
                and haversine(flat, flng, r['lat'], r['lng']) <= fradius]
    rows = rows[:20]

    result = []
    for t in rows:
        track_city = t['city'] or t['user_city'] or ''
        result.append({
            'id':            t['id'],
            'title':         t['title'],
            'genre':         t['genre'],
            'city':          track_city,
            'filename':      t['filename'],
            'cover':         t['cover'] if t['cover'] else '',
            'video':         t['video'] if t['video'] else '',
            'duration':      t['duration'],
            'caption':       t['caption'],
            'like_count':    t['like_count'],
            'play_count':    t['play_count'],
            'liked':         bool(t['liked']),
            'reposted':      bool(t['reposted']),
            'repost_count':  t['repost_count'],
            'comment_count': t['comment_count'],
            'created_at':    time_ago(t['created_at']),
            'user': {
                'username':     t['username'],
                'display_name': t['display_name'],
                'city':         t['user_city'],
                'genres':       t['genres'],
                'emoji':        t['emoji'] or '',
                'initials':     initials(t['display_name']),
                'avatar':       f'/uploads/{t["avatar"]}' if t['avatar'] else None,
                'lat':          t['lat'],
                'lng':          t['lng'],
                'pro':          bool(t['user_pro']),
            }
        })

    return jsonify(result)


# ── Upload API ────────────────────────────────────────────────────────────────

@app.route('/api/upload', methods=['POST'])
@limiter.limit('20 per hour')
def upload_track():
    err = require_login()
    if err:
        return err

    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400

    f = request.files['file']
    if not f.filename:
        return jsonify({'error': 'No file provided'}), 400
    if not allowed_audio(f):
        return jsonify({'error': 'Unsupported format or corrupted file. Please use MP3, WAV, OGG, FLAC or AAC'}), 400
    ext = f.filename.rsplit('.', 1)[-1].lower()
    if ext == 'wav':
        conn = get_db()
        user = conn.execute('SELECT pro FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn.close()
        if not user or not user['pro']:
            return jsonify({'error': 'WAV files are available to PRO users only. Upgrade to PRO or upload an MP3.', 'pro_required': True}), 403

    title    = request.form.get('title', '').strip()
    genre    = request.form.get('genre', '').strip()
    city     = request.form.get('city', '').strip()
    caption  = request.form.get('caption', '').strip()
    duration = request.form.get('duration', '').strip()

    if not title:
        return jsonify({'error': 'Please enter a track title'}), 400

    safe   = secure_filename(f.filename)
    unique = f"{session['user_id']}_{int(time.time())}_{safe}"
    save_upload(f, unique)

    # optional cover image
    cover_unique = ''
    cover_file   = request.files.get('cover')
    if cover_file and cover_file.filename:
        if allowed_image(cover_file):
            cext         = cover_file.filename.rsplit('.', 1)[-1].lower()
            csafe        = secure_filename(cover_file.filename)
            cover_unique = f"cover_{session['user_id']}_{int(time.time())}_{csafe}"
            save_upload(cover_file, cover_unique)

    # optional video (PRO only)
    video_unique = ''
    video_file   = request.files.get('video')
    if video_file and video_file.filename:
        conn_check = get_db()
        user_check = conn_check.execute('SELECT pro FROM users WHERE id = ?', (session['user_id'],)).fetchone()
        conn_check.close()
        if user_check and user_check['pro']:
            if allowed_video(video_file):
                vext         = video_file.filename.rsplit('.', 1)[-1].lower()
                vsafe        = secure_filename(video_file.filename)
                video_unique = f"video_{session['user_id']}_{int(time.time())}_{vsafe}"
                save_upload(video_file, video_unique)

    conn = get_db()
    conn.execute(
        'INSERT INTO tracks (user_id, title, genre, city, filename, cover, video, duration, caption) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (session['user_id'], title, genre, city, unique, cover_unique, video_unique, duration, caption)
    )
    track_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    uploader = conn.execute('SELECT display_name FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    followers = conn.execute(
        'SELECT follower_id FROM follows WHERE following_id = ? LIMIT 100',
        (session['user_id'],)
    ).fetchall()
    for f in followers:
        push_notif(conn, f['follower_id'], session['user_id'], 'new_track', track_id, 'track',
                   f"{uploader['display_name']} nahrál(a) nový track: \"{title}\"")
    conn.commit()
    conn.close()

    return jsonify({'ok': True})


@app.route('/api/tracks/<int:track_id>', methods=['PATCH'])
def edit_track(track_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    row = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Track not found'}), 404
    if row['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403

    title   = request.form.get('title', row['title']).strip() or row['title']
    genre   = request.form.get('genre', row['genre']).strip()
    city    = request.form.get('city', row['city']).strip()
    caption = request.form.get('caption', row['caption']).strip()

    cover_unique = row['cover']
    cover_file = request.files.get('cover')
    if cover_file and cover_file.filename:
        if allowed_image(cover_file):
            csafe = secure_filename(cover_file.filename)
            cover_unique = f"cover_{session['user_id']}_{int(time.time())}_{csafe}"
            save_upload(cover_file, cover_unique)

    conn.execute(
        'UPDATE tracks SET title=?, genre=?, city=?, caption=?, cover=? WHERE id=?',
        (title, genre, city, caption, cover_unique, track_id)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/tracks/<int:track_id>', methods=['DELETE'])
def delete_track(track_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    row = conn.execute('SELECT * FROM tracks WHERE id = ?', (track_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Track not found'}), 404
    if row['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    conn.execute('DELETE FROM tracks WHERE id = ?', (track_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Like API ──────────────────────────────────────────────────────────────────

@app.route('/api/favorites/tracks')
def get_favorite_tracks():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute(
        '''SELECT t.id, t.title, t.genre, t.filename, t.cover, u.username, u.display_name, u.emoji
           FROM likes l
           JOIN tracks t ON l.track_id = t.id
           JOIN users u ON t.user_id = u.id
           WHERE l.user_id = ?
           ORDER BY l.rowid DESC''',
        (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify([{
        'id':           r['id'],
        'title':        r['title'],
        'genre':        r['genre'],
        'filename':     r['filename'],
        'cover':        r['cover'],
        'username':     r['username'],
        'display_name': r['display_name'],
        'emoji':        r['emoji'] or '',
        'initials':     initials(r['display_name']),
    } for r in rows])


@app.route('/api/like/<int:track_id>', methods=['POST'])
def toggle_like(track_id):
    err = require_login()
    if err:
        return err

    conn     = get_db()
    existing = conn.execute('SELECT 1 FROM likes WHERE user_id = ? AND track_id = ?',
                            (session['user_id'], track_id)).fetchone()
    if existing:
        conn.execute('DELETE FROM likes WHERE user_id = ? AND track_id = ?',
                     (session['user_id'], track_id))
        conn.execute('UPDATE tracks SET like_count = MAX(0, like_count - 1) WHERE id = ?', (track_id,))
        liked = False
    else:
        conn.execute('INSERT INTO likes (user_id, track_id) VALUES (?, ?)',
                     (session['user_id'], track_id))
        conn.execute('UPDATE tracks SET like_count = like_count + 1 WHERE id = ?', (track_id,))
        liked = True

    if liked:
        t = conn.execute('SELECT user_id, title FROM tracks WHERE id = ?', (track_id,)).fetchone()
        if t:
            actor = conn.execute('SELECT display_name FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            push_notif(conn, t['user_id'], session['user_id'], 'like_track', track_id, 'track',
                       f"{actor['display_name']} se líbí tvůj track \"{t['title']}\"")
    conn.commit()
    count = conn.execute('SELECT like_count FROM tracks WHERE id = ?', (track_id,)).fetchone()['like_count']
    conn.close()

    return jsonify({'liked': liked, 'count': count})


# ── Repost ───────────────────────────────────────────────────────────────────

@app.route('/api/repost/<int:track_id>', methods=['POST'])
def toggle_repost(track_id):
    err = require_login()
    if err: return err
    conn = get_db()
    existing = conn.execute('SELECT 1 FROM reposts WHERE user_id = ? AND track_id = ?',
                            (session['user_id'], track_id)).fetchone()
    if existing:
        conn.execute('DELETE FROM reposts WHERE user_id = ? AND track_id = ?',
                     (session['user_id'], track_id))
        reposted = False
    else:
        track_owner = conn.execute('SELECT user_id FROM tracks WHERE id = ?', (track_id,)).fetchone()
        if track_owner and track_owner['user_id'] == session['user_id']:
            conn.close(); return jsonify({'error': "You can't repost your own track"}), 400
        conn.execute('INSERT INTO reposts (user_id, track_id) VALUES (?, ?)',
                     (session['user_id'], track_id))
        reposted = True
    conn.commit()
    count = conn.execute('SELECT COUNT(*) FROM reposts WHERE track_id = ?', (track_id,)).fetchone()[0]
    conn.close()
    return jsonify({'reposted': reposted, 'count': count})

@app.route('/api/profile/<username>/reposts')
def get_profile_reposts(username):
    uid = session.get('user_id', 0)
    conn = get_db()
    u = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not u: conn.close(); return jsonify([])
    rows = conn.execute('''
        SELECT t.*, u2.username, u2.display_name, u2.avatar,
               r.created_at AS reposted_at,
               EXISTS(SELECT 1 FROM likes WHERE user_id = ? AND track_id = t.id) AS liked
        FROM reposts r
        JOIN tracks t ON t.id = r.track_id
        JOIN users u2 ON u2.id = t.user_id
        WHERE r.user_id = ?
        ORDER BY r.created_at DESC
    ''', (uid, u['id'])).fetchall()
    conn.close()
    return jsonify([{
        'id': t['id'], 'title': t['title'], 'genre': t['genre'],
        'filename': t['filename'], 'cover': t['cover'], 'duration': t['duration'],
        'like_count': t['like_count'], 'play_count': t['play_count'],
        'liked': bool(t['liked']),
        'user': {'username': t['username'], 'display_name': t['display_name'],
                 'initials': initials(t['display_name']),
                 'avatar': f'/uploads/{t["avatar"]}' if t['avatar'] else None}
    } for t in rows])


# ── Comments ─────────────────────────────────────────────────────────────────

@app.route('/api/tracks/<int:track_id>/comments')
def get_comments(track_id):
    uid  = session.get('user_id', 0)
    conn = get_db()
    rows = conn.execute('''
        SELECT c.id, c.text, c.created_at, c.user_id,
               u.username, u.display_name, u.avatar, u.emoji
        FROM comments c
        JOIN users u ON c.user_id = u.id
        WHERE c.track_id = ?
        ORDER BY c.created_at ASC
    ''', (track_id,)).fetchall()
    conn.close()
    return jsonify([{
        'id':           r['id'],
        'text':         r['text'],
        'created_at':   time_ago(r['created_at']),
        'user_id':      r['user_id'],
        'is_own':       uid != 0 and r['user_id'] == uid,
        'username':     r['username'],
        'display_name': r['display_name'],
        'avatar':       f'/uploads/{r["avatar"]}' if r['avatar'] else None,
        'emoji':        r['emoji'] or '',
        'initials':     initials(r['display_name']),
    } for r in rows])


@app.route('/api/tracks/<int:track_id>/comments', methods=['POST'])
@limiter.limit('30 per hour')
def add_comment(track_id):
    err = require_login()
    if err: return err
    text = (request.json or {}).get('text', '').strip()
    if not text or len(text) > 500:
        return jsonify({'error': 'Invalid comment'}), 400
    conn = get_db()
    if not conn.execute('SELECT 1 FROM tracks WHERE id = ?', (track_id,)).fetchone():
        conn.close(); return jsonify({'error': 'Track not found'}), 404
    cur = conn.execute(
        'INSERT INTO comments (track_id, user_id, text) VALUES (?, ?, ?)',
        (track_id, session['user_id'], text)
    )
    comment_id = cur.lastrowid
    t = conn.execute('SELECT user_id, title FROM tracks WHERE id = ?', (track_id,)).fetchone()
    row = conn.execute(
        'SELECT u.username, u.display_name, u.avatar, u.emoji FROM users WHERE id = ?',
        (session['user_id'],)
    ).fetchone()
    if t:
        preview = text[:60] + ('…' if len(text) > 60 else '')
        push_notif(conn, t['user_id'], session['user_id'], 'comment_track', track_id, 'track',
                   f"{row['display_name']} komentoval tvůj track \"{t['title']}\": {preview}")
    conn.commit()
    conn.close()
    return jsonify({
        'id':           comment_id,
        'text':         text,
        'created_at':   'just now',
        'user_id':      session['user_id'],
        'is_own':       True,
        'username':     row['username'],
        'display_name': row['display_name'],
        'avatar':       f'/uploads/{row["avatar"]}' if row['avatar'] else None,
        'emoji':        row['emoji'] or '',
        'initials':     initials(row['display_name']),
    }), 201


@app.route('/api/comments/<int:comment_id>', methods=['DELETE'])
def delete_comment(comment_id):
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT user_id FROM comments WHERE id = ?', (comment_id,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Comment not found'}), 404
    if row['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    conn.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Notifications ────────────────────────────────────────────────────────────

NOTIF_ICONS = {
    'like_track':   '♥',
    'comment_track':'◎',
    'new_follower': '◉',
    'new_track':    '♪',
    'listing_like': '🛒',
    'event_save':   '◷',
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

@app.route('/api/news')
def get_news():
    conn = get_db()
    rows = conn.execute('''
        SELECT n.id, n.text, n.created_at, n.user_id,
               u.username, u.display_name, u.avatar
        FROM news n
        JOIN users u ON u.id = n.user_id
        ORDER BY n.created_at DESC
        LIMIT 50
    ''').fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'text': r['text'], 'created_at': r['created_at'],
        'user_id': r['user_id'], 'username': r['username'],
        'display_name': r['display_name'] or r['username'],
        'avatar': r['avatar']
    } for r in rows])

@app.route('/api/news', methods=['POST'])
@limiter.limit('20 per hour')
def post_news():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.get_json()
    text = (data.get('text') or '').strip()
    if not text:
        return jsonify({'error': 'Post cannot be empty'}), 400
    if len(text) > 300:
        return jsonify({'error': 'Max 300 characters'}), 400
    conn = get_db()
    cur = conn.execute('INSERT INTO news (user_id, text) VALUES (?, ?)', (session['user_id'], text))
    conn.commit()
    nid = cur.lastrowid
    row = conn.execute('''
        SELECT n.id, n.text, n.created_at, n.user_id,
               u.username, u.display_name, u.avatar
        FROM news n JOIN users u ON u.id = n.user_id WHERE n.id = ?
    ''', (nid,)).fetchone()
    conn.close()
    return jsonify({
        'id': row['id'], 'text': row['text'], 'created_at': row['created_at'],
        'user_id': row['user_id'], 'username': row['username'],
        'display_name': row['display_name'] or row['username'],
        'avatar': row['avatar']
    }), 201

@app.route('/api/news/<int:nid>', methods=['DELETE'])
def delete_news(nid):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    row = conn.execute('SELECT user_id FROM news WHERE id = ?', (nid,)).fetchone()
    if not row:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    if row['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    conn.execute('DELETE FROM news WHERE id = ?', (nid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Play count ────────────────────────────────────────────────────────────────

@app.route('/api/play/<int:track_id>', methods=['POST'])
def record_play(track_id):
    uid  = session.get('user_id')
    city = ''
    conn = get_db()
    if uid:
        u = conn.execute('SELECT city FROM users WHERE id = ?', (uid,)).fetchone()
        if u:
            city = u['city'] or ''
    conn.execute('UPDATE tracks SET play_count = play_count + 1 WHERE id = ?', (track_id,))
    conn.execute('INSERT INTO play_logs (track_id, user_id, city) VALUES (?, ?, ?)', (track_id, uid, city))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


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

@app.route('/api/trending')
def trending():
    conn  = get_db()
    rows  = conn.execute('''
        SELECT t.id, t.title, t.like_count, t.play_count, u.display_name, u.avatar
        FROM tracks t JOIN users u ON t.user_id = u.id
        ORDER BY t.like_count DESC, t.play_count DESC
        LIMIT 5
    ''').fetchall()
    conn.close()
    return jsonify([{
        'id':         r['id'],
        'title':      r['title'],
        'like_count': r['like_count'],
        'play_count': r['play_count'],
        'artist':     r['display_name'],
        'initials':   initials(r['display_name']),
        'avatar':     f'/uploads/{r["avatar"]}' if r['avatar'] else None,
    } for r in rows])


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

@app.route('/api/genres')
def genres():
    conn = get_db()
    rows = conn.execute('SELECT genre FROM tracks WHERE genre != "" UNION SELECT genres FROM users WHERE genres != ""').fetchall()
    conn.close()
    seen = set()
    result = []
    for r in rows:
        for g in r[0].split(','):
            g = g.strip()
            if g and g not in seen:
                seen.add(g)
                result.append(g)
    result.sort()
    return jsonify(result)


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
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'profile.html')


@app.route('/events')
def events_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'events.html')


@app.route('/messages')
def messages_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'messages.html')


@app.route('/bazar')
def bazar_page():
    if 'user_id' not in session:
        return redirect('/login')
    return send_from_directory('public', 'bazar.html')


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
    u = conn.execute('SELECT id, username, display_name, city, genres, bio, avatar, emoji, photo1, photo2, photo3, photo4, lat, lng, created_at, pro FROM users WHERE username = ?', (username,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    tracks = conn.execute('''
        SELECT t.*, EXISTS(SELECT 1 FROM likes WHERE user_id = ? AND track_id = t.id) AS liked
        FROM tracks t WHERE t.user_id = ?
        ORDER BY t.created_at DESC
    ''', (uid, u['id'])).fetchall()

    followers = conn.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (u['id'],)).fetchone()[0]
    following_count = conn.execute('SELECT COUNT(*) FROM follows WHERE follower_id = ?', (u['id'],)).fetchone()[0]
    is_following = bool(conn.execute('SELECT 1 FROM follows WHERE follower_id = ? AND following_id = ?', (uid, u['id'])).fetchone())
    conn.close()

    return jsonify({
        'id':              u['id'],
        'username':        u['username'],
        'display_name':    u['display_name'],
        'city':            u['city'],
        'genres':          u['genres'],
        'bio':             u['bio'],
        'avatar':          u['avatar'],
        'emoji':           u['emoji'],
        'photos':          [u['photo1'], u['photo2'], u['photo3'], u['photo4']],
        'lat':             u['lat'],
        'lng':             u['lng'],
        'initials':        initials(u['display_name']),
        'pro':             bool(u['pro']),
        'is_own':          uid != 0 and u['id'] == uid,
        'is_following':    is_following,
        'followers':       followers,
        'following_count': following_count,
        'track_count':     len(tracks),
        'tracks': [{
            'id':         t['id'],
            'title':      t['title'],
            'genre':      t['genre'],
            'city':       t['city'],
            'filename':   t['filename'],
            'cover':      t['cover'],
            'duration':   t['duration'],
            'caption':    t['caption'],
            'like_count': t['like_count'],
            'play_count': t['play_count'],
            'liked':      bool(t['liked']),
            'created_at': time_ago(t['created_at']),
        } for t in tracks]
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


@app.route('/api/profile/update', methods=['POST'])
def update_profile():
    err = require_login()
    if err: return err

    display_name = request.form.get('display_name', '').strip()
    city         = request.form.get('city', '').strip()
    genres       = request.form.get('genres', '').strip()
    bio          = request.form.get('bio', '').strip()
    emoji        = request.form.get('emoji', '').strip()
    try:
        lat = float(request.form.get('lat', ''))
    except (ValueError, TypeError):
        lat = None
    try:
        lng = float(request.form.get('lng', ''))
    except (ValueError, TypeError):
        lng = None

    allowed_emojis = ('🎙️', '🎛️', '🎹', '📹', '📸', '')
    if emoji not in allowed_emojis:
        emoji = ''

    if not display_name:
        return jsonify({'error': 'Display name cannot be empty'}), 400
    if len(display_name) > 60:
        return jsonify({'error': 'Display name is too long (max 60 characters)'}), 400
    if len(bio) > 500:
        return jsonify({'error': 'Bio is too long (max 500 characters)'}), 400
    if len(city) > 80:
        return jsonify({'error': 'City name is too long'}), 400
    if len(genres) > 200:
        return jsonify({'error': 'Genres field is too long'}), 400

    conn = get_db()
    if lat is not None and lng is not None:
        conn.execute('UPDATE users SET display_name=?, city=?, genres=?, bio=?, emoji=?, lat=?, lng=? WHERE id=?',
                     (display_name, city, genres, bio, emoji, lat, lng, session['user_id']))
    else:
        conn.execute('UPDATE users SET display_name=?, city=?, genres=?, bio=?, emoji=? WHERE id=?',
                     (display_name, city, genres, bio, emoji, session['user_id']))

    def save_img(field, col):
        f = request.files.get(field)
        if f and f.filename:
            ext = secure_filename(f.filename).rsplit('.', 1)[-1].lower()
            if ext in ('jpg', 'jpeg', 'png', 'webp') and allowed_image(f):
                name = f'{field}_{session["user_id"]}_{int(time.time())}.{ext}'
                save_upload(f, name)
                conn.execute(f'UPDATE users SET {col}=? WHERE id=?', (name, session['user_id']))

    save_img('avatar', 'avatar')
    save_img('photo1', 'photo1')
    save_img('photo2', 'photo2')
    save_img('photo3', 'photo3')
    save_img('photo4', 'photo4')

    for slot in ('photo1', 'photo2', 'photo3', 'photo4'):
        if request.form.get(f'remove_{slot}'):
            conn.execute(f'UPDATE users SET {slot}="" WHERE id=?', (session['user_id'],))

    conn.commit()
    u = conn.execute('SELECT display_name FROM users WHERE id=?', (session['user_id'],)).fetchone()
    conn.close()

    session['display_name'] = u['display_name']
    return jsonify({'ok': True})


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


@app.route('/api/profile/<username>/tracks')
def get_profile_tracks(username):
    uid   = session.get('user_id', 0)
    limit = min(int(request.args.get('limit', 50)), 100)
    conn  = get_db()
    user  = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not user:
        conn.close()
        return jsonify([])
    rows = conn.execute(
        'SELECT * FROM tracks WHERE user_id = ? ORDER BY created_at DESC LIMIT ?',
        (user['id'], limit)
    ).fetchall()
    conn.close()
    return jsonify([{
        'id':         r['id'],
        'title':      r['title'],
        'genre':      r['genre'],
        'city':       r['city'],
        'filename':   r['filename'],
        'cover':      r['cover'],
        'duration':   r['duration'],
        'like_count': r['like_count'],
        'play_count': r['play_count'],
        'created_at': time_ago(r['created_at']),
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

@app.route('/api/profile/<username>/playlists')
def get_profile_playlists(username):
    err = require_login()
    if err: return err
    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'User not found'}), 404
    is_own = user['id'] == session['user_id']
    if is_own:
        rows = conn.execute('SELECT * FROM playlists WHERE user_id = ? ORDER BY created_at DESC', (user['id'],)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM playlists WHERE user_id = ? AND is_public = 1 ORDER BY created_at DESC', (user['id'],)).fetchall()
    result = []
    for r in rows:
        tc = conn.execute('SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?', (r['id'],)).fetchone()[0]
        cover = conn.execute(
            'SELECT t.cover FROM playlist_tracks pt JOIN tracks t ON pt.track_id = t.id WHERE pt.playlist_id = ? AND t.cover != "" ORDER BY pt.position ASC LIMIT 1',
            (r['id'],)
        ).fetchone()
        result.append({
            'id':        r['id'],
            'name':      r['name'],
            'is_public': bool(r['is_public']),
            'track_count': tc,
            'cover':     cover['cover'] if cover else '',
            'created_at': r['created_at'],
        })
    conn.close()
    return jsonify(result)


@app.route('/api/playlists/<int:pid>')
def get_playlist(pid):
    err = require_login()
    if err: return err
    conn = get_db()
    pl = conn.execute('SELECT * FROM playlists WHERE id = ?', (pid,)).fetchone()
    if not pl:
        conn.close()
        return jsonify({'error': 'Playlist not found'}), 404
    if not pl['is_public'] and pl['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Private playlist'}), 403
    tracks = conn.execute(
        '''SELECT t.id, t.title, t.genre, t.filename, t.cover, t.duration, t.like_count,
                  u.display_name, u.username
           FROM playlist_tracks pt
           JOIN tracks t ON pt.track_id = t.id
           JOIN users u ON t.user_id = u.id
           WHERE pt.playlist_id = ?
           ORDER BY pt.position ASC, pt.added_at ASC''',
        (pid,)
    ).fetchall()
    conn.close()
    return jsonify({
        'id':        pl['id'],
        'name':      pl['name'],
        'is_public': bool(pl['is_public']),
        'is_own':    pl['user_id'] == session['user_id'],
        'tracks': [{
            'id':           t['id'],
            'title':        t['title'],
            'genre':        t['genre'],
            'filename':     t['filename'],
            'cover':        t['cover'],
            'duration':     t['duration'],
            'like_count':   t['like_count'],
            'display_name': t['display_name'],
            'username':     t['username'],
        } for t in tracks],
    })


@app.route('/api/playlists', methods=['POST'])
def create_playlist():
    err = require_login()
    if err: return err
    data = request.get_json()
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400
    is_public = 1 if data.get('is_public', True) else 0
    conn = get_db()
    cur = conn.execute(
        'INSERT INTO playlists (user_id, name, is_public) VALUES (?, ?, ?)',
        (session['user_id'], name, is_public)
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': pid})


@app.route('/api/playlists/<int:pid>', methods=['PATCH'])
def update_playlist(pid):
    err = require_login()
    if err: return err
    conn = get_db()
    pl = conn.execute('SELECT * FROM playlists WHERE id = ?', (pid,)).fetchone()
    if not pl or pl['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    data = request.get_json()
    name      = data.get('name', pl['name']).strip() or pl['name']
    is_public = 1 if data.get('is_public', bool(pl['is_public'])) else 0
    conn.execute('UPDATE playlists SET name = ?, is_public = ? WHERE id = ?', (name, is_public, pid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/playlists/<int:pid>', methods=['DELETE'])
def delete_playlist(pid):
    err = require_login()
    if err: return err
    conn = get_db()
    pl = conn.execute('SELECT user_id FROM playlists WHERE id = ?', (pid,)).fetchone()
    if not pl or pl['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute('DELETE FROM playlists WHERE id = ?', (pid,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/playlists/<int:pid>/tracks', methods=['POST'])
def add_to_playlist(pid):
    err = require_login()
    if err: return err
    conn = get_db()
    pl = conn.execute('SELECT user_id FROM playlists WHERE id = ?', (pid,)).fetchone()
    if not pl or pl['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    track_id = request.get_json().get('track_id')
    pos = conn.execute('SELECT COUNT(*) FROM playlist_tracks WHERE playlist_id = ?', (pid,)).fetchone()[0]
    try:
        conn.execute('INSERT INTO playlist_tracks (playlist_id, track_id, position) VALUES (?, ?, ?)', (pid, track_id, pos))
        conn.commit()
    except Exception:
        conn.close()
        return jsonify({'error': 'Track už je v playlistu'}), 409
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/playlists/<int:pid>/tracks/<int:tid>', methods=['DELETE'])
def remove_from_playlist(pid, tid):
    err = require_login()
    if err: return err
    conn = get_db()
    pl = conn.execute('SELECT user_id FROM playlists WHERE id = ?', (pid,)).fetchone()
    if not pl or pl['user_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'Not found'}), 404
    conn.execute('DELETE FROM playlist_tracks WHERE playlist_id = ? AND track_id = ?', (pid, tid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Bazar ─────────────────────────────────────────────────────────────────────

CATEGORIES = ['Kytary & Baskytary','Bicí & Perkuse','Klávesy & Syntezátory',
               'DJ & Elektronika','Studiové vybavení','Zesilovače & Aparáty',
               'Efekty & Pedály','Sluchátka & Mikrofony','Vinylové desky','Ostatní']

@app.route('/api/listings')
def get_listings():
    uid       = session.get('user_id', 0)
    category  = request.args.get('category', '').strip()
    condition = request.args.get('condition', '').strip()
    city      = request.args.get('city', '').strip()
    query     = request.args.get('q', '').strip()
    price_max = request.args.get('price_max', '')
    sort      = request.args.get('sort', 'newest')

    sql    = '''SELECT l.*, u.username, u.display_name, u.emoji,
                       EXISTS(SELECT 1 FROM listing_likes WHERE user_id=? AND listing_id=l.id) AS liked
                FROM listings l JOIN users u ON l.user_id = u.id
                WHERE l.status = 'active' '''
    params = [uid]

    if category:
        sql += ' AND l.category = ?'; params.append(category)
    if condition:
        sql += ' AND l.condition = ?'; params.append(condition)
    if city:
        sql += ' AND l.city LIKE ?'; params.append(f'%{city}%')
    if query:
        sql += ' AND (l.title LIKE ? OR l.description LIKE ?)'; params += [f'%{query}%', f'%{query}%']
    if price_max:
        try: sql += ' AND l.price <= ?'; params.append(int(price_max))
        except: pass

    order = {'newest': 'l.boosted DESC, l.created_at DESC',
             'price_asc': 'l.boosted DESC, l.price ASC',
             'price_desc': 'l.boosted DESC, l.price DESC'}.get(sort, 'l.boosted DESC, l.created_at DESC')
    sql += f' ORDER BY {order}'

    conn = get_db()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    def photos(r):
        return [f'/uploads/{r[f"photo{i}"]}' for i in range(1,6) if r[f'photo{i}']]

    return jsonify([{
        'id':           r['id'],
        'title':        r['title'],
        'description':  r['description'],
        'price':        r['price'],
        'currency':     r['currency'],
        'condition':    r['condition'],
        'category':     r['category'],
        'city':         r['city'],
        'status':       r['status'],
        'boosted':      bool(r['boosted']),
        'photos':       photos(r),
        'liked':        bool(r['liked']),
        'is_own':       uid != 0 and r['user_id'] == uid,
        'user': {
            'username':     r['username'],
            'display_name': r['display_name'],
            'emoji':        r['emoji'] or '',
            'initials':     initials(r['display_name']),
        },
        'created_at': r['created_at'],
    } for r in rows])


@app.route('/api/listings', methods=['POST'])
@limiter.limit('10 per hour')
def create_listing():
    err = require_login()
    if err: return err
    title       = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    city        = request.form.get('city', '').strip()
    category    = request.form.get('category', '').strip()
    condition   = request.form.get('condition', 'used').strip()
    try: price = int(request.form.get('price', 0))
    except: price = 0
    currency = request.form.get('currency', 'CZK').strip()

    if not title or price <= 0:
        return jsonify({'error': 'Title and price are required'}), 400
    if len(title) > 120:
        return jsonify({'error': 'Title is too long (max 120 characters)'}), 400
    if len(description) > 2000:
        return jsonify({'error': 'Description is too long (max 2000 characters)'}), 400
    if price > 10_000_000:
        return jsonify({'error': 'Price is too high'}), 400

    photos = []
    for i in range(1, 6):
        f = request.files.get(f'photo{i}')
        if f and f.filename and allowed_file(f.filename) and allowed_image(f):
            ext  = secure_filename(f.filename).rsplit('.', 1)[1].lower()
            name = f'lst_{session["user_id"]}_{int(datetime.now().timestamp()*1000)}_{i}.{ext}'
            save_upload(f, name)
            photos.append(name)
        else:
            photos.append('')

    conn = get_db()
    conn.execute(
        'INSERT INTO listings (user_id, title, description, price, currency, condition, category, city, photo1, photo2, photo3, photo4, photo5) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (session['user_id'], title, description, price, currency, condition, category, city, *photos)
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/listings/<int:lid>', methods=['PATCH'])
def update_listing(lid):
    err = require_login()
    if err: return err
    conn = get_db()
    lst = conn.execute('SELECT * FROM listings WHERE id = ?', (lid,)).fetchone()
    if not lst or lst['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    data = request.get_json()
    status = data.get('status', lst['status'])
    conn.execute('UPDATE listings SET status = ? WHERE id = ?', (status, lid))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/listings/<int:lid>', methods=['DELETE'])
def delete_listing(lid):
    err = require_login()
    if err: return err
    conn = get_db()
    lst = conn.execute('SELECT user_id FROM listings WHERE id = ?', (lid,)).fetchone()
    if not lst or lst['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Not found'}), 404
    conn.execute('DELETE FROM listings WHERE id = ?', (lid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/listings/<int:lid>/like', methods=['POST'])
def toggle_listing_like(lid):
    err = require_login()
    if err: return err
    conn = get_db()
    existing = conn.execute('SELECT 1 FROM listing_likes WHERE user_id=? AND listing_id=?',
                            (session['user_id'], lid)).fetchone()
    if existing:
        conn.execute('DELETE FROM listing_likes WHERE user_id=? AND listing_id=?', (session['user_id'], lid))
        liked = False
    else:
        conn.execute('INSERT INTO listing_likes (user_id, listing_id) VALUES (?,?)', (session['user_id'], lid))
        liked = True
        listing = conn.execute('SELECT user_id, title FROM listings WHERE id = ?', (lid,)).fetchone()
        if listing:
            actor = conn.execute('SELECT display_name FROM users WHERE id = ?', (session['user_id'],)).fetchone()
            push_notif(conn, listing['user_id'], session['user_id'], 'listing_like', lid, 'listing',
                       f"{actor['display_name']} se líbí tvůj inzerát \"{listing['title']}\"")
    conn.commit(); conn.close()
    return jsonify({'liked': liked})


SKILL_CATEGORIES = {
    'Sound & Production': ['Mixing', 'Mastering', 'Beatmaking', 'Music production', 'Audio editing & Cleanup', 'Sound design'],
    'Composition & Writing': ['Ghostwriting', 'Toplining', 'Arranging', 'Jingle & sample creation'],
    'Session musicians': ['Vocals (Lead / Backing)', 'Instrument recording', 'Voiceover / Spoken word', 'DJing'],
    'Visual & Promo': ['Cover Art', 'Music video production', 'Music photography', 'Merch design', 'Animation & Visualizers'],
    'Management & Live': ['Live Sound Engineer', 'Booking', 'PR & Social Media', 'Lighting design'],
}


@app.route('/api/skills')
def get_skills():
    category   = request.args.get('category')
    subcategory = request.args.get('subcategory')
    q          = request.args.get('q', '').strip()
    remote     = request.args.get('remote')
    sort       = request.args.get('sort', 'newest')
    user_id    = session.get('user_id')
    conn = get_db()
    where, params = ['s.status = "active"'], []
    if category:
        where.append('s.category = ?'); params.append(category)
    if subcategory:
        where.append('s.subcategory = ?'); params.append(subcategory)
    if q:
        where.append('(s.title LIKE ? OR s.description LIKE ?)'); params += [f'%{q}%', f'%{q}%']
    if remote == '1':
        where.append('s.remote = 1')
    order = {'newest': 's.created_at DESC', 'price_asc': 's.price_from ASC', 'price_desc': 's.price_from DESC'}.get(sort, 's.created_at DESC')
    sql = f'''SELECT s.*, u.username, u.display_name, u.avatar,
               (SELECT COUNT(*) FROM skill_likes sl WHERE sl.skill_id = s.id) AS likes,
               {'(SELECT 1 FROM skill_likes sl WHERE sl.skill_id=s.id AND sl.user_id=?) AS liked' if user_id else '0 AS liked'}
              FROM skill_listings s JOIN users u ON s.user_id = u.id
              WHERE {' AND '.join(where)} ORDER BY {order}'''
    rows = conn.execute(sql, ([user_id] + params) if user_id else params).fetchall()
    conn.close()
    def fmt(r):
        return {
            'id': r['id'], 'title': r['title'], 'description': r['description'],
            'category': r['category'], 'subcategory': r['subcategory'],
            'price_from': r['price_from'], 'price_to': r['price_to'], 'currency': r['currency'],
            'delivery_days': r['delivery_days'], 'city': r['city'], 'remote': bool(r['remote']),
            'likes': r['likes'], 'liked': bool(r['liked']),
            'username': r['username'], 'display_name': r['display_name'],
            'avatar': photo_url(r['avatar']), 'created_at': r['created_at'],
        }
    return jsonify([fmt(r) for r in rows])


@app.route('/api/skills/categories')
def get_skill_categories():
    return jsonify(SKILL_CATEGORIES)


@app.route('/api/skills', methods=['POST'])
def create_skill():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    d = request.get_json()
    if not d or not d.get('title') or not d.get('category'):
        return jsonify({'error': 'Title and category are required'}), 400
    conn = get_db()
    conn.execute(
        'INSERT INTO skill_listings (user_id,title,description,category,subcategory,price_from,price_to,currency,delivery_days,city,remote) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (session['user_id'], d['title'].strip(), d.get('description','').strip(),
         d['category'], d.get('subcategory',''),
         int(d.get('price_from') or 0), int(d.get('price_to') or 0),
         d.get('currency','CZK'), int(d.get('delivery_days') or 7),
         d.get('city','').strip(), 1 if d.get('remote', True) else 0)
    )
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/skills/<int:sid>', methods=['DELETE'])
def delete_skill(sid):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    row = conn.execute('SELECT user_id FROM skill_listings WHERE id=?', (sid,)).fetchone()
    if not row or row['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Zakázáno'}), 403
    conn.execute('DELETE FROM skill_listings WHERE id=?', (sid,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})


@app.route('/api/skills/<int:sid>/like', methods=['POST'])
def like_skill(sid):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    existing = conn.execute('SELECT 1 FROM skill_likes WHERE user_id=? AND skill_id=?', (session['user_id'], sid)).fetchone()
    if existing:
        conn.execute('DELETE FROM skill_likes WHERE user_id=? AND skill_id=?', (session['user_id'], sid))
        liked = False
    else:
        conn.execute('INSERT INTO skill_likes (user_id, skill_id) VALUES (?,?)', (session['user_id'], sid))
        liked = True
    conn.commit(); conn.close()
    return jsonify({'liked': liked})


@app.route('/library')
def library_page():
    return send_from_directory('public', 'library.html')

# ── Analytics (PRO) ──────────────────────────────────────────────────────────

@app.route('/analytics')
def analytics_page():
    return send_from_directory('public', 'analytics.html')

@app.route('/api/analytics')
def analytics():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    user = conn.execute('SELECT pro FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    if not user or not user['pro']:
        conn.close()
        return jsonify({'error': 'Analytics is available to PRO users only'}), 403

    uid = session['user_id']

    # own track IDs
    track_ids = [r['id'] for r in conn.execute('SELECT id FROM tracks WHERE user_id = ?', (uid,)).fetchall()]

    # overview
    overview = conn.execute('''
        SELECT COUNT(*) as track_count,
               COALESCE(SUM(play_count),0) as total_plays,
               COALESCE(SUM(like_count),0) as total_likes
        FROM tracks WHERE user_id = ?
    ''', (uid,)).fetchone()
    total_reposts = conn.execute(
        'SELECT COUNT(*) FROM reposts WHERE track_id IN ({})'.format(','.join('?' * len(track_ids)) if track_ids else '0'),
        track_ids
    ).fetchone()[0] if track_ids else 0
    total_followers = conn.execute('SELECT COUNT(*) FROM follows WHERE following_id = ?', (uid,)).fetchone()[0]

    # plays per day (last 30 days)
    plays_by_day = conn.execute('''
        SELECT date(created_at) as day, COUNT(*) as cnt
        FROM play_logs
        WHERE track_id IN ({ph}) AND created_at >= date('now', '-30 days')
        GROUP BY day ORDER BY day
    '''.format(ph=','.join('?' * len(track_ids)) if track_ids else '0'),
        track_ids
    ).fetchall() if track_ids else []

    # top cities
    top_cities = conn.execute('''
        SELECT city, COUNT(*) as cnt FROM play_logs
        WHERE track_id IN ({ph}) AND city != ''
        GROUP BY city ORDER BY cnt DESC LIMIT 10
    '''.format(ph=','.join('?' * len(track_ids)) if track_ids else '0'),
        track_ids
    ).fetchall() if track_ids else []

    # per-track stats
    tracks = conn.execute('''
        SELECT t.id, t.title, t.cover, t.play_count, t.like_count,
               (SELECT COUNT(*) FROM reposts WHERE track_id = t.id) as repost_count
        FROM tracks t WHERE t.user_id = ? ORDER BY t.play_count DESC
    ''', (uid,)).fetchall()

    # recent fans (likes + reposts on own tracks, last 50)
    recent_fans = []
    if track_ids:
        ph = ','.join('?' * len(track_ids))
        likes_rows = conn.execute(f'''
            SELECT u.username, u.display_name, u.avatar, t.title, l.created_at, 'like' as action
            FROM likes l
            JOIN users u ON l.user_id = u.id
            JOIN tracks t ON l.track_id = t.id
            WHERE l.track_id IN ({ph}) AND l.user_id != ?
            ORDER BY l.created_at DESC LIMIT 25
        ''', track_ids + [uid]).fetchall()
        repost_rows = conn.execute(f'''
            SELECT u.username, u.display_name, u.avatar, t.title, r.created_at, 'repost' as action
            FROM reposts r
            JOIN users u ON r.user_id = u.id
            JOIN tracks t ON r.track_id = t.id
            WHERE r.track_id IN ({ph}) AND r.user_id != ?
            ORDER BY r.created_at DESC LIMIT 25
        ''', track_ids + [uid]).fetchall()
        combined = sorted(list(likes_rows) + list(repost_rows),
                          key=lambda x: x['created_at'], reverse=True)[:30]
        recent_fans = [{
            'username':     r['username'],
            'display_name': r['display_name'],
            'avatar':       f'/uploads/{r["avatar"]}' if r['avatar'] else None,
            'track_title':  r['title'],
            'action':       r['action'],
            'created_at':   time_ago(r['created_at']),
        } for r in combined]

    conn.close()
    return jsonify({
        'overview': {
            'track_count':    overview['track_count'],
            'total_plays':    overview['total_plays'],
            'total_likes':    overview['total_likes'],
            'total_reposts':  total_reposts,
            'total_followers': total_followers,
        },
        'plays_by_day': [{'date': r['day'], 'count': r['cnt']} for r in plays_by_day],
        'top_cities':   [{'city': r['city'], 'count': r['cnt']} for r in top_cities],
        'tracks': [{
            'id':           t['id'],
            'title':        t['title'],
            'cover':        t['cover'] or '',
            'play_count':   t['play_count'],
            'like_count':   t['like_count'],
            'repost_count': t['repost_count'],
        } for t in tracks],
        'recent_fans': recent_fans,
    })


# ── PRO Subscription ─────────────────────────────────────────────────────────

@app.route('/api/pro/checkout', methods=['POST'])
def pro_checkout():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    if not STRIPE_PRO_PRICE_ID:
        return jsonify({'error': 'PRO subscription is not configured'}), 500
    conn = get_db()
    user = conn.execute('SELECT email, stripe_customer_id, pro FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    if user['pro']:
        return jsonify({'error': 'You are already a PRO user'}), 400
    customer_id = user['stripe_customer_id']
    try:
        if not customer_id:
            cust = stripe.Customer.create(email=user['email'] or None,
                                          metadata={'user_id': str(session['user_id'])})
            customer_id = cust['id']
            conn = get_db()
            conn.execute('UPDATE users SET stripe_customer_id = ? WHERE id = ?', (customer_id, session['user_id']))
            conn.commit(); conn.close()
        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            mode='subscription',
            line_items=[{'price': STRIPE_PRO_PRICE_ID, 'quantity': 1}],
            success_url=request.host_url + 'pro/success?session_id={CHECKOUT_SESSION_ID}',
            cancel_url=request.host_url + 'pro',
            metadata={'user_id': str(session['user_id'])},
        )
        return jsonify({'url': checkout.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/pro/portal', methods=['POST'])
def pro_portal():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    user = conn.execute('SELECT stripe_customer_id FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    if not user or not user['stripe_customer_id']:
        return jsonify({'error': 'You have no active subscription'}), 400
    try:
        portal = stripe.billing_portal.Session.create(
            customer=user['stripe_customer_id'],
            return_url=request.host_url + 'pro',
        )
        return jsonify({'url': portal.url})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/pro/status')
def pro_status():
    if 'user_id' not in session:
        return jsonify({'pro': False})
    conn = get_db()
    user = conn.execute('SELECT pro, pro_expires FROM users WHERE id = ?', (session['user_id'],)).fetchone()
    conn.close()
    is_pro = bool(user['pro']) if user else False
    if is_pro and user['pro_expires'] and user['pro_expires'] < datetime.utcnow().isoformat():
        is_pro = False
    return jsonify({'pro': is_pro})

@app.route('/pro')
@app.route('/pro/success')
def pro_page():
    return send_from_directory('public', 'pro.html')


# ── Payments ─────────────────────────────────────────────────────────────────

@app.route('/api/stripe/public-key')
def stripe_public_key():
    return jsonify({'key': STRIPE_PUBLIC_KEY})

@app.route('/api/listings/<int:lid>/checkout', methods=['POST'])
def listing_checkout(lid):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    listing = conn.execute('SELECT * FROM listings WHERE id = ? AND status = "active"', (lid,)).fetchone()
    if not listing:
        conn.close(); return jsonify({'error': 'Listing not found or already sold'}), 404
    if listing['user_id'] == session['user_id']:
        conn.close(); return jsonify({'error': "You can't buy your own listing"}), 400
    amount_czk = listing['price']
    fee_czk    = max(1, round(amount_czk * PLATFORM_FEE_LISTING))
    currency   = listing['currency'].lower()
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': currency,
                        'unit_amount': amount_czk * 100,
                        'product_data': {'name': listing['title']},
                    },
                    'quantity': 1,
                },
                {
                    'price_data': {
                        'currency': currency,
                        'unit_amount': fee_czk * 100,
                        'product_data': {'name': f'Platform fee ({int(PLATFORM_FEE_LISTING*100)} %)'},
                    },
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=request.host_url + 'payment/success?session_id={CHECKOUT_SESSION_ID}&type=listing&id=' + str(lid),
            cancel_url=request.host_url + '?bazar=1',
        )
    except stripe.error.AuthenticationError:
        conn.close(); return jsonify({'error': 'Stripe key not configured — add STRIPE_SECRET_KEY to your environment'}), 500
    order_code = str(uuid.uuid4())[:8].upper()
    conn.execute(
        'INSERT INTO orders (user_id, item_type, item_id, stripe_session_id, amount, platform_fee, currency, status, ticket_code) VALUES (?,?,?,?,?,?,?,?,?)',
        (session['user_id'], 'listing', lid, checkout.id, amount_czk, fee_czk, listing['currency'], 'pending', order_code)
    )
    conn.commit(); conn.close()
    return jsonify({'url': checkout.url})

@app.route('/api/events/<int:event_id>/ticket-types', methods=['GET'])
def get_ticket_types(event_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM ticket_types WHERE event_id = ? ORDER BY price ASC', (event_id,)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/api/events/<int:event_id>/ticket-types', methods=['POST'])
def create_ticket_type(event_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    ev = conn.execute('SELECT user_id FROM events WHERE id = ?', (event_id,)).fetchone()
    if not ev or ev['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    data = request.get_json()
    name     = (data.get('name') or '').strip()
    price    = int(data.get('price') or 0)
    capacity = int(data.get('capacity') or 0)
    currency = (data.get('currency') or 'CZK').upper()
    if not name or price < 0:
        conn.close(); return jsonify({'error': 'Invalid data'}), 400
    cur = conn.execute(
        'INSERT INTO ticket_types (event_id, name, price, currency, capacity) VALUES (?,?,?,?,?)',
        (event_id, name, price, currency, capacity)
    )
    conn.commit()
    tt = conn.execute('SELECT * FROM ticket_types WHERE id = ?', (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify(dict(tt)), 201

@app.route('/api/ticket-types/<int:tt_id>', methods=['DELETE'])
def delete_ticket_type(tt_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    tt = conn.execute('''
        SELECT tt.*, e.user_id as owner FROM ticket_types tt
        JOIN events e ON e.id = tt.event_id WHERE tt.id = ?
    ''', (tt_id,)).fetchone()
    if not tt or tt['owner'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Forbidden'}), 403
    conn.execute('DELETE FROM ticket_types WHERE id = ?', (tt_id,))
    conn.commit(); conn.close()
    return jsonify({'ok': True})

@app.route('/api/ticket-types/<int:tt_id>/checkout', methods=['POST'])
def ticket_checkout(tt_id):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    tt = conn.execute('''
        SELECT tt.*, e.title as event_title, e.date as event_date, e.user_id as owner
        FROM ticket_types tt JOIN events e ON e.id = tt.event_id WHERE tt.id = ?
    ''', (tt_id,)).fetchone()
    if not tt:
        conn.close(); return jsonify({'error': 'Ticket not found'}), 404
    if tt['owner'] == session['user_id']:
        conn.close(); return jsonify({'error': "You can't buy a ticket to your own event"}), 400
    if tt['capacity'] > 0 and tt['sold'] >= tt['capacity']:
        conn.close(); return jsonify({'error': 'Tickets are sold out'}), 400
    label    = f"{tt['event_title']} — {tt['name']} ({tt['event_date']})"
    price    = tt['price']
    fee_czk  = max(1, round(price * PLATFORM_FEE_TICKET))
    currency = tt['currency'].lower()
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[
                {
                    'price_data': {
                        'currency': currency,
                        'unit_amount': price * 100,
                        'product_data': {'name': label},
                    },
                    'quantity': 1,
                },
                {
                    'price_data': {
                        'currency': currency,
                        'unit_amount': fee_czk * 100,
                        'product_data': {'name': f'Poplatek za zpracování ({int(PLATFORM_FEE_TICKET*100)} %)'},
                    },
                    'quantity': 1,
                },
            ],
            mode='payment',
            success_url=request.host_url + 'payment/success?session_id={CHECKOUT_SESSION_ID}&type=ticket&id=' + str(tt_id),
            cancel_url=request.host_url + 'events',
        )
    except stripe.error.AuthenticationError:
        conn.close(); return jsonify({'error': 'Stripe klíč není nastaven — přidej STRIPE_SECRET_KEY do prostředí'}), 500
    ticket_code = str(uuid.uuid4())[:12].upper()
    conn.execute(
        'INSERT INTO orders (user_id, item_type, item_id, stripe_session_id, amount, platform_fee, currency, status, ticket_code) VALUES (?,?,?,?,?,?,?,?,?)',
        (session['user_id'], 'ticket', tt_id, checkout.id, price, fee_czk, tt['currency'], 'pending', ticket_code)
    )
    conn.commit(); conn.close()
    return jsonify({'url': checkout.url})

@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.get_data()
    sig = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except Exception:
        return '', 400
    etype = event['type']
    obj   = event['data']['object']

    if etype == 'checkout.session.completed':
        sid  = obj['id']
        mode = obj.get('mode', '')
        conn = get_db()
        if mode == 'subscription':
            uid = obj.get('metadata', {}).get('user_id')
            if uid:
                conn.execute('UPDATE users SET pro = 1, pro_expires = NULL WHERE id = ?', (uid,))
                conn.commit()
        else:
            order = conn.execute('SELECT * FROM orders WHERE stripe_session_id = ?', (sid,)).fetchone()
            if order and order['status'] == 'pending':
                conn.execute('UPDATE orders SET status = "paid" WHERE id = ?', (order['id'],))
                if order['item_type'] == 'ticket':
                    conn.execute('UPDATE ticket_types SET sold = sold + 1 WHERE id = ?', (order['item_id'],))
                elif order['item_type'] == 'listing':
                    conn.execute('UPDATE listings SET status = "sold" WHERE id = ?', (order['item_id'],))
                conn.commit()
        conn.close()

    elif etype in ('customer.subscription.updated', 'customer.subscription.created'):
        status      = obj.get('status')
        customer_id = obj.get('customer')
        if customer_id:
            conn = get_db()
            is_pro = 1 if status in ('active', 'trialing') else 0
            conn.execute('UPDATE users SET pro = ? WHERE stripe_customer_id = ?', (is_pro, customer_id))
            conn.commit(); conn.close()

    elif etype == 'customer.subscription.deleted':
        customer_id = obj.get('customer')
        if customer_id:
            conn = get_db()
            conn.execute('UPDATE users SET pro = 0 WHERE stripe_customer_id = ?', (customer_id,))
            conn.commit(); conn.close()

    return '', 200

@app.route('/api/my-orders')
def my_orders():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM orders WHERE user_id = ? ORDER BY created_at DESC', (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route('/payment/success')
def payment_success():
    return send_from_directory('public', 'payment_success.html')

@app.route('/api/orders/verify')
def verify_order():
    session_id = request.args.get('session_id')
    if not session_id:
        return jsonify({'error': 'Chybí session_id'}), 400
    conn = get_db()
    order = conn.execute('SELECT * FROM orders WHERE stripe_session_id = ?', (session_id,)).fetchone()
    if order and order['status'] == 'pending':
        try:
            sess = stripe.checkout.Session.retrieve(session_id)
            if sess.payment_status == 'paid':
                conn.execute('UPDATE orders SET status = "paid" WHERE id = ?', (order['id'],))
                if order['item_type'] == 'ticket':
                    conn.execute('UPDATE ticket_types SET sold = sold + 1 WHERE id = ?', (order['item_id'],))
                elif order['item_type'] == 'listing':
                    conn.execute('UPDATE listings SET status = "sold" WHERE id = ?', (order['item_id'],))
                conn.commit()
                order = conn.execute('SELECT * FROM orders WHERE id = ?', (order['id'],)).fetchone()
        except Exception:
            pass
    conn.close()
    if not order:
        return jsonify({'error': 'Order not found'}), 404
    return jsonify(dict(order))


# ── Tickets (QR) ──────────────────────────────────────────────────────────────

@app.route('/ticket/<code>')
def ticket_page(code):
    return send_from_directory('public', 'ticket.html')

@app.route('/scan')
def scan_page():
    return send_from_directory('public', 'scan.html')


@app.route('/api/ticket/<code>')
def get_ticket(code):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    order = conn.execute(
        "SELECT o.*, u.display_name, u.username, u.emoji FROM orders o JOIN users u ON o.user_id=u.id WHERE o.ticket_code=?",
        (code,)
    ).fetchone()
    if not order:
        conn.close(); return jsonify({'error': 'Ticket not found'}), 404
    # get event info
    ev = conn.execute('SELECT * FROM events WHERE id=?', (order['item_id'],)).fetchone()
    conn.close()
    if not ev:
        return jsonify({'error': 'Event not found'}), 404
    return jsonify({
        'code':         order['ticket_code'],
        'status':       order['status'],
        'holder':       order['display_name'],
        'holder_user':  order['username'],
        'holder_emoji': order['emoji'] or '',
        'event_id':     ev['id'],
        'event_title':  ev['title'],
        'event_date':   ev['date'],
        'event_time':   ev['time'],
        'event_venue':  ev['venue'],
        'event_city':   ev['city'],
        'event_owner':  ev['user_id'],
        'is_owner':     ev['user_id'] == session['user_id'],
        'is_mine':      order['user_id'] == session['user_id'],
        'amount':       order['amount'],
        'created_at':   order['created_at'],
    })

@app.route('/api/ticket/<code>/use', methods=['POST'])
def use_ticket(code):
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    conn = get_db()
    order = conn.execute("SELECT * FROM orders WHERE ticket_code=?", (code,)).fetchone()
    if not order:
        conn.close(); return jsonify({'error': 'Ticket not found'}), 404
    ev = conn.execute('SELECT user_id FROM events WHERE id=?', (order['item_id'],)).fetchone()
    if not ev or ev['user_id'] != session['user_id']:
        conn.close(); return jsonify({'error': 'Nejsi organizátor této akce'}), 403
    if order['status'] == 'used':
        conn.close(); return jsonify({'error': 'Ticket already used', 'status': 'used'}), 409
    if order['status'] not in ('valid', 'paid'):
        conn.close(); return jsonify({'error': f'Invalid ticket (status: {order["status"]})'}), 400
    conn.execute("UPDATE orders SET status='used' WHERE ticket_code=?", (code,))
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'status': 'used'})

@app.errorhandler(404)
def not_found(e):
    return send_from_directory('public', '404.html'), 404

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
