# Sprint 2 — Booking system + calendar

**Status:** Implemented. Navazuje na Sprint 1 ([stripe.md](stripe.md)), je tvrdá
závislost pro Sprinty 3, 4, 6 a 8 podle [../roadmap.md](../roadmap.md).

---

## 1. Architektonické rozhodnutí: žádné `artist_availability`

Roadmapa navrhovala novou tabulku `artist_availability` (opakující se okna) +
odvozování rezervovatelných slotů on-demand. **Nepostaveno — schválně.**

Průzkum ukázal, že `POST /api/slots` (`create_slot`) **už týdenní opakování
umí** (`recur: {days, until}`, materializuje až 200 řádků dopředu). To řeší
praktický problém, kvůli kterému `artist_availability` v roadmapě bylo
("nechci klikat každý týden"), jen jinou cestou — předgenerováním řádků místo
odvozování za běhu.

Postavit vedle toho druhou reprezentaci dostupnosti by znamenalo:
- dva zdroje pravdy o tom, co je volno → klasický zdroj drift bugů,
- invalidaci krátkodobé cache při každém create/edit/delete slotu, rezervace
  i blokace.

Pro produkt s nulou reálných rezervací to je čistá režie. **Revisit trigger:**
až bude tatér reálně narážet na limit 200 opakování, nebo až bude potřeba
měnit opakující se dostupnost zpětně pro už vygenerované týdny.

Blokace volna ale **nešly do tabulky `slots`** jako další status — mají vlastní
tabulku. Hlavní práce (kontrola překryvů v rozsahu *tatéra*, ne jednoho
`slot_id`) je potřeba tak jako tak a `slots` nese 5 sloupců o ceně, které pro
"tady nejsem" nedávají smysl.

## 2. Data model

| Tabulka | Změna |
|---|---|
| `users` | `cancel_refund_full_hours`, `cancel_refund_half_hours` (NULL = globální 96/48) |
| `slots` | `buffer_before_minutes`, `buffer_after_minutes` |
| `bookings` | `parent_booking_id`, `session_number`, `buffer_before_minutes`, `buffer_after_minutes`, `internal_note` |
| `artist_blocked_time` | **nová** — artist_id, start_at, end_at, reason |
| `booking_reschedule_requests` | **nová** — modelovaná podle `refund_requests` |

Buffery se na rezervaci **snapshotují** při vytvoření: pozdější změna slotu
nesmí retroaktivně měnit kolizní pravidla už existujících rezervací.

## 3. Kolize

- `_padded_overlap()` — překryv včetně bufferů obou stran. Stávající
  `_ranges_overlap()` zůstal nedotčený pro své bezbufferové volající.
- `_artist_blocked_overlap()` — kontrola proti blokacím **v rozsahu tatéra**,
  napříč všemi jeho sloty.
- Obojí běží v `create_booking`, `update_booking` i při přesunu.
- Buffer se smí přetáhnout přes hranice slotu — jinak by úklidový buffer
  potichu ukrajoval tatérovi poslední rezervovatelný čas dne.

**Mimo rozsah:** kolize slot-vs-slot (tatér si dnes může založit dva
překrývající se bloky a oba jsou rezervovatelné). Existující mezera, sahat na
ten invariant je větší a rizikovější změna, roadmapa ji neřeší.

## 4. Přesun rezervace

`PATCH /api/bookings/<id>/reschedule` + `GET /api/reschedule-requests` +
`POST /api/reschedule-requests/<rid>/decide`.

| Aktér | Lhůta | Chování |
|---|---|---|
| Tatér | kdykoli | přesun hned (rozšíření důvěry, kterou už měl přes `PATCH /api/bookings/<id>`) |
| Klient | ≥ 48 h | přesun hned |
| Klient | < 48 h | vznikne `pending` žádost, časy rezervace se **nemění** |

- **Validace kolizí běží vždy**, bez ohledu na aktéra i lhůtu.
- Při schvalování se kolize kontroluje **znovu** — mezi podáním a rozhodnutím
  se cíl mohl obsadit.
- **Přesun nemění cenu**, jen čas. Chrání obě strany před překvapivou změnou
  ceny a nevyžaduje přepočet celé pricing/economics mašinérie.
- **Žádný nový booking status.** "Má čekající žádost o přesun" je ortogonální
  k platebnímu/plnicímu stavu, který řídí `BOOKING_TRANSITIONS` — `confirmed`
  rezervace s čekající žádostí je pořád `confirmed`. Stejný princip jako
  `refund_requests`, které `bookings.status` taky nesahají.

## 5. Multi-session

`POST /api/bookings/<id>/follow-up`, jen tatér. Dítě série:
- `deposit_cents = 0` (záloha se platila u prvního sezení), doplatek běžnou
  cestou přes `/complete`,
- `parent_booking_id` ukazuje **vždy na první sezení** — třetí sezení se
  naváže na první, řetěz se neplete do hloubky,
- zrušení dítěte vrací 0 Kč automaticky (deposit × pct = 0), rodiče nesáhne.

**Vypuštěno z v1:** full-prepaid přes celou sérii (roadmapa ho zmiňuje jako
konfigurovatelný). Vyžaduje znát počet sezení dopředu a rozpočítat jednu
Stripe platbu přes N ještě neexistujících řádků — bez ověřené poptávky to je
zbytečná složitost v platebním kódu. Taky vypuštěn `client_note` (duplicita
s `design_note`, který už klient píše i edituje).

## 6. České svátky

Balíček `holidays` (`holidays.country_holidays('CZ')`), ne hardcoded seznam —
Velikonoční pondělí je pohyblivé a seznam by se musel ručně aktualizovat každý
rok. Import je líný a chyba se polyká: chybějící balíček nesmí shodit
zakládání termínů, jen přijdeme o varování.

**Varujeme, neblokujeme.** Čeští tatéři nemají zákonnou povinnost mít o svátku
zavřeno a část jich naopak o svátcích chce termíny navíc (klienti mají volno).
`POST /api/slots` vrací `holiday_warnings`, frontend je ukáže ve flash hlášce.

## 7. Čas: jednočasová platforma

Frontend posílá časy jako pražský wall-clock bez offsetu a DB je tak ukládá.
Server je ale porovnával proti `datetime.utcnow()` (skutečné UTC), takže
**všechny "kolik hodin před termínem" kontroly byly posunuté o pražský offset**
(+1 h zima / +2 h léto) ve prospěch klienta — včetně živých storno lhůt ze
Sprintu 1 a kontroly "termín není v minulosti".

Opraveno `_prague_now_naive()` (wall-clock proti wall-clocku) a sdíleným
`_naive_dt()` (nahradil tz-normalizaci duplikovanou na 4 místech).

**Předpoklad:** InkLink je jednočasová (CZ) platforma. Při expanzi mimo jedno
časové pásmo je tohle místo, které se musí přepsat na skutečné tz-aware
ukládání — `PLATFORM_TZ` v `server.py` je vstupní bod.

Opraven i frontendový protějšek: `calendar.html`'s `fmtDate()` používal
`toISOString()`, což lokální půlnoc převedlo na UTC a u kladného offsetu
vrátilo předchozí den — týdenní mřížka řadila termíny o sloupec vedle a
"dnes" se po 22:00 předvyplnilo na včerejšek.

## 8. Frontend

- **`calendar.html`** — přepínač "Volný termín / Blokace volna" ve stejném
  sheetu, buffery pod "Pokročilé", blokace se v mřížce renderují přerušovaně
  a bez ceny.
- **`artist-setup.html`** — dvě pole pro storno lhůty (prázdné = platformní
  default). Odstraněn osiřelý blok správy slotů/kalendáře (~303 řádků), který
  se zbytečně fetchoval při každém načtení a byl nedosažitelný.
- **`my-bookings.html`** — tlačítko "Přesunout" + modal s výběrem bloku a času,
  inline Schválit/Zamítnout pro tatéra, "+ Další sezení", indikátor série.
  Z `#editBookModal` zmizela úprava času (všechny přesuny jednou cestou) a
  modal dostal chybějící CSS — **bez něj se zobrazoval natrvalo** jako 271px
  blok uprostřed stránky.

## 9. Testy

25 nových (celkem 126): `BufferCollisionTests`, `BlockedTimeTests`,
`ReschedulePolicyTests`, `MultiSessionTests`, `CancellationPolicyOverrideTests`,
`CzechHolidayWarningTests`, `PragueTimeTests`.

`CancellationPolicyOverrideTests` je nejdůležitější regrese sprintu — pin na
to, že tatér **bez** override dostane přesně stávající chování ze Sprintu 1.

Testy počítají časy ze `server._prague_now_naive()`, ne z `datetime.now()` —
server proti pražskému wall-clocku validuje "termín není v minulosti", takže
test musí počítat ze stejné osy, ať běží na jakémkoli stroji.
