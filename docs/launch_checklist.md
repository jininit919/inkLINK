# InkLink — Launch Readiness Checklist

**Cíl:** dostat aplikaci do stavu kdy prvních 10 tatérů + 50 klientů může používat, aniž by něco tiše selhávalo.

**Odhad času:** ~2 hodiny tvé práce (bez čekání na Stripe verifikace).

---

## PŘED SPUŠTĚNÍM — Env vars

### Railway → protective-inspiration → INKLINK → Variables

Aktuální stav (co je nastaveno):

| Var | Status |
|---|---|
| `DATABASE_URL` | ✅ Postgres shared |
| `SECRET_KEY` | ✅ |
| `APP_BASE_URL` | ✅ https://www.inklink.club |
| `STRIPE_SECRET_KEY` | ⚠ **test mode** (`sk_test_...`) — přepni na `sk_live_...` až po prvním E2E testu |
| `STRIPE_PUBLISHABLE_KEY` | ⚠ **test mode** (`pk_test_...`) — přepni na `pk_live_...` současně s secret |
| `STRIPE_PUBLIC_KEY` | ⚠ **duplicit** s PUBLISHABLE_KEY — smaž ho po nasazení opraveného server.py (čte oba) |
| `STRIPE_WEBHOOK_SECRET` | ✅ (whsec_...) — regeneruj v Stripe dashboardu **až přepneš na live** |
| `ENABLE_DEPOSIT_PI` | ✅ 1 — deposit PI flow aktivní |
| `RECONCILE_TOKEN` | ✅ 64-char random |
| `CRON_SECRET` | ✅ |
| `SENTRY_DSN` | ✅ |
| `R2_*` (5 vars) | ✅ Cloudflare storage |
| `RESEND_FROM` | ✅ contact@inklink.club |
| `RESEND_API_KEY` | ❌ **CHYBÍ** — bez toho nejdou žádné emaily (viz sekci níže) |
| `ADMIN_USERNAME` | ✅ MGart |
| `VERIFY_EMAIL` | ✅ 1 |
| `APNS_*` | ❌ chybí — push pro iOS neaktivní (můžeš odložit) |
| `VAPID_*` | ✅ web push funguje |

### Musíš přidat / opravit

1. **`RESEND_API_KEY`** — otevři https://resend.com/api-keys
   - New API Key → Full access → doména `inklink.club` nebo `inklink.cz`
   - Zkopíruj klíč `re_...`
   - Railway → Variables → New Variable → `RESEND_API_KEY=re_...`
   - Deploy
   - **Test:** projdi registrací nového klienta → měl by dostat welcome email do minuty

2. **`ENABLE_DIAG`** — **nesmí být set na 1** v produkci. Zkontroluj že tam není. Diag endpointy budou vracet 404.

---

## STRIPE — přechod z test do live

**Až chceš spustit reálné platby** (ne teď, počkej na první tatér):

1. Stripe dashboard → **přepni z Test mode / Sandbox na Live mode** (toggle vpravo nahoře)
2. **Developers → API keys → Live** — zkopíruj `sk_live_...` a `pk_live_...`
3. **Developers → Webhooks → Live** — vytvoř nový endpoint:
   - URL: `https://www.inklink.club/api/stripe/webhook`
   - Events: `account.updated`, `payment_intent.succeeded`, `payment_intent.payment_failed`, `charge.refunded`, `charge.dispute.created`
   - Zkopíruj Signing secret `whsec_...`
4. **Connect → Settings** — v live mode musíš znovu opt-in:
   - Onboarding options → povol **Card payments** a **Transfers**
   - Save
5. Railway env → přepiš:
   - `STRIPE_SECRET_KEY` → `sk_live_...`
   - `STRIPE_PUBLISHABLE_KEY` → `pk_live_...`
   - `STRIPE_WEBHOOK_SECRET` → nový `whsec_...` z live webhooku
6. Deploy
7. **Prvního tatéra osobně veď onboardingem** — bude to jeho reálný Stripe účet, reálné bankovní spojení
8. Prvního klienta ať zaplatí **jen 100 Kč zálohu**, refunduj do 5 minut — otestování celého cyklu s minimálním rizikem

---

## CRON JOBS na Railway

Aktuální (viz `docs/PROD_OPS.md`):
- ✅ `inklink-cron-reconcile` — daily, běží
- ✅ `inklink-cron-reminders` — hourly, běží

### Ještě přidat (pokud chceš):

3. **Welcome emails cron** — už kód existuje, jen chybí Railway cron service:
   ```
   Schedule: 0 9 * * *   (denně v 9:00 UTC)
   Command:  curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/welcome-emails
   ```

4. **Account deletion cron** (GDPR — 30-day soft delete purge):
   ```
   Schedule: 30 3 * * *  (denně v 3:30 UTC)
   Command:  curl -sf -H "X-Cron-Token: $RECONCILE_TOKEN" https://www.inklink.club/api/cron/account-deletions
   ```

Setup přes Railway New Service (viz PROD_OPS.md § 3 pro postup).

---

## PŘED PRVNÍM UŽIVATELEM

- [ ] Web load-test — otevři inklink.club v incognito, projdi:
  - [ ] Landing → registrace → email verify (pokud VERIFY_EMAIL=1)
  - [ ] Feed loading, mapa loading
  - [ ] Profile artist → book slot (v demo módu bez Stripe zatím)
  - [ ] My-bookings — vidíš booking, cancel funguje
  - [ ] Zprávy — pošli sobě zprávu
  - [ ] Cookie banner se objeví jen jednou (opraveno)
- [ ] Mobile check — otevři na telefonu (iOS Safari, Android Chrome)
  - [ ] Nav responsive
  - [ ] Modaly se zobrazují správně
  - [ ] Touch gestures fungují
- [ ] Právní check
  - [ ] `/privacy` a `/terms` s IČO 29532744 ✅ (hotovo)
  - [ ] Cookie consent banner ✅ (hotovo)
  - [ ] GDPR export endpoint `/api/me/export` ✅ (hotovo)
  - [ ] Soft account deletion `/api/me/delete` ✅ (hotovo)

---

## MONITORING

- **Sentry:** https://sentry.io → tvůj project. Sleduj denně první týden po launchi.
- **Stripe dashboard:** Payments + Connect. Denní kontrola disputes/refunds.
- **Railway logs:** `railway logs` nebo dashboard → INKLINK service → Logs. Watch for `[ERROR]` patterns.
- **Admin dashboard:** `/admin` — telemetry events, refund requests, KPIs.

---

## PRVNÍ TATÉR — playbook

1. **Osobně** ho zaregistruj v jeho přítomnosti (video call nebo osobně)
2. Pomoc s vyplněním profilu (portfolio 3+ fotek, styly, bio)
3. Doprovod přes Stripe Connect KYC (live mode — reálný ID, reálný účet)
4. Ověř `stripe_charges_enabled=1` v `/admin` po dokončení
5. Nastav mu 1 test slot na dnešek/zítřek za 100 Kč zálohu
6. Ty jako klient rezervuj z jiného účtu, zaplať real kartou
7. **Ihned refunduj** přes admin panel
8. Ověř že Stripe dashboard ukazuje refund, telemetry_events má correct záznamy

Pokud tohle projde bez chyby, můžeš dělat marketing a přijmout další tatéry.

---

## STAV OSTATNÍCH SPRINTŮ

Podle [docs/roadmap.md](roadmap.md):

- **Sprint 1 LITE** ✅ Hotovo (deposit PI, hardening, tests)
- **Sprint 2** ⏸ Booking + calendar — až budeš mít 3+ tatéry co si stěžují na kalendář, tak to postavíme
- **Sprint 3 CRM** ⏸ Až budeš mít studio co říká "chci CRM"
- **Sprint 4 Účetnictví** ⏸ Až budeš mít 5+ tatérů co potřebují ISDOC export
- **Sprint 5 Comm (email-only)** ⏸ Welcome sequence už funguje; SMS defer
- **Sprint 6 Analytics** ⏸ Až budeš mít data
- **Sprint 7 Inventory** ⏸ Skip until demand
- **Sprint 8 Marketing** ⏸ Až Sprint 3 + 5 hotové

**Nestavěj další features před prvním real bookingem.** Real users ti řeknou co postavit.

---

## SUMMARY — Co dodělat před launch

**Musí (Blocker):**
1. Přidej `RESEND_API_KEY` do Railway env (bez toho žádné emaily)
2. Ověř že `ENABLE_DIAG` NENÍ nastavené v prod

**Silně doporučeno:**
3. Přidej welcome-emails + account-deletions cron services na Railway
4. Otestuj golden path v incognio (registrace, feed, booking demo)
5. Otestuj na mobilu

**Až budeš mít prvního tatéra:**
6. Přepni Stripe na live keys (viz sekci Stripe výše)
7. Aktivuj Connect v live mode dashboardu
8. Doprovod tatéra osobně přes onboarding

**Nikdy:**
- Nespouštět live keys před testovaným E2E flow s reálným tatérem
- Nesmazat cookie consent banner (compliance)
- Nespustit prod bez Sentry monitoringu
