# InkLink — Production Operations

Provozní příručka — env proměnné, cron schedules, monitoring, incidenty.
Pro tech specs viz `docs/pricing_engine.pdf` a `docs/PRICING_MIGRATION.md`.

## 1. Required environment variables (Railway)

### Core
| Variable | Required | Example | Note |
|---|---|---|---|
| `DATABASE_URL` | ✅ | `postgres://…` | Auto-set by Railway Postgres plugin |
| `SECRET_KEY` | ✅ | random 64 chars | Flask session signing |
| `APP_BASE_URL` | ✅ | `https://www.inklink.club` | Used in emails + OG URLs |
| `STRIPE_SECRET_KEY` | ✅ | `sk_live_…` | Test mode `sk_test_…` v staging |
| `STRIPE_WEBHOOK_SECRET` | ✅ | `whsec_…` | Z Stripe dashboard → Webhooks endpoint |
| `STRIPE_PREMIUM_PRICE_ID_CZK` | ⚠️ | `price_…` | Předplatné premia v korunách; bez něj se v CZK nedá předplatit |
| `STRIPE_PREMIUM_PRICE_ID_EUR` | | `price_…` | Totéž pro eura (16 €). Stejně `_USD`, `_GBP`, `_PLN` |
| `RESEND_API_KEY` | ⚠️ | `re_…` | Bez něj emaily nejdou ven (booking confirmations) |
| `RESEND_FROM` | ⚠️ | `InkLink <hello@inklink.cz>` | Musí být verified domain v Resend |

### Pricing engine
| Variable | Required | Default | Note |
|---|---|---|---|
| `USE_NEW_PRICING_ENGINE` | ⬜ | `0` | Set `1` to enable tiered + discounts |
| `RECONCILE_TOKEN` | ⚠️ | `(random 32 chars)` | Required for cron endpoint security |

### Monitoring
| Variable | Required | Note |
|---|---|---|
| `SENTRY_DSN` | ⚠️ | Z Sentry dashboard → Project Settings → Client Keys |
| `SENTRY_ENVIRONMENT` | ⬜ | Default `production` if DSN set, else `development` |
| `RAILWAY_GIT_COMMIT_SHA` | auto | Set by Railway, used for Sentry release tagging |

### Optional
| Variable | Default | Note |
|---|---|---|
| `VERIFY_EMAIL` | `0` | Set `1` to require email verification on signup |
| `ADMIN_USERNAME` | empty | Username co dostane admin práva bootstrap (jinak set `is_admin=1` v DB) |
| `PUSH_PUBLIC` / `PUSH_PRIVATE` | empty | Web push VAPID keys (browser notifications) |

### iOS push (APNs)
| Variable | Required | Note |
|---|---|---|
| `APNS_KEY_ID` | ⚠️ | 10-char Key ID z Apple Developer → Keys |
| `APNS_TEAM_ID` | ⚠️ | 10-char Team ID z Apple Developer → Membership |
| `APNS_BUNDLE_ID` | ⬜ | Default `club.inklink.app` — musí matchovat Xcode bundle |
| `APNS_KEY_PEM` | ⚠️ | Celý obsah `.p8` souboru (multi-line) — preferred pro Railway |
| `APNS_KEY_PATH` | ⬜ | Alternativa k `APNS_KEY_PEM` — cesta k `.p8` souboru (lokál dev) |
| `APNS_USE_SANDBOX` | ⬜ | Set `1` jen pro TestFlight sandbox build (default `0` = production) |

---

## 1.5 APNs setup (iOS push)

### Vytvořit APNs Key (jednou pro celý team)

1. [developer.apple.com](https://developer.apple.com/account) → **Certificates, IDs & Profiles** → **Keys** → **+**
2. Name: `InkLink APNs`, zaškrtni **Apple Push Notifications service (APNs)** → Continue → Register
3. Stáhni `.p8` soubor (`AuthKey_XXXXXXXXXX.p8`) — **lze stáhnout jen jednou**, ulož do 1Password
4. Pozn. Key ID (10 znaků, vidíš v Keys přehledu)
5. Team ID — Apple Developer Account → **Membership** (10 znaků)
6. Identifiers → `club.inklink.app` → Edit → zaškrtni **Push Notifications** capability

### Railway env

```
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=XXXXXXXXXX
APNS_BUNDLE_ID=club.inklink.app
APNS_KEY_PEM=-----BEGIN PRIVATE KEY-----
MIGTAgEAMBMG...
-----END PRIVATE KEY-----
```

(V Railway dashboardu klikni "Multiline" u `APNS_KEY_PEM`, paste celý obsah `.p8` souboru.)

### Co se stane v aplikaci

1. Capacitor app po loginu zavolá `PushNotifications.requestPermissions()` → `register()`
2. iOS vrátí device token (64-hex), náš `public/native.js` ho POST-ne na `/api/native/register-push`
3. Backend uloží do `push_subscriptions` s `provider='apns'`
4. Při notifikaci `send_push()` automaticky fan-outuje na všechny tokeny daného usera (web + apns)
5. Stale tokens (BadDeviceToken / Unregistered) se auto-mažou

### Test

Po nasazení v Capacitor appce po prvním přihlášení v Settings (iOS) → Notifications → InkLink → musí být allowed. V admin DB:

```sql
SELECT user_id, provider, platform, substr(endpoint, 1, 20) || '...' AS token
FROM push_subscriptions WHERE provider = 'apns';
```

Pro test poslání pushe:
```python
from server import send_push
send_push(<your_user_id>, 'Test', 'iOS push funguje 🎉', '/')
```

---

## 2. Sentry setup (error monitoring)

Sentry je už integrovaný v `server.py` (řádek 25–65). Aktivuje se přidáním `SENTRY_DSN` env.

### Vytvoření Sentry projektu

1. Sign up na [sentry.io](https://sentry.io) (free tier: 5 000 errors/měsíc — pro start dost)
2. Create project → Python → Flask
3. Skip onboarding, jdi do **Settings → Client Keys (DSN)** → copy DSN
4. Railway dashboard → Variables → přidej:
   ```
   SENTRY_DSN=https://abc123@oxxx.ingest.sentry.io/xxx
   SENTRY_ENVIRONMENT=production
   ```
5. Restart deployu (Railway to udělá auto po env change)

### Co Sentry zachytí
- ✅ Server 5xx errors + tracebacks
- ✅ Unhandled exceptions
- ✅ 10 % requests perf monitoring (traces_sample_rate=0.1)
- ❌ 4xx client errors (filterujeme, šetří quotu)
- ❌ Cookies, auth headers, request bodies (scrub)
- ❌ User IP / PII (`send_default_pii=False`)

### Release tracking
Pokud Railway exponuje `RAILWAY_GIT_COMMIT_SHA`, Sentry označí errors release tagem `inklink@<sha>`. V Sentry uvidíš "X errors v této verzi" a "Y errors po deployi".

### Test alert
Po setup zavolaš endpoint co schválně padne (např. udělej krátkou test routu `/__sentry-test` co dělá `raise Exception('test')`). V Sentry by se měl objevit do 30 s.

---

## 3. Railway cron — reconciliation daily

Railway má **Cron Jobs** od léta 2024 (predtim potřebné external scheduler). Setup:

### A) Vygeneruj token

V terminálu:
```bash
openssl rand -hex 32
```
Zkopíruj output (např. `7f8a1c2b...`). Tohle bude `RECONCILE_TOKEN`.

### B) Railway env

V Railway dashboard → Variables:
```
RECONCILE_TOKEN=<paste-token-here>
```

### C) Vytvoř cron service

Railway dashboard → **+ New** → **Empty Service**. Pojmenuj ho `inklink-cron`.

V tom service → **Settings → Cron Schedule**:
```
0 6 * * *
```
(denně v 6:00 UTC = 7:00/8:00 lokálně podle DST)

V **Settings → Custom Start Command**:
```
curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/reconcile && echo "reconcile OK" || (echo "reconcile FAILED" && exit 1)
```

V **Variables**, share s main projektem:
```
RECONCILE_TOKEN=$RECONCILE_TOKEN     (z main service)
```

### D) Co cron dělá

`GET /api/cron/reconcile?token=...` (server.py:5825+):
1. Spočítá sum(`client_pays_total`) z `economics_snapshots` za včerejšek (UTC midnight ranges)
2. Zavolá Stripe `BalanceTransaction.list()` pro stejný window, sečte
3. Vypočte diff
4. Emit event `reconciliation.completed` do `telemetry_events`
5. Pokud diff > 10 CZK → `warning: true` flag v eventu

### E) Sledování

V Railway dashboard → cron service → Logs. Vidíš poslední běh.

V admin panelu (`/admin` → Telemetry events) filtruj `event_name=reconciliation.completed` — uvidíš diff každý den.

### F) Manual trigger

```bash
curl -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/reconcile
```

Vrací JSON:
```json
{
  "window": {"start": "2026-05-20T00:00:00", "end": "2026-05-21T00:00:00"},
  "internal_total_czk": 28450.0,
  "stripe_total_czk": 28452.5,
  "diff_czk": 2.5,
  "reconciled": true
}
```

---

## 3.4 Account deletion cron

GDPR — anonymizuje účty 30 dní po jejich žádosti o smazání:

```
30 3 * * *      # denně v 3:30 UTC
curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/account-deletions
```

Najde usery s `deletion_requested_at <= now() - 30 days` a `deleted_at IS NULL`,
přepíše PII (jméno, email, telefon, bio, foto…) na placeholdery, smaže portfolio
+ push subscriptions, password_hash nastaví na unguessable token. Účetní
záznamy (bookings, economics_snapshots) zůstanou v DB s anonymizovanou FK
linkou.

Response:
```json
{
  "ok": true,
  "purged_count": 3,
  "purged_user_ids": [42, 51, 78],
  "cutoff_iso": "2026-04-23T13:00:00"
}
```

---

## 3.5 Welcome email sequence cron

Posílá 3-stupňový onboarding email klientům/tatérům:
- **Stage 1** (immediate při registraci) — uvítací mail, jak InkLink funguje
- **Stage 2** (+2 dny) — tipy podle role (klient: kde hledat, tatér: setup)
- **Stage 3** (+7 dní) — re-engagement (founding programy, mobilní app)

### Setup (Railway cron service)

Stejný setup jako reconcile, jen jiný endpoint:

```
0 9 * * *      # denně v 9:00 UTC = 10:00/11:00 lokálně
curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/welcome-emails
```

Cron každý den najde usery, kteří dosáhli `welcome_email_next_at`, pošle jim
další stage a posune timer. Idempotent — stage advance je atomický.

### Response

```json
{
  "ok": true,
  "sent_count": 7,
  "failed_count": 0,
  "sent": [{"user_id": 42, "stage": 2}, ...]
}
```

### Manual trigger / test

```bash
curl -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/welcome-emails
```

---

## 4. Booking reminder cron (existing)

Už existuje `/api/cron/booking-reminders` (server.py:3689). Posílá push + email 24 h před session. Měl by běžet **každou hodinu** nebo `*/30` (30 min):

Stejný setup jako reconcile, jen jiný schedule + endpoint:
```
*/30 * * * *      # každých 30 min
curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/booking-reminders
```

---

## 5. Common incidents

### "Stripe webhook 400"
**Symptom:** Stripe dashboard ukazuje 400 na všech webhook delivery attempts.

**Cause:** `STRIPE_WEBHOOK_SECRET` v Railway env neodpovídá secret v Stripe webhook endpoint.

**Fix:**
1. Stripe dashboard → Developers → Webhooks → klikni endpoint
2. **Signing secret → Reveal** → copy
3. Railway env `STRIPE_WEBHOOK_SECRET=whsec_...` → restart

### "Booking payment_failed loop"
**Symptom:** Klient zkouší zaplatit, dostává payment_failed pořád dokola.

**Cause:** 95 % case — klientova karta zamítnutá. Zbytek: Stripe Connect KYC ne hotový u tatéra (`stripe_charges_enabled=0`).

**Diagnostika:**
```sql
SELECT b.id, b.stripe_payment_intent_id, b.status, u.username, u.stripe_charges_enabled
FROM bookings b JOIN users u ON u.id = b.artist_id
WHERE b.status = 'payment_failed' ORDER BY b.id DESC LIMIT 10;
```

### "Reconciliation diff > 100 CZK"
**Symptom:** `reconciliation.completed` event ukazuje velký diff.

**Možné příčiny:**
1. Stripe processing delay (transactions z pozdě dne 23:55 dorazí balance 00:01 následujícího dne) — small diff = OK
2. Refund nebyl správně zachycen v `economics_snapshots` (kind='refund' chybí)
3. Currency conversion diff (rare)

**Fix:** Otevři Stripe dashboard → Reports → Balance changes pro daný den. Najdi transakci která chybí v internal. Manuálně zaktualizuj snapshot.

### "Telemetry table grows fast"
**Symptom:** `telemetry_events` table má miliony řádků.

**Fix:** Cron co maže events older than 90 days:
```sql
DELETE FROM telemetry_events WHERE created_at < NOW() - INTERVAL '90 days';
```
Spouštět týdně.

---

## 6. Backup strategy

### Postgres (Railway)

Railway automaticky dělá **daily snapshots** pro Postgres (retained 7 days zdarma, 30 days paid plan).

Pro **manual backup** local copy:
```bash
# get DATABASE_URL z Railway dashboard
pg_dump $DATABASE_URL > backup_$(date +%Y%m%d).sql
```

Důležité tabulky pro audit recovery (nikdy nesmáznout bez transit copy):
- `bookings`
- `economics_snapshots` (immutable ledger)
- `processed_stripe_events`
- `discount_redemptions`

### Cloudflare R2 (uploads)

R2 už má redundancy across 3 zones. Pro extra paranoia: nasaď `aws s3 sync` cross-region replikaci přes Cloudflare Workers — zatím není nutné.

---

## 7. Quick smoke checks

Po deploji:
```bash
# 1. Health endpoint
curl https://www.inklink.club/__health
# Expect: {"ok": true, "build_marker": "..."}

# 2. Landing loads
curl -sI https://www.inklink.club/landing | head -1
# Expect: HTTP/2 200

# 3. Stripe webhook live
# Z Stripe dashboard → Send test event → 200 OK z naší strany

# 4. KPI endpoint (admin only)
# Otevři /admin v browseru, sekce Pricing engine — KPIs by měla render čísla
```

---

## 8. Kontakty

- **Hosting** Railway — support@railway.com
- **Postgres** Railway Postgres plugin
- **Email** Resend — support@resend.com
- **Errors** Sentry — automated alerts (per Sentry settings)
- **Payments** Stripe — dashboard.stripe.com/payments
- **Object storage** Cloudflare R2 — dash.cloudflare.com
- **Domain DNS** [provider] — TBD
