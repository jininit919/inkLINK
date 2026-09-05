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

# ── Čas: jednočasová (CZ) platforma ──────────────────────────────────────────
# Frontend posílá časy tak, jak je tatér napsal (pražský wall-clock, bez
# offsetu) a DB je tak i ukládá. Porovnávat je proti datetime.utcnow() by
# posunulo každou "kolik hodin před termínem" kontrolu o pražský offset
# (+1 h zima / +2 h léto) ve prospěch klienta. Proto se wall-clock porovnává
# s wall-clockem. Pokud InkLink někdy expanduje mimo jedno časové pásmo,
# tohle je místo, které se musí přepsat na skutečné tz-aware ukládání.
PLATFORM_TZ = 'Europe/Prague'


def _prague_now_naive() -> datetime:
    """'Teď' v pražském wall-clocku jako naive datetime (srovnatelné s DB)."""
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo(PLATFORM_TZ)).replace(tzinfo=None)
    except Exception:
        # Chybějící tz databáze nesmí shodit booking flow — fallback na UTC
        # je posunutý, ale funkční (a přesně to, co dělal kód předtím).
        return datetime.utcnow()


def _naive_dt(s: str) -> datetime:
    """Naparsuje ISO 8601 na naive datetime (tz-aware vstup převede na UTC
    a offset zahodí, aby šel porovnávat s naive hodnotami v DB).
    Vyhodí ValueError na nesmyslný vstup — volající vrací 400."""
    dt = datetime.fromisoformat((s or '').replace('Z', '+00:00'))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

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
            # Filter out client errors (4xx) — neplatné inputs, missing auth atd.
            # se nedají fixnout server-side a jen zahltí Sentry quota.
            try:
                exc_info = hint.get('exc_info') if hint else None
                if exc_info:
                    exc = exc_info[1]
                    # Werkzeug HTTPExceptions 4xx → ignore
                    code = getattr(exc, 'code', None)
                    if code and 400 <= code < 500:
                        return None
            except Exception:
                pass
            return event

        # Release version — git SHA pokud Railway poskytuje (slouží pro
        # source-map style attribution v Sentry: "X errors in release abc123").
        _release = (os.environ.get('RAILWAY_GIT_COMMIT_SHA', '') or '').strip()[:7]

        sentry_sdk.init(
            dsn=SENTRY_DSN,
            environment=SENTRY_ENV,
            release=f'inklink@{_release}' if _release else None,
            traces_sample_rate=0.1,    # 10 % requestů — performance monitoring
            profiles_sample_rate=0.0,  # profiling vypnuté (šetří kvótu)
            send_default_pii=False,    # neposílat IP, cookies, user agent
            attach_stacktrace=True,    # stack frames i u manuálních captures
            integrations=[FlaskIntegration()],
            before_send=_scrub,
            ignore_errors=[KeyboardInterrupt, SystemExit],
        )
        # Tags — viditelné v Sentry side panel
        sentry_sdk.set_tag('component', 'web')
        sentry_sdk.set_tag('platform', 'railway')
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

# ── Instagram ────────────────────────────────────────────────────────────────
# Instagram API with Instagram Login (profesionální účty). Basic Display API
# Meta vypnula v prosinci 2024, takže tudy cesta nevede.
# Bez vyplněných proměnných se propojení jen neukáže — appka jede dál.
INSTAGRAM_APP_ID     = os.environ.get('INSTAGRAM_APP_ID', '').strip()
INSTAGRAM_APP_SECRET = os.environ.get('INSTAGRAM_APP_SECRET', '').strip()
INSTAGRAM_SCOPES     = 'instagram_business_basic'


def _instagram_enabled() -> bool:
    return bool(INSTAGRAM_APP_ID and INSTAGRAM_APP_SECRET)


def _instagram_redirect_uri() -> str:
    # Musí se PŘESNĚ shodovat s tím, co je v nastavení aplikace u Mety —
    # jinak Instagram vrátí chybu ještě před přihlášením.
    return APP_BASE_URL.rstrip('/') + '/api/instagram/callback'


# ── Coming-soon brána ────────────────────────────────────────────────────────
# Doména je veřejná, ale produkt se ještě staví. Anonymní návštěvník uvidí
# jen stránku s waitlistem; dovnitř se dostane přihlášený uživatel (stačí se
# přihlásit, /login je vždy otevřené) nebo kdokoli s odkazem ?preview=<token>.
# Ovládá se env proměnnou, ne deployem — vypnout jde okamžitě.
COMING_SOON       = os.environ.get('COMING_SOON', '').strip().lower() in ('1', 'true', 'yes')
COMING_SOON_TOKEN = os.environ.get('COMING_SOON_TOKEN', '').strip()
PREVIEW_COOKIE    = 'il_preview'

# Cesty, které brána NIKDY nesmí chytit. Není to kosmetika: webhook zablokovaný
# bránou znamená ztracené platby a health check znamená, že Railway prohlásí
# deploy za mrtvý a vrátí předchozí verzi.
_GATE_ALWAYS_OPEN = (
    '/__health', '/healthz', '/robots.txt', '/sitemap.xml', '/favicon.svg',
    '/manifest.json', '/theme.css', '/i18n.js', '/icons.svg', '/mobile-nav.js',
    '/sw.js', '/notifs.js',
    # Přihlášení a obnova hesla musí zůstat průchozí, jinak se dovnitř
    # nedostane ani ten, kdo účet má.
    '/login', '/verify', '/forgot-password', '/reset-password',
    # Právní stránky taky: coming-soon na zásady odkazuje jako na právní
    # základ pro sběr e-mailu do waitlistu. Odkaz končící v bráně by ten
    # základ zrušil.
    '/privacy', '/terms',
)
_GATE_OPEN_PREFIXES = ('/api/stripe/', '/api/webhook', '/uploads/', '/static/')
_GATE_OPEN_API = (
    '/api/login', '/api/register', '/api/logout', '/api/me',
    '/api/verify', '/api/forgot-password', '/api/reset-password',
    # Waitlist je celý smysl coming-soon stránky — bránou projít musí.
    '/api/waitlist',
)

_GATE_ASSET_EXT = (
    'css', 'js', 'svg', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'ico',
    'woff', 'woff2', 'ttf', 'map', 'json', 'webmanifest',
)


def _gate_is_open_path(path: str) -> bool:
    if path in _GATE_ALWAYS_OPEN or path in _GATE_OPEN_API:
        return True
    if path.startswith(_GATE_OPEN_PREFIXES):
        return True
    # Statická aktiva podle přípony — bez nich by stránka přišla o styl.
    return '.' in path and path.rsplit('.', 1)[-1].lower() in _GATE_ASSET_EXT


@app.before_request
def _coming_soon_gate():
    if not COMING_SOON:
        return None
    path = request.path or '/'
    if _gate_is_open_path(path):
        return None
    # Kdo je přihlášený, je dovnitř pozvaný — včetně demo tatérky.
    if session.get('user_id'):
        return None
    # Preview odkaz: ?preview=<token> nastaví cookie, ať se link nemusí
    # posílat při každém kliknutí znovu.
    token = (request.args.get('preview') or '').strip()
    if COMING_SOON_TOKEN and token == COMING_SOON_TOKEN:
        resp = redirect(path)
        resp.set_cookie(PREVIEW_COOKIE, COMING_SOON_TOKEN, max_age=30 * 24 * 3600,
                        httponly=True, samesite='Lax',
                        secure=(request.scheme == 'https'))
        return resp
    if COMING_SOON_TOKEN and request.cookies.get(PREVIEW_COOKIE) == COMING_SOON_TOKEN:
        return None

    # API dostane JSON, ne HTML — jinak by frontend parsoval stránku.
    if path.startswith('/api/'):
        return jsonify({'error': 'InkLink is not open to the public yet.'}), 503
    return send_from_directory('public', 'coming-soon.html')


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
STRIPE_PUBLIC_KEY     = (os.environ.get('STRIPE_PUBLISHABLE_KEY', '') or
                         os.environ.get('STRIPE_PUBLIC_KEY', ''))
STRIPE_PRO_PRICE_ID   = os.environ.get('STRIPE_PRO_PRICE_ID', '')
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
    # Pin Stripe API version explicitly. Without this, every Stripe release
    # silently changes behavior. Bump deliberately in PRs after testing.
    # https://stripe.com/docs/api/versioning
    stripe.api_version = '2024-12-18.acacia'

RESEND_API_KEY    = os.environ.get('RESEND_API_KEY', '')
VAPID_PUBLIC_KEY  = os.environ.get('PUSH_PUBLIC', '')
VAPID_PRIVATE_KEY = os.environ.get('PUSH_PRIVATE', '')

# APNs (iOS push) — token-based auth via .p8 key from Apple Developer Portal.
# Set APNS_KEY_PEM as the full PEM string (Railway env supports multi-line)
# OR APNS_KEY_PATH pointing to a mounted .p8 file.
APNS_KEY_ID    = os.environ.get('APNS_KEY_ID', '')
APNS_TEAM_ID   = os.environ.get('APNS_TEAM_ID', '')
APNS_BUNDLE_ID = os.environ.get('APNS_BUNDLE_ID', 'club.inklink.app')
APNS_KEY_PEM   = os.environ.get('APNS_KEY_PEM', '')
APNS_KEY_PATH  = os.environ.get('APNS_KEY_PATH', '')
APNS_USE_SANDBOX = os.environ.get('APNS_USE_SANDBOX', '0') == '1'

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


def delete_upload(filename):
    """Smaže objekt z úložiště. Vrací True, když je pryč.

    Existuje kvůli GDPR výmazu: vynulovat jen cestu v DB nestačí, protože
    objekt v R2 zůstane veřejně adresovatelný pro kohokoli, kdo URL zná.
    """
    if not filename:
        return False
    # Cesta smí být jen holé jméno souboru — i když jméno prošlo DB, nesmí
    # z něj jít '../' ven z uploads/.
    filename = os.path.basename(filename)
    try:
        if _s3 and R2_BUCKET:
            _s3.delete_object(Bucket=R2_BUCKET, Key=filename)
        else:
            path = os.path.join(UPLOAD_FOLDER, filename)
            if os.path.exists(path):
                os.remove(path)
        return True
    except Exception as e:
        # Volající (výmaz) musí vědět, že objekt mohl zůstat — proto se
        # tahle chyba na rozdíl od jiných logů vrací, ne jen loguje.
        try:
            app.logger.error(f'[storage] delete failed for {filename}: {e}')
        except Exception:
            print(f'[storage] delete failed for {filename}: {e}')
        return False

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

    if event == 'reschedule_requested_for_artist':
        subject = f'InkLink — {ctx.get("other_name") or "A client"} asks to move a booking'
        rows = [
            ('Client',   other),
            ('Currently', _h(ctx.get('current_when') or '')),
            ('Asks for', when),
        ]
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p><strong>{other}</strong> asked to move a booking. '
            f'Nothing changes until you approve it.</p>'
            + '<table style="margin:14px 0;font-size:13px;line-height:1.9">'
            + ''.join(f'<tr><td style="color:#888;padding-right:14px;vertical-align:top">{k}:</td>'
                      f'<td>{v}</td></tr>' for k, v in rows)
            + '</table>'
            + cta('Review the request')
        )
        return subject, header + body + footer

    if event == 'design_request_for_artist':
        subject = f'InkLink — {ctx.get("other_name") or "A client"} wants a custom design'
        rows = [
            ('From',      other),
            ('Motif',     _h(ctx.get('motif') or '')),
            ('Placement', _h(ctx.get('placement') or '')),
            ('Size',      _h(ctx.get('size') or '')),
            ('Budget',    _h(ctx.get('budget') or '')),
            ('Timing',    _h(ctx.get('timing') or '')),
            ('References', f'{ctx["photos"]} photo(s) in the thread' if ctx.get('photos') else ''),
        ]
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p><strong>{other}</strong> asked you for a custom design. '
            f'The full request is waiting in your messages.</p>'
            + '<table style="margin:14px 0;font-size:13px;line-height:1.9">'
            + ''.join(f'<tr><td style="color:#888;padding-right:14px;vertical-align:top">{k}:</td>'
                      f'<td>{v}</td></tr>' for k, v in rows if v)
            + '</table>'
            + cta('Reply to the client')
        )
        return subject, header + body + footer

    if event == 'booking_offer_for_client':
        subject = f'InkLink — {ctx.get("other_name") or "Your artist"} offers you a date'
        rows = [
            ('Artist',   other),
            ('When',     when),
            ('Duration', _h(ctx.get('duration') or '')),
            ('Price',    _h(ctx.get('price') or '')),
            ('Valid until', _h(ctx.get('valid_until') or '')),
            ('Note',     note),
        ]
        body = (
            f'<p>Hi <strong>{name}</strong>,</p>'
            f'<p><strong>{other}</strong> offered you a specific date for the work '
            f'you agreed on. It is yours once you pay the deposit — until then the '
            f'time stays open to everyone.</p>'
            + '<table style="margin:14px 0;font-size:13px;line-height:1.9">'
            + ''.join(f'<tr><td style="color:#888;padding-right:14px;vertical-align:top">{k}:</td>'
                      f'<td>{v}</td></tr>' for k, v in rows if v)
            + '</table>'
            + cta('Open the offer')
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


def _welcome_email_html(stage: int, is_artist: bool, display_name: str) -> tuple:
    """Build (subject, html) for welcome stages. stage in (1, 2, 3). Czech copy."""
    from html import escape as _h
    base = APP_BASE_URL or 'https://www.inklink.club'
    name = _h(display_name or 'tam')
    container = ('background:#0a0a0a;color:#ccc;font-family:Helvetica,Arial,sans-serif;'
                 'max-width:560px;margin:0 auto;padding:24px;border:1px solid #1a1a1a')
    btn = ('display:inline-block;padding:11px 18px;background:#c62828;color:#fff;'
           'text-decoration:none;letter-spacing:0.08em;font-size:13px')
    h1 = 'color:#eee;font-size:22px;letter-spacing:0.06em;margin:0 0 12px'
    p = 'color:#bbb;font-size:14px;line-height:1.7;margin:0 0 12px'
    li = 'color:#bbb;font-size:14px;line-height:1.7;margin-bottom:6px'
    footer = ('color:#555;font-size:11px;letter-spacing:0.06em;margin-top:32px;'
              'padding-top:16px;border-top:1px solid #1a1a1a')
    footer_html = (
        f'<p style="{footer}">InkLink · '
        f'<a href="{base}/privacy" style="color:#777">Privacy</a> · '
        f'<a href="{base}/terms" style="color:#777">Terms</a></p>')

    if stage == 1:
        subject = 'Vítej v InkLink'
        body = f'''
        <h1 style="{h1}">Vítej, {name}</h1>
        <p style="{p}">Díky, že jsi se přidal/a k InkLink — marketplace, kde tetování má férová pravidla
        a žádné DM ping-pongy.</p>
        <p style="{p}"><b style="color:#eee">Jak to funguje:</b></p>
        <ul style="padding-left:18px">
          <li style="{li}">Procházej feed skic a portfólia tatérů</li>
          <li style="{li}">Klikni na termín v kalendáři tatéra a rezervuj přes Stripe</li>
          <li style="{li}">Záloha je nevratná podle pravidel storna (96 / 48 h)</li>
          <li style="{li}">Doplatek řešíte na místě nebo přes InkLink</li>
        </ul>
        <p style="{p}"><a href="{base}" style="{btn}">Otevřít InkLink</a></p>'''
    elif stage == 2:
        if is_artist:
            subject = 'Tipy pro tatéry — jak rozjet kalendář'
            body = f'''
            <h1 style="{h1}">Tipy pro tatéry, {name}</h1>
            <p style="{p}">Pár věcí, co tě posune na první rezervaci:</p>
            <ul style="padding-left:18px">
              <li style="{li}"><b style="color:#eee">Doplň profil</b> — bio, styly, hodinová sazba.
              Klient má větší šanci kliknout když vidí konkrétní čísla.</li>
              <li style="{li}"><b style="color:#eee">Nahraj 5+ prací</b> do portfolia. Doneseš tím
              filtr stylů a klienti tě najdou.</li>
              <li style="{li}"><b style="color:#eee">Vytvoř první blok v kalendáři</b> (Nastavení tatéra
              → Kalendář). Můžeš nabízet jen part-time sloty.</li>
              <li style="{li}"><b style="color:#eee">Propoj Stripe Connect</b> — bez něj nemůžou klienti
              zaplatit zálohu, takže ani rezervovat. Trvá to ~10 min.</li>
            </ul>
            <p style="{p}"><a href="{base}/artist-setup" style="{btn}">Otevřít nastavení tatéra</a></p>'''
        else:
            subject = 'Najdi svého tatéra'
            body = f'''
            <h1 style="{h1}">Najdi svého tatéra, {name}</h1>
            <p style="{p}">Když nevíš kudy začít, máme tři cesty:</p>
            <ul style="padding-left:18px">
              <li style="{li}"><b style="color:#eee">Feed</b> — skicy a hotové práce v mapě
              tatérů kolem tebe.</li>
              <li style="{li}"><b style="color:#eee">Mapa</b> — filtruj podle města a stylu
              (blackwork, fineline, color, …).</li>
              <li style="{li}"><b style="color:#eee">Like → rezervace</b> — když ti něco padne
              do oka, otevři tatérův profil a klikni na volný termín.</li>
            </ul>
            <p style="{p}">Záloha je zpravidla 30 % a chrání jak tebe, tak tatéra. Při storně 96 h
            předem máš 100 % zpět.</p>
            <p style="{p}"><a href="{base}" style="{btn}">Otevřít feed</a></p>'''
    elif stage == 3:
        if is_artist:
            subject = 'Týden v InkLink — kde jsi?'
            body = f'''
            <h1 style="{h1}">Týden v InkLink, {name}</h1>
            <p style="{p}">Pár věcí co možná stojí za podívání:</p>
            <ul style="padding-left:18px">
              <li style="{li}"><b style="color:#eee">Founding artist program</b> — prvních 30 dnů
              0 % provize, poté 5 % do dne 90. Začíná až tvou první splněnou rezervací.</li>
              <li style="{li}"><b style="color:#eee">Sdílej svůj profil</b> — máš krátký link
              <code style="color:#eee">{base}/@username</code> co můžeš dát na IG bio.</li>
              <li style="{li}"><b style="color:#eee">Mobilní app</b> — iOS a Android přijde brzy.
              Push notifikace na nové rezervace.</li>
            </ul>
            <p style="{p}"><a href="{base}/artist-setup" style="{btn}">Otevřít nastavení</a></p>'''
        else:
            subject = 'Pár tipů pro první rezervaci'
            body = f'''
            <h1 style="{h1}">Pár tipů, {name}</h1>
            <p style="{p}">Kdyby jsi pořád váhal/a koho oslovit:</p>
            <ul style="padding-left:18px">
              <li style="{li}"><b style="color:#eee">Recenze</b> — každý tatér má veřejné hodnocení
              od reálných klientů. Klikni na hvězdičky v profilu.</li>
              <li style="{li}"><b style="color:#eee">Founding client</b> — prvních 500 registrací
              má service fee navždy zdarma. Pokud čteš tenhle mail, pravděpodobně tam patříš.</li>
              <li style="{li}"><b style="color:#eee">Discount kód?</b> Pokud máš promo, zadáš ho
              v platebním modalu před zálohou.</li>
            </ul>
            <p style="{p}"><a href="{base}" style="{btn}">Otevřít InkLink</a></p>'''
    else:
        return ('', '')

    html = f'<div style="background:#000;padding:24px 0"><div style="{container}">{body}{footer_html}</div></div>'
    return (subject, html)


def send_welcome_email_for(conn, user_id: int, stage: int) -> bool:
    """Send one welcome email stage. Returns True if Resend accepted it."""
    if not RESEND_API_KEY:
        return False
    try:
        u = conn.execute(
            'SELECT email, display_name, COALESCE(is_artist, 0) AS is_artist FROM users WHERE id=?',
            (user_id,)
        ).fetchone()
        if not u or not u['email']:
            return False
        subject, html = _welcome_email_html(stage, bool(u['is_artist']), u['display_name'] or '')
        if not subject:
            return False
        return send_email(u['email'], subject, html)
    except Exception as e:
        print(f'[welcome_email] stage={stage} user={user_id} err={e}')
        return False


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

    @property
    def rowcount(self): return self._cur.rowcount


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
                # Per-artist storno lhůty. NULL = použij globální
                # CANCEL_REFUND_FULL_HOURS / CANCEL_REFUND_HALF_HOURS.
                'cancel_refund_full_hours INTEGER DEFAULT NULL',
                'cancel_refund_half_hours INTEGER DEFAULT NULL',
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
                'is_admin INTEGER DEFAULT 0',
                # Welcome email sequence (3 stages, cron-driven). 0=not started,
                # 1=welcome sent, 2=tips sent (day 2), 3=re-engagement sent (done).
                'welcome_email_stage INTEGER DEFAULT 0',
                'welcome_email_next_at TEXT DEFAULT NULL',
                # Soft deletion (GDPR right to erasure). Two-stage:
                # 1) user requests → deletion_requested_at set, 30-day grace.
                # 2) cron after 30 days → PII scrubbed, deleted_at set.
                # Accounting data (bookings, economics_snapshots) is preserved
                # by FK; only the user row is anonymized.
                'deletion_requested_at TEXT DEFAULT NULL',
                'deleted_at TEXT DEFAULT NULL',
                # Artist liability consent — must be accepted before profile
                # save switches the account into is_artist=1. Stores ISO
                # timestamp of acceptance (NULL = not accepted).
                'artist_terms_accepted_at TEXT DEFAULT NULL'):
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

    # Zpráva může nést nabídku termínu — pak ji vlákno vykreslí jako kartu
    # místo bubliny. Odkaz na zprávě (ne naopak) drží pořadí v konverzaci
    # bez slučování dvou seznamů.
    add_col('messages', 'offer_id INTEGER DEFAULT NULL')

    # Nabídka termínu: tatér se s klientem domluví v chatu na custom práci
    # a pošle mu konkrétní termín a cenu. Slot zabere až přijetí — držet ho
    # od odeslání by z každé zapomenuté nabídky udělalo díru v kalendáři.
    c.execute('''CREATE TABLE IF NOT EXISTS booking_offers (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_id        INTEGER NOT NULL,
        client_id        INTEGER NOT NULL,
        slot_id          INTEGER NOT NULL,
        booking_start_at TEXT NOT NULL,
        duration_hours   REAL NOT NULL,
        price_kc         INTEGER NOT NULL,
        note             TEXT DEFAULT '',
        status           TEXT DEFAULT 'pending',
        booking_id       INTEGER DEFAULT NULL,
        created_slot     INTEGER DEFAULT 0,
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    add_col('booking_offers', 'created_slot INTEGER DEFAULT 0')
    # Nabídka platí týden. Bez toho by termín nabídnutý na příští jaro
    # visel v kalendáři půl roku, než by ho vypršení samo uklidilo.
    add_col('booking_offers', 'expires_at TEXT DEFAULT NULL')
    c.execute('CREATE INDEX IF NOT EXISTS idx_offers_pair '
              'ON booking_offers(artist_id, client_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_offers_client '
              'ON booking_offers(client_id, status)')

    c.execute('''CREATE TABLE IF NOT EXISTS favorite_cities (
        user_id  INTEGER NOT NULL,
        name     TEXT NOT NULL,
        lat      REAL NOT NULL,
        lng      REAL NOT NULL,
        PRIMARY KEY (user_id, name),
        FOREIGN KEY (user_id) REFERENCES users(id)
    )''')

    # ── events / event_saves (kept) ─────────────────────────────────────────
    # ── instagram_accounts ──────────────────────────────────────────────────
    # Propojení tatérova Instagramu. Jeden účet na uživatele: kdyby jich bylo
    # víc, "ze kterého importujeme" je otázka, na kterou UI nemá odpověď.
    #
    # Token je v DB v otevřené podobě. Vědomé rozhodnutí: je to read-only
    # rozsah nad médii, která tatér stejně zveřejňuje na Instagramu, platí
    # 60 dní a jde odvolat z obou stran. Kdyby sem někdy přibyl zápis
    # (publikování za tatéra), tohle je místo, kde se musí přidat šifrování.
    c.execute("""CREATE TABLE IF NOT EXISTS instagram_accounts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL UNIQUE,
        ig_user_id    TEXT NOT NULL,
        username      TEXT DEFAULT '',
        access_token  TEXT NOT NULL,
        token_expires_at TEXT DEFAULT NULL,
        connected_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_import_at TEXT DEFAULT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )""")

    # Které médium už jsme importovali. Bez toho by opakovaný import
    # portfolio duplikoval — a tatér by mazal ručně.
    c.execute("""CREATE TABLE IF NOT EXISTS instagram_imports (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        ig_media_id TEXT NOT NULL,
        portfolio_item_id INTEGER DEFAULT NULL,
        imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, ig_media_id)
    )""")
    c.execute('CREATE INDEX IF NOT EXISTS idx_ig_imports_user ON instagram_imports(user_id)')

    # ── waitlist (coming-soon stránka) ──────────────────────────────────────
    # Sbíráme jen e-mail a nepovinnou roli. Žádné jméno, žádný profil —
    # čím míň osobních údajů před spuštěním, tím míň povinností navíc.
    c.execute("""CREATE TABLE IF NOT EXISTS waitlist (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        email      TEXT NOT NULL UNIQUE,
        role       TEXT DEFAULT '',   -- artist | client | ''
        source     TEXT DEFAULT '',   -- odkud přišel zápis
        ip         TEXT DEFAULT '',   -- proti zneužití, ne pro marketing
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    c.execute('CREATE INDEX IF NOT EXISTS idx_waitlist_created ON waitlist(created_at)')

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

    # /events je veřejná (a v sitemapě) — filtr přes datum nesmí být full scan.
    c.execute('CREATE INDEX IF NOT EXISTS idx_events_date ON events(date)')

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
    # 'web' = browser VAPID push (endpoint = URL, p256dh + auth filled).
    # 'apns' = iOS native (endpoint = device token, p256dh + auth empty).
    # 'fcm'  = Android native (endpoint = registration token, p256dh + auth empty).
    add_col('push_subscriptions', "provider TEXT DEFAULT 'web'")
    add_col('push_subscriptions', "platform TEXT DEFAULT ''")
    conn.commit()

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

    # Tatér může u jedné skici nabídnout až tři velikosti, každou za svou
    # cenu. Vlastní tabulka místo šesti sloupců na položce: velikost je
    # tím pádem záznam se stejným tvarem jako kdekoliv jinde a přidat
    # čtvrtou by znamenalo změnit konstantu, ne schéma.
    #
    # portfolio_items.price_kc / estimated_hours zůstávají a drží NEJLEVNĚJŠÍ
    # variantu ("od X Kč"). Bez toho by se rozbilo řazení feedu podle ceny,
    # OG obrázky i rezervace skic bez variant.
    c.execute('''CREATE TABLE IF NOT EXISTS portfolio_item_sizes (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id         INTEGER NOT NULL,
        size_label      TEXT NOT NULL,
        price_kc        INTEGER NOT NULL,
        estimated_hours REAL NOT NULL
    )''')
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_pis_item_size '
              'ON portfolio_item_sizes(item_id, size_label)')

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
    # Buffer = odstup mezi rezervacemi (úklid, příprava). Platí jen na
    # rozestup mezi rezervacemi/blokacemi, nemusí se vejít do slotu samotného.
    add_col('slots', 'buffer_before_minutes INTEGER DEFAULT 0')
    add_col('slots', 'buffer_after_minutes INTEGER DEFAULT 0')
    # Soukromý termín vzniká z nabídky v chatu a je jen pro toho klienta.
    # Nikde se veřejně nenabízí — jinak by ho mezitím vzal někdo jiný a
    # tatér by slíbil čas, který už nemá.
    add_col('slots', 'is_private INTEGER DEFAULT 0')
    add_col('slots', "currency TEXT DEFAULT 'CZK'")

    # InkLink Premium — placený tarif jednotlivého tatéra.
    # premium_until je datum, do kdy má zaplaceno; zrušení předplatného
    # ho nezkracuje, jen se přestane prodlužovat.
    # Měna tatéra. Termíny, ceníky i rezervace ji dědí; změna se projeví
    # až na nově vypsaných termínech, aby se nepřepsaly už slíbené ceny.
    add_col('users', "currency TEXT DEFAULT 'CZK'")
    add_col('users', 'premium_until TEXT DEFAULT NULL')
    add_col('users', 'premium_customer_id TEXT DEFAULT NULL')
    add_col('users', 'premium_subscription_id TEXT DEFAULT NULL')
    add_col('users', 'premium_cancel_at_period_end INTEGER DEFAULT 0')
    c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_id  INTEGER NOT NULL,
        subject    TEXT NOT NULL,
        body       TEXT NOT NULL,
        tag        TEXT DEFAULT '',
        recipients INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_campaigns_artist ON campaigns(artist_id)')

    # Hojení — automatická sekvence po sezení (premium).
    # Text instrukcí píše tatér: každý má svůj protokol (fólie vs. Second
    # Skin, jiná mast) a platforma nemá co radit v něčem zdravotním.
    add_col('users', 'aftercare_enabled INTEGER DEFAULT 1')
    add_col('users', 'aftercare_text TEXT DEFAULT ""')
    c.execute('''CREATE TABLE IF NOT EXISTS aftercare_sent (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id INTEGER NOT NULL,
        step       TEXT NOT NULL,
        sent_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    # Idempotence: cron běží denně a Stripe-style opakování nechceme řešit
    # pokaždé znovu. Jeden krok na rezervaci, jednou.
    c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_aftercare_once '
              'ON aftercare_sent(booking_id, step)')

    # ── Blokace volna (dovolená, nemoc, jednorázové "tady nejsem") ─────────
    # Vlastní tabulka, ne status na slots: kontrola překryvů je tu v rozsahu
    # tatéra (napříč všemi jeho sloty), ne v rámci jednoho slot_id, a slots
    # nese 5 sloupců o ceně, které pro blokaci nedávají smysl.
    c.execute('''CREATE TABLE IF NOT EXISTS artist_blocked_time (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_id   INTEGER NOT NULL,
        start_at    TEXT NOT NULL,
        end_at      TEXT NOT NULL,
        reason      TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (artist_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_blocked_artist ON artist_blocked_time(artist_id)')

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
    # Sprint 1 LITE: track deposit PI retry attempts. Used to rotate
    # idempotency_key on retry (otherwise Stripe returns the same failed PI).
    add_col('bookings', 'payment_attempts INTEGER DEFAULT 0')
    # Sprint 2: multi-session série. parent_booking_id ukazuje vždy na první
    # sezení (řetěz se plochý, nezanořuje se), session_number je 1-based.
    add_col('bookings', 'parent_booking_id INTEGER DEFAULT NULL')
    add_col('bookings', 'session_number INTEGER NOT NULL DEFAULT 1')
    # Snapshot bufferů ze slotu v okamžiku rezervace — pozdější změna slotu
    # nesmí retroaktivně měnit kolizní pravidla už existujících rezervací.
    add_col('bookings', 'buffer_before_minutes INTEGER NOT NULL DEFAULT 0')
    add_col('bookings', 'buffer_after_minutes INTEGER NOT NULL DEFAULT 0')
    add_col('bookings', "internal_note TEXT DEFAULT ''")  # jen pro tatéra, klient nevidí
    c.execute('CREATE INDEX IF NOT EXISTS idx_bookings_parent ON bookings(parent_booking_id)')

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

    # subscription_tier: B2B plán studia — 'free' | 'studio' | 'studio_pro'.
    # Nesouvisí s pricing/admin.py `tier` (founding vs standard artist,
    # per-artist komisní program) — jiná osa, schválně jiný název sloupce.
    add_col('studios', "subscription_tier TEXT NOT NULL DEFAULT 'free'")

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

    # studio_id na bookings — snapshot studia, ve kterém rezervace vznikla.
    # Vyplňuje se při INSERTu (create_booking / follow-up), NE backfillem:
    # dřívější `UPDATE ... WHERE studio_id IS NULL` na každém startu procesu
    # zpětně přepisoval historii tatéra, když vstoupil do studia, a nikdy ji
    # neuklidil, když odešel. Účetnictví (Sprint 4/6) čte právě tenhle sloupec,
    # takže musí zůstat tím, čím byl v okamžiku rezervace.
    # Nullable: sólo tatér žádné studio nemá.
    add_col('bookings', 'studio_id INTEGER DEFAULT NULL')
    # Klient může sekvenci hojení kdykoliv zastavit; platí pro tuhle rezervaci.
    add_col('bookings', 'aftercare_optout_at TEXT DEFAULT NULL')
    # Kolik z rezervace pokryl kredit a kolik kvůli tomu dlužíme tatérovi.
    # Držet to na rezervaci, ne dopočítávat: až se to bude vyrovnávat,
    # musí být jasné za co.
    add_col('bookings', 'credit_used_cents INTEGER DEFAULT 0')
    add_col('bookings', 'platform_owes_artist_cents INTEGER DEFAULT 0')
    add_col('bookings', 'platform_settled_at TEXT DEFAULT NULL')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bookings_studio ON bookings(studio_id)')
    # CRM (Sprint 3) i historie klienta jezdí po obou těchhle sloupcích.
    c.execute('CREATE INDEX IF NOT EXISTS idx_bookings_artist ON bookings(artist_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_bookings_client ON bookings(client_id)')

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

    # ── CRM (Sprint 3) ─────────────────────────────────────────────────────
    # Klienta vlastní TATÉR (artist_id), ne studio. Viditelnost napříč studiem
    # se počítá za běhu (viz _crm_visible_artist_ids) — denormalizovaný
    # studio_id by u sólo tatérů (dnes prakticky všech) nedával smysl a stárnul
    # by při vstupu/odchodu ze studia. Dělící čára: peníze jsou studiové
    # (bookings.studio_id, nehýbe se), vztah je tatérův a odchází s ním.
    c.execute('''CREATE TABLE IF NOT EXISTS clients (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        artist_id          INTEGER NOT NULL,
        user_id            INTEGER DEFAULT NULL,   -- NULL = klient bez účtu (walk-in, telefon)
        name               TEXT DEFAULT '',        -- čte se JEN když user_id IS NULL
        email              TEXT DEFAULT '',        -- jinak je zdrojem pravdy users
        phone              TEXT DEFAULT '',
        tags               TEXT DEFAULT '',        -- CSV, jako users.styles
        style_preferences  TEXT DEFAULT '',
        acquisition_source TEXT DEFAULT '',        -- inklink|instagram|walk_in|referral|other
        note               TEXT DEFAULT '',        -- krátká připnutá poznámka
        created_by         INTEGER NOT NULL,       -- kdo řádek založil (ve studiu ≠ artist_id)
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        anonymized_at      TEXT DEFAULT NULL,
        FOREIGN KEY (artist_id) REFERENCES users(id),
        FOREIGN KEY (user_id)   REFERENCES users(id)
    )''')
    # Odhlášení z rozesílek. Váže se na dvojici klient–tatér, ne na adresu:
    # souhlas dal klient konkrétnímu tatérovi, ne celé platformě.
    add_col('clients', 'marketing_optout_at TEXT DEFAULT NULL')
    c.execute('CREATE INDEX IF NOT EXISTS idx_clients_artist ON clients(artist_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_clients_user ON clients(user_id)')
    # Částečný unikátní index: bez něj dvě souběžné rezervace téhož klienta
    # obě minou SELECT a obě založí řádek. Stejná syntaxe v SQLite i Postgresu.
    c.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_artist_user
                 ON clients(artist_id, user_id) WHERE user_id IS NOT NULL''')

    # Bez soft delete: měkce smazaná poznámka je pořád PII v databázi, což je
    # přesně to, co má výmaz klienta odstranit.
    c.execute('''CREATE TABLE IF NOT EXISTS client_notes (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id  INTEGER NOT NULL,
        author_id  INTEGER NOT NULL,
        body       TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (client_id) REFERENCES clients(id),
        FOREIGN KEY (author_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_client_notes_client ON client_notes(client_id)')

    c.execute('''CREATE TABLE IF NOT EXISTS tattoo_records (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id        INTEGER NOT NULL,
        booking_id       INTEGER DEFAULT NULL,   -- NULL = práce z doby před InkLinkem
        artist_id        INTEGER NOT NULL,
        session_date     TEXT NOT NULL,          -- YYYY-MM-DD, pražský wall-clock
        body_location    TEXT DEFAULT '',        -- při výmazu se maže
        style            TEXT DEFAULT '',
        size_label       TEXT DEFAULT '',
        description      TEXT DEFAULT '',        -- při výmazu se maže
        healed_photo     TEXT DEFAULT '',        -- při výmazu se maže i objekt v úložišti
        aftercare_status TEXT DEFAULT '',        -- prostý text, stavový automat až Sprint 5
        price_czk        INTEGER DEFAULT NULL,   -- při výmazu ZŮSTÁVÁ (účetnictví)
        created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        anonymized_at    TEXT DEFAULT NULL,
        FOREIGN KEY (client_id)  REFERENCES clients(id),
        FOREIGN KEY (booking_id) REFERENCES bookings(id),
        FOREIGN KEY (artist_id)  REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tattoo_records_client ON tattoo_records(client_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_tattoo_records_booking ON tattoo_records(booking_id)')

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

    # ── Pricing module — founding flags + account credits ──────────────────
    # See pricing/config.py for rate definitions. These columns store the
    # PER-USER status that pricing.calculate_booking_economics() reads.
    add_col('users', 'founding_artist INTEGER DEFAULT 0')
    add_col('users', 'founding_client INTEGER DEFAULT 0')
    add_col('users', 'founding_artist_started_at TEXT DEFAULT NULL')
    # Account credit accumulated from referrals (haler). Spent on future
    # bookings as if it were a discount. Refilled when referrer's referred
    # client completes their first booking.
    add_col('users', 'account_credit_cents INTEGER DEFAULT 0')
    # Kniha pohybů kreditu. Zůstatek na uživateli je jen rychlé čtení —
    # doložit, odkud cizí peníze přišly a kam šly, umí jen tohle.
    c.execute('''CREATE TABLE IF NOT EXISTS credit_ledger (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     INTEGER NOT NULL,
        delta_cents INTEGER NOT NULL,
        reason      TEXT NOT NULL,
        ref_type    TEXT DEFAULT '',
        ref_id      INTEGER DEFAULT NULL,
        note        TEXT DEFAULT '',
        created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_credit_ledger_user '
              'ON credit_ledger(user_id, id)')

    # Dárkové poukazy. Neuplatněné jsou závazek, ne tržba — dokud je někdo
    # neutratí, dlužíme jejich hodnotu.
    c.execute('''CREATE TABLE IF NOT EXISTS vouchers (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        code           TEXT NOT NULL UNIQUE,
        amount_cents   INTEGER NOT NULL,
        buyer_id       INTEGER,
        recipient_name TEXT DEFAULT '',
        message        TEXT DEFAULT '',
        status         TEXT DEFAULT 'awaiting_payment',
        payment_intent TEXT DEFAULT NULL,
        redeemed_by    INTEGER DEFAULT NULL,
        redeemed_at    TEXT DEFAULT NULL,
        expires_at     TEXT NOT NULL,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_vouchers_buyer ON vouchers(buyer_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_vouchers_status ON vouchers(status)')
    add_col('vouchers', "currency TEXT DEFAULT 'CZK'")

    # Platformní nastavení. Klíč/hodnota schválně: přidat další volbu má
    # být záležitost jednoho řádku, ne migrace.
    c.execute('''CREATE TABLE IF NOT EXISTS app_settings (
        key        TEXT PRIMARY KEY,
        value      TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ── economics_snapshots — immutable per-booking ledger entry ───────────
    # Created at PaymentIntent creation. NEVER updated — refunds/disputes
    # create NEW rows linked to the same booking. Critical for audit trail.
    c.execute('''CREATE TABLE IF NOT EXISTS economics_snapshots (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id   INTEGER NOT NULL,
        kind         TEXT NOT NULL,        -- 'initial' | 'refund' | 'adjust'
        snapshot     TEXT NOT NULL,        -- JSON of Economics dict
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (booking_id) REFERENCES bookings(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_econ_snap_booking ON economics_snapshots(booking_id)')

    # ── Stripe webhook idempotency ─────────────────────────────────────────
    # Stripe retries webhooks aggressively. Insert event_id BEFORE processing.
    # If insert fails (duplicate PK), return 200 OK and skip. Without this,
    # the same payment_intent.succeeded can credit the booking twice.
    c.execute('''CREATE TABLE IF NOT EXISTS processed_stripe_events (
        event_id     TEXT PRIMARY KEY,
        event_type   TEXT NOT NULL,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # ── Booking state machine audit trail ──────────────────────────────────
    # One row per successful transition_booking() call. `from_status` is
    # best-effort (read just before the guarded UPDATE, not part of the same
    # atomic statement) — fine for an audit trail, not used for any guard.
    c.execute('''CREATE TABLE IF NOT EXISTS booking_status_log (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id   INTEGER NOT NULL,
        from_status  TEXT,
        to_status    TEXT NOT NULL,
        changed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (booking_id) REFERENCES bookings(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_booking_status_log_booking ON booking_status_log(booking_id)')

    # ── Referrals — referrer ↔ referred client mapping ─────────────────────
    # The referrer's bonus credit is granted only when the referred client
    # completes their first booking — not at signup.
    c.execute('''CREATE TABLE IF NOT EXISTS referrals (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer_user_id   INTEGER NOT NULL,
        referred_user_id   INTEGER NOT NULL UNIQUE,
        code               TEXT,
        credit_granted_at  TIMESTAMP DEFAULT NULL,
        created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (referrer_user_id) REFERENCES users(id),
        FOREIGN KEY (referred_user_id) REFERENCES users(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id)')

    # ── Discount codes (general purpose, admin-issued MANUAL_PROMO + system) ──
    # WELCOME / REFERRAL are NOT rows here — they're code paths in the
    # engine. This table is for explicit one-off codes (e.g. SPRING25).
    c.execute('''CREATE TABLE IF NOT EXISTS discount_codes (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        code           TEXT NOT NULL UNIQUE,
        kind           TEXT NOT NULL,     -- 'MANUAL_PROMO'
        amount_czk     INTEGER NOT NULL,
        max_uses       INTEGER DEFAULT NULL,    -- NULL = unlimited
        used_count     INTEGER DEFAULT 0,
        expires_at     TEXT DEFAULT NULL,
        active         INTEGER DEFAULT 1,
        created_by     INTEGER,
        created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by) REFERENCES users(id)
    )''')

    # ── Discount redemptions — per-user usage tracking ─────────────────────
    # Stores "user X used code Y on booking Z" so we can enforce
    # "WELCOME once per client" and audit promo cost.
    c.execute('''CREATE TABLE IF NOT EXISTS discount_redemptions (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        booking_id      INTEGER NOT NULL,
        discount_type   TEXT NOT NULL,        -- 'WELCOME' | 'REFERRAL' | 'MANUAL_PROMO'
        discount_code   TEXT DEFAULT NULL,    -- FK-like, only for MANUAL_PROMO
        amount_czk      INTEGER NOT NULL,
        redeemed_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id)    REFERENCES users(id),
        FOREIGN KEY (booking_id) REFERENCES bookings(id)
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_disc_red_user ON discount_redemptions(user_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_disc_red_booking ON discount_redemptions(booking_id)')

    # ── Telemetry events — business events with JSON payloads ──────────────
    # Powers the net-take-rate dashboard. NOT for errors (we have logs).
    c.execute('''CREATE TABLE IF NOT EXISTS telemetry_events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        event_name   TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_telemetry_name ON telemetry_events(event_name)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry_events(created_at)')

    # ── Refund requests — client-initiated post-booking, artist/admin decides ──
    # Separate from cancellation (rules-based, auto-refund per timing). Used
    # when booking was already past, quality dispute, no-show, etc.
    c.execute('''CREATE TABLE IF NOT EXISTS refund_requests (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id    INTEGER NOT NULL,
        client_id     INTEGER NOT NULL,
        artist_id     INTEGER NOT NULL,
        amount_cents  INTEGER NOT NULL,
        reason        TEXT    NOT NULL,
        status        TEXT    NOT NULL DEFAULT 'pending',
        decision_by   INTEGER,
        decision_note TEXT    DEFAULT '',
        stripe_refund_id TEXT DEFAULT NULL,
        created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at   TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_refund_booking ON refund_requests(booking_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_refund_status ON refund_requests(status)')

    # ── Reschedule requests — klient chce přesunout pozdě (< RESCHEDULE_FREE_HOURS) ──
    # Stejný tvar jako refund_requests schválně: obojí je "žádost čekající na
    # rozhodnutí tatéra" a bookings.status se u ní nemění (přesun je ortogonální
    # k platebnímu/plnícímu stavu — confirmed rezervace s čekající žádostí je
    # pořád confirmed).
    c.execute('''CREATE TABLE IF NOT EXISTS booking_reschedule_requests (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        booking_id           INTEGER NOT NULL,
        requested_by         INTEGER NOT NULL,
        new_slot_id          INTEGER NOT NULL,
        new_booking_start_at TEXT    NOT NULL,
        new_booking_end_at   TEXT    NOT NULL,
        status               TEXT    NOT NULL DEFAULT 'pending',
        decision_by          INTEGER,
        decision_note        TEXT    DEFAULT '',
        created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at          TIMESTAMP
    )''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_reschedule_booking ON booking_reschedule_requests(booking_id)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_reschedule_status ON booking_reschedule_requests(status)')

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


_apns_client = None


def _get_apns_client():
    """Lazy singleton — apns2 token client. Returns None if not configured."""
    global _apns_client
    if _apns_client is not None:
        return _apns_client
    if not APNS_KEY_ID or not APNS_TEAM_ID:
        return None
    if not APNS_KEY_PEM and not APNS_KEY_PATH:
        return None
    try:
        from apns2.client import APNsClient
        from apns2.credentials import TokenCredentials
        import tempfile
        # apns2 expects a file path. If we have the PEM in env, write to /tmp once.
        key_path = APNS_KEY_PATH
        if not key_path and APNS_KEY_PEM:
            tmp = tempfile.NamedTemporaryFile(
                mode='w', suffix='.p8', delete=False, prefix='apns_key_'
            )
            tmp.write(APNS_KEY_PEM)
            tmp.close()
            key_path = tmp.name
        creds = TokenCredentials(
            auth_key_path=key_path,
            auth_key_id=APNS_KEY_ID,
            team_id=APNS_TEAM_ID,
        )
        _apns_client = APNsClient(credentials=creds, use_sandbox=APNS_USE_SANDBOX)
        return _apns_client
    except Exception as e:
        app.logger.error(f'[APNS] init failed: {e}')
        return None


def _send_apns_one(token: str, title: str, body: str, url: str) -> tuple:
    """Send one APNs notification. Returns (ok, should_delete)."""
    try:
        from apns2.payload import Payload
        from apns2.errors import BadDeviceToken, Unregistered, DeviceTokenNotForTopic
        client = _get_apns_client()
        if client is None:
            return (False, False)
        payload = Payload(alert={'title': title, 'body': body},
                          sound='default', badge=1, custom={'url': url})
        client.send_notification(token, payload, topic=APNS_BUNDLE_ID)
        return (True, False)
    except (BadDeviceToken, Unregistered, DeviceTokenNotForTopic):
        return (False, True)  # purge stale token
    except Exception as e:
        app.logger.error(f'[APNS] send failed for token …{token[-8:] if token else "?"}: {e}')
        return (False, False)


def send_push(user_id: int, title: str, body: str, url: str = '/'):
    """Fan-out push to all of a user's subscriptions (web VAPID + iOS APNs)."""
    try:
        conn = get_db()
        subs = conn.execute(
            "SELECT id, endpoint, p256dh, auth, COALESCE(provider, 'web') AS provider "
            "FROM push_subscriptions WHERE user_id = ?",
            (user_id,)
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f'[PUSH] db read failed: {e}')
        return

    dead_ids = []
    web_pem = None
    if VAPID_PRIVATE_KEY:
        try:
            import base64
            web_pem = base64.urlsafe_b64decode(VAPID_PRIVATE_KEY + '==')
        except Exception:
            web_pem = None

    for sub in subs:
        provider = sub['provider']
        if provider == 'web':
            if not web_pem:
                continue
            try:
                from pywebpush import webpush, WebPushException
                import json as _json
                payload = _json.dumps({'title': title, 'body': body, 'url': url})
                ep = sub['endpoint']
                aud = ep.split('/', 3)[2] if '/' in ep else ep
                webpush(
                    subscription_info={'endpoint': ep,
                                       'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}},
                    data=payload,
                    vapid_private_key=web_pem,
                    vapid_claims={'sub': 'mailto:admin@inklink.app', 'aud': 'https://' + aud},
                )
            except WebPushException as ex:
                if ex.response and ex.response.status_code in (404, 410):
                    dead_ids.append(sub['id'])
            except Exception as e:
                print(f'[PUSH/web] {e}')
        elif provider == 'apns':
            ok, purge = _send_apns_one(sub['endpoint'], title, body, url)
            if purge:
                dead_ids.append(sub['id'])
        # 'fcm' (Android) — TODO when we add Firebase

    if dead_ids:
        try:
            c2 = get_db()
            for sid in dead_ids:
                c2.execute('DELETE FROM push_subscriptions WHERE id = ?', (sid,))
            c2.commit()
            c2.close()
        except Exception as e:
            print(f'[PUSH] cleanup failed: {e}')


# Back-compat alias — callers still using send_web_push keep working.
send_web_push = send_push


def push_notif(conn, user_id, actor_id, notif_type, ref_id, ref_type, message):
    if user_id == actor_id:
        return
    conn.execute(
        'INSERT INTO notifications (user_id, actor_id, type, ref_id, ref_type, message) VALUES (?,?,?,?,?,?)',
        (user_id, actor_id, notif_type, ref_id, ref_type, message)
    )
    send_push(user_id, 'InkLink', message, '/')


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


SUBSCRIPTION_TIER_RANK = {'free': 0, 'studio': 1, 'studio_pro': 2}


# ── InkLink Premium ───────────────────────────────────────────────────────
#
# Placený tarif jednotlivého tatéra, ne studia. Stávající require_tier() je
# navázaný na studia a 403uje každého sólo tatéra — což je většina z nich —
# takže se pro tohle nedá použít.
#
# Premium přidává, nikdy neubírá: denní práce (kalendář, rezervace, zprávy,
# nabídky) zůstává celá zdarma. Za peníze je to, co tatér otevře jednou za
# měsíc — účetnictví, čísla, rozesílání.

PREMIUM_PRICE_CZK = int(os.environ.get('PREMIUM_PRICE_CZK', '390'))
PREMIUM_FEATURES  = ('accounting', 'stats', 'campaigns')


def _premium_until(conn, user_id):
    row = conn.execute('SELECT premium_until FROM users WHERE id=?', (user_id,)).fetchone()
    return (row['premium_until'] if row else None) or None


def _is_premium_from_row(row):
    until = (row.get('premium_until') if isinstance(row, dict) else row['premium_until']) or None
    if not until:
        return False
    try:
        return _naive_dt(until) > _prague_now_naive()
    except (ValueError, TypeError):
        return False


def _is_premium(conn, user_id):
    """Premium platí do data, které zaplatil. Zrušení předplatného nic
    neodebírá hned — za období, které má zaplacené, ho dostat má."""
    until = _premium_until(conn, user_id)
    if not until:
        return False
    try:
        return _naive_dt(until) > _prague_now_naive()
    except (ValueError, TypeError):
        return False


def require_premium():
    """Vrátí chybovou odpověď, nebo None. 402 schválně: 403 znamená
    'nemáš právo', tohle znamená 'ještě nezaplaceno' a frontend na to
    umí nabídnout předplatné."""
    err = require_login()
    if err: return err
    conn = get_db()
    ok = _is_premium(conn, session['user_id'])
    conn.close()
    if not ok:
        return jsonify({'error': 'InkLink Premium required', 'premium_required': True}), 402
    return None


def require_tier(min_tier, studio_id=None):
    """Guard for future B2B endpoints: caller must belong to a studio whose
    subscription_tier is >= min_tier. Not wired to any route yet — this lands
    the primitive ahead of Sprint 2's first studio-scoped endpoints, which
    will call it the same way cancel_booking calls require_login(). Resolves
    the studio from the logged-in user's studio_members row when `studio_id`
    isn't passed explicitly (a user belongs to at most one studio, per the
    UNIQUE constraint on studio_members.artist_id)."""
    err = require_login()
    if err: return err
    conn = get_db()
    if studio_id is None:
        sm = conn.execute('SELECT studio_id FROM studio_members WHERE artist_id=?',
                           (session['user_id'],)).fetchone()
        studio_id = sm['studio_id'] if sm else None
    if not studio_id:
        conn.close()
        return jsonify({'error': 'Not part of a studio'}), 403
    studio = conn.execute('SELECT subscription_tier FROM studios WHERE id=?', (studio_id,)).fetchone()
    conn.close()
    current = studio['subscription_tier'] if studio else 'free'
    if SUBSCRIPTION_TIER_RANK.get(current, 0) < SUBSCRIPTION_TIER_RANK.get(min_tier, 0):
        return jsonify({'error': f'Vyžaduje tarif {min_tier} nebo vyšší (aktuálně {current}).'}), 403
    return None


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route('/sw.js')
def service_worker():
    return send_from_directory('public', 'sw.js', mimetype='application/javascript')

@app.route('/robots.txt')
def robots():
    # Dokud běží coming-soon brána, nemá smysl nabízet robotům zbytek webu —
    # stejně by dostali jen bránu, a ta by se jim zaindexovala pod každou URL.
    if COMING_SOON:
        return Response(
            "User-agent: *\nAllow: /$\nDisallow: /\n",
            mimetype='text/plain')
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /uploads/\n"
        "Disallow: /verify\n"
        "Disallow: /artist-setup\n"
        "Disallow: /my-bookings\n"
        "Disallow: /clients\n"
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
    # Za bránou je veřejná jediná stránka; nabízet profily, které návštěvník
    # neuvidí, by generovalo jen chyby v Search Console.
    if COMING_SOON:
        return Response(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f'<url><loc>{base}/</loc><priority>1.0</priority></url>'
            '</urlset>', mimetype='application/xml')
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
    # Vývojářská paleta, ne produktová stránka. Na produkci ji nikdo
    # z venku vidět nemá — je to jen šum a zbytečná plocha navíc.
    if not app.debug and not is_admin_user(session.get('user_id')):
        return send_from_directory('public', '404.html'), 404
    return send_from_directory('public', 'style-guide.html')


@app.route('/icons.svg')
def icons_sprite():
    return send_from_directory('public', 'icons.svg', mimetype='image/svg+xml')


@app.route('/login')
def login_page():
    if 'user_id' in session:
        return redirect('/')
    return send_from_directory('public', 'login.html')


@app.route('/register')
def register_page():
    # Same page as /login (register form is a tab there); the JS reads ?ref= from URL.
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
    """Rezervace se přestěhovaly na profil jako záložka. URL zůstává, protože
    na ni míří odkazy v odeslaných e-mailech, in-app notifikacích i zástupce
    v manifestu — ty už zpětně nezměníme."""
    uid = session.get('user_id')
    if not uid:
        return redirect('/login?next=/my-bookings')
    conn = get_db()
    row = conn.execute('SELECT username FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    if not row:
        return redirect('/login')
    return redirect(f"/profile/{row['username']}#bookings")


@app.route('/calendar')
def calendar_page():
    return send_from_directory('public', 'calendar.html')


@app.route('/premium')
def premium_page():
    return send_from_directory('public', 'premium.html')


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
    ref_username = (data.get('ref') or '').strip().lower()

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

        # Founding-client auto-grant: first 500 non-artist signups get the
        # flag permanently. After the cap, no more are created. See
        # pricing/config.py for FOUNDING_CLIENT_MAX.
        try:
            from pricing import FOUNDING_CLIENT_MAX
            cnt = conn.execute('SELECT COUNT(*) AS c FROM users WHERE founding_client = 1').fetchone()
            if cnt and (cnt['c'] or 0) < FOUNDING_CLIENT_MAX:
                conn.execute('UPDATE users SET founding_client = 1 WHERE id = ?', (user['id'],))
                conn.commit()
        except Exception as _e:
            # Founding-client grant must never break signup — log and continue.
            print(f'[founding-client] grant failed for user {user["id"]}: {_e}')

        # Welcome email sequence — fire stage 1 immediately (don't gate on
        # verify; the welcome stands on its own), schedule stage 2 for +2 days.
        try:
            send_welcome_email_for(conn, user['id'], 1)
            next_at = (datetime.utcnow() + timedelta(days=2)).isoformat()
            conn.execute(
                'UPDATE users SET welcome_email_stage = 1, welcome_email_next_at = ? WHERE id = ?',
                (next_at, user['id'])
            )
            conn.commit()
        except Exception as _e:
            # Welcome email must never break signup.
            print(f'[welcome-email] stage 1 failed for user {user["id"]}: {_e}')

        # Referral tracking — if user signed up via ?ref=<username>, write a
        # referrals row. Credit isn't granted yet — only when this user
        # completes their first booking (see complete_booking).
        try:
            if ref_username and ref_username != username:
                ref_user = conn.execute(
                    'SELECT id FROM users WHERE username = ?', (ref_username,)
                ).fetchone()
                if ref_user and ref_user['id'] != user['id']:
                    conn.execute(
                        '''INSERT INTO referrals (referrer_user_id, referred_user_id, code)
                           VALUES (?, ?, ?)''',
                        (ref_user['id'], user['id'], ref_username)
                    )
                    conn.commit()
                    try:
                        from pricing import emit_event as _emit_ref
                        _emit_ref('referral.signup', {
                            'referrer_user_id': ref_user['id'],
                            'referred_user_id': user['id'],
                            'code': ref_username,
                        }, conn=conn)
                        conn.commit()
                    except Exception:
                        pass
        except Exception as _e:
            print(f'[referral] track failed for user {user["id"]}: {_e}')
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
    data       = request.get_json()
    identifier = data.get('username', '').strip().lower()
    password   = data.get('password', '')

    # Prázdný identifikátor musí skončit hned. Bez téhle kontroly se dotaz
    # níž ptá na `phone = ''` — a protože telefon je nepovinný a defaultně
    # prázdný, napáruje se na PRVNÍHO uživatele bez telefonu. Odesláním
    # prázdného jména se tak dá přihlásit k cizímu účtu, když má útočník
    # heslo, které k němu sedí.
    if not identifier or not password:
        return jsonify({'error': 'Invalid credentials'}), 401

    conn = get_db()
    # Accept username, email, or phone — users forget usernames but remember
    # the contact detail they registered with. Prázdné sloupce se nesmí
    # párovat ani kdyby identifier prošel jinudy.
    user = conn.execute(
        """SELECT * FROM users
           WHERE LOWER(username) = ?
              OR (email <> '' AND LOWER(email) = ?)
              OR (phone <> '' AND phone = ?)
           LIMIT 1""",
        (identifier, identifier, identifier)
    ).fetchone()
    conn.close()

    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401

    # Anonymized accounts can't log in (PII scrubbed, password_hash unguessable
    # but be explicit anyway in case the random hash check is ever softened).
    try:
        if 'deleted_at' in user.keys() and user['deleted_at']:
            return jsonify({'error': 'Tento účet byl smazán.'}), 410
    except Exception:
        pass

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


@app.route('/api/native/register-push', methods=['POST'])
def native_register_push():
    """Capacitor app calls this after PushNotifications.register() returns a device token.

    Body: {token: '<device-token>', platform: 'ios'|'android'}
    Stores the token in push_subscriptions with provider='apns' (iOS) or 'fcm' (Android).
    """
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.json or {}
    token = (data.get('token') or '').strip()
    platform = (data.get('platform') or '').strip().lower()
    if not token or platform not in ('ios', 'android'):
        return jsonify({'error': 'Invalid payload'}), 400
    provider = 'apns' if platform == 'ios' else 'fcm'
    conn = get_db()
    if conn._pg:
        conn.execute(
            "INSERT INTO push_subscriptions (user_id, endpoint, p256dh, auth, provider, platform) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT (endpoint) DO UPDATE SET user_id=EXCLUDED.user_id, provider=EXCLUDED.provider, platform=EXCLUDED.platform",
            (session['user_id'], token, '', '', provider, platform)
        )
    else:
        conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (user_id, endpoint, p256dh, auth, provider, platform) "
            "VALUES (?,?,?,?,?,?)",
            (session['user_id'], token, '', '', provider, platform)
        )
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/native/unregister-push', methods=['POST'])
def native_unregister_push():
    if 'user_id' not in session:
        return jsonify({'error': 'Not signed in'}), 401
    data = request.json or {}
    token = (data.get('token') or '').strip()
    if not token:
        return jsonify({'error': 'Invalid payload'}), 400
    conn = get_db()
    conn.execute(
        'DELETE FROM push_subscriptions WHERE endpoint = ? AND user_id = ?',
        (token, session['user_id'])
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
                                  cancel_refund_full_hours, cancel_refund_half_hours,
                                  stripe_account_id, stripe_charges_enabled,
                                  stripe_payouts_enabled, stripe_details_submitted,
                                  deletion_requested_at,
                                  artist_terms_accepted_at,
                                  premium_until, premium_cancel_at_period_end, currency
                           FROM users WHERE id = ?''',
                        (session['user_id'],)).fetchone()
    push_n = conn.execute('SELECT COUNT(*) FROM push_subscriptions WHERE user_id=?',
                          (session['user_id'],)).fetchone()[0]
    conn.close()
    d = dict(user)
    d['avatar_url'] = f'/uploads/{d["avatar"]}' if d.get('avatar') else None
    d['is_artist'] = bool(d.get('is_artist'))
    d['can_accept_bookings'] = bool(d.get('stripe_charges_enabled'))
    d['currency'] = _norm_currency(d.get('currency'))
    d['premium'] = _is_premium_from_row(d)
    d['premium_until'] = d.get('premium_until')
    d['push_subscriptions'] = push_n
    d['push_available'] = bool(VAPID_PUBLIC_KEY)
    # Compute purge_at for UI banner — let frontend show countdown.
    if d.get('deletion_requested_at'):
        try:
            req = datetime.fromisoformat((d['deletion_requested_at'] or '').replace('Z', '+00:00'))
            d['deletion_purge_at'] = (req + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)).isoformat()
        except Exception:
            d['deletion_purge_at'] = None
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
               u.currency,
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

    if gps_filter:
        rows = [r for r in rows if r['lat'] is not None and r['lng'] is not None
                and haversine(flat, flng, r['lat'], r['lng']) <= fradius]
    rows = rows[:20]

    sizes_by_item = _load_item_sizes(conn, [r['id'] for r in rows])
    conn.close()
    result = []
    for p in rows:
        result.append({
            'id':            p['id'],
            'image':         p['image'],
            'images':        _portfolio_images(p),
            'sizes':         sizes_by_item.get(p['id'], []),
            'caption':       p['caption'] or '',
            'kind':          p['kind'] or 'done',
            'styles':        p['styles'] or '',
            'like_count':    p['like_count'],
            'liked':         bool(p['liked']),
            'price_kc':        p['price_kc'],
            'estimated_hours': p['estimated_hours'],
            'created_at':    time_ago(p['created_at']),
            'user': {
                'currency':     _norm_currency(p['currency'] if 'currency' in p.keys() else None),
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
               u.currency,
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
    sizes_by_item = _load_item_sizes(conn, [r['id'] for r in rows])
    conn.close()

    result = []
    for p in rows:
        result.append({
            'id':            p['id'],
            'image':         p['image'],
            'images':        _portfolio_images(p),
            'sizes':         sizes_by_item.get(p['id'], []),
            'caption':       p['caption'] or '',
            'kind':          p['kind'] or 'done',
            'styles':        p['styles'] or '',
            'like_count':    p['like_count'],
            'liked':         True,
            'price_kc':        p['price_kc'],
            'estimated_hours': p['estimated_hours'],
            'created_at':    time_ago(p['created_at']),
            'user': {
                'currency':     _norm_currency(p['currency'] if 'currency' in p.keys() else None),
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
               u.currency,
               u.emoji, u.avatar, u.lat, u.lng,
               u.is_artist, u.studio, u.stripe_charges_enabled,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = u.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = u.id) AS rating_count,
               EXISTS(SELECT 1 FROM portfolio_likes WHERE user_id = ? AND item_id = p.id) AS liked
        FROM portfolio_items p
        JOIN users u ON u.id = p.user_id
        WHERE p.id = ?
    ''', (uid, item_id)).fetchone()
    sizes = _load_item_sizes(conn, [item_id]).get(item_id, []) if p else []
    conn.close()
    if not p:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'id':            p['id'],
        'image':         p['image'],
        'sizes':         sizes,
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
                               stripe_charges_enabled, currency
                        FROM users WHERE username = ?''', (username,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'User not found'}), 404

    portfolio = conn.execute('''
        SELECT p.*, EXISTS(SELECT 1 FROM portfolio_likes WHERE user_id = ? AND item_id = p.id) AS liked
        FROM portfolio_items p WHERE p.user_id = ?
        ORDER BY p.created_at DESC
    ''', (uid, u['id'])).fetchall()
    _pf_sizes = _load_item_sizes(conn, [r['id'] for r in portfolio])

    now_iso = datetime.utcnow().isoformat()
    slots = conn.execute('''
        SELECT * FROM slots
        WHERE user_id = ? AND status IN ('free','held') AND start_at >= ?
              AND COALESCE(is_private, 0) = 0
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
    # Stejný resolver jako používá rušení rezervace — jeden zdroj pravdy,
    # ať se to, co klientovi slíbíme, shoduje s tím, co mu pak vrátíme.
    _cancel_full, _cancel_half = _resolve_cancel_policy(conn, u['id'])
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
        # Storno lhůty tatéra. Bez nich frontend vypisoval natvrdo 96/48 h,
        # takže klientovi od Sprintu 2 mohl ukázat cizí podmínky.
        'currency': _norm_currency(u['currency'] if 'currency' in u.keys() else None),
        'cancel_full_hours': _cancel_full,
        'cancel_half_hours': _cancel_half,
        'portfolio_count': len(portfolio),
        'portfolio': [{
            'id':              p['id'],
            'image':           p['image'],
            'images':          _portfolio_images(p),
            'sizes':           _pf_sizes.get(p['id'], []),
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

    # Artist liability consent — must be accepted (once) before profile save
    # can activate is_artist. Existing artists are grandfathered by an already-
    # set artist_terms_accepted_at.
    conn_check = get_db()
    current = conn_check.execute(
        'SELECT is_artist, artist_terms_accepted_at FROM users WHERE id=?',
        (session['user_id'],)
    ).fetchone()
    conn_check.close()
    already_accepted = bool(current and current['artist_terms_accepted_at'])
    consent_now = (request.form.get('artist_terms_accepted', '').strip() in ('1', 'true', 'on'))
    if not already_accepted and not consent_now:
        return jsonify({
            'error': 'Před uložením musíš odsouhlasit odpovědnostní podmínky tatéra.',
            'code': 'artist_terms_required'
        }), 400
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
    # Storno lhůty: prázdné pole = None = platformní default (96/48).
    cancel_full = _opt_int('cancel_refund_full_hours')
    cancel_half = _opt_int('cancel_refund_half_hours')
    if cancel_full is not None and cancel_half is not None and cancel_half > cancel_full:
        return jsonify({'error': 'Lhůta pro poloviční refund musí být kratší než pro plný.'}), 400
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
    # Stamp consent on first acceptance (idempotent — later saves don't overwrite).
    if not already_accepted and consent_now:
        conn.execute(
            'UPDATE users SET artist_terms_accepted_at=? WHERE id=? AND artist_terms_accepted_at IS NULL',
            (datetime.utcnow().isoformat() + 'Z', session['user_id'])
        )

    if lat is not None and lng is not None:
        conn.execute('''UPDATE users SET display_name=?, city=?, bio=?, studio=?, instagram=?,
                                          styles=?, deposit_pct_default=?,
                                          hourly_rate_min=?, hourly_rate_max=?,
                                          default_payment_mode=?,
                                          cancel_refund_full_hours=?, cancel_refund_half_hours=?,
                                          lat=?, lng=?
                        WHERE id=?''',
                     (display_name, city, bio, studio, instagram, styles, deposit_pct,
                      hourly_min, hourly_max, pay_mode, cancel_full, cancel_half, lat, lng,
                      session['user_id']))
    else:
        conn.execute('''UPDATE users SET display_name=?, city=?, bio=?, studio=?, instagram=?,
                                          styles=?, deposit_pct_default=?,
                                          hourly_rate_min=?, hourly_rate_max=?,
                                          default_payment_mode=?,
                                          cancel_refund_full_hours=?, cancel_refund_half_hours=?
                        WHERE id=?''',
                     (display_name, city, bio, studio, instagram, styles, deposit_pct,
                      hourly_min, hourly_max, pay_mode, cancel_full, cancel_half,
                      session['user_id']))

    # Měna se neptá, odvozuje se z města (a později ze Stripe účtu).
    _sync_currency(conn, session['user_id'])

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
    u = conn.execute(
        'SELECT username, display_name, is_artist, artist_slug, artist_terms_accepted_at FROM users WHERE id=?',
        (session['user_id'],)
    ).fetchone()
    if u['is_artist']:
        conn.close()
        return jsonify({'ok': True, 'artist_slug': u['artist_slug']})
    # Consent required unless already accepted previously.
    body = request.get_json(silent=True) if request.is_json else None
    consent_raw = ''
    if body and 'artist_terms_accepted' in body:
        consent_raw = str(body.get('artist_terms_accepted', '')).lower()
    else:
        consent_raw = request.form.get('artist_terms_accepted', '').strip().lower()
    consent_now = consent_raw in ('1', 'true', 'on')
    if not u['artist_terms_accepted_at'] and not consent_now:
        conn.close()
        return jsonify({
            'error': 'Před aktivací tatérského profilu musíš odsouhlasit odpovědnostní podmínky.',
            'code': 'artist_terms_required'
        }), 400
    if not u['artist_terms_accepted_at']:
        conn.execute(
            'UPDATE users SET artist_terms_accepted_at=? WHERE id=?',
            (datetime.utcnow().isoformat() + 'Z', session['user_id'])
        )
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
    # Ceník po velikostech; přijde jako JSON řetězec, protože zbytek
    # formuláře je multipart kvůli fotkám.
    size_rows, size_err = _parse_sizes(request.form.get('sizes'))
    if size_err:
        return jsonify({'error': size_err}), 400

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
    # ORDER BY id DESC LIMIT 1 by při dvou souběžných uploadech vrátilo
    # cizí položku a ceník by se uložil k ní.
    item_id = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
               else conn.execute('SELECT lastval()').fetchone()[0])
    if size_rows:
        _save_item_sizes(conn, item_id, size_rows)
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'id': item_id, 'image': primary_name,
                    'images': [primary_name] + [n for n in extra_names.values() if n]})


def _load_item_sizes(conn, item_ids):
    """{item_id: [{size_label, price_kc, estimated_hours}, …]} seřazené S→M→L.

    Bere seznam id, ne jedno — feed vypisuje desítky položek naráz a dotaz
    na každou zvlášť by z jedné stránky udělal padesát dotazů."""
    ids = [int(i) for i in item_ids if i]
    if not ids:
        return {}
    marks = ','.join(['?'] * len(ids))
    rows = conn.execute(
        f'SELECT item_id, size_label, price_kc, estimated_hours '
        f'FROM portfolio_item_sizes WHERE item_id IN ({marks})', tuple(ids)).fetchall()
    order = {k: i for i, k in enumerate(SKETCH_SIZES)}
    out = {}
    for r in rows:
        out.setdefault(r['item_id'], []).append({
            'size_label':      r['size_label'],
            'price_kc':        int(r['price_kc']),
            'estimated_hours': float(r['estimated_hours']),
        })
    for v in out.values():
        v.sort(key=lambda d: order.get(d['size_label'], 99))
    return out


def _parse_sizes(raw):
    """Vrátí (rows, error). Prázdný vstup = tatér ceník nechce."""
    if raw in (None, '', []):
        return [], None
    if isinstance(raw, str):
        import json as _json
        try:
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return None, 'Špatný formát velikostí.'
    if not isinstance(raw, list):
        return None, 'Špatný formát velikostí.'
    out, seen = [], set()
    for item in raw[:len(SKETCH_SIZES)]:
        if not isinstance(item, dict):
            return None, 'Špatný formát velikostí.'
        label = (item.get('size_label') or '').strip().lower()
        if label not in SKETCH_SIZES:
            return None, 'Neznámá velikost.'
        if label in seen:
            return None, 'Každá velikost může být jen jednou.'
        # Nevyplněný řádek není chyba — tatér nemusí nabízet všechny tři.
        if item.get('price_kc') in (None, '') and item.get('estimated_hours') in (None, ''):
            continue
        try:
            price = int(item.get('price_kc'))
            hours = float(item.get('estimated_hours'))
        except (ValueError, TypeError):
            return None, 'U každé velikosti vyplň cenu i délku.'
        if price <= 0 or hours < 0.5 or hours > 24:
            return None, 'U každé velikosti vyplň cenu i délku.'
        seen.add(label)
        out.append({'size_label': label, 'price_kc': price,
                    'estimated_hours': round(hours, 2)})
    return out, None


def _save_item_sizes(conn, item_id, rows):
    """Přepíše ceník položky a srovná price_kc/estimated_hours na nejlevnější
    variantu. Ta dvojice je "od kolika" — čte ji řazení feedu, OG obrázky
    i rezervace, takže se nesmí rozejít s ceníkem."""
    conn.execute('DELETE FROM portfolio_item_sizes WHERE item_id=?', (item_id,))
    for r in rows:
        conn.execute('INSERT INTO portfolio_item_sizes '
                     '(item_id, size_label, price_kc, estimated_hours) VALUES (?,?,?,?)',
                     (item_id, r['size_label'], r['price_kc'], r['estimated_hours']))
    if rows:
        cheapest = min(rows, key=lambda r: r['price_kc'])
        conn.execute('UPDATE portfolio_items SET price_kc=?, estimated_hours=? WHERE id=?',
                     (cheapest['price_kc'], cheapest['estimated_hours'], item_id))


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
    conn.execute('DELETE FROM portfolio_item_sizes WHERE item_id=?', (item_id,))
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

    size_rows = None
    if 'sizes' in data:
        size_rows, size_err = _parse_sizes(data['sizes'])
        if size_err:
            conn.close(); return jsonify({'error': size_err}), 400

    if sets:
        params.append(item_id)
        conn.execute(f'UPDATE portfolio_items SET {", ".join(sets)} WHERE id=?', tuple(params))
    # Až po UPDATE: _save_item_sizes srovnává price_kc na nejlevnější variantu
    # a opačné pořadí by tu hodnotu hned přepsalo tou z formuláře.
    if size_rows is not None:
        _save_item_sizes(conn, item_id, size_rows)
    conn.commit()
    updated = conn.execute('SELECT * FROM portfolio_items WHERE id=?', (item_id,)).fetchone()
    item = dict(updated)
    item['sizes'] = _load_item_sizes(conn, [item_id]).get(item_id, [])
    conn.close()
    return jsonify({'ok': True, 'item': item})


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
        # Rest of the file compares against naive datetime.utcnow() throughout —
        # normalize an offset-aware input (e.g. a real 'Z'-suffixed ISO string,
        # which the calendar form doesn't send today but a valid client could)
        # down to naive UTC so it doesn't crash e_dt/s_dt comparisons below.
        if s_dt.tzinfo is not None:
            s_dt = s_dt.astimezone(timezone.utc).replace(tzinfo=None)
        if e_dt.tzinfo is not None:
            e_dt = e_dt.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return jsonify({'error': 'Špatný formát datumu (použij ISO 8601)'}), 400
    if e_dt <= s_dt:
        return jsonify({'error': 'Konec termínu musí být po startu'}), 400
    if s_dt < _prague_now_naive() - timedelta(minutes=5):
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

    def _opt_buffer(name):
        try:
            return max(0, min(240, int(data.get(name) or 0)))
        except (ValueError, TypeError):
            return 0
    buf_before = _opt_buffer('buffer_before_minutes')
    buf_after  = _opt_buffer('buffer_after_minutes')

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
                                           deposit_pct, note, price_unit, min_duration_hours,
                                           buffer_before_minutes, buffer_after_minutes, currency)
                        VALUES (?,?,?,'free',?,?,?,?,?,?,?,?,?)''',
                     (session['user_id'], ns.isoformat(), ne.isoformat(),
                      price_min, price_max, deposit_pct, note, price_unit, min_dur,
                      buf_before, buf_after, _artist_currency(conn, session['user_id'])))
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
                    'created': len(created_ids), 'ids': created_ids,
                    # Informativní, ne blokující — viz _cz_holiday_warnings.
                    'holiday_warnings': _cz_holiday_warnings([ns.date() for ns, _ in occurrences])})


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


# ── Blokace volna (dovolená, nemoc, „tady prostě nejsem") ────────────────────
# Oddělené od slotů: slot = „tady se dá rezervovat", blokace = „tady nejsem",
# a blokace platí napříč VŠEMI sloty tatéra, ne jen v jednom.

@app.route('/api/blocked-time', methods=['POST'])
def create_blocked_time():
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    start_at = (data.get('start_at') or '').strip()
    end_at   = (data.get('end_at') or '').strip()
    if not start_at or not end_at:
        return jsonify({'error': 'Vyplň začátek a konec blokace'}), 400
    try:
        s_dt = _naive_dt(start_at)
        e_dt = _naive_dt(end_at)
    except ValueError:
        return jsonify({'error': 'Špatný formát datumu (použij ISO 8601)'}), 400
    if e_dt <= s_dt:
        return jsonify({'error': 'Konec blokace musí být po začátku'}), 400
    reason = (data.get('reason') or '').strip()[:200]

    conn = get_db()
    # Blokace přes už rezervovaný čas by tiše lhala oběma stranám — rezervace
    # by dál existovala, ale tatér by si myslel, že má volno.
    clash = conn.execute(
        '''SELECT b.id FROM bookings b
           WHERE b.artist_id = ? AND b.status IN ('pending_payment','confirmed')
                 AND b.booking_start_at IS NOT NULL AND b.booking_end_at IS NOT NULL
                 AND b.booking_start_at < ? AND b.booking_end_at > ? LIMIT 1''',
        (session['user_id'], e_dt.isoformat(), s_dt.isoformat())
    ).fetchone()
    if clash:
        conn.close()
        return jsonify({'error': 'V tomhle čase máš aktivní rezervaci — nejdřív ji vyřeš.',
                        'booking_id': clash['id']}), 409

    conn.execute('''INSERT INTO artist_blocked_time (artist_id, start_at, end_at, reason)
                    VALUES (?,?,?,?)''',
                 (session['user_id'], s_dt.isoformat(), e_dt.isoformat(), reason))
    conn.commit()
    if not conn._pg:
        bid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    else:
        bid = conn.execute('SELECT lastval()').fetchone()[0]
    conn.close()
    return jsonify({'ok': True, 'id': bid})


@app.route('/api/me/blocked-time')
def my_blocked_time():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute(
        '''SELECT id, start_at, end_at, reason FROM artist_blocked_time
           WHERE artist_id = ? ORDER BY start_at''', (session['user_id'],)
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/blocked-time/<int:block_id>', methods=['DELETE'])
def delete_blocked_time(block_id):
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute('SELECT artist_id FROM artist_blocked_time WHERE id=?', (block_id,)).fetchone()
    if not row or row['artist_id'] != session['user_id']:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.execute('DELETE FROM artist_blocked_time WHERE id=?', (block_id,))
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
    _reap_expired_offers(conn, session['user_id'])
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
    _reap_expired_offers(conn, session['user_id'])
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
                'currency':    _norm_currency(b['currency'] if 'currency' in b.keys() else None),
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
    sizes_by_item = _load_item_sizes(conn, [r['id'] for r in rows])
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d['sizes'] = sizes_by_item.get(r['id'], [])
        out.append(d)
    return jsonify(out)


@app.route('/api/me/checklist')
def my_checklist():
    """Vrátí completeness checklist pro tatérský onboarding.
    UI v artist-setup zobrazí banner s progress dokud není vše hotové."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    u = conn.execute('''SELECT username, display_name, city, studio, bio, styles,
                               hourly_rate_min, hourly_rate_max,
                               stripe_charges_enabled, is_artist
                        FROM users WHERE id=?''', (uid,)).fetchone()
    portfolio_count = conn.execute('SELECT COUNT(*) FROM portfolio_items WHERE user_id=?',
                                    (uid,)).fetchone()[0]
    now_iso = _prague_now_naive().isoformat()
    upcoming_slots = conn.execute('''SELECT COUNT(*) FROM slots
                                     WHERE user_id=? AND end_at >= ?
                                           AND COALESCE(is_private, 0) = 0''',
                                  (uid, now_iso)).fetchone()[0]
    bookings_count = conn.execute(
        '''SELECT COUNT(*) FROM bookings WHERE artist_id=?
           AND status IN ('confirmed', 'completed', 'pending_payment')''',
        (uid,)
    ).fetchone()[0]
    conn.close()

    # `label` je anglicky (zdrojový jazyk API), UI si podle `key` sáhne do
    # slovníku. Překlady patří do i18n.js, ne do dvou míst.
    items = [
        {
            'key':  'profile',
            'label':'Fill in your profile (name + city or studio + bio)',
            'done': bool(u['display_name'] and (u['city'] or u['studio']) and u['bio']),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'rate',
            'label':'Set your hourly rate',
            'done': bool(u['hourly_rate_min'] or u['hourly_rate_max']),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'styles',
            'label':'Pick your tattoo styles',
            'done': bool((u['styles'] or '').strip()),
            'href': '/artist-setup#profile',
        },
        {
            'key':  'portfolio',
            'label':'Add at least 3 portfolio items',
            'done': portfolio_count >= 3,
            'href': f'/profile/{u["username"]}#portfolio',
            'count': portfolio_count,
        },
        {
            'key':  'slot',
            'label':'Publish at least one open slot',
            'done': upcoming_slots > 0,
            # Míří na /calendar, ne na /artist-setup#slots — ta kotva
            # ve stránce od Sprintu 2 neexistuje a onboarding tak posílal
            # tatéra do prázdna přesně v kroku, na kterém stojí rezervace.
            'href': '/calendar',
            'count': upcoming_slots,
        },
        {
            'key':  'stripe',
            'label':'Connect Stripe (to accept deposits)',
            'done': bool(u['stripe_charges_enabled']),
            'href': '/artist-setup#payments',
        },
        {
            'key':  'first_booking',
            'label':'Your first booking',
            'done': bookings_count > 0,
            'href': f'/profile/{u["username"]}#bookings',
            'count': bookings_count,
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
# ── Discount apply / preview API ──────────────────────────────────────────
# Volá frontend při booking checkout: "co když applikuju kód WELCOME?".
# Vrátí buď success s konečnou částkou, nebo error_code z pricing.discounts.
# Žádná DB mutace — jen validation.

@app.route('/api/discounts/preview', methods=['POST'])
@limiter.limit('60 per hour')
def discount_preview():
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip().upper()
    try:
        gross_czk = float(data.get('gross_price_czk') or 0)
        artist_id = int(data.get('artist_id') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'gross_price_czk and artist_id required'}), 400
    if gross_czk <= 0 or not artist_id:
        return jsonify({'error': 'gross_price_czk and artist_id required'}), 400

    try:
        from pricing import (
            BookingInput, DiscountInput, validate_discount,
            WELCOME_DISCOUNT_CZK, REFERRAL_BONUS_CZK,
        )
        from decimal import Decimal as _Dec
    except Exception as e:
        return jsonify({'error': f'pricing load failed: {e}'}), 500

    conn = get_db()
    me = conn.execute('SELECT id, founding_client, created_at FROM users WHERE id = ?',
                      (session['user_id'],)).fetchone()
    artist = conn.execute('SELECT founding_artist, founding_artist_started_at FROM users WHERE id = ?',
                          (artist_id,)).fetchone()
    if not me or not artist:
        conn.close()
        return jsonify({'error': 'user/artist not found'}), 404

    # Identify discount type from code
    discount_type = None
    discount_amount = _Dec('0')
    discount_code_row = None
    if code == 'WELCOME':
        discount_type = 'WELCOME'
        discount_amount = WELCOME_DISCOUNT_CZK
    elif code == 'REFERRAL':
        discount_type = 'REFERRAL'
        discount_amount = REFERRAL_BONUS_CZK
    else:
        # MANUAL_PROMO — look up in discount_codes table
        discount_code_row = conn.execute(
            'SELECT * FROM discount_codes WHERE code = ? AND active = 1',
            (code,)
        ).fetchone()
        if not discount_code_row:
            conn.close()
            return jsonify({'valid': False, 'error_code': 'UNKNOWN_CODE',
                            'message': 'Discount code not found'}), 200
        # Check expiration & max_uses
        if discount_code_row['expires_at']:
            try:
                if datetime.fromisoformat(discount_code_row['expires_at']) < datetime.utcnow():
                    conn.close()
                    return jsonify({'valid': False, 'error_code': 'EXPIRED',
                                    'message': 'Discount code expired'}), 200
            except Exception:
                pass
        if discount_code_row['max_uses'] and discount_code_row['used_count'] >= discount_code_row['max_uses']:
            conn.close()
            return jsonify({'valid': False, 'error_code': 'EXHAUSTED',
                            'message': 'Discount code limit reached'}), 200
        discount_type = 'MANUAL_PROMO'
        discount_amount = _Dec(str(discount_code_row['amount_czk']))

    # Has user used this code before?
    used_before_row = conn.execute(
        'SELECT 1 FROM discount_redemptions WHERE user_id = ? AND discount_type = ? AND (discount_code = ? OR discount_code IS NULL)',
        (session['user_id'], discount_type, code if discount_type == 'MANUAL_PROMO' else None)
    ).fetchone()
    has_used = bool(used_before_row)

    # First booking check (for WELCOME eligibility)
    is_new_client = True
    if discount_type == 'WELCOME':
        any_booking = conn.execute(
            "SELECT 1 FROM bookings WHERE client_id = ? AND status IN ('confirmed','completed','paid')",
            (session['user_id'],)
        ).fetchone()
        is_new_client = not bool(any_booking)

    started_dt = None
    if artist['founding_artist'] and artist['founding_artist_started_at']:
        try:
            started_dt = datetime.fromisoformat(artist['founding_artist_started_at'])
        except Exception:
            pass

    booking_input = BookingInput(
        gross_price_czk            = _Dec(str(gross_czk)),
        artist_founding_started_at = started_dt,
        client_founding            = bool(me['founding_client']),
    )
    d_input = DiscountInput(
        discount_type        = discount_type,
        discount_amount_czk  = discount_amount,
        booking_input        = booking_input,
        client_is_new        = is_new_client,
        client_has_used_code = has_used,
    )
    result = validate_discount(d_input)
    conn.close()

    return jsonify({
        'valid': result.valid,
        'error_code': result.error_code,
        'message': result.message,
        'discount_type': discount_type,
        'amount_czk': float(discount_amount),
        'code': code,
    })


# ── Admin: MANUAL_PROMO discount codes ────────────────────────────────────

@app.route('/api/admin/discount-codes', methods=['GET'])
def admin_list_discount_codes():
    err = require_admin()
    if err: return err
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM discount_codes ORDER BY created_at DESC LIMIT 200'
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/admin/discount-codes', methods=['POST'])
def admin_create_discount_code():
    err = require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip().upper()
    try:
        amount_czk = int(data.get('amount_czk') or 0)
    except (TypeError, ValueError):
        return jsonify({'error': 'amount_czk required'}), 400
    if not code or amount_czk <= 0:
        return jsonify({'error': 'code + amount_czk required'}), 400
    if code in ('WELCOME', 'REFERRAL'):
        return jsonify({'error': 'WELCOME and REFERRAL are reserved'}), 400

    max_uses   = data.get('max_uses')   # nullable
    expires_at = data.get('expires_at') # nullable ISO

    conn = get_db()
    try:
        conn.execute(
            '''INSERT INTO discount_codes (code, kind, amount_czk, max_uses, expires_at, created_by)
               VALUES (?, 'MANUAL_PROMO', ?, ?, ?, ?)''',
            (code, amount_czk, max_uses, expires_at, session['user_id'])
        )
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Code already exists or DB error: {e}'}), 400
    conn.close()
    return jsonify({'ok': True, 'code': code, 'amount_czk': amount_czk})


@app.route('/api/admin/discount-codes/<int:code_id>', methods=['PATCH'])
def admin_update_discount_code(code_id):
    err = require_admin()
    if err: return err
    data = request.get_json(silent=True) or {}
    sets, vals = [], []
    if 'active' in data:
        sets.append('active = ?')
        vals.append(1 if data['active'] else 0)
    if 'max_uses' in data:
        sets.append('max_uses = ?')
        vals.append(data['max_uses'])
    if 'expires_at' in data:
        sets.append('expires_at = ?')
        vals.append(data['expires_at'])
    if not sets:
        return jsonify({'error': 'nothing to update'}), 400
    vals.append(code_id)
    conn = get_db()
    conn.execute(f'UPDATE discount_codes SET {", ".join(sets)} WHERE id = ?', vals)
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── Reconciliation cron ───────────────────────────────────────────────────
# Daily job: porovnává sum(client_pays_total) na completed bookings za včerejšek
# s Stripe balance transactions. Diff > 10 CZK = warning v logu (admin
# vidí v telemetry events).
# Trigger from Railway cron: GET /api/cron/reconcile?token=<RECONCILE_TOKEN>
RECONCILE_TOKEN = os.environ.get('RECONCILE_TOKEN', '')

@app.route('/api/cron/reconcile', methods=['GET', 'POST'])
@limiter.limit('30 per hour')
def cron_reconcile():
    token = request.args.get('token', '') or request.headers.get('X-Cron-Token', '')
    if not RECONCILE_TOKEN or token != RECONCILE_TOKEN:
        return jsonify({'error': 'forbidden'}), 403

    # Yesterday's range (UTC)
    end   = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=1)

    conn = get_db()
    # Internal: sum of client_pays_total z economics_snapshots completed bookings
    rows = conn.execute('''
        SELECT es.snapshot FROM bookings b
        JOIN economics_snapshots es ON es.booking_id = b.id
        WHERE b.status = 'completed'
          AND b.completed_at >= ? AND b.completed_at < ?
          AND es.kind = 'initial'
    ''', (start.isoformat(), end.isoformat())).fetchall()
    import json as _j
    internal_total_czk = 0.0
    for r in rows:
        try:
            s = _j.loads(r['snapshot'])
            internal_total_czk += float(s.get('client_pays_total', 0))
        except Exception:
            pass

    # Stripe side: balance transactions for the same window
    stripe_total_czk = None
    if STRIPE_SECRET_KEY:
        try:
            txns = stripe.BalanceTransaction.list(
                created={'gte': int(start.timestamp()), 'lt': int(end.timestamp())},
                type='charge',
                limit=100,
            )
            stripe_total_haler = sum(t.amount for t in txns.auto_paging_iter())
            stripe_total_czk = stripe_total_haler / 100.0
        except Exception as e:
            print(f'[reconcile] Stripe API error: {e}')

    diff = None
    if stripe_total_czk is not None:
        diff = abs(internal_total_czk - stripe_total_czk)

    try:
        from pricing import emit_event
        emit_event('reconciliation.completed', {
            'window_start': start.isoformat(),
            'window_end': end.isoformat(),
            'internal_total_czk': internal_total_czk,
            'stripe_total_czk': stripe_total_czk,
            'diff_czk': diff,
            'warning': bool(diff is not None and diff > 10),
        }, conn=conn)
        conn.commit()
    except Exception:
        pass
    conn.close()

    return jsonify({
        'window': {'start': start.isoformat(), 'end': end.isoformat()},
        'internal_total_czk': internal_total_czk,
        'stripe_total_czk': stripe_total_czk,
        'diff_czk': diff,
        'reconciled': diff is None or diff <= 10,
    })


# ── Welcome email sequence cron ───────────────────────────────────────────
# Hodinový (nebo denní) cron co posílá další stage onboarding emailu pro
# usery, kteří dosáhli welcome_email_next_at. Tři stages: 1 (immediate on
# signup), 2 (+2 days), 3 (+7 days). Cron táhne 2 a 3.
# Trigger: GET /api/cron/welcome-emails?token=<RECONCILE_TOKEN>

@app.route('/api/cron/welcome-emails', methods=['GET', 'POST'])
@limiter.limit('30 per hour')
def cron_welcome_emails():
    token = request.args.get('token', '') or request.headers.get('X-Cron-Token', '')
    if not RECONCILE_TOKEN or token != RECONCILE_TOKEN:
        return jsonify({'error': 'forbidden'}), 403
    if not RESEND_API_KEY:
        return jsonify({'error': 'RESEND_API_KEY not set', 'sent': 0}), 503

    now_iso = datetime.utcnow().isoformat()
    conn = get_db()
    rows = conn.execute(
        '''SELECT id, COALESCE(welcome_email_stage, 0) AS stage
           FROM users
           WHERE COALESCE(welcome_email_stage, 0) IN (1, 2)
             AND welcome_email_next_at IS NOT NULL
             AND welcome_email_next_at <= ?
           ORDER BY id ASC LIMIT 200''',
        (now_iso,)
    ).fetchall()

    sent = []
    failed = []
    for r in rows:
        uid = r['id']
        next_stage = r['stage'] + 1   # 1→2 (day 2), 2→3 (day 7)
        ok = send_welcome_email_for(conn, uid, next_stage)
        # Advance regardless of send success — failed sends shouldn't loop
        # forever; we log and move on. If stage 3, next_at = NULL (done).
        if next_stage >= 3:
            new_next = None
        else:
            new_next = (datetime.utcnow() + timedelta(days=5)).isoformat()  # 2 → 3 = 5 days later (total 7)
        conn.execute(
            'UPDATE users SET welcome_email_stage = ?, welcome_email_next_at = ? WHERE id = ?',
            (next_stage, new_next, uid)
        )
        (sent if ok else failed).append({'user_id': uid, 'stage': next_stage})
    conn.commit()

    try:
        from pricing import emit_event as _emit
        _emit('welcome_email.batch_sent', {
            'sent_count': len(sent), 'failed_count': len(failed),
        }, conn=conn)
        conn.commit()
    except Exception:
        pass

    conn.close()
    return jsonify({
        'ok': True,
        'sent_count': len(sent),
        'failed_count': len(failed),
        'sent': sent[:50],
        'failed': failed[:50],
    })


# ── Admin: referrals leaderboard ──────────────────────────────────────────

@app.route('/api/admin/referrals/leaderboard')
def admin_referrals_leaderboard():
    err = require_admin()
    if err: return err
    conn = get_db()
    # Aggregate: per referrer, count total signups + granted, joined with their account credit.
    rows = conn.execute('''
        SELECT u.id, u.username, u.display_name,
               COALESCE(u.account_credit_cents, 0) AS account_credit_cents,
               COUNT(r.id) AS signups,
               SUM(CASE WHEN r.credit_granted_at IS NOT NULL THEN 1 ELSE 0 END) AS granted
        FROM referrals r
        JOIN users u ON u.id = r.referrer_user_id
        GROUP BY u.id, u.username, u.display_name, u.account_credit_cents
        ORDER BY granted DESC, signups DESC
        LIMIT 50
    ''').fetchall()
    totals = conn.execute('''
        SELECT COUNT(*) AS total_signups,
               SUM(CASE WHEN credit_granted_at IS NOT NULL THEN 1 ELSE 0 END) AS total_granted
        FROM referrals
    ''').fetchone()
    credit_total = conn.execute(
        'SELECT COALESCE(SUM(account_credit_cents), 0) AS c FROM users WHERE account_credit_cents > 0'
    ).fetchone()
    conn.close()
    return jsonify({
        'rows': [
            {
                'username':              r['username'],
                'display_name':          r['display_name'],
                'signups':               r['signups'] or 0,
                'granted':               r['granted'] or 0,
                'account_credit_cents':  r['account_credit_cents'] or 0,
            }
            for r in rows
        ],
        'total_signups':       (totals['total_signups'] if totals else 0) or 0,
        'total_granted':       (totals['total_granted'] if totals else 0) or 0,
        'total_credit_cents':  (credit_total['c'] if credit_total else 0) or 0,
    })


# ── Admin telemetry events feed ───────────────────────────────────────────

@app.route('/api/admin/telemetry')
def admin_telemetry():
    err = require_admin()
    if err: return err
    event_name = request.args.get('event_name') or None
    try:
        limit = min(int(request.args.get('limit') or 100), 500)
    except (TypeError, ValueError):
        limit = 100
    conn = get_db()
    if event_name:
        rows = conn.execute(
            'SELECT id, event_name, payload_json, created_at FROM telemetry_events WHERE event_name = ? ORDER BY id DESC LIMIT ?',
            (event_name, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            'SELECT id, event_name, payload_json, created_at FROM telemetry_events ORDER BY id DESC LIMIT ?',
            (limit,)
        ).fetchall()
    conn.close()
    import json as _j
    out = []
    for r in rows:
        try:
            payload = _j.loads(r['payload_json'])
        except Exception:
            payload = {}
        out.append({
            'id': r['id'],
            'event_name': r['event_name'],
            'payload': payload,
            'created_at': r['created_at'],
        })
    return jsonify(out)


@app.route('/api/admin/kpis')
def admin_kpis_endpoint():
    """Net-take-rate dashboard KPI query. Filters: ?start=ISO&end=ISO&city=&tier=
    (tier ∈ founding|standard). Default range = last 30 days."""
    err = require_admin()
    if err: return err
    try:
        from pricing.admin import admin_kpis, admin_kpis_last_30d
    except Exception as e:
        return jsonify({'error': f'pricing module load failed: {e}'}), 500

    start_s = request.args.get('start')
    end_s   = request.args.get('end')
    city    = request.args.get('city') or None
    tier    = request.args.get('tier') or None
    if tier and tier not in ('founding', 'standard'):
        return jsonify({'error': "tier must be 'founding' or 'standard'"}), 400

    conn = get_db()
    try:
        if start_s and end_s:
            start = datetime.fromisoformat(start_s)
            end   = datetime.fromisoformat(end_s)
            result = admin_kpis(conn, start, end, city=city, artist_tier=tier)
        else:
            result = admin_kpis_last_30d(conn, city=city, artist_tier=tier)
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 400
    conn.close()
    return jsonify(result)


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


def _haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in km. Returns None if any coord is missing."""
    import math
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return None
    R = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


@app.route('/api/artists/near')
def artists_near():
    """Returns artists within ?km kilometers of (?lat, ?lng), sorted by distance.
    Filters via bounding box (cheap SQL) then exact Haversine in Python.
    """
    try:
        lat = float(request.args.get('lat', ''))
        lng = float(request.args.get('lng', ''))
    except (TypeError, ValueError):
        return jsonify({'error': 'lat/lng required'}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return jsonify({'error': 'invalid lat/lng'}), 400
    try:
        km = float(request.args.get('km', '25'))
    except (TypeError, ValueError):
        km = 25.0
    km = max(1.0, min(km, 500.0))  # 1–500 km

    # Bounding box pre-filter: 1° lat ≈ 111 km; 1° lng ≈ 111 * cos(lat) km.
    # Pad a touch (km * 1.1) so the circle never gets clipped at the box edge.
    import math
    deg_lat = (km * 1.1) / 111.0
    deg_lng = (km * 1.1) / max(1.0, 111.0 * math.cos(math.radians(lat)))
    lat_min, lat_max = lat - deg_lat, lat + deg_lat
    lng_min, lng_max = lng - deg_lng, lng + deg_lng

    conn = get_db()
    rows = conn.execute('''
        SELECT id, username, display_name, city, studio, styles, avatar, lat, lng,
               (SELECT AVG(rating) FROM reviews WHERE artist_id = users.id) AS rating_avg,
               (SELECT COUNT(*)    FROM reviews WHERE artist_id = users.id) AS rating_count
        FROM users
        WHERE is_artist = 1
          AND lat IS NOT NULL AND lng IS NOT NULL
          AND lat BETWEEN ? AND ?
          AND lng BETWEEN ? AND ?
    ''', (lat_min, lat_max, lng_min, lng_max)).fetchall()
    conn.close()

    out = []
    for r in rows:
        d = _haversine_km(lat, lng, r['lat'], r['lng'])
        if d is None or d > km:
            continue
        out.append({
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
            'distance_km':   round(d, 1),
        })
    out.sort(key=lambda x: x['distance_km'])
    return jsonify({'center': {'lat': lat, 'lng': lng}, 'km': km, 'artists': out, 'count': len(out)})


@app.route('/api/sizes')
def list_sizes():
    """Velikosti tetování s mapováním na hodiny — sdílené mezi UI a backendem."""
    return jsonify([
        {'key': k, 'hours': h, 'label': lbl}
        for k, (h, lbl) in SIZE_PRESETS.items()
    ])


# ── InkLink: Bookings ─────────────────────────────────────────────────────────

# ── Booking state machine ──────────────────────────────────────────────────
# Formalizes the 9 status strings that were previously set ad-hoc across ~7
# call sites (cancel_booking, complete_booking, mark_no_show, and 4 webhook
# handlers). Each entry maps a status to the set of statuses reachable FROM
# it — derived by cross-checking every pre-existing guard in those call
# sites, so this preserves current behavior rather than silently narrowing
# it. 'disputed' is intentionally reachable from every other status (a
# chargeback can land on a charge regardless of the booking's current
# status, and Stripe's response deadline makes silently dropping that
# update the worse failure mode). cancelled_client/cancelled_artist are
# terminal except for that dispute path.
BOOKING_STATUSES = {
    'pending_payment', 'confirmed', 'payment_failed', 'completed',
    'cancelled_client', 'cancelled_artist', 'refunded', 'disputed', 'no_show',
}

BOOKING_TRANSITIONS = {
    'pending_payment':   {'confirmed', 'payment_failed', 'completed', 'disputed', 'cancelled_client', 'cancelled_artist'},
    'payment_failed':    {'disputed', 'cancelled_client', 'cancelled_artist'},
    'confirmed':         {'completed', 'disputed', 'no_show', 'refunded', 'cancelled_client', 'cancelled_artist'},
    'completed':         {'disputed', 'refunded', 'cancelled_client', 'cancelled_artist'},
    'no_show':           {'disputed', 'refunded', 'cancelled_client', 'cancelled_artist'},
    'refunded':          {'disputed', 'cancelled_client', 'cancelled_artist'},
    'disputed':          {'refunded', 'completed', 'cancelled_client', 'cancelled_artist'},
    'cancelled_client':  {'disputed'},
    'cancelled_artist':  {'disputed'},
}


def transition_booking(conn, bid, to_state, extra_set_sql='', extra_params=()):
    """Move booking `bid` to `to_state`, atomically, iff its current status is
    a legal predecessor per BOOKING_TRANSITIONS (one UPDATE with a
    WHERE status IN (...) clause — the guard is race-safe, not a separate
    SELECT-then-UPDATE). Returns True if the row changed, False if the
    booking doesn't exist or the transition isn't legal from its current
    status. Callers should treat False as a safe no-op (e.g. an out-of-order
    or replayed webhook), not necessarily an error.

    `extra_set_sql` adds more 'col=?, col2=?' fragments to the same UPDATE
    (e.g. 'confirmed_at=?'); `extra_params` supplies their values, in order,
    before `bid`.
    """
    if to_state not in BOOKING_STATUSES:
        raise ValueError(f'unknown booking status {to_state!r}')
    from_states = [s for s, nexts in BOOKING_TRANSITIONS.items() if to_state in nexts]
    if not from_states:
        return False
    prior = conn.execute('SELECT status FROM bookings WHERE id=?', (bid,)).fetchone()
    placeholders = ', '.join('?' for _ in from_states)
    sets = 'status=?' + (', ' + extra_set_sql if extra_set_sql else '')
    params = (to_state,) + tuple(extra_params) + tuple(from_states) + (bid,)
    cur = conn.execute(
        f'UPDATE bookings SET {sets} WHERE status IN ({placeholders}) AND id=?',
        params
    )
    if cur.rowcount == 0:
        return False
    try:
        conn.execute(
            'INSERT INTO booking_status_log (booking_id, from_status, to_status, changed_at) '
            'VALUES (?, ?, ?, ?)',
            (bid, prior['status'] if prior else None, to_state, datetime.utcnow().isoformat())
        )
    except Exception:
        pass
    return True


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


# Popisky jsou anglicky — angličtina je zdrojový jazyk. UI je překládá přes
# klíče `size.*`; server je používá tam, kde se text ukládá natrvalo
# (zpráva s poptávkou, e-mail), a tam by překlad podle prohlížeče odesílatele
# jen zmátl příjemce.
SIZE_PRESETS = {
    # label: (duration_hours, label)
    'mini':     (1, 'Mini'),
    'small':    (2, 'Small'),
    'medium':   (3, 'Medium'),
    'large':    (5, 'Large'),
    'xl':       (8, 'Full day'),
}

# U skici se ceníkem nabízíme tři velikosti. Klíče jsou podmnožina
# SIZE_PRESETS, aby "střední" znamenalo napříč platformou totéž — jinak
# by stejné slovo v ceníku a v rezervaci mohlo znamenat jinou délku.
SKETCH_SIZES = ('small', 'medium', 'large')

# Reference u poptávky. Stejné přípony jako u obrázkové zprávy — chodí
# stejnou cestou a zobrazují se stejnou bublinou.
MESSAGE_IMAGE_EXTS       = ('jpg', 'jpeg', 'png', 'webp', 'gif')
REFERENCE_PHOTO_MAX       = 3
MESSAGE_IMAGE_MAX_BYTES   = 12 * 1024 * 1024
REFERENCE_PHOTO_MAX_BYTES = MESSAGE_IMAGE_MAX_BYTES


def _slot_active_bookings(conn, slot_id, exclude_booking_id=None):
    """Obsazené sub-rangy slotu jako (start_iso, end_iso, buf_before, buf_after).
    Bere v úvahu jen pending_payment + confirmed (zrušené/dokončené nepřekáží).
    `exclude_booking_id` vynechá jednu rezervaci — potřeba při přesunu, aby
    rezervace nekolidovala sama se sebou."""
    sql = '''SELECT id, booking_start_at, booking_end_at,
                    buffer_before_minutes, buffer_after_minutes
             FROM bookings
             WHERE slot_id = ? AND status IN ('pending_payment','confirmed')
                   AND booking_start_at IS NOT NULL
                   AND booking_end_at IS NOT NULL'''
    params = [slot_id]
    if exclude_booking_id is not None:
        sql += ' AND id != ?'
        params.append(exclude_booking_id)
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [(r['booking_start_at'], r['booking_end_at'],
             r['buffer_before_minutes'] or 0, r['buffer_after_minutes'] or 0) for r in rows]


def _ranges_overlap(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and b_start < a_end


def _padded_overlap(a_start, a_end, a_buf_before, a_buf_after,
                    b_start, b_end, b_buf_before, b_buf_after) -> bool:
    """Překryv dvou rezervací včetně bufferů (na vstupu datetime, ne ISO).
    Buffer se schválně smí přetáhnout přes hranice slotu — jinak by úklidový
    buffer potichu ukrajoval tatérovi poslední rezervovatelný čas dne."""
    a_s = a_start - timedelta(minutes=a_buf_before or 0)
    a_e = a_end + timedelta(minutes=a_buf_after or 0)
    b_s = b_start - timedelta(minutes=b_buf_before or 0)
    b_e = b_end + timedelta(minutes=b_buf_after or 0)
    return a_s < b_e and b_s < a_e


def _artist_blocked_overlap(conn, artist_id, start_dt, end_dt) -> bool:
    """True, pokud rozsah zasahuje do blokace volna daného tatéra.
    Kontrola je v rozsahu tatéra (napříč všemi jeho sloty), ne jednoho slotu."""
    row = conn.execute(
        '''SELECT 1 FROM artist_blocked_time
           WHERE artist_id = ? AND start_at < ? AND end_at > ? LIMIT 1''',
        (artist_id, end_dt.isoformat(), start_dt.isoformat())
    ).fetchone()
    return bool(row)


def _resolve_cancel_policy(conn, artist_id):
    """(full_hours, half_hours) — per-tatér override, jinak globální default."""
    row = conn.execute(
        'SELECT cancel_refund_full_hours, cancel_refund_half_hours FROM users WHERE id=?',
        (artist_id,)
    ).fetchone()
    full = (row['cancel_refund_full_hours'] if row else None) or CANCEL_REFUND_FULL_HOURS
    half = (row['cancel_refund_half_hours'] if row else None) or CANCEL_REFUND_HALF_HOURS
    return full, half


def _cz_holiday_warnings(dates):
    """[{date, name}] pro data padající na český státní svátek.
    Varujeme, neblokujeme: tatéři nemají povinnost mít o svátku zavřeno a
    část jich naopak o svátcích chce termíny navíc (klienti mají volno).
    Import je líný a chyba se polyká — chybějící balíček nesmí shodit
    zakládání termínů, jen přijdeme o varování."""
    try:
        import holidays as _holidays
    except Exception:
        return []
    try:
        years = sorted({d.year for d in dates})
        cz = _holidays.country_holidays('CZ', years=years)
        return [{'date': d.isoformat(), 'name': cz.get(d)}
                for d in sorted(set(dates)) if cz.get(d)]
    except Exception as e:
        print(f'[holidays] lookup failed: {e}')
        return []


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
    use_credit        = bool(data.get('use_credit'))
    offer_id          = data.get('offer_id')

    # Přijetí nabídky termínu: parametry jsou dané dohodou v chatu, klient
    # do nich nesahá. Načítáme je až po otevření spojení, viz níž.
    offer = None

    if not offer_id and not slot_id:
        return jsonify({'error': 'slot_id je povinný'}), 400
    # U nabídky popis nechceme — co se dělá, je domluvené v chatu a text
    # nese sama nabídka.
    if not offer_id and not design_note:
        return jsonify({'error': 'Popiš tatérovi co chceš (lokace, motiv, velikost…).'}), 400

    conn = get_db()

    if offer_id:
        offer = conn.execute('SELECT * FROM booking_offers WHERE id=?', (offer_id,)).fetchone()
        if not offer or offer['client_id'] != session['user_id']:
            conn.close(); return jsonify({'error': 'not found'}), 404
        state = _offer_state(offer)
        if state != 'pending':
            conn.close()
            return jsonify({'error': 'Tahle nabídka už neplatí.', 'status': state}), 409
        # Nabídka přebíjí vstup z formuláře — cena i čas jsou domluvené.
        slot_id           = offer['slot_id']
        booking_start_raw = offer['booking_start_at']
        duration_raw      = offer['duration_hours']
        size_label        = ''
        portfolio_item_id = None
        design_note       = design_note or (offer['note'] or 'Custom design')

    slot = conn.execute('SELECT * FROM slots WHERE id=?', (slot_id,)).fetchone()
    if not slot:
        conn.close()
        return jsonify({'error': 'Termín nenalezen.'}), 404
    if slot['user_id'] == session['user_id']:
        conn.close()
        return jsonify({'error': 'Nemůžeš si rezervovat vlastní termín.'}), 400

    price_unit = (slot['price_unit'] if 'price_unit' in slot.keys() else None) or 'hour'
    # studio_id se bere rovnou tady poddotazem — žádný round trip navíc.
    artist = conn.execute('''SELECT id, deposit_pct_default, stripe_charges_enabled, display_name,
                                    (SELECT studio_id FROM studio_members
                                     WHERE artist_id = users.id) AS studio_id
                             FROM users WHERE id=?''', (slot['user_id'],)).fetchone()
    deposit_pct = slot['deposit_pct'] if slot['deposit_pct'] is not None else (artist['deposit_pct_default'] or 30)
    avg_price   = _slot_avg_price(slot)
    avg_hourly  = avg_price  # ze sazby — pro 'hour'

    # Pokud klient rezervuje konkrétní portfolio sketch, načti jeho fixní cenu/délku
    portfolio_item = None
    picked_size    = None   # zvolená varianta ceníku, když ho návrh má
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
        # Když má návrh ceník po velikostech, cena bez zvolené velikosti
        # neexistuje. Tiché spadnutí na "od" cenu by klientovi naúčtovalo
        # malé tetování za velké.
        item_sizes = _load_item_sizes(conn, [portfolio_item_id]).get(portfolio_item_id, [])
        if item_sizes:
            chosen = next((v for v in item_sizes if v['size_label'] == size_label), None)
            if not chosen:
                conn.close()
                return jsonify({'error': 'Vyber velikost tetování.',
                                'sizes': item_sizes}), 400
            picked_size = chosen

    try:
        slot_start = _naive_dt(slot['start_at'])
        slot_end   = _naive_dt(slot['end_at'])
    except Exception:
        conn.close()
        return jsonify({'error': 'Termín má vadný čas.'}), 500
    slot_buf_before = slot['buffer_before_minutes'] if 'buffer_before_minutes' in slot.keys() else 0
    slot_buf_after  = slot['buffer_after_minutes'] if 'buffer_after_minutes' in slot.keys() else 0
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
        total_price    = float(offer['price_kc']) if offer else avg_price   # v Kč (celkem)
        deposit_cents  = int(round(total_price * deposit_pct / 100)) * 100
    else:
        # Hodinový blok — klient si vybírá sub-range
        # 1) Spočti duration — pokud je portfolio sketch, použij jeho odhad
        if picked_size:
            duration_hours = float(picked_size['estimated_hours'])
        elif portfolio_item and portfolio_item['estimated_hours']:
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
                booking_start = _naive_dt(booking_start_raw)
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

        # 3) Kontrola kolize s existujícími rezervacemi (včetně bufferů obou stran)
        for s_iso, e_iso, ex_before, ex_after in _slot_active_bookings(conn, slot_id):
            if _padded_overlap(booking_start, booking_end, slot_buf_before, slot_buf_after,
                               _naive_dt(s_iso), _naive_dt(e_iso), ex_before, ex_after):
                conn.close()
                return jsonify({'error': 'Tento čas se kryje s jinou rezervací — vyber jiný začátek.'}), 409

        # 4) Cena: nabídka > zvolená velikost návrhu > fixní cena návrhu > duration × hourly
        if offer:
            total_price = float(offer['price_kc'])
        elif picked_size:
            total_price = float(picked_size['price_kc'])
        elif portfolio_item and portfolio_item['price_kc']:
            total_price = float(portfolio_item['price_kc'])
        else:
            total_price = avg_hourly * duration_hours
        deposit_cents = int(round(total_price * deposit_pct / 100)) * 100

    # Blokace volna platí napříč všemi sloty tatéra, takže se kontroluje až tady,
    # kde už mají obě větve (flat i hodinová) spočítané booking_start/end.
    if _artist_blocked_overlap(conn, slot['user_id'], booking_start, booking_end):
        conn.close()
        return jsonify({'error': 'Tatér má v tomhle čase blokované volno — vyber jiný termín.'}), 409

    # Celková cena (pro report a balance) — total_price je v Kč, převést na cents
    total_price_cents = int(round(total_price)) * 100

    # M7: pokud klient zvolil "zaplatit celé předem", deposit = total
    payment_mode = 'full' if pay_full else 'deposit'
    if pay_full:
        deposit_cents = total_price_cents
    balance_due_cents = max(0, total_price_cents - deposit_cents)

    # ── Pricing: legacy 8% flat vs. new tiered engine ──────────────────────
    # Feature flag USE_NEW_PRICING_ENGINE controls which path runs.
    # Legacy: flat 8 % on deposit_cents (existing behavior).
    # New: pricing.calculate_booking_economics() with tiered % + service fee
    # + founding programs + (validated) discount. Both compute platform_fee_cents
    # in haler so downstream Stripe code is identical.
    economics_snapshot = None
    discount_applied = None  # {type, amount_czk, code} pokud success
    use_new_engine = os.environ.get('USE_NEW_PRICING_ENGINE', '0') == '1'
    if use_new_engine:
        try:
            from pricing import (
                BookingInput, DiscountInput, calculate_booking_economics,
                validate_discount, emit_event,
                WELCOME_DISCOUNT_CZK, REFERRAL_BONUS_CZK,
            )
            from decimal import Decimal as _Dec
            # Load founding flags for both sides
            artist_full = conn.execute(
                'SELECT founding_artist, founding_artist_started_at FROM users WHERE id = ?',
                (slot['user_id'],)
            ).fetchone()
            client_full = conn.execute(
                'SELECT founding_client FROM users WHERE id = ?',
                (session['user_id'],)
            ).fetchone()
            started_iso = (artist_full['founding_artist_started_at']
                           if artist_full and artist_full['founding_artist'] else None)
            started_dt = None
            if started_iso:
                try:
                    started_dt = datetime.fromisoformat(started_iso)
                except Exception:
                    started_dt = None

            # Discount handling — kód v request body, optional
            discount_code = (data.get('discount_code') or '').strip().upper() if hasattr(data, 'get') else ''
            d_type, d_amount = None, _Dec('0')
            if discount_code == 'WELCOME':
                d_type, d_amount = 'WELCOME', WELCOME_DISCOUNT_CZK
            elif discount_code == 'REFERRAL':
                d_type, d_amount = 'REFERRAL', REFERRAL_BONUS_CZK
            elif discount_code:
                dc = conn.execute(
                    'SELECT * FROM discount_codes WHERE code = ? AND active = 1',
                    (discount_code,)
                ).fetchone()
                if dc and (not dc['max_uses'] or dc['used_count'] < dc['max_uses']):
                    d_type, d_amount = 'MANUAL_PROMO', _Dec(str(dc['amount_czk']))

            econ_input_base = BookingInput(
                gross_price_czk            = _Dec(str(total_price)),
                artist_founding_started_at = started_dt,
                client_founding            = bool(client_full and client_full['founding_client']),
                discount_amount_czk        = _Dec('0'),
                discount_source            = '',
                stripe_card_type           = 'card_eea',  # re-snapshot post-payment with actual
            )

            # Validate discount if user requested one
            if d_type:
                is_new = not bool(conn.execute(
                    "SELECT 1 FROM bookings WHERE client_id = ? AND status IN ('confirmed','completed','paid')",
                    (session['user_id'],)
                ).fetchone()) if d_type == 'WELCOME' else True
                used_before = bool(conn.execute(
                    'SELECT 1 FROM discount_redemptions WHERE user_id = ? AND discount_type = ? AND (discount_code = ? OR discount_code IS NULL)',
                    (session['user_id'], d_type, discount_code if d_type == 'MANUAL_PROMO' else None)
                ).fetchone())
                d_validation = validate_discount(DiscountInput(
                    discount_type        = d_type,
                    discount_amount_czk  = d_amount,
                    booking_input        = econ_input_base,
                    client_is_new        = is_new,
                    client_has_used_code = used_before,
                ))
                if d_validation.valid:
                    discount_applied = {'type': d_type, 'amount_czk': float(d_amount), 'code': discount_code}
                else:
                    # Discount nepasuje — booking pokračuje BEZ discount, ale
                    # emit event aby admin viděl.
                    try:
                        emit_event('discount.rejected', {
                            'user_id': session['user_id'],
                            'code': discount_code,
                            'error_code': d_validation.error_code,
                            'message': d_validation.message,
                        }, conn=conn)
                        conn.commit()
                    except Exception:
                        pass

            # Final economics with (validated) discount
            econ_input = BookingInput(
                gross_price_czk            = _Dec(str(total_price)),
                artist_founding_started_at = started_dt,
                client_founding            = bool(client_full and client_full['founding_client']),
                discount_amount_czk        = d_amount if discount_applied else _Dec('0'),
                discount_source            = d_type if discount_applied else '',
                stripe_card_type           = 'card_eea',
            )
            econ = calculate_booking_economics(econ_input)
            # platform_fee_cents = commission v haler (z deposit ratio,
            # protože v live módu se Stripe charge dělá z deposit_cents, ne total)
            # Při deposit-only platbě: artist_commission je v poměru k total,
            # takže pro deposit charge = commission * (deposit/total). Při full payment
            # = artist_commission přímo.
            if pay_full:
                platform_fee_cents = int(econ.artist_commission * 100)
            else:
                # proporční commission na deposit
                if total_price > 0:
                    ratio = float(deposit_cents) / float(total_price_cents)
                    platform_fee_cents = int(econ.artist_commission * 100 * ratio)
                else:
                    platform_fee_cents = 0
            economics_snapshot = econ.to_dict()
        except Exception as e:
            # Fail-safe: pokud engine vyhodí, padáme zpět na legacy 8 %.
            print(f'[pricing-engine] error, falling back to legacy: {e}')
            platform_fee_cents = int(round(deposit_cents * PLATFORM_COMMISSION_PCT / 100))
    else:
        platform_fee_cents = int(round(deposit_cents * PLATFORM_COMMISSION_PCT / 100))

    demo_mode  = not STRIPE_SECRET_KEY or not artist['stripe_charges_enabled']
    init_status = 'confirmed' if demo_mode else 'pending_payment'

    conn.execute('''INSERT INTO bookings
        (slot_id, artist_id, client_id, status, deposit_cents, platform_fee_cents,
         design_note, confirmed_at,
         booking_start_at, booking_end_at, duration_hours, size_label, portfolio_item_id,
         payment_mode, total_price_cents, balance_due_cents,
         buffer_before_minutes, buffer_after_minutes, studio_id, currency)
        VALUES (?,?,?,?,?,?,?, ?, ?,?,?,?,?, ?,?,?, ?,?,?,?)''',
        (slot_id, slot['user_id'], session['user_id'], init_status,
         deposit_cents, platform_fee_cents, design_note,
         datetime.utcnow().isoformat() if init_status == 'confirmed' else None,
         booking_start.isoformat(), booking_end.isoformat(), duration_hours, size_label,
         portfolio_item['id'] if portfolio_item else None,
         payment_mode, total_price_cents, balance_due_cents,
         slot_buf_before, slot_buf_after, artist['studio_id'],
         _norm_currency(slot['currency'] if 'currency' in slot.keys() else None)))

    if price_unit == 'flat':
        # legacy: zablokuj slot
        conn.execute("UPDATE slots SET status='held' WHERE id=?", (slot_id,))
        if init_status == 'confirmed':
            conn.execute("UPDATE slots SET status='booked' WHERE id=?", (slot_id,))
    # 'hour' bloky zůstávají 'free' — kapacitu řešíme přes booking_start/end overlap

    conn.commit()
    # Ne "poslední řádek klienta" — to je závod, když si klient odešle dvě
    # rezervace naráz. Stejný postup jako follow-up endpoint.
    if not conn._pg:
        bid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    else:
        bid = conn.execute('SELECT lastval()').fetchone()[0]

    if offer:
        # Až tady: kdyby vložení rezervace spadlo, nabídka zůstane přijatelná.
        conn.execute("UPDATE booking_offers SET status='accepted', booking_id=? WHERE id=?",
                     (bid, offer['id']))
        conn.commit()

    # CRM: rezervace zakládá (nebo najde) klientský řádek u daného tatéra.
    # Nesmí shodit rezervaci, když se něco pokazí — je to odvozený záznam.
    try:
        _crm_link_client_on_booking(conn, slot['user_id'], session['user_id'])
    except Exception as e:
        print(f'[crm] client link failed for booking {bid}: {e}')

    # Persist economics snapshot (immutable per-booking ledger entry).
    # kind='initial' = first snapshot at booking creation. Refunds/adjusts
    # create new rows linked to the same booking_id.
    if economics_snapshot:
        try:
            import json as _json_econ
            conn.execute(
                "INSERT INTO economics_snapshots (booking_id, kind, snapshot) VALUES (?, 'initial', ?)",
                (bid, _json_econ.dumps(economics_snapshot, ensure_ascii=False))
            )
            conn.commit()
            from pricing import emit_event as _emit_econ
            _emit_econ('booking.economics_calculated', {
                'booking_id': bid, 'snapshot': economics_snapshot,
            }, conn=conn)
            conn.commit()

            # Record discount redemption (per-user audit trail + max_uses tracking)
            if discount_applied:
                try:
                    conn.execute(
                        '''INSERT INTO discount_redemptions
                           (user_id, booking_id, discount_type, discount_code, amount_czk)
                           VALUES (?, ?, ?, ?, ?)''',
                        (session['user_id'], bid, discount_applied['type'],
                         discount_applied['code'] if discount_applied['type'] == 'MANUAL_PROMO' else None,
                         int(discount_applied['amount_czk']))
                    )
                    if discount_applied['type'] == 'MANUAL_PROMO':
                        conn.execute(
                            'UPDATE discount_codes SET used_count = used_count + 1 WHERE code = ?',
                            (discount_applied['code'],)
                        )
                    conn.commit()
                    _emit_econ('discount.applied', {
                        'user_id': session['user_id'], 'booking_id': bid,
                        **discount_applied,
                    }, conn=conn)
                    conn.commit()
                except Exception as _de:
                    print(f'[discount-redemption] {_de}')
        except Exception as e:
            print(f'[economics-snapshot] persist failed for booking {bid}: {e}')

    # ── Kredit ────────────────────────────────────────────────────────────
    # Kredit snižuje, co klient platí kartou — ale NE to, co dostane tatér.
    # Rozdíl doplácíme my z peněz, které za kredit držíme. Kdybychom místo
    # toho poslali tatérovi míň, zaplatil by cizí dárkový poukaz on.
    credit_used_cents = 0
    if use_credit:
        available = _credit_balance(conn, session['user_id'])
        chargeable = total_price_cents if payment_mode == 'full' else deposit_cents
        credit_used_cents = min(available, chargeable)
        if credit_used_cents > 0:
            if _credit_move(conn, session['user_id'], -credit_used_cents,
                            'booking_spend', 'booking', bid) is None:
                credit_used_cents = 0
            else:
                conn.execute(
                    'UPDATE bookings SET credit_used_cents=?, platform_owes_artist_cents=? '
                    'WHERE id=?',
                    (credit_used_cents, credit_used_cents, bid))
                conn.commit()

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

    # ── Sprint 1 LITE: create deposit PaymentIntent in live mode ──────────────
    # Gated behind ENABLE_DEPOSIT_PI env flag for safe rollout. Without the
    # flag, behavior is unchanged from before (booking sits in pending_payment
    # forever in live mode — broken but stable). With the flag, we create a
    # PI and return client_secret for frontend Stripe Elements.
    payment_block = {'mode': 'demo'} if demo_mode else None
    enable_deposit_pi = os.environ.get('ENABLE_DEPOSIT_PI', '0') == '1'
    if not demo_mode and enable_deposit_pi:
        try:
            charge_cents = total_price_cents if payment_mode == 'full' else deposit_cents
            # Kartou se strhává jen to, co kredit nepokryl.
            charge_cents = max(0, charge_cents - credit_used_cents)
            day = int(time.time() // 86400)
            pi = stripe.PaymentIntent.create(
                amount=charge_cents,
                currency=_norm_currency(slot['currency'] if 'currency' in slot.keys() else None).lower(),
                description=f'InkLink — záloha za rezervaci #{bid}',
                application_fee_amount=platform_fee_cents,
                transfer_data={'destination': artist['stripe_account_id'] if 'stripe_account_id' in artist.keys() else None},
                metadata={
                    'inklink_booking_id': str(bid),
                    'inklink_kind': 'deposit' if payment_mode != 'full' else 'full_payment',
                },
                idempotency_key=f'deposit-{slot_id}-{session["user_id"]}-{day}',
            )
            conn.execute(
                'UPDATE bookings SET stripe_payment_intent_id=?, payment_attempts=1 WHERE id=?',
                (pi.id, bid)
            )
            conn.commit()
            payment_block = {
                'mode': 'live',
                'client_secret': pi.client_secret,
                'publishable_key': STRIPE_PUBLIC_KEY,
                'payment_url': f'/pay/{bid}',
            }
        except Exception as e:
            # Don't roll back the booking — let user retry via /retry-payment-intent.
            # Booking sits in pending_payment until they retry.
            app.logger.error(f'[deposit-pi] create failed for booking {bid}: {e}')
            payment_block = {'mode': 'live', 'error': 'Stripe momentálně nedostupný, zkus za chvíli.',
                             'payment_url': f'/pay/{bid}'}

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
        'credit_used_kc':   credit_used_cents // 100,
        'total_price_cents': total_price_cents,
        'duration_hours': duration_hours,
        'booking_start_at': booking_start.isoformat(),
        'booking_end_at':   booking_end.isoformat(),
        'total_price_kc':   round(total_price),
        'payment':          payment_block,
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

    # internal_note — soukromá poznámka tatéra, klient ji nikdy nevidí
    # ani needituje. Dřív se pole tiše zahodilo a endpoint vrátil ok,
    # takže UI hlásilo "uloženo" a v DB nebylo nic.
    if 'internal_note' in data:
        if not is_artist:
            conn.close()
            return jsonify({'error': 'Soukromou poznámku může měnit jen tatér.'}), 403
        sets.append('internal_note=?')
        params.append((data['internal_note'] or '').strip()[:2000])

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

        if new_start < _prague_now_naive() - timedelta(minutes=5):
            conn.close(); return jsonify({'error': 'Nový termín nemůže být v minulosti'}), 400

        # validace vůči slotu
        slot = conn.execute('SELECT * FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
        if not slot:
            conn.close(); return jsonify({'error': 'Slot rezervace neexistuje'}), 500
        try:
            slot_start = _naive_dt(slot['start_at'])
            slot_end   = _naive_dt(slot['end_at'])
        except Exception:
            conn.close(); return jsonify({'error': 'Slot má vadný čas'}), 500
        if new_start < slot_start - timedelta(minutes=1) or new_end > slot_end + timedelta(minutes=1):
            conn.close(); return jsonify({'error': 'Nový termín nesedí do bloku tatéra ('
                                           + slot_start.strftime('%H:%M') + '–'
                                           + slot_end.strftime('%H:%M') + ').'}), 400

        if _artist_blocked_overlap(conn, b['artist_id'], new_start, new_end):
            conn.close()
            return jsonify({'error': 'V tomhle čase máš blokované volno.'}), 409

        # overlap s jinými aktivními bookings (vyjma self), včetně bufferů
        my_before = b['buffer_before_minutes'] if 'buffer_before_minutes' in b.keys() else 0
        my_after  = b['buffer_after_minutes'] if 'buffer_after_minutes' in b.keys() else 0
        for s_iso, e_iso, ex_before, ex_after in _slot_active_bookings(conn, b['slot_id'], exclude_booking_id=bid):
            if _padded_overlap(new_start, new_end, my_before, my_after,
                               _naive_dt(s_iso), _naive_dt(e_iso), ex_before, ex_after):
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
            'session_number':      r['session_number'] or 1,
            'parent_booking_id':   r['parent_booking_id'],
            'internal_note':       (r['internal_note'] or '') if role == 'artist' else '',
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


def _cancellation_refund_pct(hours_before: float, actor: str,
                             full_hours: int = None, half_hours: int = None) -> int:
    """Vrátí % refundu podle pravidel storna.
    `full_hours`/`half_hours` umožňují per-tatér override; None = globální default."""
    if actor == 'artist':
        return 100
    full = full_hours if full_hours is not None else CANCEL_REFUND_FULL_HOURS
    half = half_hours if half_hours is not None else CANCEL_REFUND_HALF_HOURS
    if hours_before >= full:
        return 100
    if hours_before >= half:
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
    # Přesnější než start slotu: rezervace může začínat uprostřed bloku.
    start_raw = b['booking_start_at'] or (slot['start_at'] if slot else None)
    try:
        start_dt = _naive_dt(start_raw)
    except Exception:
        start_dt = _prague_now_naive() + timedelta(days=7)
    hours_before = (start_dt - _prague_now_naive()).total_seconds() / 3600.0
    full_h, half_h = _resolve_cancel_policy(conn, b['artist_id'])
    refund_pct   = _cancellation_refund_pct(hours_before, actor, full_h, half_h)
    refund_cents = int(round(b['deposit_cents'] * refund_pct / 100))
    new_status   = 'cancelled_artist' if actor == 'artist' else 'cancelled_client'

    # Trigger Stripe refund if there's something to refund and a payment exists.
    # If Stripe fails, abort the cancellation — user can retry. The webhook
    # charge.refunded will write the economics_snapshots row.
    pi_id = b['stripe_payment_intent_id'] if 'stripe_payment_intent_id' in b.keys() else None
    if refund_cents > 0 and pi_id and STRIPE_SECRET_KEY:
        try:
            stripe.Refund.create(
                payment_intent=pi_id,
                amount=refund_cents,
                reason='requested_by_customer',
                idempotency_key=f'cancel-{bid}-{actor}',
                metadata={'inklink_booking_id': str(bid), 'inklink_actor': actor},
                # Destination charge: bez tohohle by InkLink refundovala z vlastního
                # balance a nechala si komisi i po vrácení peněz klientovi.
                reverse_transfer=True,
                refund_application_fee=True,
            )
        except Exception as e:
            conn.close()
            app.logger.error(f'[cancel] stripe refund failed for booking {bid}: {e}')
            return jsonify({'error': f'Refund se nepodařilo zpracovat: {e}'}), 502

    moved = transition_booking(
        conn, bid, new_status,
        extra_set_sql='cancelled_at=?, cancellation_actor=?, refund_cents=?',
        extra_params=(datetime.utcnow().isoformat(), actor, refund_cents),
    )
    if not moved:
        # Status changed concurrently between the guard above and here (e.g. a
        # webhook landed mid-request). Refund, if any, already went through —
        # don't also claim the booking as cancelled when it may no longer be.
        conn.commit()
        conn.close()
        return jsonify({'error': 'Stav rezervace se mezitím změnil, zkus to prosím znovu.'}), 409
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


# ── Přesun rezervace (reschedule) ────────────────────────────────────────────
# Tatér přesouvá vždy hned (rozšíření důvěry, kterou už má přes PATCH
# /api/bookings/<id>). Klient hned jen ≥ RESCHEDULE_FREE_HOURS předem, jinak
# vznikne žádost čekající na tatéra. Kolize se validují VŽDY, bez ohledu na
# aktéra i lhůtu. bookings.status se přesunem nemění — „čeká na přesun" je
# ortogonální k platebnímu stavu, stejně jako u refund_requests.

RESCHEDULE_FREE_HOURS = 48


def _validate_reschedule_target(conn, b, new_slot_id, start_raw, duration_raw, size_label):
    """Společná validace pro přesun i follow-up.
    Vrací (payload_dict, None) nebo (None, (error_json, status_code))."""
    slot = conn.execute('SELECT * FROM slots WHERE id=?', (new_slot_id,)).fetchone()
    if not slot:
        return None, ({'error': 'Cílový termín neexistuje.'}, 404)
    if slot['user_id'] != b['artist_id']:
        return None, ({'error': 'Cílový termín patří jinému tatérovi.'}, 400)

    try:
        slot_start = _naive_dt(slot['start_at'])
        slot_end   = _naive_dt(slot['end_at'])
    except Exception:
        return None, ({'error': 'Cílový termín má vadný čas.'}, 500)

    # Délka: explicitní > z velikosti > původní rezervace
    duration_h = None
    if duration_raw not in (None, ''):
        try:
            duration_h = float(duration_raw)
        except (ValueError, TypeError):
            return None, ({'error': 'Špatná délka.'}, 400)
    elif size_label and size_label in SIZE_PRESETS:
        duration_h = float(SIZE_PRESETS[size_label][0])
    else:
        duration_h = float(b['duration_hours'] or 0)
    if duration_h <= 0:
        return None, ({'error': 'Chybí délka rezervace.'}, 400)

    min_dur = slot['min_duration_hours'] if 'min_duration_hours' in slot.keys() else 1
    if duration_h < (min_dur or 1):
        return None, ({'error': f'Tatér přijímá min. {min_dur} h sezení.'}, 400)

    try:
        new_start = _naive_dt(start_raw) if start_raw else slot_start
    except ValueError:
        return None, ({'error': 'Špatný formát začátku.'}, 400)
    new_end = new_start + timedelta(hours=duration_h)

    if new_start < _prague_now_naive() - timedelta(minutes=5):
        return None, ({'error': 'Nový termín nemůže být v minulosti.'}, 400)
    if new_start < slot_start - timedelta(minutes=1) or new_end > slot_end + timedelta(minutes=1):
        return None, ({'error': 'Nový čas nesedí do vybraného bloku ('
                                + slot_start.strftime('%H:%M') + '–'
                                + slot_end.strftime('%H:%M') + ').'}, 400)

    if _artist_blocked_overlap(conn, b['artist_id'], new_start, new_end):
        return None, ({'error': 'Tatér má v tomhle čase blokované volno.'}, 409)

    buf_before = slot['buffer_before_minutes'] if 'buffer_before_minutes' in slot.keys() else 0
    buf_after  = slot['buffer_after_minutes'] if 'buffer_after_minutes' in slot.keys() else 0
    for s_iso, e_iso, ex_before, ex_after in _slot_active_bookings(conn, new_slot_id,
                                                                   exclude_booking_id=b['id']):
        if _padded_overlap(new_start, new_end, buf_before, buf_after,
                           _naive_dt(s_iso), _naive_dt(e_iso), ex_before, ex_after):
            return None, ({'error': 'Vybraný čas se kryje s jinou rezervací.'}, 409)

    return {'slot_id': new_slot_id, 'start': new_start, 'end': new_end,
            'duration_h': duration_h, 'buf_before': buf_before, 'buf_after': buf_after,
            'size_label': size_label or (b['size_label'] or '')}, None


@app.route('/api/bookings/<int:bid>/reschedule', methods=['PATCH'])
def reschedule_booking(bid):
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or request.form

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if uid not in (b['client_id'], b['artist_id']):
        conn.close(); return jsonify({'error': 'forbidden'}), 403
    if b['status'] in ('completed', 'cancelled_client', 'cancelled_artist', 'no_show'):
        conn.close(); return jsonify({'error': f'Rezervaci ve stavu {b["status"]} nelze přesunout.'}), 409

    new_slot_id = data.get('new_slot_id') or b['slot_id']
    try:
        new_slot_id = int(new_slot_id)
    except (ValueError, TypeError):
        conn.close(); return jsonify({'error': 'Špatné new_slot_id.'}), 400

    target, verr = _validate_reschedule_target(
        conn, b, new_slot_id, (data.get('booking_start_at') or '').strip(),
        data.get('duration_hours'), (data.get('size_label') or '').strip().lower())
    if verr:
        conn.close(); return jsonify(verr[0]), verr[1]

    is_artist = (uid == b['artist_id'])
    try:
        cur_start = _naive_dt(b['booking_start_at']) if b['booking_start_at'] else None
    except Exception:
        cur_start = None
    hours_before = ((cur_start - _prague_now_naive()).total_seconds() / 3600.0
                    if cur_start else 9999)

    # Klient pozdě → žádost místo okamžitého přesunu.
    if not is_artist and hours_before < RESCHEDULE_FREE_HOURS:
        dupe = conn.execute(
            "SELECT id FROM booking_reschedule_requests WHERE booking_id=? AND status='pending'",
            (bid,)).fetchone()
        if dupe:
            conn.close()
            return jsonify({'error': 'Pro tuhle rezervaci už čeká žádost o přesun.',
                            'request_id': dupe['id']}), 409
        conn.execute('''INSERT INTO booking_reschedule_requests
                        (booking_id, requested_by, new_slot_id,
                         new_booking_start_at, new_booking_end_at)
                        VALUES (?,?,?,?,?)''',
                     (bid, uid, target['slot_id'],
                      target['start'].isoformat(), target['end'].isoformat()))
        conn.commit()
        rid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
               else conn.execute('SELECT lastval()').fetchone()[0])
        push_notif(conn, b['artist_id'], uid, 'reschedule_requested', bid, 'booking',
                   f'Klient žádá o přesun rezervace na {target["start"].strftime("%d.%m. %H:%M")}.')
        conn.commit()

        # Mail navíc k in-app notifikaci: žádost čeká na tatéra a dokud ji
        # nevyřídí, termín se nehne. Kdyby se o ní dozvěděl až při příštím
        # otevření appky, může to být po původním termínu.
        # send_booking_email nikdy nevyhazuje — výpadek Resendu nesmí shodit
        # samotnou žádost, ta je už uložená a commitnutá.
        requester = conn.execute('SELECT display_name, username FROM users WHERE id=?',
                                 (uid,)).fetchone()
        send_booking_email(conn, b['artist_id'], 'reschedule_requested_for_artist', {
            'other_name':   (requester['display_name'] or requester['username']) if requester else '',
            'current_when': _fmt_booking_when(b['booking_start_at'], b['duration_hours']),
            'when':         _fmt_booking_when(target['start'].isoformat(), b['duration_hours']),
            'booking_url':  APP_BASE_URL + '/calendar',
        })
        conn.close()
        return jsonify({'ok': True, 'applied': False, 'status': 'pending',
                        'request_id': rid, 'hours_before': round(hours_before, 1)})

    _apply_reschedule(conn, bid, target)
    conn.commit()
    other_id = b['artist_id'] if uid == b['client_id'] else b['client_id']
    push_notif(conn, other_id, uid, 'booking_rescheduled', bid, 'booking',
               f'Rezervace přesunuta na {target["start"].strftime("%d.%m. %H:%M")}.')
    conn.commit()
    updated = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'applied': True, 'booking': dict(updated)})


def _apply_reschedule(conn, bid, target):
    """Zapíše nový čas na rezervaci. Cena se schválně nemění — přesun mění
    KDY, ne ZA KOLIK."""
    conn.execute('''UPDATE bookings
                    SET slot_id=?, booking_start_at=?, booking_end_at=?, duration_hours=?,
                        size_label=?, buffer_before_minutes=?, buffer_after_minutes=?
                    WHERE id=?''',
                 (target['slot_id'], target['start'].isoformat(), target['end'].isoformat(),
                  target['duration_h'], target['size_label'],
                  target['buf_before'], target['buf_after'], bid))


@app.route('/api/reschedule-requests')
def list_reschedule_requests():
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    rows = conn.execute('''
        SELECT rr.*, b.artist_id, b.client_id, b.booking_start_at AS current_start_at,
               uc.display_name AS client_name, ua.display_name AS artist_name
        FROM booking_reschedule_requests rr
        JOIN bookings b ON rr.booking_id = b.id
        JOIN users uc ON b.client_id = uc.id
        JOIN users ua ON b.artist_id = ua.id
        WHERE b.client_id = ? OR b.artist_id = ?
        ORDER BY rr.created_at DESC LIMIT 100
    ''', (uid, uid)).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/reschedule-requests/<int:rid>/decide', methods=['POST'])
def decide_reschedule_request(rid):
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or request.form
    decision = (data.get('decision') or '').strip().lower()
    note = (data.get('note') or '').strip()[:500]
    if decision not in ('approve', 'reject'):
        return jsonify({'error': 'decision musí být approve|reject'}), 400

    conn = get_db()
    rr = conn.execute('SELECT * FROM booking_reschedule_requests WHERE id=?', (rid,)).fetchone()
    if not rr:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if rr['status'] != 'pending':
        conn.close(); return jsonify({'error': 'Žádost už byla vyřešena.'}), 409
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (rr['booking_id'],)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'Rezervace neexistuje.'}), 404
    if uid != b['artist_id'] and not is_admin_user(uid):
        conn.close(); return jsonify({'error': 'Rozhodnout může jen tatér nebo admin.'}), 403

    if decision == 'approve':
        # Znovu validovat — mezi podáním žádosti a rozhodnutím se cílový čas
        # mohl obsadit nebo zablokovat.
        target, verr = _validate_reschedule_target(
            conn, b, rr['new_slot_id'], rr['new_booking_start_at'], None, b['size_label'] or '')
        if verr:
            conn.close()
            return jsonify({'error': f'Termín už nelze potvrdit: {verr[0]["error"]}'}), verr[1]
        _apply_reschedule(conn, b['id'], target)

    conn.execute('''UPDATE booking_reschedule_requests
                    SET status=?, decision_by=?, decision_note=?, resolved_at=CURRENT_TIMESTAMP
                    WHERE id=?''',
                 ('approved' if decision == 'approve' else 'rejected', uid, note, rid))
    conn.commit()
    push_notif(conn, rr['requested_by'], uid,
               'reschedule_' + ('approved' if decision == 'approve' else 'rejected'),
               b['id'], 'booking',
               'Přesun rezervace schválen.' if decision == 'approve'
               else 'Tatér přesun rezervace zamítl.')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'status': 'approved' if decision == 'approve' else 'rejected'})


@app.route('/api/bookings/<int:bid>/follow-up', methods=['POST'])
def create_follow_up_booking(bid):
    """Tatér naplánuje další sezení navazující na existující rezervaci.
    Dítě série nemá zálohu (ta se platila u prvního sezení) — doplatek se
    řeší stávající cestou přes /complete."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or request.form

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if uid != b['artist_id']:
        conn.close(); return jsonify({'error': 'Další sezení může naplánovat jen tatér.'}), 403

    new_slot_id = data.get('new_slot_id') or b['slot_id']
    try:
        new_slot_id = int(new_slot_id)
    except (ValueError, TypeError):
        conn.close(); return jsonify({'error': 'Špatné new_slot_id.'}), 400

    target, verr = _validate_reschedule_target(
        conn, b, new_slot_id, (data.get('booking_start_at') or '').strip(),
        data.get('duration_hours'), (data.get('size_label') or '').strip().lower())
    if verr:
        conn.close(); return jsonify(verr[0]), verr[1]

    # Řetěz se plochý: dítě ukazuje vždy na první sezení série.
    root_id = b['parent_booking_id'] or b['id']
    next_num = (conn.execute(
        'SELECT MAX(session_number) AS m FROM bookings WHERE id=? OR parent_booking_id=?',
        (root_id, root_id)).fetchone()['m'] or 1) + 1

    total_price_cents = 0
    try:
        total_price_cents = int(data.get('total_price_cents') or 0)
    except (ValueError, TypeError):
        total_price_cents = 0

    conn.execute('''INSERT INTO bookings
        (slot_id, artist_id, client_id, status, deposit_cents, platform_fee_cents,
         design_note, confirmed_at, booking_start_at, booking_end_at, duration_hours,
         size_label, payment_mode, total_price_cents, balance_due_cents,
         buffer_before_minutes, buffer_after_minutes, parent_booking_id, session_number,
         studio_id)
        VALUES (?,?,?,'confirmed',0,0,?,?,?,?,?,?,'deposit',?,?,?,?,?,?,?)''',
        (target['slot_id'], b['artist_id'], b['client_id'],
         (data.get('design_note') or b['design_note'] or '').strip()[:1000],
         datetime.utcnow().isoformat(),
         target['start'].isoformat(), target['end'].isoformat(), target['duration_h'],
         target['size_label'], total_price_cents, total_price_cents,
         target['buf_before'], target['buf_after'], root_id, next_num,
         # Série zůstane ve studiu, kde začala — tatér mohl mezitím přejít jinam.
         b['studio_id']))
    conn.commit()
    child_id = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
                else conn.execute('SELECT lastval()').fetchone()[0])
    push_notif(conn, b['client_id'], uid, 'follow_up_scheduled', child_id, 'booking',
               f'Tatér naplánoval další sezení na {target["start"].strftime("%d.%m. %H:%M")}.')
    conn.commit()
    child = conn.execute('SELECT * FROM bookings WHERE id=?', (child_id,)).fetchone()
    conn.close()
    return jsonify({'ok': True, 'id': child_id, 'session_number': next_num,
                    'booking': dict(child)})


@app.route('/api/bookings/<int:bid>/mark-no-show', methods=['POST'])
def mark_no_show(bid):
    """Tatér (nebo admin) označí, že klient na potvrzenou rezervaci nedorazil.
    Záloha propadá (žádný refund) — lze označit až po proběhlém termínu."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if uid != b['artist_id'] and not is_admin_user(uid):
        conn.close()
        return jsonify({'error': 'forbidden'}), 403
    if b['status'] != 'confirmed':
        conn.close()
        return jsonify({'error': 'Jen potvrzenou rezervaci lze označit jako no-show.'}), 409

    slot = conn.execute('SELECT * FROM slots WHERE id=?', (b['slot_id'],)).fetchone()
    try:
        start_dt = datetime.fromisoformat(slot['start_at'].replace('Z', '+00:00'))
    except Exception:
        start_dt = None
    if start_dt and start_dt > datetime.utcnow():
        conn.close()
        return jsonify({'error': 'Termín ještě neproběhl.'}), 409

    if not transition_booking(conn, bid, 'no_show'):
        conn.close()
        return jsonify({'error': 'Stav rezervace se mezitím změnil, zkus to prosím znovu.'}), 409
    conn.commit()
    push_notif(conn, b['client_id'], uid, 'booking_no_show', bid, 'booking',
               'Rezervace označena jako no-show — záloha propadá.')
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'status': 'no_show'})


@app.route('/api/me/export', methods=['GET'])
@limiter.limit('5 per hour')
def my_export():
    """GDPR Article 20 — data portability. Returns a ZIP of JSON files with
    everything we have about the current user. Rate-limited (5/h) to discourage
    abuse, but the user can always request again later.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']

    import io as _io
    import zipfile as _zf
    import json as _json

    conn = get_db()

    def fetch_dicts(sql, params=()):
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # Profile (drop password_hash + verify_code — never export auth secrets)
    user = conn.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
    if not user:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    profile = dict(user)
    for sensitive in ('password_hash', 'verify_code', 'verify_expires'):
        profile.pop(sensitive, None)

    bookings_as_client = fetch_dicts(
        'SELECT * FROM bookings WHERE client_id=? ORDER BY id DESC', (uid,)
    )
    bookings_as_artist = fetch_dicts(
        'SELECT * FROM bookings WHERE artist_id=? ORDER BY id DESC', (uid,)
    )
    portfolio = fetch_dicts(
        'SELECT * FROM portfolio_items WHERE user_id=? ORDER BY id DESC', (uid,)
    )
    messages_sent = fetch_dicts(
        'SELECT * FROM messages WHERE sender_id=? ORDER BY id DESC', (uid,)
    )
    messages_received = fetch_dicts(
        'SELECT * FROM messages WHERE receiver_id=? ORDER BY id DESC', (uid,)
    )
    refund_requests = fetch_dicts(
        'SELECT * FROM refund_requests WHERE client_id=? OR artist_id=? ORDER BY id DESC',
        (uid, uid)
    )
    referrals_made = fetch_dicts(
        'SELECT * FROM referrals WHERE referrer_user_id=? ORDER BY id DESC', (uid,)
    )
    referrals_received = fetch_dicts(
        'SELECT * FROM referrals WHERE referred_user_id=? ORDER BY id DESC', (uid,)
    )
    reviews_written = fetch_dicts(
        'SELECT * FROM reviews WHERE client_id=? ORDER BY id DESC', (uid,)
    )
    reviews_received = fetch_dicts(
        'SELECT * FROM reviews WHERE artist_id=? ORDER BY id DESC', (uid,)
    )
    # Notifications are user-scoped
    notifications = fetch_dicts(
        'SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 1000',
        (uid,)
    )
    # Push subscriptions (mask the device token — keep only provider + created_at)
    push_subs_raw = conn.execute(
        "SELECT id, provider, platform, created_at FROM push_subscriptions WHERE user_id=?",
        (uid,)
    ).fetchall()
    push_subscriptions = [dict(r) for r in push_subs_raw]

    conn.close()

    bundle = {
        'export_metadata': {
            'generated_at_utc': datetime.utcnow().isoformat(),
            'user_id':          uid,
            'gdpr_article':     'Article 20 — Right to data portability',
            'note':             'Password hashes and verification codes are intentionally excluded.',
        },
        'profile':              profile,
        'bookings_as_client':   bookings_as_client,
        'bookings_as_artist':   bookings_as_artist,
        'portfolio':            portfolio,
        'messages_sent':        messages_sent,
        'messages_received':    messages_received,
        'refund_requests':      refund_requests,
        'referrals_made':       referrals_made,
        'referrals_received':   referrals_received,
        'reviews_written':      reviews_written,
        'reviews_received':     reviews_received,
        'notifications':        notifications,
        'push_subscriptions':   push_subscriptions,
    }

    # Build ZIP in-memory: one JSON file per section + README.
    buf = _io.BytesIO()
    with _zf.ZipFile(buf, 'w', _zf.ZIP_DEFLATED) as zf:
        zf.writestr(
            'README.txt',
            'InkLink — export osobních údajů (GDPR článek 20)\n\n'
            'Tento ZIP obsahuje všechna data, která o tobě máme uložena.\n'
            'Soubory jsou ve formátu JSON (čitelný strojově i lidsky).\n\n'
            'Pokud chceš data smazat, napiš nám na gdpr@inklink.cz.\n'
        )
        for key, value in bundle.items():
            zf.writestr(f'{key}.json', _json.dumps(value, ensure_ascii=False, indent=2, default=str))

    buf.seek(0)
    from flask import send_file
    fname = f'inklink-export-{user["username"]}-{datetime.utcnow().strftime("%Y%m%d")}.zip'
    return send_file(buf, mimetype='application/zip', as_attachment=True, download_name=fname)


ACCOUNT_DELETION_GRACE_DAYS = 30


def _anonymize_user(conn, uid: int) -> None:
    """Scrub PII from users row. Keeps id (FK integrity) + accounting joins.
    Sets deleted_at to mark the row as terminal. Idempotent."""
    import secrets as _secrets
    # Free up the username slot but keep it referentially valid via a stable
    # placeholder. Two anonymized users with same id won't collide.
    placeholder = f'deleted-{uid}'
    random_hash = 'deleted!' + _secrets.token_hex(16)  # unguessable, locks login
    now_iso = datetime.utcnow().isoformat()
    conn.execute('''
        UPDATE users SET
            username      = ?,
            display_name  = 'Smazaný účet',
            email         = '',
            phone         = '',
            city          = '',
            bio           = '',
            avatar        = '',
            studio        = '',
            instagram     = '',
            styles        = '',
            lat           = NULL,
            lng           = NULL,
            password_hash = ?,
            verify_code   = NULL,
            verify_expires= NULL,
            calendar_token= NULL,
            deleted_at    = ?
        WHERE id = ?
    ''', (placeholder, random_hash, now_iso, uid))
    # Wipe portfolio items (privacy policy: "Portfolio se smaže s účtem")
    conn.execute('DELETE FROM portfolio_item_sizes WHERE item_id IN '
                 '(SELECT id FROM portfolio_items WHERE user_id = ?)', (uid,))
    conn.execute('DELETE FROM portfolio_items WHERE user_id = ?', (uid,))
    # Clear active push subscriptions
    conn.execute('DELETE FROM push_subscriptions WHERE user_id = ?', (uid,))


@app.route('/api/me/delete', methods=['POST'])
def request_account_deletion():
    """Request soft deletion. 30-day grace; cron does the actual anonymization.
    Rejects if user has future bookings — they must be cancelled first per ToS.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    typed = (data.get('confirm_username') or '').strip().lower()

    conn = get_db()
    u = conn.execute('SELECT username, deletion_requested_at, deleted_at FROM users WHERE id=?',
                     (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if u['deleted_at']:
        conn.close()
        return jsonify({'error': 'Účet je už smazán.'}), 409
    if u['deletion_requested_at']:
        conn.close()
        return jsonify({'error': 'Žádost o smazání už čeká.'}), 409

    # Confirmation by username — prevents accidental deletion via XSS / CSRF / muscle memory
    if typed != (u['username'] or '').lower():
        conn.close()
        return jsonify({'error': 'Pro potvrzení napiš své uživatelské jméno přesně.'}), 400

    # Block deletion when there are future bookings (confirmed or pending payment)
    now_iso = datetime.utcnow().isoformat()
    active = conn.execute('''
        SELECT COUNT(*) AS c FROM bookings
        WHERE (client_id = ? OR artist_id = ?)
          AND status IN ('confirmed', 'pending_payment')
          AND booking_start_at IS NOT NULL
          AND booking_start_at > ?
    ''', (uid, uid, now_iso)).fetchone()
    if (active['c'] or 0) > 0:
        conn.close()
        return jsonify({
            'error': f'Máš ještě {active["c"]} aktivní rezervaci. Zruš ji v /my-bookings a pak požádej znovu.',
            'active_bookings': active['c'],
        }), 409

    purge_at = (datetime.utcnow() + timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)).isoformat()
    conn.execute('UPDATE users SET deletion_requested_at = ? WHERE id = ?', (now_iso, uid))
    conn.commit()

    try:
        from pricing import emit_event as _emit
        _emit('account.deletion_requested', {'user_id': uid}, conn=conn)
        conn.commit()
    except Exception:
        pass

    # Email confirmation if Resend configured
    try:
        user_email = conn.execute('SELECT email, display_name FROM users WHERE id=?', (uid,)).fetchone()
        if user_email and user_email['email'] and RESEND_API_KEY:
            base = APP_BASE_URL or 'https://www.inklink.club'
            send_email(user_email['email'], 'InkLink — žádost o smazání účtu přijata', f'''
            <div style="background:#000;padding:24px 0"><div style="background:#0a0a0a;color:#ccc;font-family:Helvetica,Arial,sans-serif;max-width:560px;margin:0 auto;padding:24px;border:1px solid #1a1a1a">
              <h1 style="color:#eee;font-size:22px;letter-spacing:0.06em;margin:0 0 12px">Žádost o smazání přijata</h1>
              <p style="color:#bbb;font-size:14px;line-height:1.7">Ahoj {(user_email['display_name'] or '').strip() or 'tam'},</p>
              <p style="color:#bbb;font-size:14px;line-height:1.7">Tvůj účet bude trvale anonymizován <b style="color:#eee">{ACCOUNT_DELETION_GRACE_DAYS} dní</b> ode dneška. Do té doby si můžeš žádost rozmyslet a zrušit ji v nastavení.</p>
              <p style="color:#bbb;font-size:14px;line-height:1.7"><a href="{base}/artist-setup#account" style="color:#c62828">Otevřít nastavení účtu</a></p>
              <p style="color:#888;font-size:12px;line-height:1.7;margin-top:18px">Po anonymizaci se ztratí: profil, portfolio, profilové údaje. Záznamy o platbách zůstanou v účetnictví po dobu 10 let (zákon o účetnictví) — ale bez vazby na tvou totožnost.</p>
            </div></div>
            ''')
    except Exception:
        pass

    conn.close()
    # Log the user out so the "delete" feels final
    session.clear()
    return jsonify({
        'ok': True,
        'requested_at': now_iso,
        'purge_at': purge_at,
        'grace_days': ACCOUNT_DELETION_GRACE_DAYS,
    })


@app.route('/api/me/delete-cancel', methods=['POST'])
def cancel_account_deletion():
    """Cancel a pending deletion request (only valid before purge_at)."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    u = conn.execute('SELECT deletion_requested_at, deleted_at FROM users WHERE id=?',
                     (uid,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if u['deleted_at']:
        conn.close()
        return jsonify({'error': 'Účet je už nenávratně smazán.'}), 410
    if not u['deletion_requested_at']:
        conn.close()
        return jsonify({'error': 'Žádná žádost o smazání neexistuje.'}), 404
    conn.execute('UPDATE users SET deletion_requested_at = NULL WHERE id = ?', (uid,))
    conn.commit()
    try:
        from pricing import emit_event as _emit
        _emit('account.deletion_cancelled', {'user_id': uid}, conn=conn)
        conn.commit()
    except Exception:
        pass
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/cron/account-deletions', methods=['GET', 'POST'])
@limiter.limit('30 per hour')
def cron_account_deletions():
    """Daily cron — anonymize users whose grace period expired. Trigger:
    GET /api/cron/account-deletions?token=$RECONCILE_TOKEN"""
    token = request.args.get('token', '') or request.headers.get('X-Cron-Token', '')
    if not RECONCILE_TOKEN or token != RECONCILE_TOKEN:
        return jsonify({'error': 'forbidden'}), 403
    cutoff = (datetime.utcnow() - timedelta(days=ACCOUNT_DELETION_GRACE_DAYS)).isoformat()
    conn = get_db()
    rows = conn.execute(
        '''SELECT id FROM users
           WHERE deletion_requested_at IS NOT NULL
             AND deletion_requested_at <= ?
             AND deleted_at IS NULL
           ORDER BY id ASC LIMIT 200''',
        (cutoff,)
    ).fetchall()
    purged_ids = []
    for r in rows:
        try:
            _anonymize_user(conn, r['id'])
            purged_ids.append(r['id'])
        except Exception as e:
            print(f'[account-deletion] failed for user {r["id"]}: {e}')
    conn.commit()

    try:
        from pricing import emit_event as _emit
        _emit('account.deletion_batch_purged', {
            'purged_count': len(purged_ids), 'user_ids': purged_ids,
        }, conn=conn)
        conn.commit()
    except Exception:
        pass

    conn.close()
    return jsonify({
        'ok': True,
        'purged_count': len(purged_ids),
        'purged_user_ids': purged_ids,
        'cutoff_iso': cutoff,
    })


@app.route('/api/me/referrals', methods=['GET'])
def my_referrals():
    """Returns current user's referral link + stats:
    {
      link: 'https://www.inklink.club/register?ref=username',
      code: 'username',
      bonus_czk: 300,
      account_credit_czk: 0,
      total_signups: 0,
      total_granted: 0,
      referred: [{username, display_name, status: 'signed_up'|'granted', signed_up_at, granted_at}]
    }
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    me = conn.execute(
        'SELECT username, account_credit_cents FROM users WHERE id = ?', (uid,)
    ).fetchone()
    if not me:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    rows = conn.execute(
        '''SELECT r.created_at AS signed_up_at, r.credit_granted_at,
                  u.username, u.display_name
           FROM referrals r
           JOIN users u ON u.id = r.referred_user_id
           WHERE r.referrer_user_id = ?
           ORDER BY r.created_at DESC LIMIT 200''',
        (uid,)
    ).fetchall()
    conn.close()

    referred = []
    granted = 0
    for r in rows:
        is_granted = bool(r['credit_granted_at'])
        if is_granted:
            granted += 1
        referred.append({
            'username':      r['username'],
            'display_name':  r['display_name'],
            'status':        'granted' if is_granted else 'signed_up',
            'signed_up_at':  r['signed_up_at'],
            'granted_at':    r['credit_granted_at'],
        })

    try:
        from pricing.config import REFERRAL_BONUS_CZK
        bonus_czk = float(REFERRAL_BONUS_CZK)
    except Exception:
        bonus_czk = 300.0

    base = APP_BASE_URL or 'https://www.inklink.club'
    return jsonify({
        'link': f"{base}/register?ref={me['username']}",
        'code': me['username'],
        'bonus_czk': bonus_czk,
        'account_credit_czk': float(me['account_credit_cents'] or 0) / 100,
        'total_signups': len(referred),
        'total_granted': granted,
        'referred': referred,
    })


@app.route('/pay/<int:bid>')
def deposit_pay_page(bid):
    """Serves the public deposit-pay page (Stripe Elements). Auth gating via
    booking_id only — URL is the capability. Anyone with the link can pay."""
    return send_from_directory('public', 'deposit-pay.html')


@app.route('/api/pay/<int:bid>')
def deposit_pay_info(bid):
    """Public — returns safe display fields + client_secret for Stripe Elements.
    No auth required (the URL is the capability). Returns 410 if booking is
    already past pending_payment (frontend redirects to /my-bookings)."""
    conn = get_db()
    b = conn.execute('''SELECT b.*, ua.display_name AS a_name, ua.studio AS a_studio,
                               ua.avatar AS a_avatar
                        FROM bookings b
                        JOIN users ua ON ua.id = b.artist_id
                        WHERE b.id = ?''', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'Rezervace neexistuje.'}), 404
    conn.close()

    # If booking moved past pending_payment, nothing to pay.
    if b['status'] not in ('pending_payment', 'payment_failed'):
        return jsonify({'error': 'Tato rezervace už nečeká na platbu.',
                        'status': b['status']}), 410

    charge_cents = b['total_price_cents'] if b['payment_mode'] == 'full' else b['deposit_cents']
    pi_id = b['stripe_payment_intent_id']
    client_secret = None
    if pi_id and not pi_id.startswith('demo_pi_'):
        # Fetch fresh client_secret from Stripe — PI may have rotated.
        try:
            pi = stripe.PaymentIntent.retrieve(pi_id)
            client_secret = pi.client_secret
        except Exception as e:
            print(f'[pay-info] PI retrieve failed: {e}')

    return jsonify({
        'id':                 b['id'],
        'amount_cents':       charge_cents,
        'currency':           (b['currency'] or 'CZK'),
        'client_secret':      client_secret,
        'publishable_key':    STRIPE_PUBLIC_KEY,
        'artist_name':        b['a_name'],
        'artist_studio':      b['a_studio'] or '',
        'artist_avatar_url':  f'/uploads/{b["a_avatar"]}' if b['a_avatar'] else None,
        'when':               b['booking_start_at'],
        'design_note':        b['design_note'] or '',
        'status':             b['status'],
        'payment_attempts':   b['payment_attempts'] if 'payment_attempts' in b.keys() else 0,
    })


@app.route('/api/bookings/<int:bid>/retry-payment-intent', methods=['POST'])
def retry_deposit_payment_intent(bid):
    """When the first PI failed (status='payment_failed'), create a fresh PI
    with a rotated idempotency_key. Auth: client_id only — privacy.
    Allowed states: pending_payment, payment_failed.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'Rezervace neexistuje.'}), 404
    if uid != b['client_id']:
        conn.close()
        return jsonify({'error': 'Retry může spustit jen klient rezervace.'}), 403
    if b['status'] not in ('pending_payment', 'payment_failed'):
        conn.close()
        return jsonify({'error': f'Booking ve stavu {b["status"]} nemůže retry.'}), 409
    if not STRIPE_SECRET_KEY:
        conn.close()
        return jsonify({'error': 'Stripe není nakonfigurovaný.'}), 503

    artist = conn.execute(
        'SELECT stripe_account_id, stripe_charges_enabled FROM users WHERE id=?',
        (b['artist_id'],)
    ).fetchone()
    if not artist or not artist['stripe_charges_enabled']:
        conn.close()
        return jsonify({'error': 'Tatér nemá aktivní platby — nelze retry.'}), 409

    attempt = (b['payment_attempts'] if 'payment_attempts' in b.keys() else 0) + 1
    charge_cents = b['total_price_cents'] if b['payment_mode'] == 'full' else b['deposit_cents']
    try:
        pi = stripe.PaymentIntent.create(
            amount=charge_cents,
            currency=(b['currency'] or 'CZK').lower(),
            description=f'InkLink — záloha za rezervaci #{bid} (retry #{attempt})',
            application_fee_amount=b['platform_fee_cents'] or 0,
            transfer_data={'destination': artist['stripe_account_id']},
            metadata={
                'inklink_booking_id': str(bid),
                'inklink_kind': 'deposit_retry',
                'inklink_attempt': str(attempt),
            },
            idempotency_key=f'deposit-retry-{bid}-{attempt}',
        )
    except Exception as e:
        conn.close()
        print(f'[deposit-retry] PI create failed: {e}')
        return jsonify({'error': f'Stripe: {e}'}), 502

    conn.execute(
        '''UPDATE bookings SET stripe_payment_intent_id=?, payment_attempts=?,
                                status=CASE WHEN status='payment_failed' THEN 'pending_payment' ELSE status END
           WHERE id=?''',
        (pi.id, attempt, bid)
    )
    conn.commit()
    conn.close()
    return jsonify({
        'ok': True,
        'client_secret':   pi.client_secret,
        'publishable_key': STRIPE_PUBLIC_KEY,
        'payment_url':     f'/pay/{bid}',
        'attempt':         attempt,
    })


@app.route('/api/bookings/<int:bid>/refund-request', methods=['POST'])
def create_refund_request(bid):
    """Client requests a refund post-booking (e.g. quality dispute, no-show).
    Body: {amount_kc?, reason}. Default amount = full paid deposit.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    reason = (data.get('reason') or '').strip()
    if len(reason) < 10:
        return jsonify({'error': 'Důvod musí mít alespoň 10 znaků.'}), 400
    if len(reason) > 1000:
        return jsonify({'error': 'Důvod je moc dlouhý (max 1000 znaků).'}), 400

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close()
        return jsonify({'error': 'Rezervace nenalezena.'}), 404
    if uid != b['client_id']:
        conn.close()
        return jsonify({'error': 'Žádost o refund může podat jen klient.'}), 403

    # Determine what's refundable: paid - already-refunded
    paid_cents = (b['deposit_cents'] or 0) + (b['balance_paid_cents'] if 'balance_paid_cents' in b.keys() and b['balance_paid_cents'] else 0)
    already_refunded = b['refund_cents'] or 0
    max_refundable = max(0, paid_cents - already_refunded)
    if max_refundable <= 0:
        conn.close()
        return jsonify({'error': 'Není co vracet (nic zaplaceno nebo už vráceno).'}), 400

    # Custom amount or full
    try:
        req_kc = data.get('amount_kc')
        if req_kc is not None:
            req_cents = int(round(float(req_kc) * 100))
        else:
            req_cents = max_refundable
    except (ValueError, TypeError):
        conn.close()
        return jsonify({'error': 'Neplatná částka.'}), 400
    if req_cents <= 0 or req_cents > max_refundable:
        conn.close()
        return jsonify({'error': f'Částka musí být mezi 1 a {max_refundable // 100} Kč.'}), 400

    # No duplicate pending requests on the same booking
    existing = conn.execute(
        "SELECT id FROM refund_requests WHERE booking_id=? AND status='pending'",
        (bid,)
    ).fetchone()
    if existing:
        conn.close()
        return jsonify({'error': 'Pro tuto rezervaci už máš čekající žádost.'}), 409

    conn.execute(
        '''INSERT INTO refund_requests (booking_id, client_id, artist_id, amount_cents, reason, status)
           VALUES (?, ?, ?, ?, ?, 'pending')''',
        (bid, uid, b['artist_id'], req_cents, reason)
    )
    rid = conn.execute('SELECT last_insert_rowid() AS id').fetchone()['id']

    # Notify artist
    push_notif(conn, b['artist_id'], uid, 'refund_requested', bid, 'booking',
               f'Klient žádá vrácení {req_cents // 100} Kč.')
    conn.commit()

    try:
        from pricing import emit_event as _emit
        _emit('refund.requested', {
            'booking_id': bid, 'request_id': rid, 'amount_cents': req_cents,
            'client_id': uid, 'artist_id': b['artist_id'],
        }, conn=conn)
        conn.commit()
    except Exception:
        pass

    conn.close()
    return jsonify({'ok': True, 'id': rid, 'amount_cents': req_cents, 'status': 'pending'})


@app.route('/api/refund-requests', methods=['GET'])
def list_refund_requests():
    """Returns refund requests visible to the current user:
    - clients see their own
    - artists see those on bookings they own
    - admins see all (filter by ?all=1)
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    show_all = request.args.get('all') == '1'
    me = conn.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    is_admin = bool(me and me['is_admin'])

    if show_all and is_admin:
        rows = conn.execute(
            '''SELECT rr.*, c.username AS client_username, a.username AS artist_username,
                      b.design_note, b.deposit_cents
               FROM refund_requests rr
               JOIN users c ON c.id = rr.client_id
               JOIN users a ON a.id = rr.artist_id
               JOIN bookings b ON b.id = rr.booking_id
               ORDER BY rr.created_at DESC LIMIT 200'''
        ).fetchall()
    else:
        rows = conn.execute(
            '''SELECT rr.*, c.username AS client_username, a.username AS artist_username,
                      b.design_note, b.deposit_cents
               FROM refund_requests rr
               JOIN users c ON c.id = rr.client_id
               JOIN users a ON a.id = rr.artist_id
               JOIN bookings b ON b.id = rr.booking_id
               WHERE rr.client_id = ? OR rr.artist_id = ?
               ORDER BY rr.created_at DESC LIMIT 200''',
            (uid, uid)
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            'id': r['id'],
            'booking_id': r['booking_id'],
            'amount_cents': r['amount_cents'],
            'reason': r['reason'],
            'status': r['status'],
            'decision_note': r['decision_note'] or '',
            'created_at': r['created_at'],
            'resolved_at': r['resolved_at'],
            'client_username': r['client_username'],
            'artist_username': r['artist_username'],
            'role': 'client' if r['client_id'] == uid else ('artist' if r['artist_id'] == uid else 'admin'),
        })
    return jsonify(out)


@app.route('/api/refund-requests/<int:rid>/decide', methods=['POST'])
def decide_refund_request(rid):
    """Artist (or admin) approves or rejects a refund request.
    Body: {decision: 'approve'|'reject', note?: str}
    On approve → triggers Stripe.Refund.create(). On reject → just marks status.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    decision = (data.get('decision') or '').strip().lower()
    note = (data.get('note') or '').strip()[:500]
    if decision not in ('approve', 'reject'):
        return jsonify({'error': 'Decision must be approve or reject.'}), 400

    conn = get_db()
    me = conn.execute('SELECT is_admin FROM users WHERE id=?', (uid,)).fetchone()
    is_admin = bool(me and me['is_admin'])
    rr = conn.execute('SELECT * FROM refund_requests WHERE id=?', (rid,)).fetchone()
    if not rr:
        conn.close()
        return jsonify({'error': 'Refund request not found.'}), 404
    if rr['status'] != 'pending':
        conn.close()
        return jsonify({'error': 'Žádost už byla vyřešena.'}), 409
    if uid != rr['artist_id'] and not is_admin:
        conn.close()
        return jsonify({'error': 'Rozhodnout může jen tatér nebo admin.'}), 403

    refund_id = None
    if decision == 'approve':
        b = conn.execute(
            'SELECT stripe_payment_intent_id, balance_payment_intent_id FROM bookings WHERE id=?',
            (rr['booking_id'],)
        ).fetchone()
        # Pick whichever PI is set (initial deposit first, then balance charge)
        pi_id = (b['stripe_payment_intent_id'] if b and 'stripe_payment_intent_id' in b.keys() else None) \
                 or (b['balance_payment_intent_id'] if b and 'balance_payment_intent_id' in b.keys() else None)
        if not pi_id:
            conn.close()
            return jsonify({'error': 'Booking nemá záznam o platbě — refund nelze provést přes Stripe.'}), 400
        if not STRIPE_SECRET_KEY:
            conn.close()
            return jsonify({'error': 'Stripe není nakonfigurovaný.'}), 500
        try:
            refund = stripe.Refund.create(
                payment_intent=pi_id,
                amount=rr['amount_cents'],
                reason='requested_by_customer',
                idempotency_key=f'rrq-{rid}',
                metadata={
                    'inklink_refund_request_id': str(rid),
                    'inklink_booking_id': str(rr['booking_id']),
                    'decided_by': str(uid),
                },
                reverse_transfer=True,
                refund_application_fee=True,
            )
            refund_id = refund.id
        except Exception as e:
            conn.close()
            app.logger.error(f'[refund decide] stripe failed for request {rid}: {e}')
            return jsonify({'error': f'Stripe refund selhal: {e}'}), 502

    new_status = 'approved' if decision == 'approve' else 'rejected'
    conn.execute(
        '''UPDATE refund_requests
           SET status=?, decision_by=?, decision_note=?, stripe_refund_id=?,
               resolved_at=CURRENT_TIMESTAMP
           WHERE id=?''',
        (new_status, uid, note, refund_id, rid)
    )

    # Notify client
    push_notif(conn, rr['client_id'], uid, 'refund_decided', rr['booking_id'], 'booking',
               f'Tvoje žádost o refund {rr["amount_cents"] // 100} Kč byla {"schválena" if decision == "approve" else "zamítnuta"}.')
    conn.commit()

    try:
        from pricing import emit_event as _emit
        _emit('refund.decided', {
            'request_id': rid, 'booking_id': rr['booking_id'], 'decision': decision,
            'amount_cents': rr['amount_cents'], 'decided_by': uid,
            'stripe_refund_id': refund_id,
        }, conn=conn)
        conn.commit()
    except Exception:
        pass

    conn.close()
    return jsonify({'ok': True, 'status': new_status, 'stripe_refund_id': refund_id})


def _booking_outstanding_cents(b):
    """Kolik z celkové ceny ještě není zaplaceno.

    Záloha + doplatky přes InkLink + hotovost na místě se musí sečíst do
    celkové ceny, jinak vyúčtování nesedí a tatér to zjistí až od účetní.
    Starší rezervace mají total_price_cents = 0 (cena se tehdy neukládala);
    u nich nemáme z čeho počítat, takže nic nevymáháme."""
    total = b['total_price_cents'] or 0
    if total <= 0:
        return None
    paid = (b['deposit_cents'] or 0) + (b['balance_paid_cents'] or 0) \
           + (b['onsite_amount_cents'] or 0)
    return max(0, total - paid)


@app.route('/api/bookings/<int:bid>/completion-info')
def booking_completion_info(bid):
    """Co zbývá doplatit. Bez tohohle čísla tatér při dokončování hádá a
    typicky nechá nulu — a rezervace pak navždy visí nedoplacená."""
    err = require_login()
    if err: return err
    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if session['user_id'] != b['artist_id']:
        conn.close(); return jsonify({'error': 'forbidden'}), 403
    conn.close()
    outstanding = _booking_outstanding_cents(b)
    return jsonify({
        'total_kc':       (b['total_price_cents'] or 0) // 100,
        'deposit_kc':     (b['deposit_cents'] or 0) // 100,
        'balance_paid_kc': (b['balance_paid_cents'] or 0) // 100,
        'onsite_kc':      (b['onsite_amount_cents'] or 0) // 100,
        'outstanding_kc': None if outstanding is None else outstanding // 100,
    })


@app.route('/api/bookings/<int:bid>/complete', methods=['POST'])
def complete_booking(bid):
    """Tatér potvrdí, že rezervace proběhla. Přijímá:
       - onsite_kc          → hotovost / karta vybraná na místě (mimo platformu)
       - balance_kc         → částka, kterou si tatér vyžádá od klienta přes InkLink
                              (vytvoří se balance-charge a klient dostane mail s linkem)
       - final_price_kc     → skutečná konečná cena, když se od domluvené liší
                              (sleva, kratší práce, doobjednávka)

    Součet záloha + doplatek + hotovost musí dát konečnou cenu. Kdyby to
    nemuselo sedět, rezervace by zůstávaly navždy nedoplacené a tatér by
    to zjistil až od účetní.
    """
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    try:
        onsite_kc  = max(0, int(data.get('onsite_kc')  or 0))
        balance_kc = max(0, int(data.get('balance_kc') or 0))
    except (ValueError, TypeError):
        return jsonify({'error': 'Neplatná částka'}), 400
    final_price_raw = data.get('final_price_kc')
    final_price_kc = None
    if final_price_raw not in (None, ''):
        try:
            final_price_kc = max(0, int(final_price_raw))
        except (ValueError, TypeError):
            return jsonify({'error': 'Neplatná konečná cena'}), 400
    onsite_cents = onsite_kc * 100

    conn = get_db()
    b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return jsonify({'error': 'not found'}), 404
    if session['user_id'] != b['artist_id']:
        conn.close(); return jsonify({'error': 'Pouze tatér může označit rezervaci jako dokončenou.'}), 403
    if b['status'] not in ('confirmed', 'pending_payment'):
        conn.close(); return jsonify({'error': 'Tuto rezervaci nelze dokončit.'}), 409

    # Konečná cena se od domluvené může lišit — sleva, kratší práce,
    # doobjednávka. Zapisujeme ji dřív, než z ní počítáme, co zbývá.
    if final_price_kc is not None and final_price_kc * 100 != (b['total_price_cents'] or 0):
        deposit = b['deposit_cents'] or 0
        if final_price_kc * 100 < deposit:
            conn.close()
            return jsonify({'error': 'Konečná cena je nižší než zaplacená záloha. '
                                     'Rozdíl vrať přes refund, ne přes cenu.'}), 400
        conn.execute('UPDATE bookings SET total_price_cents=?, balance_due_cents=? WHERE id=?',
                     (final_price_kc * 100, max(0, final_price_kc * 100 - deposit), bid))
        conn.commit()
        b = conn.execute('SELECT * FROM bookings WHERE id=?', (bid,)).fetchone()

    # Účetnictví musí sedět z principu, ne z dobré vůle. Když si tatér
    # spočítá jinak, řekneme mu o kolik — ať to opraví částkami, nebo
    # konečnou cenou.
    outstanding = _booking_outstanding_cents(b)
    if outstanding is not None and (onsite_kc + balance_kc) * 100 != outstanding:
        diff = (onsite_kc + balance_kc) * 100 - outstanding
        conn.close()
        return jsonify({
            'error': ('Zadané částky nesedí na to, co zbývá doplatit '
                      f'({outstanding // 100} Kč). Rozdíl je {abs(diff) // 100} Kč '
                      f'{"navíc" if diff > 0 else "chybí"}.'),
            'outstanding_kc': outstanding // 100,
            'entered_kc': onsite_kc + balance_kc,
        }), 400

    # Pokud tatér chce vystavit balance, spusť to PŘED dokončením — když selže
    # (např. částka přesahuje zbytek), zachová se status='confirmed' a tatér může retry.
    balance_result = None
    if balance_kc > 0:
        balance_result = _create_balance_charge(bid, balance_kc, session['user_id'])
        if balance_result.get('error'):
            conn.close()
            return jsonify({'error': f"Doplatek selhal: {balance_result['error']}"}), 400

    moved = transition_booking(
        conn, bid, 'completed',
        extra_set_sql='completed_at=?, onsite_amount_cents=?',
        extra_params=(datetime.utcnow().isoformat(), onsite_cents),
    )
    if not moved:
        conn.commit()  # keep any balance charge created above
        conn.close()
        return jsonify({'error': 'Stav rezervace se mezitím změnil, zkus to prosím znovu.'}), 409
    conn.commit()

    # Founding-artist clock start: pokud je tatér v programu a ještě nemá
    # started_at, set ho na timestamp prvního completed bookingu. Tím začíná
    # 30denní free window + 60denní flat 5 % window. Viz pricing/config.py.
    try:
        a_row = conn.execute(
            'SELECT founding_artist, founding_artist_started_at FROM users WHERE id = ?',
            (b['artist_id'],)
        ).fetchone()
        if a_row and a_row['founding_artist'] and not a_row['founding_artist_started_at']:
            conn.execute(
                'UPDATE users SET founding_artist_started_at = ? WHERE id = ?',
                (datetime.utcnow().isoformat(), b['artist_id'])
            )
            conn.commit()
            try:
                from pricing import emit_event as _emit_fa
                _emit_fa('founding_artist.clock_started', {
                    'artist_id': b['artist_id'], 'booking_id': bid,
                }, conn=conn)
                conn.commit()
            except Exception:
                pass
    except Exception as _e:
        print(f'[founding-artist clock] failed for booking {bid}: {_e}')

    # Telemetry: booking completed
    try:
        from pricing import emit_event as _emit_done
        _emit_done('booking.completed', {
            'booking_id': bid, 'artist_id': b['artist_id'], 'client_id': b['client_id'],
        }, conn=conn)
        conn.commit()
    except Exception:
        pass

    # Referral credit grant — if the client was referred AND this is their
    # first completed booking AND credit wasn't yet granted, add bonus to
    # referrer's account_credit_cents and mark the referral row as granted.
    try:
        ref_row = conn.execute(
            '''SELECT id, referrer_user_id FROM referrals
               WHERE referred_user_id = ? AND credit_granted_at IS NULL''',
            (b['client_id'],)
        ).fetchone()
        if ref_row:
            prior = conn.execute(
                '''SELECT COUNT(*) AS c FROM bookings
                   WHERE client_id = ? AND status = 'completed' AND id != ?''',
                (b['client_id'], bid)
            ).fetchone()
            if (prior['c'] or 0) == 0:
                from pricing.config import REFERRAL_BONUS_CZK
                bonus_cents = int(REFERRAL_BONUS_CZK * 100)
                _credit_move(conn, ref_row['referrer_user_id'], bonus_cents,
                             'referral_bonus', 'booking', bid)
                conn.execute(
                    "UPDATE referrals SET credit_granted_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (ref_row['id'],)
                )
                conn.commit()
                push_notif(conn, ref_row['referrer_user_id'], b['client_id'],
                           'referral_bonus', bid, 'booking',
                           f'Tvůj referral dokončil rezervaci! +{bonus_cents // 100} Kč na účtu.')
                conn.commit()
                try:
                    from pricing import emit_event as _emit_ref
                    _emit_ref('referral.bonus_granted', {
                        'referrer_user_id': ref_row['referrer_user_id'],
                        'referred_user_id': b['client_id'],
                        'booking_id': bid, 'bonus_cents': bonus_cents,
                    }, conn=conn)
                    conn.commit()
                except Exception:
                    pass
    except Exception as _e:
        print(f'[referral] grant failed for booking {bid}: {_e}')

    # Instrukce k hojení hned, ne až dalším cronem.
    _send_aftercare_first(conn, bid)

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
            # Daily-rotating idempotency_key: same artist re-issuing the same
            # balance amount on the same day → same PI. Next day → fresh PI.
            day = int(time.time() // 86400)
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
                idempotency_key=f'balance-{booking_id}-{cents}-{day}',
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
        'last_msg':     r['last_msg'] if r['last_msg'] else '📷 Fotka',   # nabídka nese text v content
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
    # Prošlá nabídka nemá ve vlákně vypadat jako živá — a je to zároveň
    # jediné místo, kde ji uvidí i klient.
    _reap_expired_offers(conn, session['user_id'])
    _reap_expired_offers(conn, other_id)
    offers = {
        r['id']: _offer_dict(r, session['user_id'])
        for r in conn.execute(
            'SELECT * FROM booking_offers WHERE (artist_id=? AND client_id=?) '
            'OR (artist_id=? AND client_id=?)',
            (session['user_id'], other_id, other_id, session['user_id'])).fetchall()
    }
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
            'offer':        offers.get(m['offer_id']) if m['offer_id'] else None,
            'mine':         m['sender_id'] == session['user_id'],
            'created_at':   time_ago(m['created_at']),
        } for m in rows]
    })


@app.route('/api/messages/<int:other_id>', methods=['POST'])
@limiter.limit('60 per minute')
def send_message(other_id):
    err = require_login()
    if err: return err
    uid = session['user_id']

    # Příjemce musí existovat a nesmí to být pisatel sám. Bez téhle kontroly
    # se zpráva na neexistující id uložila, vrátila ok:true a pak zmizela —
    # výpis konverzací ji odfiltruje JOINem na users. A monolog sám se sebou
    # se v tom výpisu naopak objevil jako plnohodnotná konverzace.
    if other_id == uid:
        return jsonify({'error': 'Sám sobě psát nemůžeš.'}), 400
    conn = get_db()
    if not conn.execute('SELECT 1 FROM users WHERE id=?', (other_id,)).fetchone():
        conn.close()
        return jsonify({'error': 'Uživatel nenalezen.'}), 404

    # image message
    if 'image' in request.files:
        img = request.files['image']
        ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in (img.filename or '') else ''
        if ext not in MESSAGE_IMAGE_EXTS:
            conn.close()
            return jsonify({'error': 'Unsupported image format'}), 400
        # Bez stropu projde cokoliv až do MAX_CONTENT_LENGTH (500 MB) —
        # rovnou do R2 a rovnou do vlákna, které se pak nenačte.
        img.seek(0, os.SEEK_END)
        size = img.tell()
        img.seek(0)
        if size > MESSAGE_IMAGE_MAX_BYTES:
            conn.close()
            return jsonify({'error': 'Fotka je větší než 12 MB.'}), 400
        safe   = secure_filename(img.filename) or f'photo.{ext}'
        unique = f"msg_{uid}_{int(time.time())}_{safe}"
        save_upload(img, unique)
        conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type, image) VALUES (?, ?, ?, ?, ?)',
                     (uid, other_id, '', 'image', unique))
        conn.commit(); conn.close()
        return jsonify({'ok': True})

    data    = request.get_json(silent=True) or {}
    content = data.get('content', '').strip()
    if not content:
        conn.close()
        return jsonify({'error': 'Message cannot be empty'}), 400
    if len(content) > 2000:
        conn.close()
        return jsonify({'error': 'Message is too long'}), 400

    conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type) VALUES (?, ?, ?, ?)',
                 (uid, other_id, content, 'text'))
    conn.commit()
    conn.close()

    return jsonify({'ok': True})


# Popis velikosti pro čitelný výpis v poptávce i v e-mailu.
def _size_label_text(key):
    preset = SIZE_PRESETS.get(key)
    return preset[1] if preset else (key or '')


@app.route('/api/design-requests', methods=['POST'])
@limiter.limit('6 per hour')
def create_design_request():
    """Klient poptá u tatéra vlastní návrh.

    Nezakládá vlastní frontu: složí strukturovanou zprávu do konverzace
    a pošle tatérovi e-mail. Poptávka je začátek rozhovoru, ne rezervace —
    tatér z ní udělá termín tak jako z každé jiné domluvy."""
    err = require_login()
    if err: return err
    uid  = session['user_id']
    # Referenční fotky nutí formulář do multipartu; JSON zůstává kvůli
    # klientům, kteří žádnou fotku neposílají.
    data = request.get_json(silent=True) or request.form

    username = (data.get('artist') or '').strip().lower()
    motif    = (data.get('motif') or '').strip()[:1000]
    placement = (data.get('placement') or '').strip()[:120]
    size_label = (data.get('size_label') or '').strip().lower()
    budget_raw = data.get('budget_kc')
    timing     = (data.get('timing') or '').strip()[:120]

    if not username:
        return jsonify({'error': 'Chybí tatér.'}), 400
    if len(motif) < 10:
        return jsonify({'error': 'Popiš motiv aspoň pár slovy.'}), 400
    if size_label and size_label not in SIZE_PRESETS:
        return jsonify({'error': 'Neznámá velikost.'}), 400
    budget_kc = None
    if budget_raw not in (None, ''):
        try:
            budget_kc = max(0, int(budget_raw))
        except (ValueError, TypeError):
            return jsonify({'error': 'Rozpočet zadej jako číslo.'}), 400

    # Reference posíláme jako obrázkové zprávy do stejného vlákna — vlastní
    # úložiště by znamenalo druhou cestu k témuž a v konverzaci by chyběly.
    photos = [f for f in request.files.getlist('photos') if f and f.filename]
    if len(photos) > REFERENCE_PHOTO_MAX:
        return jsonify({'error': f'Nejvýš {REFERENCE_PHOTO_MAX} referenční fotky.'}), 400
    for f in photos:
        ext = f.filename.rsplit('.', 1)[-1].lower() if '.' in f.filename else ''
        if ext not in MESSAGE_IMAGE_EXTS:
            return jsonify({'error': 'Fotka musí být JPG, PNG, WEBP nebo GIF.'}), 400
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(0)
        if size > REFERENCE_PHOTO_MAX_BYTES:
            return jsonify({'error': 'Fotka je větší než 12 MB.'}), 400

    conn = get_db()
    artist = conn.execute(
        'SELECT id, username, display_name, is_artist FROM users WHERE LOWER(username)=?',
        (username,)).fetchone()
    if not artist or not artist['is_artist']:
        conn.close(); return jsonify({'error': 'Tatér nenalezen.'}), 404
    if artist['id'] == uid:
        conn.close(); return jsonify({'error': 'Sám sobě návrh poptat nemůžeš.'}), 400

    # Text skládá server, ne prohlížeč — zpráva je trvalý záznam a měla by
    # mít pořád stejný tvar, ať ji odešle kdokoliv odkudkoliv.
    rows = [('Motif', motif), ('Placement', placement),
            ('Size', _size_label_text(size_label)),
            ('Budget', f'{budget_kc:,} CZK'.replace(',', ' ') if budget_kc else ''),
            ('Timing', timing)]
    content = 'Custom design request\n' + '\n'.join(
        f'{k}: {v}' for k, v in rows if v)
    conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type) '
                 'VALUES (?,?,?,?)', (uid, artist['id'], content[:2000], 'text'))

    # Fotky až po textu, ať vlákno čte "co chci" a pak "jak to má vypadat".
    base_ts = int(time.time())
    for i, f in enumerate(photos):
        name = f'msg_{uid}_{base_ts}_{i}_{secure_filename(f.filename)}'
        save_upload(f, name)
        conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type, image) '
                     'VALUES (?,?,?,?,?)', (uid, artist['id'], '', 'image', name))

    me = conn.execute('SELECT username, display_name FROM users WHERE id=?', (uid,)).fetchone()
    who = me['display_name'] or me['username']
    push_notif(conn, artist['id'], uid, 'design_request', uid, 'user',
               f'{who} žádá o vlastní návrh.')
    conn.commit()

    send_booking_email(conn, artist['id'], 'design_request_for_artist', {
        'other_name':  who,
        'motif':       motif,
        'placement':   placement,
        'size':        _size_label_text(size_label),
        'budget':      f'{budget_kc:,} CZK'.replace(',', ' ') if budget_kc else '',
        'timing':      timing,
        'photos':      len(photos),
        'booking_url': f'{APP_BASE_URL}/messages?user={me["username"]}',
    })
    conn.close()
    return jsonify({'ok': True, 'thread': f'/messages?user={artist["username"]}'})


# ── Nabídky termínu ───────────────────────────────────────────────────────
#
# Tatér se s klientem domluví v chatu na custom práci a pošle mu konkrétní
# termín a cenu. Klient přijme → vznikne běžná rezervace se zálohou.
#
# Slot zabere až přijetí. Držet ho od odeslání by z každé zapomenuté
# nabídky udělalo díru v kalendáři a nikdo by ji neuklidil.

OFFER_VALID_DAYS = 7


def _offer_state(row, now=None):
    """Nabídka je mrtvá, když vypršela platnost nebo když už začal
    nabízený termín. Počítá se za běhu — stav v databázi by mohl zaostat."""
    if row['status'] != 'pending':
        return row['status']
    now = now or _prague_now_naive()
    for field in ('expires_at', 'booking_start_at'):
        raw = row[field] if field in row.keys() else None
        if not raw:
            continue
        try:
            if _naive_dt(raw) <= now:
                return 'expired'
        except (ValueError, TypeError):
            pass
    return 'pending'


def _reap_expired_offers(conn, artist_id):
    """Označí prošlé nabídky a uklidí po nich soukromé termíny.

    Běží při čtení tatérova kalendáře, ne cronem: mrtvý blok má zmizet
    přesně ve chvíli, kdy se na kalendář někdo dívá. Maže jen prázdné
    termíny — na tom s rezervací už nezáleží, jestli nabídka propadla."""
    now = _prague_now_naive().isoformat()
    rows = conn.execute(
        "SELECT * FROM booking_offers WHERE artist_id=? AND status='pending' "
        "AND (COALESCE(expires_at, booking_start_at) <= ? OR booking_start_at <= ?)",
        (artist_id, now, now)).fetchall()
    if not rows:
        return
    for r in rows:
        conn.execute("UPDATE booking_offers SET status='expired' WHERE id=?", (r['id'],))
        if r['created_slot']:
            busy = conn.execute(
                "SELECT 1 FROM bookings WHERE slot_id=? AND status IN "
                "('pending_payment','confirmed') LIMIT 1", (r['slot_id'],)).fetchone()
            if not busy:
                conn.execute('DELETE FROM slots WHERE id=? AND COALESCE(is_private,0)=1',
                             (r['slot_id'],))
    conn.commit()


def _offer_dict(row, uid):
    return {
        'id':               row['id'],
        'status':           _offer_state(row),
        'expires_at':       row['expires_at'] if 'expires_at' in row.keys() else None,
        'mine':             row['artist_id'] == uid,
        'booking_start_at': row['booking_start_at'],
        'duration_hours':   float(row['duration_hours']),
        'price_kc':         int(row['price_kc']),
        'currency':         _norm_currency(row['currency'] if 'currency' in row.keys() else None),
        'note':             row['note'] or '',
        'booking_id':       row['booking_id'],
    }


@app.route('/api/booking-offers', methods=['POST'])
@limiter.limit('30 per hour')
def create_booking_offer():
    """Tatér nabídne klientovi konkrétní termín za dohodnutou cenu."""
    err = require_login()
    if err: return err
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}

    try:
        client_id = int(data.get('client_id') or 0)
        slot_id   = int(data.get('slot_id') or 0)   # 0 = termín teprve vytvoříme
    except (ValueError, TypeError):
        return jsonify({'error': 'Špatný klient nebo termín.'}), 400
    if not client_id:
        return jsonify({'error': 'Chybí klient.'}), 400
    if client_id == uid:
        return jsonify({'error': 'Sám sobě termín nabídnout nemůžeš.'}), 400

    try:
        duration_hours = float(data.get('duration_hours') or 0)
        price_kc       = int(data.get('price_kc') or 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Cena a délka musí být čísla.'}), 400
    if duration_hours < 0.5 or duration_hours > 24:
        return jsonify({'error': 'Délka musí být mezi 0,5 a 24 hodinami.'}), 400
    if price_kc <= 0:
        return jsonify({'error': 'Zadej dohodnutou cenu.'}), 400
    note = (data.get('note') or '').strip()[:500]

    conn = get_db()
    if not conn.execute('SELECT 1 FROM users WHERE id=?', (client_id,)).fetchone():
        conn.close(); return jsonify({'error': 'Klient nenalezen.'}), 404

    # Tatér může termín rovnou vytvořit, když žádný vypsaný nemá. Vzniká
    # soukromý blok přesně na délku sezení — veřejně se nenabízí, takže ho
    # mezitím nikdo jiný nevezme.
    created_slot = 0
    if not slot_id:
        raw_start = (data.get('booking_start_at') or '').strip()
        if not raw_start:
            conn.close(); return jsonify({'error': 'Vyber termín, nebo zadej datum a čas.'}), 400
        try:
            start = _naive_dt(raw_start)
        except ValueError:
            conn.close(); return jsonify({'error': 'Špatný formát začátku.'}), 400
        end = start + timedelta(hours=duration_hours)
        if start <= _prague_now_naive():
            conn.close(); return jsonify({'error': 'Termín už je v minulosti.'}), 400
        if _artist_blocked_overlap(conn, uid, start, end):
            conn.close(); return jsonify({'error': 'V tuhle dobu máš blokované volno.'}), 409
        # Kolize s čímkoliv, co už v ten čas máš — jinak by nový blok
        # slíbil čas, na kterém už někdo sedí.
        clash = conn.execute(
            "SELECT 1 FROM bookings WHERE artist_id=? AND status IN ('pending_payment','confirmed') "
            "AND booking_start_at < ? AND booking_end_at > ? LIMIT 1",
            (uid, end.isoformat(), start.isoformat())).fetchone()
        if clash:
            conn.close(); return jsonify({'error': 'Tenhle čas se kryje s jinou rezervací.'}), 409
        conn.execute(
            'INSERT INTO slots (user_id, start_at, end_at, status, price_min, price_max, '
            "price_unit, min_duration_hours, note, is_private) "
            "VALUES (?,?,?,'free',0,0,'hour',1,?,1)",
            (uid, start.isoformat(), end.isoformat(), note[:200]))
        slot_id = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
                   else conn.execute('SELECT lastval()').fetchone()[0])
        created_slot = 1

    slot = conn.execute('SELECT * FROM slots WHERE id=?', (slot_id,)).fetchone()
    if not slot or slot['user_id'] != uid:
        conn.close(); return jsonify({'error': 'Termín nenalezen.'}), 404

    try:
        slot_start = _naive_dt(slot['start_at'])
        slot_end   = _naive_dt(slot['end_at'])
        raw_start  = (data.get('booking_start_at') or '').strip()
        start      = _naive_dt(raw_start) if raw_start else slot_start
    except ValueError:
        conn.close(); return jsonify({'error': 'Špatný formát začátku.'}), 400
    end = start + timedelta(hours=duration_hours)

    if start <= _prague_now_naive():
        conn.close(); return jsonify({'error': 'Termín už je v minulosti.'}), 400
    if start < slot_start - timedelta(minutes=1) or end > slot_end + timedelta(minutes=1):
        conn.close(); return jsonify({'error': 'Termín se nevejde do bloku.'}), 400

    # Stejná kolizní kontrola jako u běžné rezervace — nabízet obsazený čas
    # znamená slíbit něco, co při přijetí stejně spadne.
    buf_before = slot['buffer_before_minutes'] or 0
    buf_after  = slot['buffer_after_minutes'] or 0
    for s_iso, e_iso, ex_before, ex_after in _slot_active_bookings(conn, slot_id):
        if _padded_overlap(start, end, buf_before, buf_after,
                           _naive_dt(s_iso), _naive_dt(e_iso), ex_before, ex_after):
            conn.close()
            return jsonify({'error': 'Tenhle čas se kryje s jinou rezervací.'}), 409

    # Nová nabídka ruší předchozí — platí ta, na které jste se domluvili
    # naposled. Jinak by klient mohl přijmout tu starou a levnější.
    conn.execute("UPDATE booking_offers SET status='cancelled' "
                 "WHERE artist_id=? AND client_id=? AND status='pending'", (uid, client_id))
    expires_at = min(_prague_now_naive() + timedelta(days=OFFER_VALID_DAYS), start)
    conn.execute(
        'INSERT INTO booking_offers '
        '(artist_id, client_id, slot_id, booking_start_at, duration_hours, price_kc, note, '
        ' created_slot, expires_at) VALUES (?,?,?,?,?,?,?,?,?)',
        (uid, client_id, slot_id, start.isoformat(), duration_hours, price_kc, note,
         created_slot, expires_at.isoformat()))
    offer_id = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
                else conn.execute('SELECT lastval()').fetchone()[0])

    me = conn.execute('SELECT username, display_name FROM users WHERE id=?', (uid,)).fetchone()
    who = me['display_name'] or me['username']
    conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type, offer_id) '
                 'VALUES (?,?,?,?,?)',
                 (uid, client_id, note or 'Booking offer', 'offer', offer_id))
    push_notif(conn, client_id, uid, 'booking_offer', offer_id, 'user',
               f'{who} ti nabízí termín.')
    conn.commit()

    send_booking_email(conn, client_id, 'booking_offer_for_client', {
        'other_name':  who,
        'when':        start.strftime('%d.%m.%Y %H:%M'),
        'valid_until': expires_at.strftime('%d.%m.%Y'),
        'duration':    f'{duration_hours:g} h',
        'price':       f'{price_kc:,} CZK'.replace(',', ' '),
        'design_note': note,
        'booking_url': f'{APP_BASE_URL}/messages?user={me["username"]}',
    })
    conn.close()
    return jsonify({'ok': True, 'offer_id': offer_id})


@app.route('/api/booking-offers/<int:offer_id>/decline', methods=['POST'])
def decline_booking_offer(offer_id):
    """Odmítnout smí klient, zrušit tatér — obojí končí mrtvou nabídkou."""
    err = require_login()
    if err: return err
    uid  = session['user_id']
    conn = get_db()
    row = conn.execute('SELECT * FROM booking_offers WHERE id=?', (offer_id,)).fetchone()
    if not row or uid not in (row['artist_id'], row['client_id']):
        conn.close(); return jsonify({'error': 'not found'}), 404
    if _offer_state(row) != 'pending':
        conn.close(); return jsonify({'error': 'S touhle nabídkou už se nedá nic dělat.'}), 409
    new_status = 'cancelled' if uid == row['artist_id'] else 'declined'
    conn.execute('UPDATE booking_offers SET status=? WHERE id=?', (new_status, offer_id))
    # Termín vyrobený kvůli téhle nabídce nemá bez ní důvod existovat.
    # Mazat ho smíme jen dokud je prázdný — jinak bychom smazali blok,
    # na kterém už sedí jiná rezervace.
    if row['created_slot']:
        busy = conn.execute(
            "SELECT 1 FROM bookings WHERE slot_id=? AND status IN "
            "('pending_payment','confirmed') LIMIT 1", (row['slot_id'],)).fetchone()
        if not busy:
            conn.execute('DELETE FROM slots WHERE id=? AND COALESCE(is_private,0)=1',
                         (row['slot_id'],))
    other = row['client_id'] if uid == row['artist_id'] else row['artist_id']
    push_notif(conn, other, uid, 'booking_offer_' + new_status, offer_id, 'user',
               'Nabídka termínu byla zrušena.' if new_status == 'cancelled'
               else 'Klient nabídku termínu odmítl.')
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'status': new_status})


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

# ── Instagram: propojení účtu ────────────────────────────────────────────────
# Tok: /connect → Instagram → /callback → krátkodobý token → dlouhodobý (60 dní).
# Token se nikam neloguje a ven z API nikdy nejde.

IG_AUTH_URL  = 'https://www.instagram.com/oauth/authorize'
IG_TOKEN_URL = 'https://api.instagram.com/oauth/access_token'
IG_GRAPH     = 'https://graph.instagram.com'


@app.route('/api/instagram/connect')
def instagram_connect():
    err = require_login()
    if err: return err
    if not _instagram_enabled():
        return jsonify({'error': 'Instagram is not configured on this server.'}), 503

    # state chrání proti CSRF: bez něj by šlo uživateli podstrčit callback
    # a připojit mu cizí Instagram účet.
    import secrets as _secrets
    state = _secrets.token_urlsafe(24)
    session['ig_oauth_state'] = state

    from urllib.parse import urlencode
    return redirect(IG_AUTH_URL + '?' + urlencode({
        'client_id':     INSTAGRAM_APP_ID,
        'redirect_uri':  _instagram_redirect_uri(),
        'response_type': 'code',
        'scope':         INSTAGRAM_SCOPES,
        'state':         state,
    }))


@app.route('/api/instagram/callback')
def instagram_callback():
    err = require_login()
    if err: return err
    if not _instagram_enabled():
        return redirect('/artist-setup#profile?ig=unconfigured')

    # Jednorázový state — druhé použití už neprojde.
    expected = session.pop('ig_oauth_state', None)
    if not expected or request.args.get('state') != expected:
        return redirect('/artist-setup?ig=state')
    if request.args.get('error') or not request.args.get('code'):
        return redirect('/artist-setup?ig=denied')

    import requests as _rq
    try:
        tok = _rq.post(IG_TOKEN_URL, data={
            'client_id':     INSTAGRAM_APP_ID,
            'client_secret': INSTAGRAM_APP_SECRET,
            'grant_type':    'authorization_code',
            'redirect_uri':  _instagram_redirect_uri(),
            'code':          request.args['code'],
        }, timeout=15).json()
        short_token = tok.get('access_token')
        ig_user_id  = str(tok.get('user_id') or '')
        if not short_token or not ig_user_id:
            raise ValueError('no access_token in response')

        # Krátkodobý token platí hodinu; bez výměny by propojení do hodiny
        # umřelo a tatér by nevěděl proč.
        lng = _rq.get(f'{IG_GRAPH}/access_token', params={
            'grant_type':     'ig_exchange_token',
            'client_secret':  INSTAGRAM_APP_SECRET,
            'access_token':   short_token,
        }, timeout=15).json()
        token = lng.get('access_token') or short_token
        expires_in = int(lng.get('expires_in') or 0)

        me = _rq.get(f'{IG_GRAPH}/me', params={
            'fields': 'id,username',
            'access_token': token,
        }, timeout=15).json()
        username = me.get('username') or ''
    except Exception as e:
        # Token ani kód se do logu nedostanou — jen typ chyby.
        app.logger.error(f'[instagram] connect failed: {type(e).__name__}')
        return redirect('/artist-setup?ig=error')

    expires_at = (_prague_now_naive() + timedelta(seconds=expires_in)).isoformat() if expires_in else None
    uid = session['user_id']
    conn = get_db()
    conn.execute('DELETE FROM instagram_accounts WHERE user_id=?', (uid,))
    conn.execute("""INSERT INTO instagram_accounts
                    (user_id, ig_user_id, username, access_token, token_expires_at)
                    VALUES (?,?,?,?,?)""",
                 (uid, ig_user_id, username, token, expires_at))
    conn.commit()
    conn.close()
    return redirect('/artist-setup?ig=ok')


@app.route('/api/instagram/status')
def instagram_status():
    err = require_login()
    if err: return err
    conn = get_db()
    row = conn.execute(
        'SELECT ig_user_id, username, connected_at, token_expires_at, last_import_at '
        'FROM instagram_accounts WHERE user_id=?', (session['user_id'],)).fetchone()
    imported = conn.execute(
        'SELECT COUNT(*) FROM instagram_imports WHERE user_id=?',
        (session['user_id'],)).fetchone()[0]
    conn.close()
    if not row:
        return jsonify({'connected': False, 'available': _instagram_enabled()})
    # Token se ven nikdy neposílá.
    return jsonify({
        'connected':        True,
        'available':        True,
        'username':         row['username'],
        'connected_at':     row['connected_at'],
        'token_expires_at': row['token_expires_at'],
        'last_import_at':   row['last_import_at'],
        'imported_count':   imported,
    })


@app.route('/api/instagram/disconnect', methods=['POST'])
def instagram_disconnect():
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    conn.execute('DELETE FROM instagram_accounts WHERE user_id=?', (uid,))
    # Historii importů necháváme: opětovné propojení nemá znovu natáhnout
    # fotky, které si tatér mezitím z portfolia smazal.
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


def _ig_account(conn, uid):
    return conn.execute(
        'SELECT * FROM instagram_accounts WHERE user_id=?', (uid,)).fetchone()


@app.route('/api/instagram/media')
def instagram_media():
    """Média z propojeného účtu, s příznakem, co už je naimportované."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    acc = _ig_account(conn, uid)
    if not acc:
        conn.close()
        return jsonify({'error': 'Instagram is not connected.'}), 404
    done = {r['ig_media_id'] for r in conn.execute(
        'SELECT ig_media_id FROM instagram_imports WHERE user_id=?', (uid,)).fetchall()}
    conn.close()

    import requests as _rq
    try:
        r = _rq.get(f'{IG_GRAPH}/me/media', params={
            'fields': 'id,caption,media_type,media_url,thumbnail_url,permalink,timestamp',
            'limit':  50,
            'access_token': acc['access_token'],
        }, timeout=20).json()
    except Exception as e:
        app.logger.error(f'[instagram] media fetch failed: {type(e).__name__}')
        return jsonify({'error': 'Could not reach Instagram.'}), 502
    if 'error' in r:
        # Vypršelý nebo odvolaný token je běžný stav, ne chyba serveru —
        # frontend na něj má reagovat nabídkou znovupropojení.
        return jsonify({'error': 'Instagram rejected the request.',
                        'reconnect': True}), 401

    items = []
    for m in (r.get('data') or []):
        # VIDEO nemá statický obrázek k zobrazení v portfoliu; bereme
        # náhled. CAROUSEL_ALBUM vrací jen první snímek, což stačí.
        if m.get('media_type') == 'VIDEO' and not m.get('thumbnail_url'):
            continue
        items.append({
            'id':        m.get('id'),
            'caption':   (m.get('caption') or '')[:300],
            'type':      m.get('media_type'),
            'thumb':     m.get('thumbnail_url') or m.get('media_url'),
            'permalink': m.get('permalink'),
            'timestamp': m.get('timestamp'),
            'imported':  m.get('id') in done,
        })
    return jsonify({'media': items, 'username': acc['username']})


@app.route('/api/instagram/import', methods=['POST'])
@limiter.limit('20 per hour')
def instagram_import():
    """Naimportuje vybraná média do portfolia.

    Obrázky se STAHUJÍ k nám. URL z Instagramu jsou podepsané a expirují,
    takže odkazovat na ně přímo by znamenalo portfolio, které se za pár dní
    rozsype na prázdné rámečky.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or {}
    ids  = [str(i) for i in (data.get('ids') or [])][:30]
    kind = 'sketch' if data.get('kind') == 'sketch' else 'done'
    if not ids:
        return jsonify({'error': 'Pick at least one photo.'}), 400

    conn = get_db()
    acc = _ig_account(conn, uid)
    if not acc:
        conn.close()
        return jsonify({'error': 'Instagram is not connected.'}), 404
    already = {r['ig_media_id'] for r in conn.execute(
        'SELECT ig_media_id FROM instagram_imports WHERE user_id=?', (uid,)).fetchall()}

    import requests as _rq
    imported, skipped, failed = 0, 0, 0
    for mid in ids:
        if mid in already:
            skipped += 1
            continue
        try:
            m = _rq.get(f'{IG_GRAPH}/{mid}', params={
                'fields': 'id,caption,media_type,media_url,thumbnail_url',
                'access_token': acc['access_token'],
            }, timeout=20).json()
            url = m.get('media_url') if m.get('media_type') != 'VIDEO' else m.get('thumbnail_url')
            if not url:
                failed += 1
                continue
            img = _rq.get(url, timeout=30)
            img.raise_for_status()
            # Instagram servíruje JPEG; přípona se řídí typem odpovědi,
            # ne koncovkou v URL (ta nese podpisové parametry).
            ext = 'png' if 'png' in (img.headers.get('Content-Type') or '') else 'jpg'
            name = f'ig_{uid}_{mid}_{int(datetime.now().timestamp()*1000)}.{ext}'
            import io as _io
            from werkzeug.datastructures import FileStorage as _FS
            save_upload(_FS(stream=_io.BytesIO(img.content), filename=name), name)

            conn.execute(
                'INSERT INTO portfolio_items (user_id, image, caption, kind) VALUES (?,?,?,?)',
                (uid, name, (m.get('caption') or '')[:300], kind))
            conn.commit()
            pid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
                   else conn.execute('SELECT lastval()').fetchone()[0])
            conn.execute(
                'INSERT INTO instagram_imports (user_id, ig_media_id, portfolio_item_id) VALUES (?,?,?)',
                (uid, mid, pid))
            conn.commit()
            imported += 1
        except Exception as e:
            # Jedna vadná fotka nesmí shodit celý import — tatér by nevěděl,
            # co se stihlo a co ne.
            app.logger.warning(f'[instagram] import of one item failed: {type(e).__name__}')
            failed += 1

    conn.execute('UPDATE instagram_accounts SET last_import_at=? WHERE user_id=?',
                 (_prague_now_naive().isoformat(), uid))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'imported': imported, 'skipped': skipped, 'failed': failed})


@app.route('/api/waitlist', methods=['POST'])
@limiter.limit('10 per hour')
def join_waitlist():
    """Zápis do waitlistu z coming-soon stránky.

    Veřejný nepřihlášený zápis, takže rate limit není volitelný. Ukládáme
    minimum — e-mail a nepovinnou roli; čím míň údajů před spuštěním, tím
    míň povinností navíc.
    """
    data  = request.get_json(silent=True) or request.form
    email = (data.get('email') or '').strip().lower()[:190]
    role  = (data.get('role') or '').strip().lower()
    if role not in ('artist', 'client', ''):
        role = ''

    # Záměrně mírná validace: přísná regex na e-maily odmítá platné adresy.
    # Skutečné ověření je stejně až odeslaný e-mail.
    if not email or '@' not in email or '.' not in email.split('@')[-1] or ' ' in email:
        return jsonify({'error': 'Enter a valid email address.'}), 400

    conn = get_db()
    existing = conn.execute('SELECT id FROM waitlist WHERE email=?', (email,)).fetchone()
    if existing:
        conn.close()
        # Odpověď musí být BAJT PO BAJTU stejná jako u nového zápisu.
        # Dřív se vracelo navíc 'already': True, což šlo použít k ověření,
        # jestli je konkrétní adresa na seznamu.
        return jsonify({'ok': True})
    try:
        conn.execute(
            'INSERT INTO waitlist (email, role, source, ip) VALUES (?,?,?,?)',
            (email, role, (data.get('source') or 'coming-soon').strip()[:40],
             (request.remote_addr or '')[:64]))
        conn.commit()
    except Exception as e:
        # Souběžný zápis téhož e-mailu spadne na UNIQUE indexu. Z pohledu
        # návštěvníka je to úspěch, ne chyba.
        try:
            app.logger.warning(f'[waitlist] insert failed for {email}: {e}')
        except Exception:
            pass
        conn.close()
        return jsonify({'ok': True})
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/admin/waitlist')
def admin_waitlist():
    """Export waitlistu pro admina — bez něj je seznam k ničemu."""
    err = require_login()
    if err: return err
    if not is_admin_user(session['user_id']):
        return jsonify({'error': 'not found'}), 404
    conn = get_db()
    rows = conn.execute(
        'SELECT id, email, role, source, created_at FROM waitlist ORDER BY created_at DESC'
    ).fetchall()
    conn.close()

    # CSV, protože se seznamem se reálně pracuje v mailingu nebo tabulce,
    # ne v JSON prohlížeči.
    if request.args.get('format') == 'csv':
        import csv, io as _io
        buf = _io.StringIO()
        w = csv.writer(buf)
        w.writerow(['email', 'role', 'source', 'created_at'])
        for r in rows:
            w.writerow([r['email'], r['role'], r['source'], r['created_at']])
        return Response(
            buf.getvalue(), mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=waitlist.csv'})

    return jsonify({'count': len(rows), 'entries': [dict(r) for r in rows]})


@app.route('/api/events')
def get_events():
    uid   = session.get('user_id', 0)
    _now  = _prague_now_naive()
    year  = int(request.args.get('year',  _now.year))
    month = int(request.args.get('month', _now.month))
    genre = request.args.get('genre', '').strip()

    city  = request.args.get('city', '').strip()
    try:
        flat    = float(request.args.get('lat', ''))
        flng    = float(request.args.get('lng', ''))
        fradius = float(request.args.get('radius', 50))
        gps_filter = True
    except (ValueError, TypeError):
        gps_filter = False

    # Rozsah data má přednost před měsícem. Týdenní pohled v kalendáři přes
    # přelom měsíce by s `date LIKE '2026-09%'` utnul půlku týdne.
    date_from = request.args.get('from', '').strip()
    date_to   = request.args.get('to', '').strip()
    artist_id = request.args.get('artist_id', '').strip()

    conn   = get_db()
    if date_from and date_to:
        params = [date_from, date_to]
        date_clause = 'e.date >= ? AND e.date <= ?'
    else:
        params = [f'{year}-{month:02d}%']
        date_clause = 'e.date LIKE ?'

    query  = f'''
        SELECT e.*, u.username, u.display_name, u.emoji, u.lat AS user_lat, u.lng AS user_lng
        FROM events e
        JOIN users u ON e.user_id = u.id
        WHERE {date_clause}
    '''
    if artist_id.isdigit():
        query += ' AND e.user_id = ?'
        params.append(int(artist_id))
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
    today = _prague_now_naive().strftime('%Y-%m-%d')
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
    today = _prague_now_naive().strftime('%Y-%m-%d')
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
    # Veřejné. Akce tatéra jsou marketingová plocha — nemá smysl je schovávat
    # za login, když je profil sám veřejný a je v sitemapě.
    conn = get_db()
    user = conn.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
    if not user:
        conn.close(); return jsonify([])
    today = _prague_now_naive().strftime('%Y-%m-%d')
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
                # Hour-bucketed idempotency: protects against double-click within an hour
                # (rapid duplicate would create 2 orphan accounts), but allows retry after
                # a fix without waiting 24h (Stripe caches failure responses per key).
                idempotency_key=f'connect-account-{u["id"]}-{int(time.time() // 3600)}',
            )
            acct_id = acct.id
            conn.execute('UPDATE users SET stripe_account_id=? WHERE id=?', (acct_id, u['id']))
            conn.commit()

        day = int(time.time() // 86400)
        link = stripe.AccountLink.create(
            account=acct_id,
            refresh_url=f'{_origin()}/api/artist/connect/refresh',
            return_url=f'{_origin()}/api/artist/connect/return',
            type='account_onboarding',
            idempotency_key=f'connect-link-onboard-{u["id"]}-{day}',
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
        day = int(time.time() // 86400)
        link = stripe.AccountLink.create(
            account=u['stripe_account_id'],
            refresh_url=f'{_origin()}/api/artist/connect/refresh',
            return_url=f'{_origin()}/api/artist/connect/return',
            type='account_onboarding',
            idempotency_key=f'connect-link-refresh-{session["user_id"]}-{day}',
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
        link = stripe.Account.create_login_link(
            u['stripe_account_id'],
            idempotency_key=f'connect-dashboard-{u["stripe_account_id"]}-{int(time.time() // 60)}',
        )
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

@app.route('/api/_diag/stripe-connect', methods=['GET'])
def diag_stripe_connect():
    """TEMPORARY diagnostic. Gated by ENABLE_DIAG=1 env — OFF by default in prod.
    To use: set ENABLE_DIAG=1 in Railway env, run diag, unset the flag."""
    if os.environ.get('ENABLE_DIAG', '0') != '1':
        return jsonify({'error': 'not enabled — set ENABLE_DIAG=1 to use'}), 404
    token = request.args.get('token', '')
    if not RECONCILE_TOKEN or token != RECONCILE_TOKEN:
        return jsonify({'error': 'forbidden'}), 403
    out = {'sk_prefix': (STRIPE_SECRET_KEY or '')[:14] + '...', 'api_version': stripe.api_version}
    try:
        bal = stripe.Balance.retrieve()
        out['balance_ok'] = True
        out['balance_currencies'] = sorted({b.currency for b in bal.available})
    except Exception as e:
        out['balance_error'] = str(e)[:300]
    try:
        acct = stripe.Account.retrieve()
        out['platform_id'] = acct.id
        out['platform_type'] = acct.type
        out['platform_country'] = acct.country
        out['platform_charges_enabled'] = acct.charges_enabled
        out['platform_details_submitted'] = acct.details_submitted
        caps = {}
        try:
            for k, v in (acct.capabilities or {}).items():
                caps[k] = v
        except Exception:
            pass
        out['platform_capabilities'] = caps
        out['platform_business_type'] = getattr(acct, 'business_type', None)
    except Exception as e:
        out['account_retrieve_error'] = str(e)[:300]
    for acct_type in ('standard', 'express', 'custom'):
        try:
            kwargs = {'type': acct_type, 'country': 'CZ',
                      'email': f'diag-{acct_type}-{int(time.time())}@inklink.test'}
            if acct_type in ('express', 'custom'):
                kwargs['capabilities'] = {
                    'card_payments': {'requested': True},
                    'transfers': {'requested': True},
                }
                kwargs['business_type'] = 'individual'
            # Minute-bucketed: repeated diag calls within the same minute reuse
            # the same test account instead of piling up orphan Connect accounts.
            kwargs['idempotency_key'] = f'diag-{acct_type}-{int(time.time() // 60)}'
            test_acct = stripe.Account.create(**kwargs)
            out[f'create_{acct_type}_ok'] = True
            out[f'create_{acct_type}_id'] = test_acct.id
        except Exception as e:
            out[f'create_{acct_type}_error'] = str(e)[:400]
    return jsonify(out)


@app.route('/api/_diag/set-stripe-account', methods=['POST', 'GET'])
def diag_set_stripe_account():
    """TEMPORARY workaround. Gated by ENABLE_DIAG=1 env — OFF by default.
    Usage: ?token=...&username=USER&acct_id=acct_XXX"""
    if os.environ.get('ENABLE_DIAG', '0') != '1':
        return jsonify({'error': 'not enabled — set ENABLE_DIAG=1 to use'}), 404
    token = request.args.get('token', '')
    if not RECONCILE_TOKEN or token != RECONCILE_TOKEN:
        return jsonify({'error': 'forbidden'}), 403
    username = (request.args.get('username') or '').strip().lower()
    acct_id = (request.args.get('acct_id') or '').strip()
    if not username or not acct_id.startswith('acct_'):
        return jsonify({'error': 'need ?username=X&acct_id=acct_...'}), 400
    conn = get_db()
    u = conn.execute('SELECT id, username FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        conn.close()
        return jsonify({'error': f'user {username} not found'}), 404
    conn.execute(
        'UPDATE users SET stripe_account_id=?, stripe_charges_enabled=1, stripe_details_submitted=1, '
        'is_artist=1, verified_artist_at=COALESCE(verified_artist_at, ?) WHERE id=?',
        (acct_id, datetime.utcnow().isoformat(), u['id'])
    )
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'user_id': u['id'], 'username': u['username'], 'stripe_account_id': acct_id})


# EU + Iceland, Liechtenstein, Norway — matches pricing/config.py's comment
# ("CZ/EEA codes = card_eea, anything else = card_non_eea").
EEA_COUNTRY_CODES = {
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR', 'DE', 'GR',
    'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL', 'PL', 'PT', 'RO', 'SK',
    'SI', 'ES', 'SE', 'IS', 'LI', 'NO',
}


def _reconcile_card_country(conn, booking_id, pi_obj):
    """Re-snapshot economics with the actual card country from a succeeded
    PaymentIntent. The pre-payment estimate always assumes 'card_eea'
    (pricing/config.py); this corrects the stripe_fee/inklink_net figures
    for reporting once we know the real card. Doesn't touch anything already
    charged — the Stripe application_fee_amount was fixed at PI creation.
    No-op if the booking has no 'initial' snapshot (legacy flat-fee path) or
    the country can't be read.
    """
    try:
        charges = pi_obj.get('charges') if isinstance(pi_obj, dict) else pi_obj.charges
        charge_list = (charges.get('data') if isinstance(charges, dict) else getattr(charges, 'data', None)) or []
        if not charge_list:
            return
        first = charge_list[0]
        pmd = first.get('payment_method_details') if isinstance(first, dict) else first.payment_method_details
        card = (pmd.get('card') if isinstance(pmd, dict) else getattr(pmd, 'card', None)) if pmd else None
        country = (card.get('country') if isinstance(card, dict) else getattr(card, 'country', None)) if card else None
        if not country:
            return
        card_type = 'card_eea' if country.upper() in EEA_COUNTRY_CODES else 'card_non_eea'

        row = conn.execute(
            "SELECT snapshot FROM economics_snapshots WHERE booking_id=? AND kind='initial' "
            "ORDER BY id DESC LIMIT 1", (booking_id,)
        ).fetchone()
        if not row:
            return
        import json as _json_cc
        prev = _json_cc.loads(row['snapshot'])
        if card_type == 'card_eea':
            return  # pre-payment estimate already assumed card_eea — nothing changed

        from pricing import stripe_fee_for, emit_event as _emit_cc
        from decimal import Decimal as _Dec
        client_pays_total = _Dec(str(prev['client_pays_total']))
        corrected_fee = stripe_fee_for(client_pays_total, card_type=card_type)
        original_fee = _Dec(str(prev['stripe_fee']))
        if corrected_fee == original_fee:
            return
        fee_delta = corrected_fee - original_fee
        corrected_net = _Dec(str(prev['inklink_net'])) - fee_delta
        snapshot = dict(prev)
        snapshot['stripe_fee'] = float(corrected_fee)
        snapshot['inklink_net'] = float(corrected_net)
        snapshot['effective_take_rate'] = (
            float(corrected_net / client_pays_total) if client_pays_total else 0.0
        )
        snapshot['card_country'] = country
        snapshot['stripe_card_type'] = card_type
        conn.execute(
            "INSERT INTO economics_snapshots (booking_id, kind, snapshot) VALUES (?, 'adjust', ?)",
            (booking_id, _json_cc.dumps(snapshot, ensure_ascii=False))
        )
        conn.commit()
        _emit_cc('booking.card_country_reconciled', {
            'booking_id': booking_id, 'card_country': country, 'card_type': card_type,
            'fee_delta_czk': float(fee_delta),
        }, conn=conn)
        conn.commit()
    except Exception as e:
        print(f'[card-country] reconcile failed for booking {booking_id}: {e}')


# ── Měny ──────────────────────────────────────────────────────────────────
#
# Měna patří tatérovi, ne divákovi. Stripe strhává v jedné měně, výplatní
# účet má jednu měnu a ceny nastavuje tatér — takže Čech, který se dívá na
# berlínského tatéra, vidí eura. Přepočítávat pro zobrazení by lhalo:
# stržená částka by stejně byla v eurech.
#
# Všechny podporované měny mají dvě desetinná místa, takže *_cents platí
# beze změny. Jen a Won by to rozbily (nulová desetinná místa u Stripe),
# proto v seznamu nejsou.

CURRENCIES = {
    'CZK': {'symbol': 'Kč',  'locale': 'cs-CZ'},
    'EUR': {'symbol': '€',   'locale': 'de-DE'},
    'USD': {'symbol': '$',   'locale': 'en-US'},
    'GBP': {'symbol': '£',   'locale': 'en-GB'},
    'PLN': {'symbol': 'zł',  'locale': 'pl-PL'},
}
DEFAULT_CURRENCY = 'CZK'


# Měna se neptá, odvozuje se. Autoritativní je země Stripe účtu — ta určuje,
# v čem tatérovi můžou přijít peníze, takže hádat proti ní nemá smysl. Než
# se Stripe připojí, jede se podle města.
COUNTRY_CURRENCY = {
    'CZ': 'CZK', 'PL': 'PLN', 'GB': 'GBP', 'US': 'USD',
    # Eurozóna
    'SK': 'EUR', 'DE': 'EUR', 'AT': 'EUR', 'FR': 'EUR', 'IT': 'EUR', 'ES': 'EUR',
    'NL': 'EUR', 'BE': 'EUR', 'IE': 'EUR', 'PT': 'EUR', 'FI': 'EUR', 'GR': 'EUR',
    'SI': 'EUR', 'EE': 'EUR', 'LV': 'EUR', 'LT': 'EUR', 'LU': 'EUR', 'MT': 'EUR',
    'CY': 'EUR', 'HR': 'EUR',
}
# Města, která nabízí filtr. Slovenská jsou v eurozóně — bez tohohle by
# tatér z Bratislavy účtoval v korunách.
CITY_COUNTRY = {
    'praha': 'CZ', 'brno': 'CZ', 'ostrava': 'CZ', 'plzen': 'CZ', 'plzeň': 'CZ',
    'liberec': 'CZ', 'olomouc': 'CZ', 'budejovice': 'CZ', 'hradec': 'CZ',
    'bratislava': 'SK', 'kosice': 'SK', 'košice': 'SK', 'zilina': 'SK',
    'žilina': 'SK', 'presov': 'SK', 'prešov': 'SK', 'nitra': 'SK',
    'warszawa': 'PL', 'krakow': 'PL', 'kraków': 'PL', 'wroclaw': 'PL',
    'berlin': 'DE', 'wien': 'AT', 'vienna': 'AT', 'london': 'GB',
}


def _country_from_city(city):
    key = (city or '').strip().lower()
    if not key:
        return None
    for name, cc in CITY_COUNTRY.items():
        if name in key:
            return cc
    return None


def _derive_currency(conn, user_id, stripe_country=None):
    """Vrátí měnu, kterou má tatér mít. Nic neukládá — o zápis se stará
    volající, aby se rozhodnutí dalo i jen zobrazit."""
    if stripe_country:
        cc = stripe_country.strip().upper()
        if cc in COUNTRY_CURRENCY:
            return COUNTRY_CURRENCY[cc]
    row = conn.execute('SELECT city FROM users WHERE id=?', (user_id,)).fetchone()
    cc = _country_from_city(row['city'] if row else None)
    return COUNTRY_CURRENCY.get(cc, DEFAULT_CURRENCY)


def _sync_currency(conn, user_id, stripe_country=None):
    cur = _derive_currency(conn, user_id, stripe_country)
    conn.execute('UPDATE users SET currency=? WHERE id=?', (cur, user_id))
    return cur


def _norm_currency(code):
    code = (code or '').strip().upper()
    return code if code in CURRENCIES else DEFAULT_CURRENCY


def _artist_currency(conn, artist_id):
    row = conn.execute('SELECT currency FROM users WHERE id=?', (artist_id,)).fetchone()
    return _norm_currency(row['currency'] if row else None)


@app.route('/api/currencies')
def list_currencies():
    """Sdílené mezi UI a serverem, ať se seznam nerozejde."""
    return jsonify({
        'default': DEFAULT_CURRENCY,
        'currencies': [{'code': c, **v} for c, v in CURRENCIES.items()],
    })


# ── Kredit ────────────────────────────────────────────────────────────────
#
# Kredit jsou cizí peníze, které držíme my. To znamená dvě povinnosti:
# umět kdykoliv doložit, odkud přišly a kam šly, a nezapomenout, že
# nejsou naše. Proto účetní kniha (credit_ledger) a ne jen číslo na
# uživateli — číslo se dá přepsat, kniha ne.
#
# users.account_credit_cents zůstává jako rychlý zůstatek pro čtení, ale
# zdrojem pravdy je kniha. Test hlídá, že se nerozejdou.

CREDIT_REASONS = (
    'referral_bonus',    # bonus za doporučení
    'voucher_redeem',    # uplatněný dárkový poukaz
    'booking_spend',     # utraceno při rezervaci (záporné)
    'booking_refund',    # vráceno zpět při zrušení
    'admin_adjust',      # ruční oprava
)


def _credit_balance(conn, user_id):
    row = conn.execute('SELECT COALESCE(account_credit_cents, 0) AS c FROM users WHERE id=?',
                       (user_id,)).fetchone()
    return (row['c'] if row else 0) or 0


def _credit_move(conn, user_id, delta_cents, reason, ref_type=None, ref_id=None, note=''):
    """Zapíše pohyb do knihy a srovná zůstatek. Nikdy nepustí zůstatek pod
    nulu — utratit se dá jen to, co tam je.

    Vrací nový zůstatek, nebo None když by šel do minusu."""
    if reason not in CREDIT_REASONS:
        raise ValueError(f'neznámý důvod pohybu kreditu: {reason}')
    delta = int(delta_cents)
    if delta == 0:
        return _credit_balance(conn, user_id)
    current = _credit_balance(conn, user_id)
    if current + delta < 0:
        return None
    conn.execute(
        'INSERT INTO credit_ledger (user_id, delta_cents, reason, ref_type, ref_id, note) '
        'VALUES (?,?,?,?,?,?)',
        (user_id, delta, reason, ref_type or '', ref_id, note[:200]))
    conn.execute('UPDATE users SET account_credit_cents = COALESCE(account_credit_cents, 0) + ? '
                 'WHERE id = ?', (delta, user_id))
    return current + delta


@app.route('/api/me/credit')
def my_credit():
    err = require_login()
    if err: return err
    uid  = session['user_id']
    conn = get_db()
    rows = conn.execute(
        'SELECT delta_cents, reason, ref_type, ref_id, note, created_at '
        'FROM credit_ledger WHERE user_id=? ORDER BY id DESC LIMIT 50', (uid,)).fetchall()
    balance = _credit_balance(conn, uid)
    conn.close()
    return jsonify({
        'balance_kc': balance / 100,
        'history': [{
            'amount_kc': r['delta_cents'] / 100,
            'reason':    r['reason'],
            'ref_type':  r['ref_type'] or '',
            'ref_id':    r['ref_id'],
            'note':      r['note'] or '',
            'at':        r['created_at'],
        } for r in rows],
    })


# ── Vzhled poukazu ────────────────────────────────────────────────────────
#
# Grafiku dělá člověk v Canvě, ne my v CSS. Nahraje obrázek na pozadí a
# řekne, kam na něm patří částka, kód, jméno a vzkaz. Bez šablony se
# poukaz vykreslí prostou výchozí kartou — nikdy nesmí zůstat prázdný.

VOUCHER_FIELDS = ('amount', 'code', 'recipient', 'message')
VOUCHER_DEFAULT_LAYOUT = {
    'amount':    {'x': 50, 'y': 34, 'size': 9.0, 'color': '#0a0a0a', 'align': 'center'},
    'code':      {'x': 50, 'y': 66, 'size': 3.4, 'color': '#0a0a0a', 'align': 'center'},
    'recipient': {'x': 50, 'y': 50, 'size': 2.6, 'color': '#2a2a2a', 'align': 'center'},
    'message':   {'x': 50, 'y': 57, 'size': 2.0, 'color': '#5a5a5a', 'align': 'center'},
}


def _setting_get(conn, key, default=None):
    row = conn.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
    return row['value'] if row else default


def _setting_set(conn, key, value):
    conn.execute('DELETE FROM app_settings WHERE key=?', (key,))
    conn.execute('INSERT INTO app_settings (key, value) VALUES (?,?)', (key, value))


def _voucher_template(conn):
    """{'image': 'soubor.png'|None, 'layout': {...}}. Chybějící pole
    doplní výchozími — po přidání dalšího pole nesmí spadnout render
    starých šablon."""
    import json as _json
    image = _setting_get(conn, 'voucher_template_image')
    raw   = _setting_get(conn, 'voucher_template_layout')
    layout = {k: dict(v) for k, v in VOUCHER_DEFAULT_LAYOUT.items()}
    if raw:
        try:
            for k, v in (_json.loads(raw) or {}).items():
                if k in layout and isinstance(v, dict):
                    layout[k].update({kk: v[kk] for kk in ('x', 'y', 'size', 'color', 'align')
                                      if kk in v})
        except (ValueError, TypeError):
            pass
    return {'image': image, 'layout': layout}


@app.route('/api/admin/voucher-template', methods=['GET', 'POST'])
def admin_voucher_template():
    err = require_admin()
    if err: return err
    conn = get_db()

    if request.method == 'POST':
        import json as _json
        img = request.files.get('image')
        if img and img.filename:
            ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in img.filename else ''
            if ext not in ('png', 'jpg', 'jpeg', 'webp'):
                conn.close()
                return jsonify({'error': 'Obrázek musí být PNG, JPG nebo WEBP.'}), 400
            img.seek(0, os.SEEK_END); size = img.tell(); img.seek(0)
            if size > MESSAGE_IMAGE_MAX_BYTES:
                conn.close(); return jsonify({'error': 'Obrázek je větší než 12 MB.'}), 400
            name = f'voucher_bg_{int(time.time())}.{ext}'
            save_upload(img, name)
            _setting_set(conn, 'voucher_template_image', name)

        raw = request.form.get('layout') or (request.get_json(silent=True) or {}).get('layout')
        if raw:
            layout = _json.loads(raw) if isinstance(raw, str) else raw
            clean = {}
            for f in VOUCHER_FIELDS:
                v = (layout or {}).get(f) or {}
                clean[f] = {
                    'x': max(0.0, min(100.0, float(v.get('x', VOUCHER_DEFAULT_LAYOUT[f]['x'])))),
                    'y': max(0.0, min(100.0, float(v.get('y', VOUCHER_DEFAULT_LAYOUT[f]['y'])))),
                    'size': max(0.5, min(30.0, float(v.get('size', VOUCHER_DEFAULT_LAYOUT[f]['size'])))),
                    'color': str(v.get('color') or VOUCHER_DEFAULT_LAYOUT[f]['color'])[:9],
                    'align': v.get('align') if v.get('align') in ('left', 'center', 'right')
                             else VOUCHER_DEFAULT_LAYOUT[f]['align'],
                }
            _setting_set(conn, 'voucher_template_layout', _json.dumps(clean))
        conn.commit()

    tpl = _voucher_template(conn)
    conn.close()
    return jsonify(tpl)


@app.route('/api/admin/voucher-template', methods=['DELETE'])
def admin_voucher_template_reset():
    err = require_admin()
    if err: return err
    conn = get_db()
    conn.execute("DELETE FROM app_settings WHERE key LIKE 'voucher_template_%'")
    conn.commit(); conn.close()
    return jsonify({'ok': True})


# ── Dárkové poukazy ───────────────────────────────────────────────────────
#
# Poukaz je koupený kredit. Platí u kteréhokoliv tatéra a peníze do
# uplatnění držíme my — to je rozhodnutí produktu, ne technické. Plyne
# z něj, že nesplacené poukazy jsou náš závazek, ne tržba: dokud je někdo
# neutratí, dlužíme jejich hodnotu. Admin proto vidí součet zvlášť.
#
# Kód se nepočítá z ničeho, co by šlo uhodnout. Nula, O, I a jednička
# v abecedě nejsou schválně — poukaz se opisuje z papíru.

VOUCHER_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
VOUCHER_VALID_MONTHS = 12
VOUCHER_MIN_KC = 500
VOUCHER_MAX_KC = 50000


def _voucher_code():
    import secrets
    raw = ''.join(secrets.choice(VOUCHER_ALPHABET) for _ in range(12))
    return f'{raw[:4]}-{raw[4:8]}-{raw[8:]}'


@app.route('/api/vouchers', methods=['POST'])
@limiter.limit('10 per hour')
def create_voucher():
    """Založí poukaz. V demo režimu (bez Stripe) je rovnou platný, jinak
    čeká na zaplacení — poukaz, za který nikdo nezaplatil, by byl kredit
    z ničeho."""
    err = require_login()
    if err: return err
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    try:
        amount_kc = int(data.get('amount_kc') or 0)
    except (ValueError, TypeError):
        return jsonify({'error': 'Zadej částku.'}), 400
    if amount_kc < VOUCHER_MIN_KC or amount_kc > VOUCHER_MAX_KC:
        return jsonify({'error': f'Poukaz může být na {VOUCHER_MIN_KC}–'
                                 f'{VOUCHER_MAX_KC} Kč.'}), 400
    recipient = (data.get('recipient_name') or '').strip()[:80]
    message   = (data.get('message') or '').strip()[:300]

    conn = get_db()
    expires = (_prague_now_naive() + timedelta(days=30 * VOUCHER_VALID_MONTHS)).isoformat()
    demo = not STRIPE_SECRET_KEY
    for _ in range(5):                      # kolize kódu jsou nepravděpodobné, ne nemožné
        code = _voucher_code()
        try:
            conn.execute(
                'INSERT INTO vouchers (code, amount_cents, buyer_id, recipient_name, '
                'message, status, expires_at, currency) VALUES (?,?,?,?,?,?,?,?)',
                (code, amount_kc * 100, uid, recipient, message,
                 'active' if demo else 'awaiting_payment', expires,
                 _artist_currency(conn, uid)))
            conn.commit()
            break
        except Exception:
            code = None
    if not code:
        conn.close(); return jsonify({'error': 'Nepovedlo se vygenerovat kód.'}), 500
    vid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
           else conn.execute('SELECT lastval()').fetchone()[0])
    conn.close()
    return jsonify({'ok': True, 'id': vid, 'code': code, 'amount_kc': amount_kc,
                    'expires_at': expires, 'status': 'active' if demo else 'awaiting_payment',
                    'print_url': f'/vouchers/{code}'})


@app.route('/api/vouchers/redeem', methods=['POST'])
@limiter.limit('20 per hour')
def redeem_voucher():
    err = require_login()
    if err: return err
    uid  = session['user_id']
    code = (request.get_json(silent=True) or {}).get('code', '')
    code = code.strip().upper().replace(' ', '')
    if len(code) < 8:
        return jsonify({'error': 'Zadej kód z poukazu.'}), 400

    conn = get_db()
    v = conn.execute('SELECT * FROM vouchers WHERE code=?', (code,)).fetchone()
    if not v:
        conn.close(); return jsonify({'error': 'Takový poukaz neznáme.'}), 404
    if v['status'] == 'redeemed':
        conn.close(); return jsonify({'error': 'Tenhle poukaz už byl uplatněný.'}), 409
    if v['status'] != 'active':
        conn.close(); return jsonify({'error': 'Poukaz zatím není platný.'}), 409
    try:
        if _naive_dt(v['expires_at']) <= _prague_now_naive():
            conn.close(); return jsonify({'error': 'Poukazu vypršela platnost.'}), 409
    except (ValueError, TypeError):
        pass

    # Označit dřív, než připíšeme kredit: kdyby to spadlo mezi tím, radši
    # neuplatněný poukaz než kredit ze vzduchu.
    changed = conn.execute(
        "UPDATE vouchers SET status='redeemed', redeemed_by=?, redeemed_at=? "
        "WHERE id=? AND status='active'",
        (uid, _prague_now_naive().isoformat(), v['id'])).rowcount
    conn.commit()
    if not changed:
        conn.close(); return jsonify({'error': 'Tenhle poukaz už byl uplatněný.'}), 409

    balance = _credit_move(conn, uid, v['amount_cents'], 'voucher_redeem', 'voucher', v['id'])
    conn.commit(); conn.close()
    return jsonify({'ok': True, 'amount_kc': v['amount_cents'] // 100,
                    'balance_kc': (balance or 0) / 100})


@app.route('/api/vouchers/mine')
def my_vouchers():
    err = require_login()
    if err: return err
    conn = get_db()
    rows = conn.execute(
        'SELECT id, code, amount_cents, recipient_name, status, expires_at, created_at '
        'FROM vouchers WHERE buyer_id=? ORDER BY id DESC LIMIT 50',
        (session['user_id'],)).fetchall()
    conn.close()
    return jsonify([{
        'id': r['id'], 'code': r['code'], 'amount_kc': r['amount_cents'] // 100,
        'recipient_name': r['recipient_name'] or '', 'status': r['status'],
        'expires_at': r['expires_at'], 'created_at': r['created_at'],
        'print_url': f'/vouchers/{r["code"]}',
    } for r in rows])


def _voucher_render(v, tpl, preview=False):
    """Vykreslí poukaz.

    Bez nahrané šablony jede vestavěný lístek: logo a druh nahoře, částka
    v levém dolním rohu, kód na útržku pod čárkovanou linkou. Čárkovaná
    čára tu má význam — je to útržek, ne linka na psaní.

    Volná plocha vpravo dole je schválně prázdná: na vytištěný poukaz si
    dárce dopíše, co chce. Digitálně se do ní vysadí vzkaz zadaný při
    koupi, takže místo nezůstane hluché ani na obrazovce.

    Se šablonou z Canvy je poukaz obrázek na pozadí a texty v poměrných
    souřadnicích — velikosti v cqw, aby vypadal stejně na telefonu
    i na papíře.
    """
    from html import escape as _h
    cur = _norm_currency(v['currency'] if 'currency' in v.keys() else None)
    amount_n = v['amount_cents'] // 100
    amount = f'{amount_n:,}'.replace(',', ' ')
    symbol = CURRENCIES[cur]['symbol']
    try:
        exp = _naive_dt(v['expires_at']).strftime('%d. %m. %Y')
    except (ValueError, TypeError):
        exp = ''
    used = v['status'] == 'redeemed'
    site = _h(APP_BASE_URL.replace('https://', '').replace('http://', ''))
    fine = ('Uplatníš na <b>%s</b> — zadáš kód a částka se ti připíše jako kredit. '
            'Platí u kteréhokoliv tatéra%s.' % (site, f' do {exp}' if exp else ''))
    stamp = '<div class="used">UPLATNĚNO</div>' if used else ''

    if tpl['image']:
        L = tpl['layout']
        values = {'amount': f'{amount} {symbol}', 'code': v['code'],
                  'recipient': (f'Pro {v["recipient_name"]}' if v['recipient_name'] else ''),
                  'message': v['message'] or ''}

        def field(name):
            val = values.get(name) or ''
            if not val:
                return ''
            f = L[name]
            # Zarovnání posunem celého bloku, ne text-align uvnitř — jinak
            # by se dlouhý vzkaz choval jinak než krátký kód.
            shift = {'left': '0', 'center': '-50%', 'right': '-100%'}[f['align']]
            return (f'<div style="position:absolute;left:{f["x"]}%;top:{f["y"]}%;'
                    f'transform:translate({shift},-50%);font-size:{f["size"]}cqw;'
                    f'color:{_h(f["color"])};text-align:{f["align"]};max-width:86%;'
                    f'line-height:1.35;letter-spacing:'
                    f'{"0.18em" if name == "code" else "0.02em"};'
                    f'white-space:pre-wrap">{_h(val)}</div>')

        inner = (f'<img src="/uploads/{_h(tpl["image"])}" alt="" style="width:100%;display:block">'
                 + field('amount') + field('recipient') + field('message')
                 + field('code') + stamp)
        card_class = 'v'
    else:
        note = (f'<div class="msg">{_h(v["message"])}</div>' if v['message'] else '')
        to = (f'<div class="to">Pro {_h(v["recipient_name"])}</div>'
              if v['recipient_name'] else '')
        inner = f'''
      <div class="top">
        <img class="lg" src="/img/inklink-logo.png" alt="inklink">
        <div class="kind">Dárkový poukaz</div>
      </div>
      <div class="amt">{amount}</div>
      <div class="cur">{_h(symbol)}</div>
      <div class="stub">
        <div class="left">
          <div class="cd">{_h(v['code'])}</div>
        </div>
        <div class="right">{to}{note}</div>
      </div>{stamp}'''
        card_class = 'v card'

    return f'''<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dárkový poukaz — InkLink</title>
<style>
  @page {{ margin: 0; size: auto; }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#d9d3c6;font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;
    display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}}
  .v{{position:relative;width:100%;max-width:640px;container-type:inline-size;overflow:hidden}}
  .card{{background:#faf8f3;border:1px solid #0a0a0a;aspect-ratio:5/3;
    display:flex;flex-direction:column;padding:5.5cqw 6cqw;color:#0a0a0a}}
  .card .top{{display:flex;justify-content:space-between;align-items:flex-start;gap:3cqw}}
  .card .lg{{width:26%;display:block}}
  .card .kind{{font-size:2.1cqw;letter-spacing:0.28em;text-transform:uppercase;
    color:#8a8a8a;padding-top:0.8cqw;white-space:nowrap}}
  .card .amt{{margin-top:auto;font-size:15cqw;line-height:0.86;letter-spacing:-0.02em}}
  .card .cur{{font-size:3.4cqw;letter-spacing:0.16em;color:#5a5a5a;margin-top:1.4cqw}}
  .card .stub{{margin-top:auto;border-top:1px dashed #b8b1a2;padding-top:3cqw;
    display:flex;justify-content:space-between;align-items:flex-end;gap:4cqw}}
  .card .cd{{font-family:'DM Mono',ui-monospace,'SFMono-Regular',Menlo,monospace;
    font-size:3.4cqw;letter-spacing:0.14em;white-space:nowrap}}
  /* Volná plocha na dopsání rukou. Digitálně ji zaplní vzkaz z objednávky. */
  .card .right{{text-align:right;max-width:52%;min-height:6cqw}}
  .card .to{{font-size:2.4cqw;letter-spacing:0.04em}}
  .card .msg{{font-size:2cqw;line-height:1.6;color:#5a5a5a;white-space:pre-wrap;
    margin-top:0.6cqw}}
  .used{{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
    font-size:7cqw;letter-spacing:0.2em;color:rgba(198,40,40,0.45);
    transform:rotate(-14deg);pointer-events:none}}
  .fine{{margin-top:14px;max-width:640px;font-size:11px;color:#5a5a5a;line-height:1.8;
    text-align:center}}
  @media print {{
    body{{background:#fff;padding:0;display:block}}
    .v{{max-width:none;margin:0 auto}}
    .fine{{margin:10px auto 0}}
  }}
</style>
<div>
  <div class="{card_class}">{inner}</div>
  <div class="fine">{fine}</div>
</div>'''


@app.route('/vouchers/<code>')
def voucher_print(code):
    """Poukaz k vytisknutí i k poslání odkazem. Bez přihlášení — dárce ho
    posílá dál a obdarovaný účet mít nemusí, dokud kód neuplatní."""
    conn = get_db()
    v = conn.execute('SELECT * FROM vouchers WHERE code=?',
                     (code.strip().upper(),)).fetchone()
    if not v or v['status'] == 'awaiting_payment':
        conn.close()
        return _plain_page('Takový poukaz neznáme.'), 404
    tpl = _voucher_template(conn)
    conn.close()
    return Response(_voucher_render(v, tpl), mimetype='text/html')


@app.route('/api/admin/voucher-preview')
def admin_voucher_preview():
    """Náhled na vymyšlených datech, ať se dá šablona doladit bez toho,
    aby se kvůli tomu kupoval poukaz."""
    err = require_admin()
    if err: return err
    conn = get_db()
    tpl = _voucher_template(conn)
    conn.close()
    fake = {'code': 'ABCD-2K5X-QW74', 'amount_cents': 300000,
            'recipient_name': 'Jan Novák', 'message': 'Ať se ti to povede!',
            'status': 'active', 'currency': DEFAULT_CURRENCY,
            'expires_at': (_prague_now_naive() + timedelta(days=365)).isoformat()}
    return Response(_voucher_render(fake, tpl, preview=True), mimetype='text/html')


# ── Premium: hojení ───────────────────────────────────────────────────────
#
# Po sezení běží sekvence sama. Tatérovi to bere práci, kterou stejně
# dělá — jen ve 23:00 a pořád stejnou. Zhojená fotka na konci je navíc
# to, co portfolio prodává: skica ukazuje záměr, zhojená práce důkaz.
#
# Instrukce píše tatér, ne platforma. Každý má svůj protokol a InkLink
# nemá co radit v něčem, co se hojí na cizí kůži.

AFTERCARE_STEPS = (
    # (klíč, den po sezení). Nultý krok neposílá cron, ale rovnou
    # dokončení rezervace — instrukce mají dorazit, než klient odejde.
    ('day7',  7),
    ('day30', 30),
)
AFTERCARE_FIRST_STEP = 'day0'
# Okno, ve kterém se krok ještě smí poslat. Bez něj by zapnutí premia
# vyslalo celou historii najednou — klient by dostal tři maily o tetování
# z loňska.
AFTERCARE_WINDOW_DAYS = 3


def _aftercare_token(booking_id):
    import hashlib, hmac
    key = (app.secret_key if isinstance(app.secret_key, bytes)
           else str(app.secret_key).encode())
    return hmac.new(key, f'aftercare:{booking_id}'.encode(),
                    hashlib.sha256).hexdigest()[:32]


def _aftercare_email(step, ctx):
    from html import escape as _h
    artist = _h(ctx.get('artist_name') or '')
    stop   = _h(ctx.get('stop_url') or '')

    # Bez oslovení jménem schválně: české vokativy se automaticky
    # skloňovat nedají a "Ahoj Tereza" zní hůř než prosté "Ahoj".
    # Ze stejného důvodu se vyhýbáme rodovým příčestím ("napsal/a").
    def wrap(inner):
        return (
            '<div style="background:#faf8f3;color:#1a1a1a;font-family:Helvetica,Arial,sans-serif;'
            'padding:40px;max-width:520px;margin:0 auto">'
            f'<div style="font-size:22px;letter-spacing:0.12em;margin-bottom:24px">{artist}</div>'
            + inner +
            '<p style="color:#8a8a8a;font-size:11px;line-height:1.7;margin-top:36px;'
            'border-top:1px solid #ddd6c8;padding-top:16px">'
            f'Tyhle zprávy ti posílá {artist} přes InkLink, aby se tetování dobře zhojilo.<br>'
            f'<a href="{stop}" style="color:#8a8a8a">Nechci k tomuhle tetování další zprávy</a>'
            '</p></div>')

    def button(url, label):
        return (f'<p style="margin-top:24px"><a href="{_h(url or "#")}" '
                'style="display:inline-block;background:#0a0a0a;color:#faf8f3;padding:13px 26px;'
                'text-decoration:none;letter-spacing:0.1em;text-transform:uppercase;'
                f'font-size:12px">{_h(label)}</a></p>')

    p = lambda txt: f'<p style="font-size:14px;line-height:1.7">{txt}</p>'

    if step == 'day0':
        care = _h(ctx.get('care_text') or '')
        care_block = (f'<div style="background:#f1ece0;padding:16px 18px;margin:18px 0;'
                      f'font-size:14px;line-height:1.7;white-space:pre-wrap">{care}</div>'
                      if care else
                      p('Drž se toho, co jsme si řekli na místě — můj postup platí.'))
        return ('Jak se postarat o čerstvé tetování',
                wrap('<p>Ahoj,</p>'
                     + p('máš čerstvé tetování. Prvních pár dní rozhoduje o tom, jak bude '
                         'vypadat napořád — dej si na něj pozor.')
                     + care_block
                     + p('Kdyby něco vypadalo divně, napiš mi.')))

    if step == 'day7':
        return ('Jak se hojí?',
                wrap('<p>Ahoj,</p>'
                     + p('máš za sebou první týden. V téhle fázi to většinou svědí a kůže '
                         'se olupuje — to je normální. Hlavně nešťourat a nestrhávat strupy.')
                     + p('Kdyby bylo místo horké, oteklé nebo by bolest sílila, ozvi se mi.')
                     + button(ctx.get('message_url'), 'Napsat mi')))

    return ('Ukážeš, jak se to zhojilo?',
            wrap('<p>Ahoj,</p>'
                 + p('od tetování uplynul měsíc, takže už by mělo být zhojené. Pošleš mi '
                     'fotku? Zhojená práce vypadá jinak než čerstvá a nic lepšího nemůžu '
                     'ostatním ukázat.')
                 + button(ctx.get('photo_url'), 'Poslat fotku')
                 + (f'<p style="font-size:13px;line-height:1.7;color:#555;margin-top:22px">'
                    f'A kdyby ti zbyla chvilka, '
                    f'<a href="{_h(ctx.get("review_url") or "#")}" style="color:#1a1a1a">'
                    f'napiš pár vět do recenze</a>. Pomůže to dalším, kdo hledá tatéra.</p>'
                    if ctx.get('ask_review') else '')))


def _send_aftercare_first(conn, booking_id):
    """Instrukce k hojení odcházejí hned při dokončení, ne dalším cronem —
    klient je má mít, než odejde ze studia. Nikdy nesmí shodit dokončení
    rezervace: když mail selže, peníze i stav jsou pořád v pořádku."""
    try:
        b = conn.execute('''
            SELECT b.id, b.client_id, ua.display_name AS artist_name,
                   ua.username AS artist_username, ua.aftercare_text,
                   ua.premium_until, ua.aftercare_enabled
            FROM bookings b JOIN users ua ON ua.id = b.artist_id
            WHERE b.id = ?''', (booking_id,)).fetchone()
        if not b or not b['aftercare_enabled']:
            return False
        if not _is_premium_from_row({'premium_until': b['premium_until']}):
            return False
        if conn.execute('SELECT 1 FROM aftercare_sent WHERE booking_id=? AND step=?',
                        (booking_id, AFTERCARE_FIRST_STEP)).fetchone():
            return False
        conn.execute('INSERT INTO aftercare_sent (booking_id, step) VALUES (?,?)',
                     (booking_id, AFTERCARE_FIRST_STEP))
        conn.commit()
        subject, html = _aftercare_email(AFTERCARE_FIRST_STEP, {
            'artist_name': b['artist_name'] or b['artist_username'],
            'care_text':   b['aftercare_text'] or '',
            'stop_url':    f'{APP_BASE_URL}/aftercare/stop?b={b["id"]}'
                           f'&t={_aftercare_token(b["id"])}',
        })
        u = conn.execute('SELECT email FROM users WHERE id=?', (b['client_id'],)).fetchone()
        if u and u['email']:
            return send_email(u['email'], subject, html)
    except Exception as e:
        app.logger.error(f'[aftercare] first step failed for booking {booking_id}: {e}')
    return False


@app.route('/api/cron/aftercare', methods=['GET', 'POST'])
def cron_aftercare():
    """Denní cron. Idempotentní přes aftercare_sent — druhé volání
    v tentýž den nepošle nic navíc."""
    err = _check_cron_auth()
    if err: return err
    now  = _prague_now_naive()
    conn = get_db()
    sent, skipped = [], 0

    for step, days in AFTERCARE_STEPS:
        newest = (now - timedelta(days=days)).isoformat()
        oldest = (now - timedelta(days=days + AFTERCARE_WINDOW_DAYS)).isoformat()
        rows = conn.execute('''
            SELECT b.id, b.client_id, b.artist_id, b.completed_at,
                   ua.display_name AS artist_name, ua.username AS artist_username,
                   ua.aftercare_text, ua.premium_until, ua.aftercare_enabled,
                   uc.display_name AS client_name,
                   (SELECT COUNT(*) FROM reviews r WHERE r.booking_id = b.id) AS has_review
            FROM bookings b
            JOIN users ua ON ua.id = b.artist_id
            JOIN users uc ON uc.id = b.client_id
            WHERE b.status = 'completed'
              AND b.completed_at IS NOT NULL
              AND b.completed_at <= ? AND b.completed_at > ?
              AND b.aftercare_optout_at IS NULL
              AND COALESCE(ua.aftercare_enabled, 1) = 1
              AND NOT EXISTS (SELECT 1 FROM aftercare_sent s
                               WHERE s.booking_id = b.id AND s.step = ?)
        ''', (newest, oldest, step)).fetchall()

        for b in rows:
            # Sekvence je premium funkce tatéra, ne klienta.
            if not _is_premium_from_row({'premium_until': b['premium_until']}):
                skipped += 1
                continue
            token = _aftercare_token(b['id'])
            subject, html = _aftercare_email(step, {
                'client_name': b['client_name'],
                'artist_name': b['artist_name'] or b['artist_username'],
                'care_text':   b['aftercare_text'] or '',
                'stop_url':    f'{APP_BASE_URL}/aftercare/stop?b={b["id"]}&t={token}',
                'photo_url':   f'{APP_BASE_URL}/aftercare/photo?b={b["id"]}&t={token}',
                'message_url': f'{APP_BASE_URL}/messages?user={b["artist_username"]}',
                'review_url':  f'{APP_BASE_URL}/profile/{b["artist_username"]}#book-{b["id"]}',
                'ask_review':  not b['has_review'],
            })
            # Zapsat PŘED odesláním: při pádu mezi krokem a zápisem radši
            # neposlat podruhé než poslat dvakrát.
            try:
                conn.execute('INSERT INTO aftercare_sent (booking_id, step) VALUES (?,?)',
                             (b['id'], step))
                conn.commit()
            except Exception:
                continue
            u = conn.execute('SELECT email FROM users WHERE id=?', (b['client_id'],)).fetchone()
            if u and u['email'] and send_email(u['email'], subject, html):
                sent.append({'booking': b['id'], 'step': step})
    conn.close()
    return jsonify({'ok': True, 'sent': len(sent), 'skipped_not_premium': skipped,
                    'detail': sent})


@app.route('/aftercare/stop')
def aftercare_stop():
    try:
        bid = int(request.args.get('b') or 0)
    except (TypeError, ValueError):
        bid = 0
    import hmac as _hmac
    ok = bid and _hmac.compare_digest((request.args.get('t') or ''), _aftercare_token(bid))
    if ok:
        conn = get_db()
        conn.execute('UPDATE bookings SET aftercare_optout_at=? WHERE id=?',
                     (_prague_now_naive().isoformat(), bid))
        conn.commit(); conn.close()
    return _plain_page('Hotovo, další zprávy k tomuhle tetování už nepřijdou.'
                       if ok else 'Odkaz je neplatný.')


@app.route('/aftercare/photo', methods=['GET', 'POST'])
def aftercare_photo():
    """Fotka bez přihlášení. Klient si po měsíci nepamatuje heslo a
    přihlašovací obrazovka je přesně to místo, kde to vzdá."""
    try:
        bid = int(request.args.get('b') or request.form.get('b') or 0)
    except (TypeError, ValueError):
        bid = 0
    token = (request.args.get('t') or request.form.get('t') or '').strip()
    import hmac as _hmac
    if not bid or not _hmac.compare_digest(token, _aftercare_token(bid)):
        return _plain_page('Odkaz je neplatný.')

    conn = get_db()
    b = conn.execute('SELECT id, client_id, artist_id FROM bookings WHERE id=?', (bid,)).fetchone()
    if not b:
        conn.close(); return _plain_page('Rezervace nenalezena.')

    if request.method == 'POST':
        img = request.files.get('photo')
        if not img or not img.filename:
            conn.close(); return _plain_page('Nevybral(a) jsi fotku.')
        ext = img.filename.rsplit('.', 1)[-1].lower() if '.' in img.filename else ''
        if ext not in MESSAGE_IMAGE_EXTS:
            conn.close(); return _plain_page('Fotka musí být JPG, PNG, WEBP nebo GIF.')
        img.seek(0, os.SEEK_END); size = img.tell(); img.seek(0)
        if size > MESSAGE_IMAGE_MAX_BYTES:
            conn.close(); return _plain_page('Fotka je větší než 12 MB.')
        name = f'healed_{bid}_{int(time.time())}_{secure_filename(img.filename) or "photo." + ext}'
        save_upload(img, name)
        # Do vlákna, ne do tiché složky: tatér ji má vidět tam, kde spolu
        # mluví, a může na ni rovnou odpovědět.
        conn.execute('INSERT INTO messages (sender_id, receiver_id, content, content_type, image) '
                     'VALUES (?,?,?,?,?)', (b['client_id'], b['artist_id'], '', 'image', name))
        conn.execute('UPDATE tattoo_records SET healed_photo=? WHERE booking_id=?', (name, bid))
        push_notif(conn, b['artist_id'], b['client_id'], 'healed_photo', bid, 'booking',
                   'Klient poslal fotku zhojeného tetování.')
        conn.commit(); conn.close()
        return _plain_page('Díky! Fotka dorazila.')

    conn.close()
    return Response(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>InkLink</title>'
        '<div style="font-family:Helvetica,Arial,sans-serif;background:#faf8f3;color:#1a1a1a;'
        'min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px">'
        '<form method="post" enctype="multipart/form-data" style="max-width:380px;width:100%">'
        f'<input type="hidden" name="b" value="{bid}">'
        f'<input type="hidden" name="t" value="{token}">'
        '<p style="line-height:1.7;margin-bottom:18px">Pošli fotku zhojeného tetování.</p>'
        '<input type="file" name="photo" accept="image/*" required '
        'style="width:100%;padding:10px;background:#f1ece0;border:1px solid #a8a399">'
        '<button type="submit" style="margin-top:14px;width:100%;padding:13px;background:#0a0a0a;'
        'color:#faf8f3;border:none;letter-spacing:0.1em;text-transform:uppercase;font-size:12px;'
        'cursor:pointer">Odeslat</button></form></div>',
        mimetype='text/html')


def _plain_page(msg):
    from html import escape as _h
    return Response(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>InkLink</title>'
        '<div style="font-family:Helvetica,Arial,sans-serif;background:#faf8f3;color:#1a1a1a;'
        'min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px">'
        f'<div style="max-width:380px;text-align:center;line-height:1.7">{_h(msg)}</div></div>',
        mimetype='text/html')


@app.route('/api/me/aftercare', methods=['GET', 'PATCH'])
def my_aftercare():
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    if request.method == 'PATCH':
        data = request.get_json(silent=True) or {}
        if 'text' in data:
            conn.execute('UPDATE users SET aftercare_text=? WHERE id=?',
                         ((data.get('text') or '').strip()[:2000], uid))
        if 'enabled' in data:
            conn.execute('UPDATE users SET aftercare_enabled=? WHERE id=?',
                         (1 if data.get('enabled') else 0, uid))
        conn.commit()
    u = conn.execute('SELECT aftercare_text, aftercare_enabled, premium_until '
                     'FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()
    return jsonify({
        'text': u['aftercare_text'] or '',
        'enabled': bool(u['aftercare_enabled']),
        'premium': _is_premium_from_row({'premium_until': u['premium_until']}),
        'steps': [{'step': s, 'days': d} for s, d in AFTERCARE_STEPS],
    })


# ── Premium: rozesílání klientům ──────────────────────────────────────────
#
# Právní základ je oprávněný zájem podle §7 zákona o některých službách
# informační společnosti: obchodní sdělení vlastním zákazníkům o obdobné
# službě. Drží to jen při třech podmínkách, a všechny tři jsou vynucené
# kódem, ne dobrou vůlí odesílatele:
#
#   1. příjemce u toho tatéra opravdu byl (rezervace, ne jen poptávka),
#   2. v každém mailu je jednoklikové odhlášení bez přihlášení,
#   3. odhlášení platí okamžitě a napořád.
#
# Rozesílá se jen VLASTNÍM klientům, ne klientům kolegů ze studia —
# souhlas se váže na vztah s konkrétním tatérem, ne na adresu studia.

CAMPAIGN_MAX_RECIPIENTS = 500
CAMPAIGN_MIN_INTERVAL_MINUTES = 30


def _campaign_token(client_id):
    """Podpis přes SECRET_KEY: odkaz musí fungovat bez přihlášení, ale
    nesmí jít uhodnout ani odvodit pro cizí id."""
    import hashlib, hmac
    key = (app.secret_key if isinstance(app.secret_key, bytes)
           else str(app.secret_key).encode())
    return hmac.new(key, f'campaign:{client_id}'.encode(), hashlib.sha256).hexdigest()[:32]


def _campaign_recipients(conn, artist_id, tag=None):
    """Klienti, kterým se smí psát. Bez e-mailu, po odhlášení nebo po
    výmazu se nepíše — a bez proběhlé rezervace taky ne, ta je tím
    zákaznickým vztahem, o který se oprávněný zájem opírá."""
    sql = '''
        SELECT c.id, c.name, c.email, c.tags, c.user_id
        FROM clients c
        WHERE c.artist_id = ?
          AND c.anonymized_at IS NULL
          AND c.marketing_optout_at IS NULL
          AND COALESCE(NULLIF(c.email, ''), (SELECT email FROM users WHERE id = c.user_id)) <> ''
          AND EXISTS (SELECT 1 FROM bookings b
                       WHERE b.artist_id = c.artist_id
                         AND b.client_id = c.user_id
                         AND b.status IN ('confirmed','completed'))
    '''
    params = [artist_id]
    if tag:
        sql += ' AND c.tags LIKE ?'
        params.append(f'%{tag}%')
    rows = conn.execute(sql, tuple(params)).fetchall()
    out = []
    for r in rows:
        email = (r['email'] or '').strip()
        if not email and r['user_id']:
            u = conn.execute('SELECT email FROM users WHERE id=?', (r['user_id'],)).fetchone()
            email = (u['email'] or '').strip() if u else ''
        if email:
            out.append({'client_id': r['id'], 'name': r['name'] or '', 'email': email})
    return out


@app.route('/api/me/campaigns/recipients')
def campaign_recipients():
    err = require_premium()
    if err: return err
    conn = get_db()
    rows = _campaign_recipients(conn, session['user_id'],
                               (request.args.get('tag') or '').strip() or None)
    conn.close()
    # Adresy nevracíme — tatér je zná, ale výpis by z toho udělal
    # exportovatelný seznam a to není potřeba k ničemu.
    return jsonify({'count': len(rows),
                    'names': [r['name'] for r in rows if r['name']][:20],
                    'max': CAMPAIGN_MAX_RECIPIENTS})


@app.route('/api/me/campaigns', methods=['POST'])
@limiter.limit('6 per hour')
def send_campaign():
    err = require_premium()
    if err: return err
    uid  = session['user_id']
    data = request.get_json(silent=True) or {}
    subject = (data.get('subject') or '').strip()[:150]
    body    = (data.get('body') or '').strip()[:4000]
    tag     = (data.get('tag') or '').strip() or None
    if len(subject) < 3:
        return jsonify({'error': 'Doplň předmět.'}), 400
    if len(body) < 20:
        return jsonify({'error': 'Napiš aspoň pár vět.'}), 400
    if not RESEND_API_KEY:
        return jsonify({'error': 'Odesílání e-mailů není nastavené.'}), 503

    conn = get_db()
    # Odstup mezi rozesílkami: bez něj by jeden překlep znamenal pět
    # stejných mailů a odhlášení celé klientely.
    last = conn.execute('SELECT created_at FROM campaigns WHERE artist_id=? '
                        'ORDER BY id DESC LIMIT 1', (uid,)).fetchone()
    if last:
        try:
            if _naive_dt(last['created_at']) > _prague_now_naive() - timedelta(
                    minutes=CAMPAIGN_MIN_INTERVAL_MINUTES):
                conn.close()
                return jsonify({'error': f'Další rozesílku můžeš poslat za '
                                         f'{CAMPAIGN_MIN_INTERVAL_MINUTES} minut.'}), 429
        except (ValueError, TypeError):
            pass

    recipients = _campaign_recipients(conn, uid, tag)
    if not recipients:
        conn.close()
        return jsonify({'error': 'Nikdo, komu by se dalo napsat.'}), 400
    if len(recipients) > CAMPAIGN_MAX_RECIPIENTS:
        conn.close()
        return jsonify({'error': f'Nejvýš {CAMPAIGN_MAX_RECIPIENTS} příjemců.'}), 400

    artist = conn.execute('SELECT username, display_name FROM users WHERE id=?',
                          (uid,)).fetchone()
    who = artist['display_name'] or artist['username']
    conn.execute('INSERT INTO campaigns (artist_id, subject, body, tag, recipients) '
                 'VALUES (?,?,?,?,?)', (uid, subject, body, tag or '', len(recipients)))
    conn.commit()
    conn.close()

    sent = failed = 0
    for r in recipients:
        html = _campaign_email_html(who, artist['username'], subject, body, r)
        if send_email(r['email'], f'{who}: {subject}', html):
            sent += 1
        else:
            failed += 1
    return jsonify({'ok': True, 'sent': sent, 'failed': failed})


def _campaign_email_html(who, username, subject, body, recipient):
    from html import escape as _h
    unsub = (f'{APP_BASE_URL}/unsubscribe?c={recipient["client_id"]}'
             f'&t={_campaign_token(recipient["client_id"])}')
    # Bez jména: české vokativy se automaticky skloňovat nedají a
    # "Ahoj Tereza" zní hůř než prosté "Ahoj".
    greeting = 'Ahoj,'
    return (
        '<div style="background:#faf8f3;color:#1a1a1a;font-family:Helvetica,Arial,sans-serif;'
        'padding:40px;max-width:520px;margin:0 auto">'
        f'<div style="font-size:22px;letter-spacing:0.12em;margin-bottom:24px">{_h(who)}</div>'
        f'<p>{greeting}</p>'
        f'<div style="font-size:14px;line-height:1.7;white-space:pre-wrap">{_h(body)}</div>'
        f'<p style="margin-top:28px"><a href="{_h(APP_BASE_URL)}/profile/{_h(username)}" '
        'style="display:inline-block;background:#0a0a0a;color:#faf8f3;padding:12px 22px;'
        'text-decoration:none;font-size:13px;letter-spacing:0.1em">REZERVOVAT TERMÍN</a></p>'
        '<p style="color:#8a8a8a;font-size:11px;line-height:1.7;margin-top:36px;'
        'border-top:1px solid #ddd6c8;padding-top:16px">'
        'Posílá ti ho tatér, u kterého máš tetování.<br>'
        f'<a href="{_h(unsub)}" style="color:#8a8a8a">Nechci už dostávat nabídky</a> — '
        'odhlášení platí okamžitě.</p></div>'
    )


@app.route('/unsubscribe')
def unsubscribe_page():
    """Bez přihlášení a na jeden klik. Odhlašovací odkaz, který po někom
    chce heslo, není odhlašovací odkaz."""
    try:
        cid = int(request.args.get('c') or 0)
    except (TypeError, ValueError):
        cid = 0
    token = (request.args.get('t') or '').strip()
    import hmac as _hmac
    ok = cid and token and _hmac.compare_digest(token, _campaign_token(cid))
    if ok:
        conn = get_db()
        conn.execute('UPDATE clients SET marketing_optout_at=? WHERE id=?',
                     (_prague_now_naive().isoformat(), cid))
        conn.commit(); conn.close()
    msg = ('Odhlášeno. Už ti nebudeme posílat nabídky.' if ok
           else 'Odkaz je neplatný nebo prošlý.')
    return Response(
        '<!doctype html><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>InkLink</title>'
        '<div style="font-family:Helvetica,Arial,sans-serif;background:#faf8f3;color:#1a1a1a;'
        'min-height:100vh;display:flex;align-items:center;justify-content:center;padding:32px">'
        f'<div style="max-width:380px;text-align:center;line-height:1.7">{msg}</div></div>',
        mimetype='text/html')


# ── Premium: statistiky ───────────────────────────────────────────────────

@app.route('/api/me/stats')
def premium_stats():
    """Čísla, na která se tatér dívá jednou za měsíc. Nic z toho nemění
    denní práci — proto to smí být placené."""
    err = require_premium()
    if err: return err
    uid  = session['user_id']
    conn = get_db()
    now  = _prague_now_naive()
    since = (now - timedelta(days=180)).isoformat()

    rows = conn.execute('''
        SELECT b.status, b.booking_start_at, b.duration_hours, b.portfolio_item_id,
               b.total_price_cents, b.deposit_cents, b.platform_fee_cents,
               b.balance_paid_cents, b.onsite_amount_cents, b.refund_cents
        FROM bookings b
        WHERE b.artist_id = ? AND COALESCE(b.booking_start_at, b.created_at) >= ?
    ''', (uid, since)).fetchall()

    kc = lambda c: (c or 0) / 100
    by_month, weekday, hour = {}, [0] * 7, {}
    done = unpaid = cancelled = 0
    for b in rows:
        st = b['status']
        if st in ('cancelled_client', 'cancelled_artist'):
            cancelled += 1
        if st == 'pending_payment':
            unpaid += 1
        if st in ('confirmed', 'completed'):
            done += 1
        try:
            d = _naive_dt(b['booking_start_at'])
        except (ValueError, TypeError):
            continue
        m = d.strftime('%Y-%m')
        e = by_month.setdefault(m, {'month': m, 'bookings': 0, 'revenue': 0.0, 'hours': 0.0})
        e['bookings'] += 1
        e['revenue']  += kc(b['deposit_cents']) + kc(b['balance_paid_cents']) \
                         + kc(b['onsite_amount_cents']) - kc(b['refund_cents']) \
                         - kc(b['platform_fee_cents'])
        e['hours']    += float(b['duration_hours'] or 0)
        if st in ('confirmed', 'completed'):
            weekday[d.weekday()] += 1
            hour[d.hour] = hour.get(d.hour, 0) + 1

    # Které skici se reálně rezervují a které jen leží. Tohle je jediné
    # číslo, podle kterého se dá rozhodnout, co kreslit dál.
    items = conn.execute('''
        SELECT p.id, p.caption, p.kind, p.created_at,
               (SELECT COUNT(*) FROM bookings bb
                 WHERE bb.portfolio_item_id = p.id
                   AND bb.status IN ('confirmed','completed')) AS booked,
               p.like_count
        FROM portfolio_items p
        WHERE p.user_id = ? AND p.kind = 'sketch'
        ORDER BY booked DESC, p.like_count DESC
    ''', (uid,)).fetchall()
    conn.close()

    total = len(rows)
    return jsonify({
        'period_days': 180,
        'totals': {
            'bookings': total,
            'done': done,
            'unpaid_deposit': unpaid,
            'cancelled': cancelled,
            # Podíl zrušených dává smysl jen proti počtu, ne absolutně.
            'cancelled_pct': round(cancelled * 100.0 / total, 1) if total else 0.0,
            'revenue': round(sum(m['revenue'] for m in by_month.values()), 2),
            'hours': round(sum(m['hours'] for m in by_month.values()), 1),
        },
        'by_month': sorted(by_month.values(), key=lambda m: m['month']),
        'weekday': weekday,
        'by_hour': [{'hour': h, 'count': c} for h, c in sorted(hour.items())],
        'sketches': [{
            'id': i['id'], 'caption': i['caption'] or '', 'booked': i['booked'],
            'likes': i['like_count'] or 0,
            'age_days': max(0, (now - _naive_dt(i['created_at'])).days)
                        if i['created_at'] else None,
        } for i in items],
    })


# ── Premium: účetní export ────────────────────────────────────────────────

ACCOUNTING_COLUMNS = [
    ('date',        'Datum'),
    ('booking_id',  'Č. rezervace'),
    ('client',      'Klient'),
    ('description', 'Popis'),
    ('hours',       'Hodin'),
    ('status',      'Stav'),
    ('total',       'Cena celkem'),
    ('deposit',     'Záloha přes InkLink'),
    ('balance_inklink', 'Doplatek přes InkLink'),
    ('onsite',      'Zaplaceno na místě'),
    ('refunded',    'Vráceno'),
    ('commission',  'Provize InkLink'),
    ('outstanding', 'Zbývá doplatit'),
    ('net',         'Čistý příjem'),
]


def _accounting_rows(conn, artist_id, date_from, date_to):
    """Řádky pro účetní. Klíč je datum sezení, ne datum platby — účetní
    potřebuje vědět, kdy byla služba poskytnuta.

    Zrušené rezervace se nevynechávají: když u nich zůstala nevrácená
    záloha, je to zdanitelný příjem a v přiznání chybět nesmí."""
    rows = conn.execute('''
        SELECT b.*, uc.display_name AS c_name, uc.username AS c_username
        FROM bookings b
        JOIN users uc ON uc.id = b.client_id
        WHERE b.artist_id = ?
          AND COALESCE(b.booking_start_at, b.created_at) >= ?
          AND COALESCE(b.booking_start_at, b.created_at) <= ?
        ORDER BY COALESCE(b.booking_start_at, b.created_at) ASC
    ''', (artist_id, date_from, date_to + 'T23:59:59')).fetchall()

    kc = lambda cents: round((cents or 0) / 100, 2)
    out = []
    for b in rows:
        deposit  = kc(b['deposit_cents'])
        balance  = kc(b['balance_paid_cents'])
        onsite   = kc(b['onsite_amount_cents'])
        refund   = kc(b['refund_cents'])
        fee      = kc(b['platform_fee_cents']) + kc(b['balance_charge_fee_cents'])
        # Čistý příjem = co tatérovi reálně zůstalo. Provize se strhává jen
        # z toho, co prošlo platformou; hotovost na místě je celá jeho.
        net = round(deposit + balance + onsite - refund - fee, 2)
        # U dokončených vyjde vždycky nula — dokončit jinak nejde. U těch,
        # co teprve proběhnou, je to očekávaný zbytek, ne díra v účetnictví.
        outstanding = max(0.0, round(kc(b['total_price_cents']) - deposit - balance - onsite, 2))
        out.append({
            'date':        (b['booking_start_at'] or b['created_at'] or '')[:10],
            'booking_id':  b['id'],
            'client':      b['c_name'] or b['c_username'],
            'description': (b['design_note'] or '').replace('\n', ' ').strip()[:200],
            'hours':       b['duration_hours'] or '',
            'status':      b['status'],
            'total':       kc(b['total_price_cents']),
            'deposit':     deposit,
            'balance_inklink': balance,
            'onsite':      onsite,
            'refunded':    refund,
            'commission':  fee,
            'outstanding': outstanding,
            'net':         net,
        })
    return out


@app.route('/api/me/accounting/export')
def accounting_export():
    err = require_premium()
    if err: return err
    uid = session['user_id']

    today = _prague_now_naive().date()
    date_from = (request.args.get('from') or today.replace(day=1).isoformat())[:10]
    date_to   = (request.args.get('to') or today.isoformat())[:10]
    try:
        datetime.fromisoformat(date_from); datetime.fromisoformat(date_to)
    except ValueError:
        return jsonify({'error': 'Špatný formát data (YYYY-MM-DD).'}), 400
    if date_from > date_to:
        return jsonify({'error': 'Začátek období je po jeho konci.'}), 400

    conn = get_db()
    rows = _accounting_rows(conn, uid, date_from, date_to)
    u = conn.execute('SELECT username, display_name FROM users WHERE id=?', (uid,)).fetchone()
    conn.close()

    if (request.args.get('format') or 'csv').lower() == 'json':
        return jsonify({'from': date_from, 'to': date_to, 'rows': rows,
                        'columns': [{'key': k, 'label': l} for k, l in ACCOUNTING_COLUMNS]})

    import csv, io
    buf = io.StringIO()
    # Středník a BOM: český Excel jinak rozhodí sloupce i diakritiku.
    w = csv.writer(buf, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    w.writerow([label for _, label in ACCOUNTING_COLUMNS])
    for r in rows:
        w.writerow([str(r[k]).replace('.', ',') if isinstance(r[k], float) else r[k]
                    for k, _ in ACCOUNTING_COLUMNS])
    # Součtový řádek — účetní ho stejně udělá, ať se nemusí trefovat.
    total = lambda k: round(sum(r[k] for r in rows), 2)
    w.writerow([])
    w.writerow(['CELKEM', '', '', '', '', '',
                str(total('total')).replace('.', ','),
                str(total('deposit')).replace('.', ','),
                str(total('balance_inklink')).replace('.', ','),
                str(total('onsite')).replace('.', ','),
                str(total('refunded')).replace('.', ','),
                str(total('commission')).replace('.', ','),
                str(total('outstanding')).replace('.', ','),
                str(total('net')).replace('.', ',')])

    safe = (u['username'] or 'tater').replace('/', '_')
    name = f'inklink-ucetnictvi-{safe}-{date_from}_{date_to}.csv'
    return Response('﻿' + buf.getvalue(),
                    mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{name}"'})


# ── Premium: předplatné ───────────────────────────────────────────────────
#
# Stripe Billing, ne Connect. Connect posílá peníze OD klienta TATÉROVI;
# tohle je platba OD tatéra NÁM. Jsou to dva různé produkty a míchat je
# do jednoho toku by znamenalo, že provize a předplatné sdílí osud.

PREMIUM_PRICE_ID = os.environ.get('STRIPE_PREMIUM_PRICE_ID', '').strip()


@app.route('/api/premium/status')
def premium_status():
    err = require_login()
    if err: return err
    conn = get_db()
    u = conn.execute('SELECT premium_until, premium_subscription_id, '
                     'premium_cancel_at_period_end FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    conn.close()
    return jsonify({
        'active':          _is_premium_from_row(dict(u)),
        'until':           u['premium_until'],
        'cancel_at_end':   bool(u['premium_cancel_at_period_end']),
        'has_subscription': bool(u['premium_subscription_id']),
        'price_czk':       PREMIUM_PRICE_CZK,
        'features':        list(PREMIUM_FEATURES),
        # Bez ceníku ve Stripe se nedá předplatit; frontend pak nabídne
        # kontakt místo tlačítka, které by stejně spadlo.
        'available':       bool(STRIPE_SECRET_KEY and PREMIUM_PRICE_ID),
    })


@app.route('/api/premium/checkout', methods=['POST'])
def premium_checkout():
    err = require_login()
    if err: return err
    if not STRIPE_SECRET_KEY or not PREMIUM_PRICE_ID:
        return jsonify({'error': 'Předplatné zatím není spuštěné.'}), 503
    uid  = session['user_id']
    conn = get_db()
    u = conn.execute('SELECT username, email, display_name, premium_customer_id '
                     'FROM users WHERE id=?', (uid,)).fetchone()
    if _is_premium(conn, uid):
        conn.close()
        return jsonify({'error': 'Premium už máš aktivní.'}), 409

    customer_id = u['premium_customer_id']
    try:
        if not customer_id:
            cust = stripe.Customer.create(
                email=u['email'] or None,
                name=u['display_name'] or u['username'],
                metadata={'inklink_user_id': str(uid), 'username': u['username']},
            )
            customer_id = cust.id
            conn.execute('UPDATE users SET premium_customer_id=? WHERE id=?', (customer_id, uid))
            conn.commit()

        sess = stripe.checkout.Session.create(
            mode='subscription',
            customer=customer_id,
            line_items=[{'price': PREMIUM_PRICE_ID, 'quantity': 1}],
            # Uživatele hledáme podle metadat, ne podle e-mailu — ten si
            # může kdykoliv změnit a přiřazení by se rozpadlo.
            subscription_data={'metadata': {'inklink_user_id': str(uid)}},
            metadata={'inklink_user_id': str(uid)},
            success_url=f'{APP_BASE_URL}/premium?paid=1',
            cancel_url=f'{APP_BASE_URL}/premium',
            locale='cs',
        )
    except Exception as e:
        conn.close()
        app.logger.error(f'[premium] checkout failed for user {uid}: {e}')
        return jsonify({'error': 'Platbu se nepovedlo založit.'}), 502
    conn.close()
    return jsonify({'url': sess.url})


@app.route('/api/premium/portal', methods=['POST'])
def premium_portal():
    """Správu i zrušení předplatného řeší Stripe. Vlastní zrušovací
    formulář by znamenal držet stav na dvou místech."""
    err = require_login()
    if err: return err
    conn = get_db()
    u = conn.execute('SELECT premium_customer_id FROM users WHERE id=?',
                     (session['user_id'],)).fetchone()
    conn.close()
    if not u or not u['premium_customer_id']:
        return jsonify({'error': 'Nemáš žádné předplatné.'}), 404
    try:
        portal = stripe.billing_portal.Session.create(
            customer=u['premium_customer_id'],
            return_url=f'{APP_BASE_URL}/premium',
        )
    except Exception as e:
        app.logger.error(f'[premium] portal failed: {e}')
        return jsonify({'error': 'Správu předplatného se nepovedlo otevřít.'}), 502
    return jsonify({'url': portal.url})


def _premium_user_from_subscription(conn, sub):
    """Najdi tatéra podle metadat, jinak podle customer id."""
    meta = (sub.get('metadata') or {}) if isinstance(sub, dict) else (sub.metadata or {})
    uid = meta.get('inklink_user_id')
    if uid:
        try:
            return int(uid)
        except (TypeError, ValueError):
            pass
    cust = sub.get('customer') if isinstance(sub, dict) else sub.customer
    if cust:
        row = conn.execute('SELECT id FROM users WHERE premium_customer_id=?', (cust,)).fetchone()
        if row:
            return row['id']
    return None


def _apply_premium_subscription(conn, sub):
    """Zdrojem pravdy je Stripe. Ukládáme si jen datum, do kdy je
    zaplaceno — kdyby webhook vypadl, premium samo doběhne a nezůstane
    zapnuté napořád."""
    uid = _premium_user_from_subscription(conn, sub)
    if not uid:
        return None
    g = (lambda k: sub.get(k)) if isinstance(sub, dict) else (lambda k: getattr(sub, k, None))
    status = g('status')
    period_end = g('current_period_end')
    cancel_at_end = 1 if g('cancel_at_period_end') else 0
    sub_id = g('id')

    if status in ('active', 'trialing', 'past_due') and period_end:
        until = datetime.utcfromtimestamp(int(period_end)).isoformat()
        conn.execute('UPDATE users SET premium_until=?, premium_subscription_id=?, '
                     'premium_cancel_at_period_end=? WHERE id=?',
                     (until, sub_id, cancel_at_end, uid))
    elif status in ('canceled', 'unpaid', 'incomplete_expired'):
        # Datum nezkracujeme: za období, které má zaplacené, ho dostat má.
        conn.execute('UPDATE users SET premium_subscription_id=NULL, '
                     'premium_cancel_at_period_end=0 WHERE id=?', (uid,))
    conn.commit()
    return uid


@app.route('/api/stripe/webhook', methods=['POST'])
def stripe_webhook():
    """Webhook handler. Verifikuje podpis proti RAW request body bytes
    (parsovaný JSON by signature rozbil). Idempotency přes
    processed_stripe_events: insert event_id before processing — pokud
    UNIQUE constraint fails, return 200 OK a skip (Stripe retries
    aggressively a duplicate by mohl ztrojit commission)."""
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
    event_id = event['id'] if isinstance(event, dict) else event.id

    # Idempotency: insert event_id PRE processing. Pokud už existuje,
    # vrátíme 200 OK a pas. To zabraňuje double-charge pri Stripe retry.
    conn_idem = get_db()
    try:
        conn_idem.execute(
            'INSERT INTO processed_stripe_events (event_id, event_type) VALUES (?, ?)',
            (event_id, etype)
        )
        conn_idem.commit()
    except Exception:
        # Duplicate event_id = už zpracováno. Return 200 a skip.
        conn_idem.close()
        return '', 200
    conn_idem.close()

    if etype in ('customer.subscription.created', 'customer.subscription.updated',
                 'customer.subscription.deleted'):
        conn_sub = get_db()
        try:
            uid = _apply_premium_subscription(conn_sub, obj)
            app.logger.info(f'[premium] {etype} → user {uid}')
        except Exception as e:
            app.logger.error(f'[premium] {etype} failed: {e}')
        finally:
            conn_sub.close()
        return '', 200

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
        # Země Stripe účtu určuje, v čem tatérovi přijdou peníze — proti
        # tomu nemá smysl držet vlastní odhad z města.
        country = (obj.get('country') if isinstance(obj, dict) else getattr(obj, 'country', None))
        if country:
            row = conn.execute('SELECT id FROM users WHERE stripe_account_id=?',
                               (acct_id,)).fetchone()
            if row:
                _sync_currency(conn, row['id'], country)
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

    elif etype == 'payment_intent.succeeded':
        # Payment cleared → booking confirm + re-snapshot economics s actual
        # card type (viz _reconcile_card_country). Funds už tečou tatérovi
        # přes destination charges (current architecture), takže žádný
        # manuální transfer.
        pi_id = obj.get('id') if isinstance(obj, dict) else obj.id
        meta = obj.get('metadata') if isinstance(obj, dict) else (obj.metadata or {})
        booking_id = None
        try:
            booking_id = int(meta.get('inklink_booking_id') or 0) or None
        except Exception:
            booking_id = None
        if not booking_id:
            # Fallback: najít booking podle PI ID
            conn = get_db()
            row = conn.execute(
                'SELECT id FROM bookings WHERE stripe_payment_intent_id = ? OR balance_payment_intent_id = ?',
                (pi_id, pi_id)
            ).fetchone()
            if row:
                booking_id = row['id']
            conn.close()
        if booking_id:
            conn = get_db()
            transition_booking(conn, booking_id, 'confirmed', extra_set_sql='confirmed_at=?',
                                extra_params=(datetime.utcnow().isoformat(),))
            conn.commit()
            _reconcile_card_country(conn, booking_id, obj)
            try:
                from pricing import emit_event as _emit_pi
                _emit_pi('booking.payment_succeeded', {
                    'booking_id': booking_id, 'payment_intent_id': pi_id,
                }, conn=conn)
                conn.commit()
            except Exception:
                pass
            conn.close()

    elif etype == 'payment_intent.payment_failed':
        pi_id = obj.get('id') if isinstance(obj, dict) else obj.id
        conn = get_db()
        row = conn.execute(
            'SELECT id FROM bookings WHERE stripe_payment_intent_id = ? OR balance_payment_intent_id = ?',
            (pi_id, pi_id)
        ).fetchone()
        if row:
            transition_booking(conn, row['id'], 'payment_failed')
            conn.commit()
            try:
                from pricing import emit_event as _emit_pf
                _emit_pf('booking.payment_failed', {
                    'booking_id': row['id'], 'payment_intent_id': pi_id,
                }, conn=conn)
                conn.commit()
            except Exception:
                pass
        conn.close()

    elif etype == 'charge.refunded':
        # Stripe-side refund (initiated z dashboardu nebo přes API). Ukládáme
        # ekonomickou stopu jako new snapshot kind='refund' — net pro nás
        # je loss Stripe fee (Stripe fee se nevrací).
        ch_id = obj.get('id') if isinstance(obj, dict) else obj.id
        pi_id = obj.get('payment_intent') if isinstance(obj, dict) else obj.payment_intent
        amount_refunded = obj.get('amount_refunded') if isinstance(obj, dict) else obj.amount_refunded
        conn = get_db()
        row = conn.execute(
            'SELECT id, total_price_cents FROM bookings WHERE stripe_payment_intent_id = ? OR balance_payment_intent_id = ?',
            (pi_id, pi_id)
        ).fetchone()
        if row:
            try:
                import json as _json_r
                # refund_loss = Stripe fee na původní platbě (nevrací se).
                # Zatím použijeme heuristiku ~1.5% * amount_refunded + 6 CZK.
                stripe_fee_loss_haler = int(amount_refunded * 0.015 + 600)
                snapshot = {
                    'refund_amount_haler': int(amount_refunded or 0),
                    'refund_amount_czk': float(int(amount_refunded or 0)) / 100,
                    'refund_loss_czk': float(stripe_fee_loss_haler) / 100,
                    'charge_id': ch_id,
                    'payment_intent_id': pi_id,
                }
                conn.execute(
                    "INSERT INTO economics_snapshots (booking_id, kind, snapshot) VALUES (?, 'refund', ?)",
                    (row['id'], _json_r.dumps(snapshot))
                )
                # transition_booking's table excludes cancelled_client/cancelled_artist
                # and 'refunded'/'disputed' as sources — protects against an
                # out-of-order webhook stomping a more specific/serious status.
                transition_booking(conn, row['id'], 'refunded', extra_set_sql='refund_cents=?',
                                    extra_params=(int(amount_refunded or 0),))
                conn.commit()
                from pricing import emit_event as _emit_rf
                _emit_rf('booking.refunded', {
                    'booking_id': row['id'], 'snapshot': snapshot,
                }, conn=conn)
                conn.commit()
            except Exception as e:
                print(f'[webhook refunded] {e}')
        conn.close()

    elif etype == 'charge.dispute.created':
        # Chargeback opened. Freeze the artist's payouts (so InkLink isn't
        # still paying them out while the dispute is live), mark booking
        # disputed, alert admin. Neautomaticky neodpovídáme na spor samotný —
        # admin to řeší z Stripe dashboardu; jen zastavíme peníze proudící ven.
        pi_id = obj.get('payment_intent') if isinstance(obj, dict) else obj.payment_intent
        dispute_reason = obj.get('reason') if isinstance(obj, dict) else obj.reason
        conn = get_db()
        row = conn.execute(
            'SELECT id, artist_id FROM bookings WHERE stripe_payment_intent_id = ?', (pi_id,)
        ).fetchone()
        if row:
            # Guard: neposílat duplicitní admin alert/telemetry/freeze, pokud
            # se stejný dispute event nějak zpracuje podruhé (dedup na
            # event_id je primární obrana, tohle je defense-in-depth).
            moved = transition_booking(conn, row['id'], 'disputed')
            conn.commit()
            if not moved:
                conn.close()
                return '', 200
            artist = conn.execute(
                'SELECT stripe_account_id FROM users WHERE id=?', (row['artist_id'],)
            ).fetchone()
            if artist and artist['stripe_account_id']:
                try:
                    stripe.Account.modify(
                        artist['stripe_account_id'],
                        settings={'payouts': {'schedule': {'interval': 'manual'}}},
                    )
                except Exception as e:
                    print(f'[dispute] payout freeze failed for {artist["stripe_account_id"]}: {e}')
            try:
                from pricing import emit_event as _emit_dp
                _emit_dp('booking.disputed', {
                    'booking_id': row['id'], 'reason': dispute_reason,
                    'payment_intent_id': pi_id,
                }, conn=conn)
                conn.commit()
            except Exception:
                pass
        conn.close()

    elif etype == 'charge.dispute.closed':
        # Dispute resolved (won/lost/warning_closed). Resume the artist's
        # normal payout schedule and move the booking off 'disputed' — 'won'
        # means InkLink/artist keep the funds (closest existing status:
        # completed); anything else means the funds are gone via chargeback
        # (closest existing status: refunded, same practical effect for the
        # client as a voluntary refund).
        pi_id = obj.get('payment_intent') if isinstance(obj, dict) else obj.payment_intent
        dispute_status = obj.get('status') if isinstance(obj, dict) else obj.status
        conn = get_db()
        row = conn.execute(
            'SELECT id, artist_id FROM bookings WHERE stripe_payment_intent_id = ?', (pi_id,)
        ).fetchone()
        if row:
            to_state = 'completed' if dispute_status == 'won' else 'refunded'
            moved = transition_booking(conn, row['id'], to_state)
            conn.commit()
            artist = conn.execute(
                'SELECT stripe_account_id FROM users WHERE id=?', (row['artist_id'],)
            ).fetchone()
            if artist and artist['stripe_account_id']:
                try:
                    stripe.Account.modify(
                        artist['stripe_account_id'],
                        settings={'payouts': {'schedule': {'interval': 'daily'}}},
                    )
                except Exception as e:
                    print(f'[dispute] payout resume failed for {artist["stripe_account_id"]}: {e}')
            if moved:
                try:
                    from pricing import emit_event as _emit_dc
                    _emit_dc('booking.dispute_closed', {
                        'booking_id': row['id'], 'dispute_status': dispute_status,
                        'payment_intent_id': pi_id,
                    }, conn=conn)
                    conn.commit()
                except Exception:
                    pass
        conn.close()

    return '', 200

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


# ── CRM: viditelnost klientů ─────────────────────────────────────────────────
# Dva helpery schválně, ne jeden. _crm_visible_artist_ids se splice-uje do
# IN (...) a použije ho JEDINÝ dotaz (seznam klientů); všechno ostatní jde přes
# _crm_get_client. Kdyby se dynamické IN (...) skládalo na deseti místech, je
# to deset nezávislých příležitostí zřetězit tam syrový int.
#
# INVARIANT: každý potomek (poznámka, záznam, zdravotní poznámka) se autorizuje
# PŘES SVÉHO KLIENTA, nikdy podle vlastního id. Tam CRM reálně teče — ne
# v seznamu, který si každý pamatuje otestovat.

def _crm_visible_artist_ids(conn, user_id):
    """Id tatérů, jejichž klienty smí `user_id` vidět: vždy on sám, plus
    kolegové ze stejného studia (pokud v nějakém je)."""
    ids = {user_id}
    m = conn.execute('SELECT studio_id FROM studio_members WHERE artist_id=?',
                     (user_id,)).fetchone()
    if m and m['studio_id']:
        rows = conn.execute('SELECT artist_id FROM studio_members WHERE studio_id=?',
                            (m['studio_id'],)).fetchall()
        ids.update(r['artist_id'] for r in rows)
    return sorted(ids)


def _crm_get_client(conn, user_id, client_id):
    """Klient, pokud na něj `user_id` vidí, jinak None (volající vrací 404 —
    403 by prozradilo, že takové id existuje)."""
    row = conn.execute('SELECT * FROM clients WHERE id=?', (client_id,)).fetchone()
    if not row:
        return None
    if row['artist_id'] not in _crm_visible_artist_ids(conn, user_id):
        return None
    return row


def _crm_client_dict(conn, row):
    """Klient pro API. Když je navázaný na účet, je zdrojem pravdy `users` —
    jinak by tatér volal telefon, který si klient před půl rokem změnil."""
    d = dict(row)
    contact_source = 'manual'
    if row['user_id']:
        u = conn.execute(
            'SELECT display_name, email, phone, username, avatar FROM users WHERE id=?',
            (row['user_id'],)).fetchone()
        if u:
            contact_source = 'user'
            d['name'] = u['display_name'] or u['username'] or d.get('name') or ''
            d['email'] = u['email'] or ''
            d['phone'] = u['phone'] or ''
            d['username'] = u['username']
            d['avatar_url'] = f'/uploads/{u["avatar"]}' if u['avatar'] else None
    d['contact_source'] = contact_source
    return d


def _crm_link_client_on_booking(conn, artist_id, client_user_id, created_by=None):
    """Založí (nebo najde) klientský řádek při vytvoření rezervace.
    Částečný unikátní index (artist_id, user_id) ošetří souběh — druhý INSERT
    spadne a my si řádek prostě znovu přečteme."""
    row = conn.execute('SELECT id FROM clients WHERE artist_id=? AND user_id=?',
                       (artist_id, client_user_id)).fetchone()
    if row:
        return row['id']
    try:
        conn.execute(
            '''INSERT INTO clients (artist_id, user_id, acquisition_source, created_by)
               VALUES (?,?,'inklink',?)''',
            (artist_id, client_user_id, created_by or artist_id))
        conn.commit()
    except Exception:
        conn.rollback()
    row = conn.execute('SELECT id FROM clients WHERE artist_id=? AND user_id=?',
                       (artist_id, client_user_id)).fetchone()
    return row['id'] if row else None


# ── CRM: endpointy ───────────────────────────────────────────────────────────
# Routujeme přes AKTÉRA (/api/clients), ne přes tenanta
# (/api/studios/<id>/clients): nikdo nemůže podstrčit cizí studio_id, takže
# celá třída IDOR chyb zmizí a sólo tatér funguje stejně jako členové studia.
# CRM se schválně negatuje přes require_tier — ten 403uje každého bez studia.

@app.route('/api/clients')
def list_clients():
    err = require_login()
    if err: return err
    uid = session['user_id']
    q    = (request.args.get('q') or '').strip()
    tag  = (request.args.get('tag') or '').strip()
    sort = (request.args.get('sort') or 'recent').strip()
    try:
        limit = min(100, max(1, int(request.args.get('limit', 50))))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = max(0, int(request.args.get('offset', 0)))
    except (ValueError, TypeError):
        offset = 0

    conn = get_db()
    visible = _crm_visible_artist_ids(conn, uid)
    placeholders = ','.join('?' for _ in visible)
    where = [f'c.artist_id IN ({placeholders})']
    params = list(visible)

    if q:
        # Hledá i v navázaném účtu, ne jen v ručně zadaných polích.
        where.append('''(LOWER(c.name) LIKE LOWER(?) OR LOWER(c.email) LIKE LOWER(?)
                         OR c.phone LIKE ?
                         OR LOWER(COALESCE(u.display_name,'')) LIKE LOWER(?)
                         OR LOWER(COALESCE(u.email,'')) LIKE LOWER(?))''')
        params += [f'%{q}%'] * 5
    if tag:
        where.append('LOWER(c.tags) LIKE LOWER(?)')
        params.append(f'%{tag}%')

    where_sql = ' AND '.join(where)
    # ORDER BY z whitelistu, nikdy z uživatelského vstupu.
    order = {'recent': 'c.updated_at DESC',
             'created': 'c.created_at DESC',
             'name': "COALESCE(NULLIF(u.display_name,''), c.name) COLLATE NOCASE"}.get(sort, 'c.updated_at DESC')

    total = conn.execute(
        f'SELECT COUNT(*) AS n FROM clients c LEFT JOIN users u ON c.user_id = u.id WHERE {where_sql}',
        tuple(params)).fetchone()['n']
    rows = conn.execute(
        f'''SELECT c.* FROM clients c LEFT JOIN users u ON c.user_id = u.id
            WHERE {where_sql} ORDER BY {order} LIMIT ? OFFSET ?''',
        tuple(params) + (limit, offset)).fetchall()
    out = [_crm_client_dict(conn, r) for r in rows]
    conn.close()
    return jsonify({'clients': out, 'total': total, 'limit': limit, 'offset': offset})


@app.route('/api/clients', methods=['POST'])
def create_client():
    err = require_login()
    if err: return err
    uid = session['user_id']
    data = request.get_json(silent=True) or request.form
    name = (data.get('name') or '').strip()[:120]
    if not name:
        return jsonify({'error': 'Jméno klienta je povinné.'}), 400

    conn = get_db()
    conn.execute('''INSERT INTO clients (artist_id, user_id, name, email, phone, tags,
                                         style_preferences, acquisition_source, note, created_by)
                    VALUES (?,NULL,?,?,?,?,?,?,?,?)''',
                 (uid, name,
                  (data.get('email') or '').strip()[:120],
                  (data.get('phone') or '').strip()[:40],
                  (data.get('tags') or '').strip()[:200],
                  (data.get('style_preferences') or '').strip()[:200],
                  (data.get('acquisition_source') or 'manual').strip()[:40],
                  (data.get('note') or '').strip()[:1000],
                  uid))
    conn.commit()
    cid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
           else conn.execute('SELECT lastval()').fetchone()[0])
    row = conn.execute('SELECT * FROM clients WHERE id=?', (cid,)).fetchone()
    out = _crm_client_dict(conn, row)
    conn.close()
    return jsonify({'ok': True, 'id': cid, 'client': out})


@app.route('/api/clients/<int:client_id>')
def get_client(client_id):
    err = require_login()
    if err: return err
    conn = get_db()
    row = _crm_get_client(conn, session['user_id'], client_id)
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    out = _crm_client_dict(conn, row)

    bookings, spent = [], 0
    if row['user_id']:
        brows = conn.execute(
            '''SELECT id, status, booking_start_at, duration_hours, total_price_cents,
                      deposit_cents, session_number, parent_booking_id
               FROM bookings WHERE artist_id=? AND client_id=?
               ORDER BY booking_start_at DESC LIMIT 100''',
            (row['artist_id'], row['user_id'])).fetchall()
        bookings = [dict(b) for b in brows]
        # Lifetime value se počítá při čtení — cachovaný sloupec bez
        # invalidace je přesně ta chyba, co byla na bookings.studio_id.
        spent = sum((b['total_price_cents'] or 0) for b in bookings
                    if b['status'] in ('confirmed', 'completed'))

    notes = [dict(n) for n in conn.execute(
        '''SELECT n.*, u.display_name AS author_name FROM client_notes n
           LEFT JOIN users u ON n.author_id = u.id
           WHERE n.client_id=? ORDER BY n.created_at DESC''', (client_id,)).fetchall()]
    records = [_record_json(t) for t in conn.execute(
        'SELECT * FROM tattoo_records WHERE client_id=? ORDER BY session_date DESC',
        (client_id,)).fetchall()]
    conn.close()

    out['bookings'] = bookings
    out['notes'] = notes
    out['tattoo_records'] = records
    out['lifetime_value_cents'] = spent
    return jsonify(out)


@app.route('/api/clients/<int:client_id>', methods=['PATCH'])
def update_client(client_id):
    err = require_login()
    if err: return err
    data = request.get_json(silent=True) or request.form
    conn = get_db()
    row = _crm_get_client(conn, session['user_id'], client_id)
    if not row:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    sets, params = [], []
    limits = {'name': 120, 'email': 120, 'phone': 40, 'tags': 200,
              'style_preferences': 200, 'acquisition_source': 40, 'note': 1000}
    for field, maxlen in limits.items():
        if field in data:
            sets.append(f'{field}=?')
            params.append((data.get(field) or '').strip()[:maxlen])
    if not sets:
        conn.close()
        return jsonify({'ok': True, 'no_changes': True})
    sets.append('updated_at=?')
    params.append(datetime.utcnow().isoformat())
    params.append(client_id)
    conn.execute(f'UPDATE clients SET {", ".join(sets)} WHERE id=?', tuple(params))
    conn.commit()
    out = _crm_client_dict(conn, conn.execute('SELECT * FROM clients WHERE id=?',
                                              (client_id,)).fetchone())
    conn.close()
    return jsonify({'ok': True, 'client': out})


# ── CRM: poznámky ke klientovi ───────────────────────────────────────────────
# Potomci se autorizují PŘES KLIENTA (_crm_get_client), nikdy podle vlastního
# id — jinak by stačilo uhodnout note_id a poznámka cizího studia je venku.

@app.route('/api/clients/<int:client_id>/notes', methods=['POST'])
def create_client_note(client_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    body = ((request.get_json(silent=True) or request.form).get('body') or '').strip()[:5000]
    if not body:
        return jsonify({'error': 'Poznámka nesmí být prázdná.'}), 400
    conn = get_db()
    if not _crm_get_client(conn, uid, client_id):
        conn.close()
        return jsonify({'error': 'not found'}), 404
    conn.execute('INSERT INTO client_notes (client_id, author_id, body) VALUES (?,?,?)',
                 (client_id, uid, body))
    conn.commit()
    nid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
           else conn.execute('SELECT lastval()').fetchone()[0])
    conn.close()
    return jsonify({'ok': True, 'id': nid})


def _crm_get_note(conn, uid, note_id):
    """Poznámka + její klient, jen když na klienta uživatel vidí."""
    note = conn.execute('SELECT * FROM client_notes WHERE id=?', (note_id,)).fetchone()
    if not note:
        return None, None
    client = _crm_get_client(conn, uid, note['client_id'])
    if not client:
        return None, None
    return note, client


@app.route('/api/client-notes/<int:note_id>', methods=['PATCH'])
def update_client_note(note_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    body = ((request.get_json(silent=True) or request.form).get('body') or '').strip()[:5000]
    if not body:
        return jsonify({'error': 'Poznámka nesmí být prázdná.'}), 400
    conn = get_db()
    note, client = _crm_get_note(conn, uid, note_id)
    if not note:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if note['author_id'] != uid and client['artist_id'] != uid:
        conn.close()
        return jsonify({'error': 'Upravit může jen autor nebo tatér klienta.'}), 403
    conn.execute('UPDATE client_notes SET body=?, updated_at=? WHERE id=?',
                 (body, datetime.utcnow().isoformat(), note_id))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


@app.route('/api/client-notes/<int:note_id>', methods=['DELETE'])
def delete_client_note(note_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    note, client = _crm_get_note(conn, uid, note_id)
    if not note:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if note['author_id'] != uid and client['artist_id'] != uid:
        conn.close()
        return jsonify({'error': 'Smazat může jen autor nebo tatér klienta.'}), 403
    conn.execute('DELETE FROM client_notes WHERE id=?', (note_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True})


# ── CRM: záznamy o tetováních ────────────────────────────────────────────────
# Záznam je historie práce, ne rezervace: booking_id smí být NULL (práce
# z doby před InkLinkem, kterou tatér doplňuje ručně). Autorizace jde vždy
# přes klienta, nikdy přes vlastní id záznamu.

TATTOO_RECORD_FIELDS = ('booking_id', 'session_date', 'body_location', 'style',
                        'size_label', 'description', 'aftercare_status', 'price_czk')


def _record_json(r):
    return {
        'id':               r['id'],
        'client_id':        r['client_id'],
        'booking_id':       r['booking_id'],
        'artist_id':        r['artist_id'],
        'session_date':     r['session_date'],
        'body_location':    r['body_location'] or '',
        'style':            r['style'] or '',
        'size_label':       r['size_label'] or '',
        'description':      r['description'] or '',
        'healed_photo':     f"/uploads/{r['healed_photo']}" if r['healed_photo'] else '',
        'aftercare_status': r['aftercare_status'] or '',
        'price_czk':        r['price_czk'],
        'created_at':       r['created_at'],
        'anonymized':       bool(r['anonymized_at']),
    }


def _parse_record_payload(src, existing=None):
    """Vytáhne pole záznamu z JSON i z multipartu (kvůli fotce).

    Vrací (data, error). `existing` je řádek při PATCHi — chybějící klíč pak
    znamená "neměň", ne "vymaž".
    """
    def given(k):
        return k in src

    data = {}
    for k in TATTOO_RECORD_FIELDS:
        if not given(k):
            continue
        v = src.get(k)
        if k in ('booking_id', 'price_czk'):
            if v in (None, '', 'null'):
                data[k] = None
            else:
                try:
                    data[k] = int(v)
                except (TypeError, ValueError):
                    return None, f'Pole {k} musí být číslo.'
        else:
            data[k] = str(v or '').strip()[:2000]

    date = data.get('session_date', existing['session_date'] if existing else None)
    if not date:
        return None, 'Datum sezení je povinné.'
    try:
        datetime.strptime(date[:10], '%Y-%m-%d')
    except ValueError:
        return None, 'Datum sezení musí být ve tvaru RRRR-MM-DD.'
    if 'session_date' in data:
        data['session_date'] = date[:10]
    return data, None


def _save_healed_photo(client_id):
    """Vrací (filename, error). Prázdné jméno = fotka nebyla poslána."""
    f = request.files.get('healed_photo')
    if not (f and f.filename):
        return '', None
    if not (allowed_file(f.filename) and allowed_image(f)):
        return None, 'Fotka musí být obrázek (jpg, png, webp).'
    ext = secure_filename(f.filename).rsplit('.', 1)[1].lower()
    name = f'heal_{client_id}_{int(datetime.now().timestamp() * 1000)}.{ext}'
    save_upload(f, name)
    return name, None


def _booking_belongs_to_client(conn, uid, client, booking_id):
    """Bez téhle kontroly je booking_id volný ukazatel, kterým by šlo
    natáhnout cizí rezervaci do vlastního CRM. Prázdná hodnota je v pořádku —
    záznam bez rezervace je legitimní (práce z doby před InkLinkem)."""
    if not booking_id:
        return True
    bk = conn.execute('SELECT client_id, artist_id FROM bookings WHERE id=?',
                      (booking_id,)).fetchone()
    if not bk or bk['artist_id'] not in _crm_visible_artist_ids(conn, uid):
        return False
    # client['user_id'] je NULL u walk-in klienta bez účtu — ten na platformě
    # žádnou rezervaci mít nemůže, takže je odmítnutí správné.
    return bool(client['user_id']) and bk['client_id'] == client['user_id']


@app.route('/api/clients/<int:client_id>/tattoo-records', methods=['POST'])
def create_tattoo_record(client_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    client = _crm_get_client(conn, uid, client_id)
    if not client:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    src = request.get_json(silent=True) or request.form
    data, perr = _parse_record_payload(src)
    if perr:
        conn.close()
        return jsonify({'error': perr}), 400

    if not _booking_belongs_to_client(conn, uid, client, data.get('booking_id')):
        conn.close()
        return jsonify({'error': 'Rezervace nepatří k tomuhle klientovi.'}), 400

    photo, perr = _save_healed_photo(client_id)
    if perr:
        conn.close()
        return jsonify({'error': perr}), 400

    conn.execute(
        '''INSERT INTO tattoo_records
           (client_id, booking_id, artist_id, session_date, body_location, style,
            size_label, description, healed_photo, aftercare_status, price_czk)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)''',
        (client_id, data.get('booking_id'), uid, data['session_date'],
         data.get('body_location', ''), data.get('style', ''), data.get('size_label', ''),
         data.get('description', ''), photo, data.get('aftercare_status', ''),
         data.get('price_czk')))
    conn.commit()
    rid = (conn.execute('SELECT last_insert_rowid()').fetchone()[0] if not conn._pg
           else conn.execute('SELECT lastval()').fetchone()[0])
    conn.close()
    return jsonify({'ok': True, 'id': rid})


def _crm_get_record(conn, uid, record_id):
    """Záznam + jeho klient, jen když na klienta uživatel vidí."""
    rec = conn.execute('SELECT * FROM tattoo_records WHERE id=?', (record_id,)).fetchone()
    if not rec:
        return None, None
    client = _crm_get_client(conn, uid, rec['client_id'])
    if not client:
        return None, None
    return rec, client


@app.route('/api/tattoo-records/<int:record_id>', methods=['PATCH'])
def update_tattoo_record(record_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    rec, client = _crm_get_record(conn, uid, record_id)
    if not rec:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if rec['anonymized_at']:
        conn.close()
        return jsonify({'error': 'Záznam je anonymizovaný a nelze ho upravovat.'}), 409

    src = request.get_json(silent=True) or request.form
    data, perr = _parse_record_payload(src, existing=rec)
    if perr:
        conn.close()
        return jsonify({'error': perr}), 400

    if 'booking_id' in data and not _booking_belongs_to_client(
            conn, uid, client, data['booking_id']):
        conn.close()
        return jsonify({'error': 'Rezervace nepatří k tomuhle klientovi.'}), 400

    photo, perr = _save_healed_photo(rec['client_id'])
    if perr:
        conn.close()
        return jsonify({'error': perr}), 400
    old_photo = rec['healed_photo']
    if photo:
        data['healed_photo'] = photo

    if not data:
        conn.close()
        return jsonify({'ok': True})

    cols = ', '.join(f'{k}=?' for k in data)
    conn.execute(f'UPDATE tattoo_records SET {cols} WHERE id=?',
                 (*data.values(), record_id))
    conn.commit()
    conn.close()
    # Až po commitu: kdyby update spadl, stará fotka musí zůstat platná.
    if photo and old_photo:
        delete_upload(old_photo)
    return jsonify({'ok': True})


@app.route('/api/tattoo-records/<int:record_id>', methods=['DELETE'])
def delete_tattoo_record(record_id):
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    rec, client = _crm_get_record(conn, uid, record_id)
    if not rec:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    # Záznam smí smazat jen tatér, který ho pořídil, nebo vlastník klienta.
    # Kolega ze studia na něj vidí, ale mazat cizí historii práce nesmí.
    if rec['artist_id'] != uid and client['artist_id'] != uid:
        conn.close()
        return jsonify({'error': 'Smazat může jen autor záznamu nebo tatér klienta.'}), 403
    conn.execute('DELETE FROM tattoo_records WHERE id=?', (record_id,))
    conn.commit()
    conn.close()
    if rec['healed_photo']:
        delete_upload(rec['healed_photo'])
    return jsonify({'ok': True})


# ── CRM: GDPR (export, výmaz, slučování) ─────────────────────────────────────
# Vzor je _anonymize_user: PII vynulovat, řádek nechat, účetnictví zachovat
# (zákon o účetnictví, 10letá retence). Kaskády se NEDAJÍ použít — SQLite
# bez PRAGMA foreign_keys=ON cizí klíče nevynucuje a kód ho nikde nezapíná.

def _crm_can_erase(conn, uid, client):
    """Výmaz smí vlastník klienta, nebo admin studia, ve kterém vlastník je.

    Řadový kolega ze studia klienta VIDÍ, ale mazat cizí klientelu nesmí —
    to je nevratná operace na datech, jejichž správcem je někdo jiný.
    """
    if client['artist_id'] == uid:
        return True
    m = conn.execute('SELECT studio_id FROM studio_members WHERE artist_id=?',
                     (client['artist_id'],)).fetchone()
    return bool(m and m['studio_id'] and _studio_admin_check(conn, uid, m['studio_id']))


@app.route('/api/clients/<int:client_id>/export')
def export_client(client_id):
    """Přenositelnost dat pro JEDNOHO klienta, vydává tatér.

    Zdravotní poznámky patří sem, a ne do /api/me/export: ten je platformní
    a vydává ho sám uživatel, tyhle údaje ale vede tatér jako jejich správce.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    conn = get_db()
    client = _crm_get_client(conn, uid, client_id)
    if not client:
        conn.close()
        return jsonify({'error': 'not found'}), 404

    out = {'client': _crm_client_dict(conn, client),
           'exported_at': _prague_now_naive().isoformat(),
           'exported_by': uid}
    out['notes'] = [dict(n) for n in conn.execute(
        'SELECT * FROM client_notes WHERE client_id=? ORDER BY created_at', (client_id,)).fetchall()]
    out['tattoo_records'] = [_record_json(t) for t in conn.execute(
        'SELECT * FROM tattoo_records WHERE client_id=? ORDER BY session_date',
        (client_id,)).fetchall()]
    if client['user_id']:
        out['bookings'] = [dict(b) for b in conn.execute(
            'SELECT * FROM bookings WHERE artist_id=? AND client_id=? ORDER BY booking_start_at',
            (client['artist_id'], client['user_id'])).fetchall()]
    else:
        out['bookings'] = []

    conn.close()
    return jsonify(out)


@app.route('/api/clients/<int:client_id>/erase', methods=['POST'])
def erase_client(client_id):
    """Výmaz jednoho klienta u jednoho tatéra. Nevratné."""
    err = require_login()
    if err: return err
    uid = session['user_id']
    typed = ((request.get_json(silent=True) or request.form).get('confirm') or '').strip()

    conn = get_db()
    client = _crm_get_client(conn, uid, client_id)
    if not client:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if not _crm_can_erase(conn, uid, client):
        conn.close()
        return jsonify({'error': 'Vymazat klienta může jen jeho tatér nebo admin studia.'}), 403
    if client['anonymized_at']:
        conn.close()
        return jsonify({'error': 'Klient je už vymazaný.'}), 409
    # Vypsané potvrzení jako u /api/me/delete — chrání před CSRF i překlikem.
    if typed != 'VYMAZAT':
        conn.close()
        return jsonify({'error': 'Pro potvrzení napiš VYMAZAT.'}), 400

    now_iso = _prague_now_naive().isoformat()
    client_user_id = client['user_id']

    # Fotky hojení jsou samostatné objekty v úložišti; vynulovat cestu v DB
    # nestačí, objekt by zůstal veřejně adresovatelný pro kohokoli s URL.
    photos = [r['healed_photo'] for r in conn.execute(
        'SELECT healed_photo FROM tattoo_records WHERE client_id=? AND healed_photo != ""',
        (client_id,)).fetchall()]

    # 1) clients — PII pryč VČETNĚ user_id. Ponechaný odkaz na účet
    #    re-identifikuje přes users, což je přesně to, co má výmaz zrušit.
    conn.execute('''UPDATE clients SET user_id=NULL, name='', email='', phone='',
                    tags='', style_preferences='', acquisition_source='', note='',
                    anonymized_at=?, updated_at=? WHERE id=?''',
                 (now_iso, now_iso, client_id))
    # 2) poznámky — tvrdě smazat. Soft delete by nechal PII v databázi,
    #    tedy pravý opak toho, co má výmaz udělat.
    conn.execute('DELETE FROM client_notes WHERE client_id=?', (client_id,))
    # 3) tattoo_records — řádek se ROZDĚLÍ: popisné údaje o těle pryč,
    #    účetní kostra (booking_id, artist_id, session_date, price_czk) zůstává.
    conn.execute('''UPDATE tattoo_records SET body_location='', description='',
                    healed_photo='', aftercare_status='', anonymized_at=?
                    WHERE client_id=?''', (now_iso, client_id))
    # 4) bookings — design_note i internal_note. Do internal_note tatéři reálně
    #    píšou i věci jako "volat po 18:00": nejcitlivější pole v celé tabulce
    #    a nejlevnější výhra celého výmazu.
    if client_user_id:
        conn.execute('''UPDATE bookings SET design_note='', internal_note=''
                        WHERE artist_id=? AND client_id=?''',
                     (client['artist_id'], client_user_id))
    conn.commit()
    conn.close()

    failed = [p for p in photos if not delete_upload(p)]
    return jsonify({'ok': True, 'photos_deleted': len(photos) - len(failed),
                    'photos_failed': len(failed)})


@app.route('/api/clients/<int:client_id>/merge', methods=['POST'])
def merge_clients(client_id):
    """Sloučí duplicitního klienta do `client_id`. Zdroj se pak smaže.

    V1 vyžaduje shodný artist_id. Slučování napříč tatéry je otázka
    vlastnictví dat, ne UI — špatná odpověď je incident, ne překlep.
    """
    err = require_login()
    if err: return err
    uid = session['user_id']
    src_id = (request.get_json(silent=True) or request.form).get('source_id')
    try:
        src_id = int(src_id)
    except (TypeError, ValueError):
        return jsonify({'error': 'Chybí source_id.'}), 400
    if src_id == client_id:
        return jsonify({'error': 'Nelze sloučit klienta sám se sebou.'}), 400

    conn = get_db()
    target = _crm_get_client(conn, uid, client_id)
    source = _crm_get_client(conn, uid, src_id)
    if not target or not source:
        conn.close()
        return jsonify({'error': 'not found'}), 404
    if target['artist_id'] != source['artist_id']:
        conn.close()
        return jsonify({'error': 'Sloučit lze jen klienty stejného tatéra.'}), 400
    if target['anonymized_at'] or source['anonymized_at']:
        conn.close()
        return jsonify({'error': 'Vymazaného klienta nelze slučovat.'}), 409
    # Oba mohou být navázaní na účet, ale na různý — pak to nejsou duplicity,
    # ale dva lidé, a sloučení by smíchalo cizí zdravotní historie.
    if target['user_id'] and source['user_id'] and target['user_id'] != source['user_id']:
        conn.close()
        return jsonify({'error': 'Klienti jsou navázaní na různé účty.'}), 400

    conn.execute('UPDATE client_notes SET client_id=? WHERE client_id=?', (client_id, src_id))
    conn.execute('UPDATE tattoo_records SET client_id=? WHERE client_id=?', (client_id, src_id))
    fill = {}
    for col in ('name', 'email', 'phone', 'tags', 'style_preferences',
                'acquisition_source', 'note'):
        if not (target[col] or '').strip() and (source[col] or '').strip():
            fill[col] = source[col]
    if not target['user_id'] and source['user_id']:
        fill['user_id'] = source['user_id']
    if fill:
        cols = ', '.join(f'{k}=?' for k in fill)
        conn.execute(f'UPDATE clients SET {cols}, updated_at=? WHERE id=?',
                     (*fill.values(), _prague_now_naive().isoformat(), client_id))
    conn.execute('DELETE FROM clients WHERE id=?', (src_id,))
    conn.commit()
    conn.close()
    return jsonify({'ok': True, 'merged_into': client_id})


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

@app.route('/clients')
def clients_page():
    return send_from_directory('public', 'clients.html')

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
        # Stav brány, ať se nemusí hádat, jestli proměnná v prostředí dosedla.
        # Hodnotu tokenu nikdy neprozrazujeme, jen jestli vůbec je nastavený.
        'coming_soon': COMING_SOON,
        'coming_soon_token_set': bool(COMING_SOON_TOKEN),
        # Jen NÁZVY proměnných, které vypadají příbuzně — žádné hodnoty.
        # Odhalí překlep i mezeru v názvu, která je v Railway UI neviditelná.
        'coming_soon_env_seen': sorted(
            repr(k) for k in os.environ if 'COMING' in k.upper()),
        # Tiché vypínače. Když některý nesedí, nic nespadne — jen přestanou
        # chodit maily nebo běhat crony, což se pozná až na chybějící
        # rezervaci. Hodnoty nikdy neprozrazujeme, jen jestli jsou nastavené.
        'emails_enabled': bool(RESEND_API_KEY),
        # Výchozí onboarding@resend.dev doručuje JEN majiteli účtu Resend.
        # Klientům z něj nikdy nic nepřijde a nikde to nezahlásí.
        'email_from': RESEND_FROM,
        'email_from_is_shared_sandbox': 'resend.dev' in RESEND_FROM,
        'cron_token_set': bool(RECONCILE_TOKEN),
        'stripe_mode': ('off' if not STRIPE_SECRET_KEY
                        else 'live' if STRIPE_SECRET_KEY.startswith('sk_live')
                        else 'test'),
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
