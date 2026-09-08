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
| `STRIPE_PUBLISHABLE_KEY` | ✅ | `pk_live_…` | Pro Stripe Elements ve frontendu |
| `STRIPE_WEBHOOK_SECRET` | ✅ | `whsec_…` | Z Stripe dashboard → Webhooks endpoint |
| `ENABLE_DEPOSIT_PI` | ✅ | `1` | Bez toho rezervace v live módu uvízne v pending_payment |
| `RESEND_API_KEY` | ⚠️ | `re_…` | Bez něj emaily nejdou ven (booking confirmations) |
| `RESEND_FROM` | ⚠️ | `contact@inklink.club` | **Musí být verified doména.** Výchozí `onboarding@resend.dev` doručuje jen majiteli účtu Resend — klientům z něj nikdy nic nepřijde a Resend to nezahlásí jako chybu |
| `CRON_SECRET` | ⚠️ | `(random 32+)` | Autorizace cronů `/api/cron/*` |

### Premium (předplatné)
Cena je pro každou měnu vlastní (390 Kč · 16 € · 17 $ · 14 £ · 79 zł), ne přepočet
kurzem — Stripe Billing účtuje přes Price objekt, libovolnou částku poslat nejde.
Každá měna proto potřebuje vlastní Price ve Stripe. Bez něj se v té měně
předplatit nedá a stránka nabídne kontakt místo tlačítka, které by spadlo.

| Variable | Required | Example | Note |
|---|---|---|---|
| `STRIPE_PREMIUM_PRICE_ID_CZK` | ⚠️ | `price_…` | 390 Kč / měsíc, recurring |
| `STRIPE_PREMIUM_PRICE_ID_EUR` | ⬜ | `price_…` | 16 € / měsíc |
| `STRIPE_PREMIUM_PRICE_ID_USD` | ⬜ | `price_…` | 17 $ / měsíc |
| `STRIPE_PREMIUM_PRICE_ID_GBP` | ⬜ | `price_…` | 14 £ / měsíc |
| `STRIPE_PREMIUM_PRICE_ID_PLN` | ⬜ | `price_…` | 79 zł / měsíc |
| `PREMIUM_PRICE_CZK` | ⬜ | `390` | Jen zobrazovaná cena v CZK; musí sedět s Price ve Stripe |

Ve Stripe webhooku zaškrtnout `customer.subscription.created`, `.updated`
a `.deleted` — bez nich se premium po zaplacení nezapne.

### Brána před veřejností
| Variable | Required | Example | Note |
|---|---|---|---|
| `COMING_SOON` | ⬜ | `1` | Schová aplikaci za waitlist stránku |
| `COMING_SOON_TOKEN` | ⬜ | `(náhodný řetězec)` | `?preview=<token>` bránu otevře a uloží cookie |

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
| `STRIPE_PRO_PRICE_ID` | empty | **Nenastavovat** — načítá se, ale nikde se nepoužívá (zbytek po starším plánu) |
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

### Jak zjistit, co je opravdu nastavené

Tahle tabulka zastarává. Skutečný stav běžící aplikace vrátí health endpoint —
hodnoty neprozrazuje, jen jestli dosedly:

```
curl -s https://www.inklink.club/__health | python3 -m json.tool
```

| Pole | Co znamená |
|---|---|
| `emails_enabled` | je vidět `RESEND_API_KEY` |
| `email_from_is_shared_sandbox` | `true` = odesílá se z `resend.dev` a klientům nic nedorazí |
| `cron_token_set` | je vidět `RECONCILE_TOKEN` |
| `stripe_mode` | `off` / `test` / `live` |
| `coming_soon` | běží brána |
| `coming_soon_env_seen` | názvy proměnných obsahujících COMING — odhalí překlep i mezeru |

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

## 5.5 Dárkové poukazy a kredit

Poukaz je **náš závazek, ne tržba**. Peníze za něj přijdou na náš účet
(`mode='payment'`, ne Connect destination charge) a leží tam, dokud je někdo
neutratí. Admin proto vidí součet neuplatněných poukazů zvlášť — účetně je to
dluh vůči držitelům kódů.

Když klient zaplatí kreditem, **tatér dostane svoje celé**: kredit snižuje jen
to, co jde z karty, a rozdíl doplácíme my z peněz, které za poukaz držíme
(`bookings.platform_owes_artist_cents`). Kdybychom místo toho poslali tatérovi
míň, zaplatil by cizí dárek on.

Kde se to může zaseknout:

| Situace | Chování |
|---|---|
| Kredit pokryje celou zálohu | Rezervace se potvrdí bez Stripu (`payment.mode='credit'`) — nula se strhnout nedá |
| Na kartě by zbylo pod minimem Stripu | Kredit se sníží tak, aby karta strhla přesně minimum |
| Opuštěný checkout | Řádek se smaže po 2 h (`VOUCHER_UNPAID_TTL_HOURS`) |
| Opakovaný webhook | Podmíněný UPDATE — uplatněný poukaz se nevrátí do hry (a mail chodí jen při skutečné aktivaci) |
| Zrušení rezervace placené kreditem | Refund se dělí: nejdřív zpátky na kartu, zbytek jako kredit |

Limity a nabízené částky jsou **per měnu** (`VOUCHER_LIMITS`). Měna se odvozuje
ze země, nikde se nevybírá.

**Za zálohy placené kreditem dlužíme tatérům** — destination charge pošle jen
to, co prošlo kartou. Rozdíl doplácíme zvlášť přes Stripe Transfer z našeho
balance, kde peníze za poukazy leží. Dělá to cron `/api/cron/credit-payouts`
(viz sekce 3.6). Součet nezaplacených dluhů je v adminu u kreditu, tatér ho
vidí v účetním exportu ve sloupci „Z poukazu — doplatí InkLink".

Platí se **až po dokončení sezení**, schválně: platit dopředu by znamenalo při
každém zrušení řešit reversal. Takhle se nikdy nic nevrací.

**Kredit napříč měnami je nedořešený**: zůstatek se vede v měně, ve které
vznikl, ale utratit se dá i u tatéra s jinou měnou. Kurzové riziko neseme my.
Zatím je to vědomě přijaté — než objem naroste, sledovat součty v adminu.

## 3.55 Chybějící crony (welcome-emails, account-deletions, credit-payouts)

### Čtyři vrstvy, proč crony nikdy neběžely

Než se to rozchodilo, stálo v cestě tohle — a každá vrstva schovávala tu
další, takže oprava jedné nic nezměnila:

1. **Služby neměly připojený zdroj.** Cron schedule byl nastavený, ale
   Railway neměl co nasadit → *„There is no active deployment."*
2. **Coming-soon brána vracela na `/api/cron/*` 503.** Cron chodí bez
   session, takže ho odbavila jako kohokoliv jiného.
3. **V image chybí `curl`.** Nixpacks ho do Python image nedává.
4. **Dva různé zámky.** `booking-reminders` a `aftercare` hlídal
   `CRON_SECRET` přes Bearer, zbytek `RECONCILE_TOKEN` přes `X-Cron-Token`.
   Jeden spouštěč tak vždycky dva joby minul na 401. Dnes se bere obojí.

Tři joby navíc neměly v Railway nic, co by je spouštělo: `welcome-emails`,
`account-deletions` a `credit-payouts`.

**Nedělej pro ně novou službu.** Služba `inklink-cron` už existuje a spouští
`reconcile` — Railway v ní umí spustit jeden příkaz podle rozvrhu, a ten
příkaz může být klidně smyčka přes všechny čtyři joby.

**Nevolej crony curlem.** Image, kterou Nixpacks staví z tohohle repa, curl
neobsahuje — běh skončí na `curl: command not found`. Slouží k tomu
`scripts/run_crons.py`; Python v image je z definice.

Railway → služba → **Settings → Custom Start Command**:

```
python scripts/run_crons.py reconcile aftercare welcome-emails account-deletions credit-payouts
```

a u té třicetiminutové:

```
python scripts/run_crons.py booking-reminders
```

Jeden spadlý job nezastaví ostatní; nenulový návratový kód se vrátí až na
konci, aby Railway běh označil za neúspěšný. Odpovědi jobů se vypisují do
logu celé — je v nich `purged_count`, `paid`, `failed`, takže je poznat, co
job udělal, ne jen že doběhl.

Služba potřebuje ve **Variables** `RECONCILE_TOKEN`. Proměnné se v Railway
nedědí mezi službami, každá ho musí mít vlastní.

Po prvním běhu se koukni do **Deployments → logu** té služby. Čtyři řádky
`OK` znamenají hotovo. `account-deletions` navíc vrací `purged_count`,
`credit-payouts` vrací `paid` a `failed` — nenulové `failed` několik dní po
sobě znamená prázdný Stripe balance, ne chybu v kódu.

## 1.6 Web push — vygenerování klíčů

`PUSH_PUBLIC` a `PUSH_PRIVATE` nebyly nikdy nastavené, takže se notifikace
ukládaly a nikam nedoručovaly. Formát není libovolný a spletl se tu už
jednou — použij skript, ne ruční base64:

```bash
python3 scripts/gen_vapid.py
```

Vypíše obě proměnné rovnou ve tvaru pro Railway → služba **INKLINK** →
Variables. `PUSH_PRIVATE` nikam neposílej a ulož si ho do správce hesel.

`PUSH_PUBLIC` jde rovnou do prohlížeče jako `applicationServerKey`, proto
musí být base64url nekomprimovaného bodu bez zarovnání (87 znaků).
`PUSH_PRIVATE` čeká pywebpush jako base64url **řetězec** (43 znaků) —
dekódované bajty odmítne, a `except` v `send_push` tu chybu spolkne.

Ověření: `/__health` → `web_push_set: true`.

## 3.55.1 Souhlas klienta — klíč a cron

**`MEDICAL_NOTES_KEY` je povinný**, jinak formulář vrací 503 a upozornění se
neposílají. Vygeneruj ho jednou a **nikdy nepřegeneruj** — bez původního klíče
se už žádný podepsaný souhlas nedá přečíst:

```bash
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Do Railway jako `MEDICAL_NOTES_KEY`. Zálohuj ho mimo Railway.

Upozornění musí běžet často, jinak se do okna 5–20 minut před sezením
netrefí. Ve službě `inklink-cron-reminders` změň rozvrh na `*/5 * * * *` a
příkaz na:

```
python scripts/run_crons.py booking-reminders consent-nudge
```

`booking-reminders` je idempotentní (hlídá `reminder_sent_at`), takže mu
častější běh nevadí.

## 3.6 Doplatky tatérům za kredit (cron)

Posílá tatérům tu část zálohy, kterou klient zaplatil kreditem z poukazu.

```
15 4 * * *      # denně v 4:15 UTC
curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/credit-payouts
```

Bere dokončené rezervace s `platform_owes_artist_cents > 0` a `credit_paid_at
IS NULL`, na každou pošle Stripe Transfer na účet tatéra. `idempotency_key` je
odvozený od id rezervace, takže dvojí spuštění nepošle peníze dvakrát.

Response:
```json
{"ok": true, "paid": [12, 15], "paid_cents": 40000, "skipped": [], "failed": []}
```

Co hlídat:

| Pole | Znamená |
|---|---|
| `failed` | typicky nedostatek prostředků na balance — příští běh to dožene |
| `skipped` s `no_stripe_account` | tatér nemá napojený Stripe; dluh nezaniká, jen čeká |

Když `failed` neprázdné víc dní po sobě, dojdi se podívat na Stripe balance —
peníze za poukazy tam musí být dřív, než se z nich vyplácí.

## 5.6 Meta / Instagram — co bude chtít App Review

Import portfolia z Instagramu je **urychlovač onboardingu, ne nosná funkce**.
Bez nakonfigurované Mety se karta v `artist-setup` vůbec nezobrazí, takže
spuštění to neblokuje.

### Do Railway

| Proměnná | Kde ji vzít |
|---|---|
| `INSTAGRAM_APP_ID` | Use cases → Instagram API → **API setup with Instagram login** → *Instagram app ID*. **Ne** App settings → Basic — tam je Meta App ID a to je jiné číslo. |
| `INSTAGRAM_APP_SECRET` | tamtéž, *Instagram app secret* (do chatu ani do gitu nepatří) |
| `INSTAGRAM_TOKEN_KEY` | vygeneruj: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Šifruje uložené přístupové tokeny. **Bez něj se propojení vůbec nenabídne.** |

Kód volá `instagram.com/oauth/authorize`, takže musí dostat **Instagram**
app ID. S Meta App ID se přihlášení nerozjede a chybu uvidí až uživatel.

> **Pozor na kopírování.** 8. 9. 2026 skončilo v `INSTAGRAM_APP_ID`
> vedoucí rovnítko (`=2128923884358929`) — zkopírovalo se z řádku
> `INSTAGRAM_APP_ID=...`. Řetězec byl neprázdný, takže vše vypadalo
> nastavené, ale Instagram by `client_id==…` odmítl ještě před
> přihlášením. Od té doby `_instagram_enabled()` vyžaduje samé číslice
> a `/__health` hlásí `instagram_app_id_numeric` a `instagram_secret_len`
> (Meta dává 32 znaků). Ověř oboje po každé změně proměnných.

### Do nastavení aplikace u Mety

Musí sedět **přesně**, jinak Instagram vrátí chybu ještě před přihlášením:

```
OAuth redirect URI:        https://www.inklink.club/api/instagram/callback
Deauthorize callback:      https://www.inklink.club/api/instagram/deauthorize
Data deletion callback:    https://www.inklink.club/api/instagram/data-deletion
Privacy policy:            https://www.inklink.club/privacy
```

Callbacky pro Metu **procházejí coming-soon bránou** (viz `_GATE_OPEN_PREFIXES`)
— Meta je volá server na server bez session a za bránou by dostala 503.
Chráněné jsou podpisem `signed_request` přes app secret, ne bránou.

Hotovo k 8. 9. 2026: rozsah `instagram_business_basic` přidaný
(*Ready for testing*), všechny tři adresy uložené v *Business login
settings*, produkční endpointy ověřené (POST vrací 400
`invalid signed_request`, ne 404 ani 503). Zbývá App Review.

Smazání dat maže **token a záznam o importech, ne portfolio**. Fotky, které
si tatér přenesl, jsou jeho vlastní práce; žádost u Mety se týká propojení.

### Na co se to zasekne

- **Rozsah `instagram_business_basic` vyžaduje App Review.** Sám sobě
  portfolio natáhneš hned, ostatní tatéři až po schválení. Trvá to týdny —
  začít se musí dřív než den před spuštěním.
- **Reviewer se musí dostat na web.** Dokud běží `COMING_SOON`, uvidí
  waitlist. Pošli mu v žádosti odkaz s `?preview=<COMING_SOON_TOKEN>` nebo
  bránu na dobu review vypni.
- **Ověření firmy** (Business Verification) chce doklady k IČO.
  Podnikatelský účet je *InkLink* (`business_id=929249429732359`); k IČO
  29532744 patří jméno **Matěj Gajdoš** (OSVČ), ne „InkLink s.r.o."
- Tatér navíc potřebuje **profesionální Instagram účet** (Business/Creator),
  osobní nestačí. Je to v UI napsané, ale počítej s dotazy.

### Než se žádost podá

Reviewer si musí projít **celý flow sám**. Nejčastější důvod zamítnutí
není kód, ale to, že se reviewer nikam nedostane nebo nepochopí, co má
dělat. Bez těchhle čtyř věcí žádost nepodávej:

| | co | proč |
|---|---|---|
| 1 | `COMING_SOON_TOKEN` v Railway | Bez něj **neexistuje odkaz**, kterým reviewera pustíš dovnitř — uvidí waitlist. Guard je `if COMING_SOON_TOKEN and token == …`, prázdný token nepustí nikoho. |
| 2 | Testovací účet tatéra na InkLinku | Meta chce e-mail + heslo. Účet musí mít `is_artist=1`, jinak `/artist-setup` nic neukáže. |
| 3 | Business Verification | Chce doklady k IČO 29532744 (Matěj Gajdoš, OSVČ). Trvá to — začni dřív než zbytek. |
| 4 | Screencast | Musí být vidět **od přihlášení až po naimportovanou fotku**, ne jen výsledek. |

Reviewer používá **vlastní** profesionální Instagram účet — ten mu
neposkytuješ. Tvůj testovací účet je jen vstupenka do InkLinku.

### Text do žádosti

Meta chce anglicky. Konkrétně, bez marketingu — popiš data, ne přínos.

> **How will your app use instagram_business_basic?**
>
> InkLink is a booking platform for tattoo artists. Artists build a
> portfolio on their InkLink profile so clients can browse their work
> before booking.
>
> We use `instagram_business_basic` to read the artist's own Instagram
> username and their own media, and to show those photos in a picker.
> The artist selects which photos to copy into their InkLink portfolio.
> Nothing is read or imported without the artist explicitly choosing it,
> and we only ever access the account of the artist who connected it.
>
> We do not read other users' content, comments, messages, hashtags or
> insights. We request no other permission.

### Instrukce pro reviewera

Ať jsou doslovné a klikací. Reviewer nezná produkt.

```
1. Open <PREVIEW_LINK>  (this unlocks the pre-launch gate for 30 days)
2. Log in:  <TEST_EMAIL> / <TEST_PASSWORD>
3. Go to  https://www.inklink.club/artist-setup
4. Scroll to the "Instagram" card
5. Click "Connect Instagram"
6. Log in with a professional (Business or Creator) Instagram account
7. You return to /artist-setup — your username is shown and a grid of
   your Instagram photos loads
8. Tick one or more photos and click Import
9. The photos now appear in the portfolio on your public profile
10. "Disconnect" removes the token and the import records
```

`<PREVIEW_LINK>` je `https://www.inklink.club/?preview=<COMING_SOON_TOKEN>`.

### Kde to v kódu žije

| část | kde |
|---|---|
| karta v UI | `public/artist-setup.html`, `#igCard` (skrytá, dokud `/api/instagram/status` nevrátí `available`) |
| start OAuth | `GET /api/instagram/connect` — CSRF přes `state` v session |
| návrat | `GET /api/instagram/callback` → `?ig=ok\|state\|denied\|error\|unconfigured` |
| výpis médií | `GET /api/instagram/media` |
| import | `POST /api/instagram/import` |
| odpojení | `POST /api/instagram/disconnect` |

Chybové stavy se tatérovi ukazují jako flash hláška podle `?ig=`. **Query
musí být před fragmentem** — `#profile?ig=…` skončí v `location.hash`,
`location.search` je prázdný a hláška se nezobrazí vůbec. Bylo to tak
a hlídá to test `test_callback_puts_state_in_query_not_fragment`.

### Uložený token

Přístupový token je klíč od Instagram účtu tatéra, takže se v databázi
ukládá zašifrovaný (Fernet, prefix `v1:`) klíčem `INSTAGRAM_TOKEN_KEY`.
Meta se na uložení Platform Data ptá v Data Protection Assessment.

Klíč je **vlastní, ne `MEDICAL_NOTES_KEY`**. Ztráta klíče od souhlasů je
nevratná a právně drahá; ztráta tohohle znamená jen, že se tatéři propojí
znovu. Jeden sdílený klíč by ty dvě váhy spojil dohromady.

- Bez klíče se propojení nenabídne vůbec — `_instagram_enabled()` je
  `False` a `/__health` hlásí `instagram_token_key_set: false`. Ukládat
  token načisto jen proto, že proměnná chybí, je horší než funkci nemít.
- Záznamy bez prefixu `v1:` se čtou jako nešifrované, aby upgrade tiše
  neodpojil nikoho, kdo se propojil dřív.
- Špatný klíč vede na prázdný token, čitelnou chybu a nové propojení —
  ne na pád. V logu je jen `token decrypt failed`, nikdy hodnota.

## 5.7 Mapa tatérů

**API klíč nepotřebuje** — ale pozor, dlaždice se kvůli tomu jednou
měnily. CARTO (`basemaps.cartocdn.com`) svoje basemapy uzavřelo za klíč:
dlaždice se pořád načtou, jen přes ně jde vodoznak „API KEY REQUIRED",
takže se to nepozná ze status kódu, jedině okem. Mapa proto jede na
standardních dlaždicích OpenStreetMap (`tile.openstreetmap.org`), které
klíč nechtějí. Jsou světlé, takže je CSS filtr obrací
(`invert(1) grayscale(1)`) na tmavou mapu. Klíč nechce ani geokodér.

Podmínkou OSM je identifikující se provoz a atribuce — obojí splňujeme.
Kdyby provoz narostl, je to první místo, které narazí; pak je na řadě
placený dlaždicový poskytovatel.

Stejné dlaždice používá i `events.html` (dvě místa) — při změně je
potřeba upravit obojí.

Prázdná byla z jiného důvodu: `/api/artists/map` vrací jen tatéry s
`lat`/`lng`, server je uměl uložit, ale **formulář je nikdy neposílal** a
geokódování v projektu nebylo. Souřadnice teď dopočítá server sám z
adresy studia (nepovinné pole) nebo z města.

| | |
|---|---|
| geokodér | Nominatim (OpenStreetMap), bez klíče |
| cache | tabulka `geo_cache`, klíčem je text dotazu |
| kdy se volá | při uložení profilu, když nedorazí `lat`/`lng` |
| když selže | profil se uloží bez souřadnic, chyba jen do logu |

**Nominatim si klade podmínky:** identifikovat se v User-Agentu (posíláme
`InkLink/1.0 (+<APP_BASE_URL>)`) a nechodit častěji než jednou za vteřinu.
Profil se ukládá zřídka a opakované dotazy odchytí cache, takže se do
limitu vejdeme. Kdyby přibylo tatérů skokem, je to první místo, které
narazí — pak je na řadě Mapy.cz (lepší česká data, ale chce klíč).

Negativní odpověď se cachuje, výpadek geokodéru ne — jinak by jedna
nedostupnost zamkla adresu jako „neexistuje".

### Staré účty

Souřadnice se počítají při ukládání profilu, takže účty založené dřív
je nemají a na mapě nejsou. Dopočítá je:

```bash
railway run python scripts/backfill_geo.py            # jen vypíše
railway run python scripts/backfill_geo.py --apply    # zapíše
```

Bere jen tatéry bez souřadnic, takže se dá pouštět opakovaně — druhý běh
zpracuje jen ty, na kterých ten první selhal. Pauzu kvůli limitu
Nominatimu čeká jen tehdy, když se opravdu šlo na síť; deset tatérů
z Prahy tak stojí jeden dotaz.

Kdo adresu nevyplní, dostane souřadnice města, takže všichni z Prahy
sedí na jednom bodě. Mapa proto duplicitní souřadnice rozseje do spirály
(zlatý úhel, první kruh ~50 m), aby šlo kliknout na každého. Není to
skutečná poloha — u tatérů bez adresy ani být nemůže.

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
