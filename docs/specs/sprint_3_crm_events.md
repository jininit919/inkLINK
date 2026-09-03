# Sprint 3 — CRM klientů + veřejné akce

**Status:** Implementováno. Navazuje na Sprint 2
([sprint_2_booking_calendar.md](sprint_2_booking_calendar.md)), je závislost
pro Sprint 5 (aftercare) a Sprint 6 (analytika studia) podle
[../roadmap.md](../roadmap.md).

---

## 1. Architektonické rozhodnutí: klienta vlastní tatér, ne studio

Roadmapa říká „studio-scoped". **Nepostaveno tak — schválně.** Scopování přes
`studio_id` má dvě fatální vady:

- sólo tatéři (dnes prakticky všichni, včetně živé demo tatérky) by dostali
  403 přes `require_tier`,
- zdědilo by to nestabilitu `bookings.studio_id`, který se do Sprintu 3
  nevyplňoval při INSERTu (viz §2).

Místo toho: **`clients.artist_id` je vlastník, viditelnost napříč studiem se
počítá za běhu** — tatér vidí klienta, když `artist_id = já` NEBO
`artist_id ∈ (členové mého studia)`.

Dělící čára je **peníze vs. vztah**: `bookings` a `economics_snapshots` nesou
`studio_id` a nikdy se nehýbou, takže studiu při odchodu tatéra zůstane celá
účetní historie a ztratí jen měkkou CRM vrstvu. To je správná ztráta — v ČR je
tatér ve studiu typicky OSVČ na křesle a je správcem svých vlastních
klientských dat. Automatický převod klientely na studio je zpracování, ke
kterému klient nedal souhlas.

**Revisit trigger:** až si studio reálně vyžádá sdílenou klientelu jako
majetek studia (typicky zaměstnanecký model místo pronájmu křesla).

### Dva helpery, ne jeden

| Helper | Použití |
|---|---|
| `_crm_visible_artist_ids(conn, uid)` | splice do `IN (...)`, **jediný** volající — seznam klientů |
| `_crm_get_client(conn, uid, cid)` | **všechno ostatní**, `None` → volající vrací 404 |

**Invariant: každý potomek se autorizuje přes svého klienta, nikdy přes vlastní
id.** `DELETE /api/client-notes/<nid>` jde přes note → `client_id` →
`_crm_get_client`. Právě tam CRM reálně teče — ne v seznamu, který si každý
pamatuje otestovat.

**404, ne 403**, když je zdroj neviditelný. 403 je orákulum na existenci.

**`is_admin_user` neobchází `_crm_get_client`.** Platformní admin čte
telemetrii; klientela tatéra mu tudy přístupná být nemá. Je na to test.

## 2. Fáze 0 — `studio_id` a plumbing

`bookings.studio_id` se **nevyplňoval při INSERTu** — jen backfillem
v `init_db()` při startu procesu. Důsledky:

- nové rezervace měly NULL až do restartu,
- backfill zpětně přepsal historii tatéra, když vstoupil do studia,
- a nikdy ji neuklidil, když odešel.

Opraveno: `studio_id` se skládá do už existujícího dotazu na tatéra jedním
poddotazem (žádný round trip navíc), backfill smazán. `studio_id` je **snapshot
místa konání**, ne živý odkaz. Follow-up dědí `studio_id` rodiče — série
zůstane tam, kde začala.

Při té příležitosti opraven závod na `bookings` id
(`SELECT ... ORDER BY id DESC LIMIT 1` → `last_insert_rowid()`/`lastval()`)
a doplněny chybějící indexy `bookings(artist_id)`, `bookings(client_id)`,
`events(date)`.

## 3. Data model

| Tabulka | Poznámka |
|---|---|
| `clients` | `artist_id` vlastník, `user_id` NULLable (walk-in). Částečný unikátní index `(artist_id, user_id) WHERE user_id IS NOT NULL` |
| `client_notes` | bez soft delete |
| `tattoo_records` | `booking_id` NULLable — práce z doby před InkLinkem |

`name/email/phone` na `clients` se čtou **jen když `user_id IS NULL`**; jinak
je zdrojem pravdy `users`, jinak by tatér volal telefon, který si klient před
půl rokem změnil.

**Bez `lifetime_value_czk`** (roadmapa ji chce): denormalizovaná cache bez
invalidace je přesně ta chyba, co byla na `bookings.studio_id`. `SUM()` při
čtení.

**Bez soft delete poznámek** (roadmapa ho chce): měkce smazaná poznámka je
pořád PII v databázi, tedy pravý opak toho, co má výmaz udělat.

## 4. Zdravotní poznámky — postaveno a zase odstraněno

Sprint 3 původně obsahoval šifrované zdravotní poznámky (Fernet, klíč z env,
auditní log každého přístupu). **Odstraněno na vyžádání ještě před tím, než to
kdokoli použil** — v produkci ani lokálně nezůstal jediný řádek dat.

Důvod je správný a stojí za zapsání: údaje o zdraví jsou **zvláštní kategorie
podle čl. 9 GDPR**. Šifrování a auditní log jsou z toho ta nejsnazší část;
těžké je právní podloží (výslovný souhlas s jasným rozsahem), posouzení vlivu
(DPIA), režim uchovávání a to, že se závazek objeví v zásadách ochrany údajů,
kde ho pak musíme reálně plnit. Pro produkt bez jediného uživatele téhle
funkce je to čistá zátěž.

**Co z toho zůstalo:** `bookings.internal_note` a `client_notes` jsou volný
text, kam tatér technicky napsat cokoli může. Proto je výmaz klienta obě pole
čistí (viz §5). Rozdíl je v tom, že se jako zdravotní databáze **netváříme**
a nesbíráme je strukturovaně — což je přesně ta hranice, která odděluje běžný
provozní záznam od zpracování zvláštní kategorie údajů.

**Revisit trigger:** až o to tatéři sami řeknou a bude čas na DPIA. Kód je
v historii gitu ve Sprintu 3 (commity `edc8e6f` a `04f2155`).

## 5. GDPR: výmaz jednoho klienta

Podle vzoru `_anonymize_user` — vynulovat PII, řádek nechat, účetnictví
zachovat (10letá retence dle zákona o účetnictví).

| Tabulka | Co se stane |
|---|---|
| `clients` | PII pryč **včetně `user_id`** (ponechaný odkaz re-identifikuje přes `users`) |
| `client_notes` | **tvrdě smazat** |
| `tattoo_records` | **řádek se rozdělí** — `body_location`/`description`/`healed_photo`/`aftercare_status` pryč; `booking_id`/`artist_id`/`session_date`/`price_czk` zůstává |
| `bookings` | vyčistit `design_note` a `internal_note` |

`bookings.internal_note` je soukromé pole tatéra, kam se reálně píšou věci
jako „volat po 18:00" — nejvyšší hodnota za dva řádky kódu.

**Fotky hojení se mažou i z úložiště** (`delete_upload()`, nový vedle
`save_upload()`). Vynulovat cestu v DB nestačí — objekt v R2 zůstane veřejně
adresovatelný pro kohokoli s URL.

⚠️ **`ON DELETE CASCADE` tu nefunguje.** SQLite bez `PRAGMA foreign_keys=ON`
cizí klíče nevynucuje a kód ho nikde nezapíná. Potomci se mažou explicitně.

Oprávnění: vlastník **nebo admin studia**, ne řadový člen. Vidět cizí klientelu
a nevratně ji smazat jsou dvě různá oprávnění. Potvrzení vypsáním `VYMAZAT`.

## 6. Slučování klientů

V1 vyžaduje **shodný `artist_id`** — napříč tatéry je „čí je pak klient"
otázka vlastnictví dat, ne UI, a špatná odpověď je incident.

Odmítá se i sloučení dvou klientů navázaných na **různé účty**: to nejsou
duplicity, ale dva lidé, a sloučení by jednomu z nich přepsalo historii pod
rukama.

## 7. Události

**Nestavěla se nová stránka — `/events` už veřejným kalendářem akcí byla.**
Tabulky, API, měsíční mřížka, mapa i filtry existovaly (fork), jen s nulou
záznamů a nepropojené. Byla ale **rozbitá, ne zamčená**: `init()` dělal
`if (!res.ok) redirect('/login')`, jenže `/api/me` vrací nepřihlášenému
**200 s tělem `null`**, ne 401 — redirect se nespustil a kód spadl na
`me.display_name`. Stránka je přitom v sitemapě s `priority 0.7`.

- `GET /api/events` rozšířen o `from`/`to`/`artist_id`, **ne nový
  `/api/events/calendar`** — vedle už žije `/api/calendar` a dvě čtecí cesty
  znamenají dvě místa k opravě. Rozsah je navíc podmínka, ne kosmetika:
  `date LIKE '2026-09%'` utne každý týden přes přelom měsíce v půlce.
- `GET /api/profile/<u>/events` zveřejněn.
- `calendar.html` — akce jako fialové pilulky vedle blokací. Protože se tahají
  po týdnech, `shiftWeek()`/`goToToday()` volají `loadWeek()`, ne `render()`.
  V mřížce tatéra **jen jeho vlastní** akce; cizí patří na `/events`, tady by
  z kalendáře udělaly nástěnku.
- `PRESET_GENRES` byly **hudební žánry** (Pop, Hip Hop, K-Pop, Metal, Jazz) —
  pozůstatek forku. Nahrazeny typy tatérských akcí. Sloupec v DB zůstává
  `genre` (~23 referencí, 0 řádků), přejmenováno jen v UI.

## 8. Endpointy

`GET/POST /api/clients` · `GET/PATCH /api/clients/<id>` ·
`POST /api/clients/<id>/notes` ·
`PATCH/DELETE /api/client-notes/<nid>` ·
`POST /api/clients/<id>/tattoo-records` · `PATCH/DELETE /api/tattoo-records/<rid>` ·
`GET /api/clients/<id>/export` · `POST /api/clients/<id>/erase` ·
`POST /api/clients/<id>/merge`

**Routováno přes aktéra, ne přes tenanta** — `GET /api/clients`, ne
`GET /api/studios/<id>/clients`. Maže to celou třídu IDOR (nikdo nepodstrčí
cizí `studio_id`) a odpadá potřeba chybějícího „člen tohohle studia" guardu.

**CRM se negatuje přes `require_tier`** — 403uje každého sólo tatéra. (Ten
helper navíc plete „nemá studio" s „nemá tarif"; sólo tatér je `free`, ne bez
tarifu.)

Per-klient export je vedle platformního `/api/me/export` schválně: ten
vydává sám uživatel o sobě, tenhle vydává tatér o svém klientovi. Jsou to
dvě různé role správce a dva různé rozsahy dat.

## 9. Testy

48 nových (celkem 136).

- **`CrossStudioLeakageTests`** — tabulkově přes **každý** endpoint s
  `<client_id>`/`<note_id>`/`<record_id>`: sólo tatér → 404, cizí studio → 404,
  nepřihlášený → 401, **platformní admin → 404**, kolega ze studia → 200.
- **`CrmScopeTests`** — odchod ze studia viditelnost odebere, vstup ji přidá.
  Tenhle test obhajuje počítanou viditelnost proti denormalizovanému sloupci.
- **`ClientAutoLinkTests`** — první rezervace řádek založí, druhá už ne, jiný
  tatér = vlastní řádek.
- **`TattooRecordTests`** — cizí `booking_id` se nedá navázat; PATCH nesahá na
  nezmíněná pole; kolega ze studia vidí, ale nemaže.
- **`ClientGdprTests`** — výmaz vynuluje PII i `user_id`, poznámky tvrdě smaže,
  rezervace a ceny nechá, `internal_note` vyčistí a `tattoo_records` rozdělí;
  řadový člen studia → 403, admin studia → 200.
- **`StudioIdInsertTests`** (přepis) — `studio_id` při INSERTu, NULL u sólo,
  follow-up dědí rodiče, pozdější vstup do studia historii nepřepíše.
- **`PublicEventsTests`** — veřejné čtení, rozsah přes přelom měsíce,
  `is_own`/`is_saved` u nepřihlášeného nesmí vyjít `true` kvůli `uid == 0`.

## 10. Vědomé škrty (v1)

Cachovaná `lifetime_value_czk`; `GET /api/studios/<id>/clients`; gating přes
`require_tier`; soft delete poznámek; slučování napříč tatéry;
`aftercare_followup_status` jako stavový automat (patří do Sprintu 5);
samostatný `/api/clients/<id>/history` (složeno do detailu); nová veřejná
kalendářová stránka.
