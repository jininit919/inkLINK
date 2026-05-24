# InkLink Mobile (Capacitor) — vývoj iOS + Android

InkLink mobile app je Capacitor wrapper kolem stávajícího webu — sdílí kód,
deploy, design. iOS app je shellu kolem [www.inklink.club](https://www.inklink.club).

## Architektura

```
┌─────────────────────────┐
│  iOS App / Android APK  │  ← Capacitor wrapper
│   (Xcode / Studio)      │     + native plugins (push, share, haptics, camera)
└──────────┬──────────────┘
           │ WebView
           ▼
┌─────────────────────────┐
│  www.inklink.club       │  ← Railway (Flask + paper-mode UI)
│  /api/*                 │     beze změny — web a app sdílí backend
└─────────────────────────┘
```

## První instalace (jen jednou na novém Macu)

Capacitor projekt je v `mobile/` subdir (ne v root), ať Railway buildpack
správně rozpozná Python jako primary jazyk podle requirements.txt.

```bash
cd /Users/matejgajdos/Desktop/inklink/mobile
npm install
npx cap add ios
npx cap add android   # volitelně
npx cap sync ios
```

## Kam se aplikace připojuje

`capacitor.config.json` → `server.url` určuje, jaký web build mobile shell loaduje.

- **Production:** `https://www.inklink.club` (default)
- **Lokální dev test:** dočasně změň na `http://192.168.x.x:5002` (tvoje LAN IP)
  a v terminálu spusť `python3 server.py`, pak `npx cap sync ios`

```bash
# Po každé změně capacitor.config.json:
npx cap sync ios
npx cap sync android
```

## iOS workflow

### Otevřít Xcode

```bash
npx cap open ios
```

### První spuštění (simulator)

1. V Xcode top bar vyber iOS Simulator (např. iPhone 15 Pro)
2. `Cmd + R` → app se sestaví a spustí v simulátoru
3. Otevře se WebView s www.inklink.club v native shellu

### Spuštění na fyzickém iPhonu

1. Připoj iPhone přes USB, povol "Trust this computer" v iOS
2. Xcode → App target → Signing & Capabilities → vyber svůj Apple Developer team
3. Bundle ID `club.inklink.app` (musí být unique v tvém týmu)
4. Dropdown vlevo nahoře → vyber svůj iPhone
5. `Cmd + R` → app se sestaví, podepíše a nainstaluje
6. Na iPhonu: Settings → General → VPN & Device Management → "Trust [tvůj cert]"

### App Store Connect submission

1. **App Store Connect → My Apps → + → New App**
   - Platform: iOS
   - Name: **InkLink**
   - Bundle ID: `club.inklink.app`
   - SKU: `inklink-app`
2. Xcode → App target → Signing & Capabilities → tvůj Apple Developer team
3. Increment build number (Xcode → App target → General → Build: 2, 3, …)
4. **Product → Archive** → po dokončení Window → Organizer → vybrat archive →
   **Distribute App** → App Store Connect → Upload
5. App Store Connect: vyplň
   - **Description** (čeština): "Tetování v ČR — feed skic, mapa tatérů,
     bezpečná rezervace přes Stripe. Žádné DM ping-pongy."
   - **Keywords:** tetování, tattoo, rezervace, tatér, studio, booking
   - **Category:** Lifestyle (primary), Social Networking (secondary)
   - **Screenshots:** 6.5" (iPhone 14/15 Pro Max), 5.5" (iPhone 8 Plus)
     - Doporučené: feed, profil tatéra, mapa, sketch detail, booking flow
   - **Privacy Policy URL:** `https://www.inklink.club/privacy`
   - **Age Rating:** 17+ (tetování je "Realistic Violence + Frequent/Intense
     Mature/Suggestive Themes" minimum)
6. **TestFlight tab** → invite testers (internal beta = no review, external = 24 h)
7. **App Review:** 1–7 dní

### App Review — co Apple bude kontrolovat

1. **Guideline 3.1.5(a) — Real-World Services:** tetování JE real-world service,
   takže **Stripe Connect je explicit povolen** (ne in-app purchase). Apple
   nepožaduje 30 % komisi z bookingu (zákaz IAP platí jen pro digital goods).
2. **Guideline 4.2 — Minimum Functionality:** app musí nabízet víc než web.
   Plníme: push notifikace, native share sheet, haptics, camera access.
3. **Guideline 5.1.1 — Data Collection:** privacy policy nasměrovat na
   `/privacy` (už máme), Privacy Manifest přidáme v Xcode (App Store Connect
   bude požadovat seznam dat).

### Žádný RevenueCat / IAP

InkLink je marketplace pro real-world service (tetování). **Žádné digital
subscriptions, žádné virtual goods.** Stripe Connect zajišťuje booking platby
mezi klientem a tatérem — Apple nevyžaduje IAP.

(Pro porovnání: hear-me-out má RevenueCat pro "PRO subscription" = digital,
Apple bere 30 %. InkLink to nemá.)

## Android workflow

### Otevřít Android Studio

```bash
npx cap open android
```

### Signing key (jednou)

```bash
keytool -genkey -v -keystore ~/inklink-android-release.jks \
  -keyalg RSA -keysize 2048 -validity 10000 -alias inklink-release
```

Heslo ulož do 1Password. **Ztráta = nikdy nemůžeš updatovat app.**

Do `~/.gradle/gradle.properties` (NE commitovat):

```
INKLINK_KEYSTORE_FILE=/Users/matejgajdos/inklink-android-release.jks
INKLINK_KEYSTORE_PASSWORD=...
INKLINK_KEY_ALIAS=inklink-release
INKLINK_KEY_PASSWORD=...
```

### Build a release bundle

Android Studio → **Build → Generate Signed Bundle / APK → App Bundle** → vybrat
keystore → produkční build. `.aab` upload do Google Play Console.

## Native plugins — co je instalováno

| Plugin | Použití |
|---|---|
| `@capacitor/push-notifications` | iOS APNs + Android FCM, server-side push z VAPID stack |
| `@capacitor/haptics` | Haptic feedback na lajky / book confirm |
| `@capacitor/share` | Native share sheet pro sketch URL |
| `@capacitor/camera` | Capture / pick z galerie pro portfolio upload |
| `@capacitor/splash-screen` | Paper-mode splash (logo na off-white) |
| `@capacitor/status-bar` | Tmavý text na paper bg (style: DARK) |

JS bridge je v `public/native.js` — loaduje se na základních stránkách,
detekuje `Capacitor.isNativePlatform()` a wrapper-uje native volání tak, aby
web fungoval stejně bez Capacitor.

## Push notifikace — APNs setup

iOS push potřebuje **APNs certifikát** + **Push Notifications capability**:

1. Apple Developer Portal → Identifiers → `club.inklink.app` → Edit →
   zaškrtnout **Push Notifications**
2. Vytvořit APNs Key (jednou pro celý team): Keys → + → APNs → stáhnout `.p8`
   soubor (uložit do 1Password, nelze stáhnout znovu)
3. Backend (`server.py`) potřebuje doinstalovat `apns2` lib pro APNs push.
   Alternativa: jen registrace tokenu, push posílá samostatný service
4. V app `native.js` zavolá `PushNotifications.requestPermissions()` →
   `register()` → server uloží token do `push_subscriptions` (rozšíříme
   schema o `provider`: `web` / `apns` / `fcm`)

(Pro MVP může push běžet jen přes web VAPID na webu, native push doděláme
v druhé iteraci.)

## App ikony a splash — automatika přes @capacitor/assets

Source images jsou v `mobile/assets/`:

| Soubor | Rozměr | Použití |
|---|---|---|
| `icon.png` | 1024×1024 | iOS App Store icon (white bg, "ink" black blob) |
| `icon-foreground.png` | 1024×1024 | Android adaptive icon — foreground (transparent, jen logo) |
| `icon-background.png` | 1024×1024 | Android adaptive icon — background (paper #faf8f3 solid) |
| `splash.png` | 2732×2732 | Splash light mode — logo centered na paper bg |
| `splash-dark.png` | 2732×2732 | Splash dark mode (zatím stejné) |

Po `npx cap add ios` (a optionally `android`) spusť:

```bash
cd mobile
npm install                # nainstaluje @capacitor/assets
npm run assets:generate    # vygeneruje všechny iOS + Android velikosti
```

Tool přepočítá:
- iOS: `ios/App/App/Assets.xcassets/AppIcon.appiconset/` (9 velikostí) + `Splash.imageset/`
- Android: `android/app/src/main/res/mipmap-*/` (5 hustot) + `drawable-*/`

Po regeneraci ikon vždy spusť `npx cap sync` aby se to propíchlo do Xcode/Studio projektů.

### Regenerování ikon ze zdroje

Pokud chceš upravit logo (např. změnit barvu, scale), uprav soubory v `mobile/assets/`
nebo přegeneruj z původního zdroje:

```bash
# Source: ~/Downloads/inkLink mini.jpg (2000×2000 RGB)
# Skript: scripts/build_app_assets.py (regeneruje 5 souborů v mobile/assets/)
python3 scripts/build_app_assets.py
cd mobile && npm run assets:generate && npx cap sync
```

## Známé issues / TODO

- [ ] Safe-area inset CSS — notch + home indicator (theme.css)
- [ ] Backend `/api/native/register-push` endpoint pro APNs token
- [x] iOS App Icon resources (1024 + příslušné velikosti) — generuje `npm run assets:generate`
- [x] Splash screen image — paper bg, logo centered, light + dark variant
- [ ] Pull-to-refresh — Capacitor `@capacitor/app` background events
- [ ] Camera permission strings v `Info.plist` (NSCameraUsageDescription)
- [ ] Privacy Manifest (`PrivacyInfo.xcprivacy`) pro App Store
