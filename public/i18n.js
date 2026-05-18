// InkLink i18n — client-side translation
// Usage in HTML: <span data-i18n="key.name">fallback text</span>
//                <a data-i18n-attr="key:title,key2:aria-label" title="...">
//                <p data-i18n-html="key.with.html">
// Switcher:      InkLinkI18N.set('en')   /   InkLinkI18N.get()
(function () {
  const STORE_KEY = 'il_lang';
  const SUPPORTED = ['cs', 'en'];
  const FALLBACK = 'cs';

  // ── Dictionary ───────────────────────────────────────────────────────────────
  const STRINGS = {
    cs: {
      // NAV
      'nav.howItWorks': 'Jak to funguje',
      'nav.features':   'Funkce',
      'nav.pricing':    'Ceník',
      'nav.faq':        'FAQ',
      'nav.signIn':     'Přihlásit',
      'nav.start':      'Začít',

      // HERO
      'hero.tag':       'CZ · Tattoo Marketplace · Live',
      'hero.title1':    'PROHLÉDNI SKICY.',
      'hero.title2':    'NAJDI TATÉRA.',
      'hero.sub':       'Vyber si skicu nebo objev tatéra podle stylu a města. Rezervuj zálohou nebo celou částku předem.',
      'hero.ctaPrimary':'Prohlížet skicy',
      'hero.ctaMap':    'Mapa tatérů',

      // STATS
      'stats.artists':       'Tatérů',
      'stats.artistsSub':    'aktivních v ČR',
      'stats.sketches':      'Skic',
      'stats.sketchesSub':   'k rezervaci',
      'stats.commission':    'Provize',
      'stats.commissionSub': 'jen z bookingu',
      'stats.reminder':      'Připomínka',
      'stats.reminderSub':   'před termínem',

      // HOW IT WORKS
      'how.eyebrow':   'Jak to funguje',
      'how.title':     'Tři kroky od skicy k tetování.',
      'how.lead':      'Žádné scrollování přes Instagramy. Žádné nečitelné DM zprávy. Tetování si rezervuješ stejně snadno jako lístek do kina.',
      'how.s1Title':   'Najdi co se ti líbí',
      'how.s1Desc':    'Prohlížej feed skic, mapu nebo profily tatérů. Filtruj podle stylu, města, ceny. Lajkuj si oblíbené skicy nebo sleduj tatéry pro pozdější rezervaci.',
      'how.s2Title':   'Vyber termín',
      'how.s2Desc':    'Otevři tatérův kalendář, vyber volný blok a velikost (Mini 1 h · Velké 5 h · Celý den 8 h). Cena se počítá automaticky.',
      'how.s3Title':   'Plať bezpečně',
      'how.s3Desc':    'Záloha nebo celá částka předem přes Stripe. Peníze jsou chráněné a tatérovi se uvolní až poté, co oba potvrdíte dokončení tetování.',

      // FEATURES
      'feat.eyebrow':  'Co dostaneš',
      'feat.title':    'Vše pro jednu objednávku tetování na jednom místě.',
      'feat.lead':     'Od prvního prolistování skic až po push notifikaci 24 hodin před termínem. Vše navržené tak, ať klient i tatér ušetří půlhodinu DM korespondence.',
      'feat.f1Title':  'Feed skic',
      'feat.f1Desc':   'Curated feed dostupných skic. Filtruj podle stylu, města, ceny. Najdi to, co tě chytne.',
      'feat.f2Title':  'Mapa tatérů',
      'feat.f2Desc':   'Černobílá mapa s pinkami tatérů. Klikni a uvidíš jejich portfolio a hodnocení.',
      'feat.f3Title':  'Chráněná platba',
      'feat.f3Desc':   'Plať zálohu nebo celou částku předem přes Stripe. Peníze se tatérovi uvolní až po vzájemném potvrzení dokončení.',
      'feat.f4Title':  'Recenze a hodnocení',
      'feat.f4Desc':   'Recenze od reálných klientů po skutečném tetování. Žádné falešné hvězdičky.',
      'feat.f5Title':  'Připomínka 24 h před',
      'feat.f5Desc':   'E-mail i push notifikace den před termínem. Nezapomeneš.',
      'feat.f6Title':  'Kalendář export',
      'feat.f6Desc':   'Stáhni si .ics nebo si přidej InkLink kalendář do Apple / Google jako odběr.',
      'feat.f7Title':  'Lajky & uložené',
      'feat.f7Desc':   'Lajkuj skicy a vracej se k nim. Tvůj wishlist pro příští kus.',
      'feat.f8Title':  'Sdílení na Stories',
      'feat.f8Desc':   'Jedním klikem hotový obrázek pro Instagram Story s odkazem na skicu.',

      // SPLIT
      'split.eyebrow':       'Pro koho',
      'split.title':         'Dvě strany, jedna síť.',
      'split.clientSub':     'Klient',
      'split.clientTitle':   'Najdi si svého tatéra.',
      'split.clientL1':      '<strong>Browse zdarma</strong> — feed, mapa i profily otevřené i bez účtu.',
      'split.clientL2':      '<strong>Filtruj podle stylu</strong> — fineline, blackwork, traditional, realism…',
      'split.clientL3':      '<strong>Rezervuj v 60 vteřinách</strong> — záloha přes Stripe, potvrzení e-mailem.',
      'split.clientL4':      '<strong>Měj přehled</strong> — všechny rezervace, kalendář, recenze v jednom dashboardu.',
      'split.clientCta':     'Prohlížet feed',
      'split.artistSub':     'Tatér',
      'split.artistTitle':   'Dostaň bookingy, ne DMs.',
      'split.artistL1':      '<strong>Portfolio zdarma</strong> — nahraj skicy 1–4 foto, nastav cenu a styl.',
      'split.artistL2':      '<strong>Stripe Connect</strong> — peníze chodí přímo na tvůj účet po dokončení.',
      'split.artistL3':      '<strong>Earnings dashboard</strong> — co kolik vyneslo, co je v procesu, výplaty.',
      'split.artistL4':      '<strong>Žádné měsíční poplatky</strong> — platíš jen procento z toho, co skutečně vyděláš.',
      'split.artistCta':     'Stát se tatérem',

      // PRICING
      'pricing.eyebrow':     'Ceník',
      'pricing.title':       'Transparentně. Žádné překvapení.',
      'pricing.lead':        'Klienti za používání InkLinku neplatí nic. Tatéři platí jen procento z bookingu — žádné setup fees, žádné měsíční předplatné.',
      'pricing.clientTitle': 'Klient',
      'pricing.clientLead':  'Browse, filtruj, lajkuj a rezervuj — vše bez poplatku za InkLink.',
      'pricing.clientL1':    'Neomezené procházení skic a profilů',
      'pricing.clientL2':    'Rezervace zálohou nebo celou částkou předem',
      'pricing.clientL3':    'Peníze chráněné Stripem do dokončení tetování',
      'pricing.clientL4':    'Připomínka 24 h před termínem',
      'pricing.clientL5':    'Recenze a kalendář export',
      'pricing.clientCta':   'Procházet',
      'pricing.artistTitle': 'Tatér',
      'pricing.artistUnit':  'z bookingu',
      'pricing.artistLead':  'Žádné měsíční poplatky. Platíš jen z toho, co skutečně vyděláš.',
      'pricing.artistL1':    'Neomezené portfolio (skicy + hotové práce)',
      'pricing.artistL2':    'Stripe Connect — peníze přímo na tvůj účet',
      'pricing.artistL3':    'Kalendář s opakováním a slot managementem',
      'pricing.artistL4':    'Earnings dashboard a měsíční PDF reporty',
      'pricing.artistL5':    'E-mail + push notifikace o bookingech',
      'pricing.artistL6':    'Profil s mapou, rating, sdílení na Stories',
      'pricing.artistCta':   'Začít jako tatér',
      'pricing.featured':    'Doporučujeme',

      // TESTIMONIALS
      'test.eyebrow': 'Z první ruky',
      'test.title':   'Co o InkLinku říkají.',
      'test.t1Quote': 'Konečně mi rezervace nezabere týden DM ping-pongu. Vyber, zaplať, hotovo.',
      'test.t1Role':  'Klient · Praha',
      'test.t2Quote': 'Klient platí dopředu, peníze čekají na Stripe a uvolní se až po dokončení. Žádné no-showy, jistota pro obě strany.',
      'test.t2Role':  'Tatér · Brno',
      'test.t3Quote': 'Mapa a filtrace mi pomohly najít tatérku s blackwork stylem v Olomouci za 10 minut.',
      'test.t3Role':  'Klient · Olomouc',

      // FAQ
      'faq.eyebrow': 'FAQ',
      'faq.title':   'Často se ptáte.',
      'faq.q1':      'Kolik si InkLink bere z bookingu?',
      'faq.a1':      '8 % z každé úspěšné rezervace, žádné měsíční poplatky. Peníze nikdy nedržíme — chodí přímo na účet tatéra přes Stripe Connect.',
      'faq.q2':      'Můžu prohlížet bez účtu?',
      'faq.a2':      'Ano. Feed, mapa, profily tatérů i jednotlivé skicy jsou veřejně dostupné. Účet potřebuješ až ve chvíli, kdy chceš rezervovat termín nebo psát zprávu.',
      'faq.q3':      'Co když termín nestihnu nebo chci zrušit?',
      'faq.a3':      'Zrušení 48+ hodin před termínem je bez sankce — záloha se ti vrátí. Při zrušení do 48 hodin pravidla určuje konkrétní tatér ve svém profilu.',
      'faq.q4':      'Jak fungují platby a kdy dostane tatér peníze?',
      'faq.a4':      'Klient platí přes Stripe — buď zálohu (typicky 30 %, zbytek po dokončení) nebo celou částku předem. Peníze jsou chráněné a tatérovi se uvolní až ve chvíli, kdy obě strany potvrdí, že je tetování dokončené. Po uvolnění Stripe peníze obvykle převede na bankovní účet do 2 pracovních dnů.',
      'faq.q5':      'Co když nastane spor mezi klientem a tatérem?',
      'faq.a5':      'Pokud jedna ze stran nepotvrdí dokončení, peníze zůstanou pozdrženy a my pomůžeme situaci vyřešit. Stripe poskytuje dispute resolution pro problémy s kartou, my zase prostředkování mezi klientem a tatérem podle pravidel platformy.',
      'faq.q6':      'Funguje to na mobilu?',
      'faq.a6':      'Ano, InkLink je PWA — můžeš si ho přidat na home screen a chovat se jako nativní apka, včetně push notifikací. Žádné stahování z App Store.',
      'faq.q7':      'Komu napsat, když něco nefunguje?',
      'faq.a7':      'Napiš nám na <a href="mailto:contact@inklink.club" style="color:var(--tx-1);border-bottom:1px solid var(--tx-3)">contact@inklink.club</a>. Reagujeme do 24 hodin v pracovní dny.',

      // FINAL
      'final.title':       'READY TO INK?',
      'final.lead':        'Vytvoř si účet a najdi tatéra ještě dnes. Nebo se zaregistruj jako tatér a začni přijímat bookingy během odpoledne.',
      'final.ctaPrimary':  'Vytvořit účet',
      'final.ctaSecondary':'Jen se podívat',

      // FOOTER
      'footer.tag':       'Tattoo Booking Network · Praha, CZ',
      'footer.feed':      'Feed',
      'footer.map':       'Mapa',
      'footer.signIn':    'Přihlásit',
      'footer.privacy':   'Ochrana údajů',
      'footer.terms':     'Podmínky',
      'footer.contact':   'Kontakt',
    },

    en: {
      // NAV
      'nav.howItWorks': 'How it works',
      'nav.features':   'Features',
      'nav.pricing':    'Pricing',
      'nav.faq':        'FAQ',
      'nav.signIn':     'Sign in',
      'nav.start':      'Get started',

      // HERO
      'hero.tag':       'CZ · Tattoo Marketplace · Live',
      'hero.title1':    'BROWSE SKETCHES.',
      'hero.title2':    'FIND YOUR ARTIST.',
      'hero.sub':       'Pick a sketch you love or discover an artist by style and city. Book with a deposit or pay the full amount upfront.',
      'hero.ctaPrimary':'Browse sketches',
      'hero.ctaMap':    'Artist map',

      // STATS
      'stats.artists':       'Artists',
      'stats.artistsSub':    'active in CZ',
      'stats.sketches':      'Sketches',
      'stats.sketchesSub':   'available to book',
      'stats.commission':    'Commission',
      'stats.commissionSub': 'on booking only',
      'stats.reminder':      'Reminder',
      'stats.reminderSub':   'before appointment',

      // HOW IT WORKS
      'how.eyebrow':   'How it works',
      'how.title':     'Three steps from sketch to tattoo.',
      'how.lead':      'No scrolling through Instagrams. No confusing DM threads. Booking a tattoo as easy as buying a movie ticket.',
      'how.s1Title':   'Find what you love',
      'how.s1Desc':    'Browse the sketches feed, the map, or artist profiles. Filter by style, city, price. Like favorites or follow artists for later.',
      'how.s2Title':   'Pick a time slot',
      'how.s2Desc':    "Open the artist's calendar, choose a free block and a size (Mini 1 h · Large 5 h · Full day 8 h). Price is calculated automatically.",
      'how.s3Title':   'Pay safely',
      'how.s3Desc':    'Deposit or full upfront via Stripe. Money is protected and only released to the artist once both of you confirm the tattoo is done.',

      // FEATURES
      'feat.eyebrow':  'What you get',
      'feat.title':    'Everything for one tattoo order in one place.',
      'feat.lead':     'From the first sketch you scroll past to the push notification 24 hours before the appointment. Built so both client and artist save half an hour of DMs.',
      'feat.f1Title':  'Sketches feed',
      'feat.f1Desc':   'Curated feed of available sketches. Filter by style, city, price. Find what catches your eye.',
      'feat.f2Title':  'Artist map',
      'feat.f2Desc':   'Black and white map with artist pins. Click to see their portfolio and rating.',
      'feat.f3Title':  'Protected payment',
      'feat.f3Desc':   'Pay deposit or full amount upfront via Stripe. Money is released to the artist only after mutual confirmation of completion.',
      'feat.f4Title':  'Reviews and ratings',
      'feat.f4Desc':   'Reviews from real clients after actual tattoos. No fake stars.',
      'feat.f5Title':  '24h reminder',
      'feat.f5Desc':   'Email and push notification the day before. You won\'t forget.',
      'feat.f6Title':  'Calendar export',
      'feat.f6Desc':   'Download .ics or subscribe to your InkLink calendar in Apple / Google.',
      'feat.f7Title':  'Likes & saves',
      'feat.f7Desc':   'Like sketches and come back to them. Your wishlist for the next piece.',
      'feat.f8Title':  'Stories sharing',
      'feat.f8Desc':   'One click for a ready-made Instagram Story image linking to the sketch.',

      // SPLIT
      'split.eyebrow':       'For whom',
      'split.title':         'Two sides, one network.',
      'split.clientSub':     'Client',
      'split.clientTitle':   'Find your tattoo artist.',
      'split.clientL1':      '<strong>Browse free</strong> — feed, map, profiles open even without an account.',
      'split.clientL2':      '<strong>Filter by style</strong> — fineline, blackwork, traditional, realism…',
      'split.clientL3':      '<strong>Book in 60 seconds</strong> — deposit via Stripe, confirmation by email.',
      'split.clientL4':      '<strong>Stay on top</strong> — all bookings, calendar, reviews in one dashboard.',
      'split.clientCta':     'Browse the feed',
      'split.artistSub':     'Artist',
      'split.artistTitle':   'Get bookings, not DMs.',
      'split.artistL1':      '<strong>Free portfolio</strong> — upload 1–4 photos per sketch, set price and style.',
      'split.artistL2':      '<strong>Stripe Connect</strong> — money goes straight to your account after completion.',
      'split.artistL3':      '<strong>Earnings dashboard</strong> — what earned what, what\'s pending, payouts.',
      'split.artistL4':      '<strong>No monthly fees</strong> — you only pay a percentage of what you actually earn.',
      'split.artistCta':     'Become an artist',

      // PRICING
      'pricing.eyebrow':     'Pricing',
      'pricing.title':       'Transparent. No surprises.',
      'pricing.lead':        'Clients pay nothing for using InkLink. Artists only pay a percentage of each booking — no setup fees, no monthly subscription.',
      'pricing.clientTitle': 'Client',
      'pricing.clientLead':  'Browse, filter, like and book — all with no InkLink fee.',
      'pricing.clientL1':    'Unlimited browsing of sketches and profiles',
      'pricing.clientL2':    'Book with deposit or full amount upfront',
      'pricing.clientL3':    'Money protected by Stripe until tattoo completion',
      'pricing.clientL4':    'Reminder 24 h before appointment',
      'pricing.clientL5':    'Reviews and calendar export',
      'pricing.clientCta':   'Browse',
      'pricing.artistTitle': 'Artist',
      'pricing.artistUnit':  'per booking',
      'pricing.artistLead':  'No monthly fees. You only pay from what you actually earn.',
      'pricing.artistL1':    'Unlimited portfolio (sketches + finished work)',
      'pricing.artistL2':    'Stripe Connect — money straight to your account',
      'pricing.artistL3':    'Calendar with recurrence and slot management',
      'pricing.artistL4':    'Earnings dashboard and monthly PDF reports',
      'pricing.artistL5':    'Email + push notifications for bookings',
      'pricing.artistL6':    'Profile with map, rating, Stories sharing',
      'pricing.artistCta':   'Start as artist',
      'pricing.featured':    'Recommended',

      // TESTIMONIALS
      'test.eyebrow': 'First-hand',
      'test.title':   'What people say about InkLink.',
      'test.t1Quote': "Booking finally doesn't take a week of DM ping-pong. Pick, pay, done.",
      'test.t1Role':  'Client · Prague',
      'test.t2Quote': 'The client pays upfront, money sits at Stripe and is released only after completion. No no-shows, security for both sides.',
      'test.t2Role':  'Artist · Brno',
      'test.t3Quote': 'The map and filters helped me find a blackwork artist in Olomouc in 10 minutes.',
      'test.t3Role':  'Client · Olomouc',

      // FAQ
      'faq.eyebrow': 'FAQ',
      'faq.title':   'Common questions.',
      'faq.q1':      'How much does InkLink charge per booking?',
      'faq.a1':      '8 % of each successful booking, no monthly fees. We never hold the money — it goes directly to the artist\'s account via Stripe Connect.',
      'faq.q2':      'Can I browse without an account?',
      'faq.a2':      'Yes. Feed, map, artist profiles and individual sketches are publicly accessible. You need an account only when you want to book or message someone.',
      'faq.q3':      'What if I can\'t make it or want to cancel?',
      'faq.a3':      'Cancellation 48+ hours before the appointment is penalty-free — the deposit is refunded. For cancellations within 48 hours, the artist\'s own profile rules apply.',
      'faq.q4':      'How do payments work and when does the artist get paid?',
      'faq.a4':      'The client pays via Stripe — either a deposit (typically 30 %, the rest after completion) or the full amount upfront. Money is protected and released to the artist only when both parties confirm the tattoo is done. After release, Stripe usually transfers funds to the bank account within 2 business days.',
      'faq.q5':      'What happens if there\'s a dispute between client and artist?',
      'faq.a5':      'If one side does not confirm completion, the money stays on hold and we help resolve the situation. Stripe provides dispute resolution for card issues; we mediate between client and artist according to platform rules.',
      'faq.q6':      'Does it work on mobile?',
      'faq.a6':      'Yes, InkLink is a PWA — you can add it to your home screen and use it like a native app, including push notifications. No App Store download.',
      'faq.q7':      'Who do I contact if something doesn\'t work?',
      'faq.a7':      'Email us at <a href="mailto:contact@inklink.club" style="color:var(--tx-1);border-bottom:1px solid var(--tx-3)">contact@inklink.club</a>. We respond within 24 hours on business days.',

      // FINAL
      'final.title':       'READY TO INK?',
      'final.lead':        'Create an account and find your artist today. Or sign up as an artist and start receiving bookings this afternoon.',
      'final.ctaPrimary':  'Create account',
      'final.ctaSecondary':'Just have a look',

      // FOOTER
      'footer.tag':       'Tattoo Booking Network · Prague, CZ',
      'footer.feed':      'Feed',
      'footer.map':       'Map',
      'footer.signIn':    'Sign in',
      'footer.privacy':   'Privacy',
      'footer.terms':     'Terms',
      'footer.contact':   'Contact',
    },
  };

  // ── Core ─────────────────────────────────────────────────────────────────────
  function detect() {
    const saved = localStorage.getItem(STORE_KEY);
    if (saved && SUPPORTED.includes(saved)) return saved;
    const browser = ((navigator.language || FALLBACK).slice(0, 2)).toLowerCase();
    return SUPPORTED.includes(browser) ? browser : FALLBACK;
  }

  let lang = detect();

  function t(key) {
    const dict = STRINGS[lang] || STRINGS[FALLBACK];
    if (dict && dict[key] != null) return dict[key];
    const fallback = STRINGS[FALLBACK];
    if (fallback && fallback[key] != null) return fallback[key];
    return key;
  }

  function apply() {
    document.documentElement.lang = lang;
    document.querySelectorAll('[data-i18n]').forEach(el => {
      const key = el.getAttribute('data-i18n');
      el.textContent = t(key);
    });
    document.querySelectorAll('[data-i18n-html]').forEach(el => {
      const key = el.getAttribute('data-i18n-html');
      el.innerHTML = t(key);
    });
    document.querySelectorAll('[data-i18n-attr]').forEach(el => {
      const pairs = (el.getAttribute('data-i18n-attr') || '').split(',');
      pairs.forEach(p => {
        const idx = p.indexOf(':');
        if (idx < 0) return;
        const k = p.slice(0, idx).trim();
        const a = p.slice(idx + 1).trim();
        el.setAttribute(a, t(k));
      });
    });
    // Update language switcher UI if present
    document.querySelectorAll('[data-i18n-switch]').forEach(b => {
      b.classList.toggle('active', b.getAttribute('data-i18n-switch') === lang);
    });
    // Dispatch event so animations can re-trigger if needed
    document.dispatchEvent(new CustomEvent('il-i18n-applied', { detail: { lang } }));
  }

  function set(newLang) {
    if (!SUPPORTED.includes(newLang)) return;
    lang = newLang;
    try { localStorage.setItem(STORE_KEY, lang); } catch {}
    apply();
  }

  function get() { return lang; }

  window.InkLinkI18N = { t, set, get, apply, supported: SUPPORTED };

  // Apply on DOM ready (or immediately if already loaded)
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', apply);
  } else {
    apply();
  }
})();
