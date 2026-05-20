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
      'nav.signIn':     'přihlásit',
      'nav.start':      'začít',

      // HERO — editorial manifesto landing
      'hero.eyebrow':   'tetování · rezervace · inkoust co drží',
      'hero.line1':     'Prohlížej skicy,',
      'hero.line2':     'najdi tatéra.',
      'hero.line3':     '',
      'hero.lede':      'Marketplace pro tetování v Česku. Vyber si skicu nebo objev tatéra podle stylu — a rezervuj bezpečně přes Stripe. Žádné DM ping-pongy, žádné nejistoty.',
      'hero.ctaFeed':   'prohlédnout skicy',
      'hero.ctaMap':    'mapa tatérů',
      // (legacy keys pro případnou kompatibilitu)
      'hero.tag':       'CZ · Tattoo Marketplace · Live',
      'hero.title1':    'PROHLÉDNI SKICY.',
      'hero.title2':    'NAJDI TATÉRA.',
      'hero.sub':       'Vyber si skicu nebo objev tatéra podle stylu a města. Rezervuj zálohou nebo celou částku předem.',
      'hero.ctaPrimary':'Prohlížet skicy',

      // MANIFESTO — poetic stanzas (data-i18n-html podporuje inline tagy)
      'manifesto.eyebrow': 'manifest',
      'manifesto.title':   'proč InkLink',
      'manifesto.s1':      'Inkoust drží <b>navždy</b>.<br>Důvěra by měla taky.',
      'manifesto.s2':      'Tetování je rozhodnutí na celý život.<br>Rezervace by neměla být <b>loterie</b>.',
      'manifesto.s3':      'Tatér si vydělá. Klient zaplatí jen jednou.<br>Peníze čekají, dokud není <b>hotovo</b>.',
      'manifesto.s4':      'Žádné DM. Žádné zálohy v hotovosti.<br>Žádný <b>stres</b>.',

      // FINAL — closing CTA (nový landing)
      'final.claim':       'začni.',
      'final.ctaFeed':     'prohlédnout feed',
      'final.ctaArtist':   'stát se tatérem',

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
      'how.eyebrow':   'jak to funguje',
      'how.title':     'tři kroky',
      'how.lead':      'Žádné scrollování přes Instagramy. Žádné nečitelné DM zprávy. Tetování si rezervuješ stejně snadno jako lístek do kina.',
      'how.s1Title':   'vybereš si skicu',
      'how.s1Body':    'Z feedu, z mapy, z profilu tatéra. Filtruj podle stylu, města, ceny. Lajkuj si oblíbené.',
      'how.s1Desc':    'Prohlížej feed skic, mapu nebo profily tatérů. Filtruj podle stylu, města, ceny. Lajkuj si oblíbené skicy nebo sleduj tatéry pro pozdější rezervaci.',
      'how.s2Title':   'rezervuješ termín',
      'how.s2Body':    'Záloha nebo plná částka přes Stripe. Otevři kalendář tatéra, vyber volný blok, zaplať.',
      'how.s2Desc':    'Otevři tatérův kalendář, vyber volný blok a velikost (Mini 1 h · Velké 5 h · Celý den 8 h). Cena se počítá automaticky.',
      'how.s3Title':   'tetuješ se',
      'how.s3Body':    'Peníze tatéra čekají na Stripe — uvolní se, až oba potvrdíte dokončení. Jistota pro obě strany.',
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
      'final.eyebrow':     'připraven?',
      'final.lede':        'Bez registrace si můžeš prohlédnout feed. Účet potřebuješ až pro rezervaci.',

      // FOOTER
      'footer.tag':       'tattoo booking · praha · 2026',
      'footer.feed':      'feed',
      'footer.map':       'mapa',
      'footer.signIn':    'přihlásit',
      'footer.privacy':   'privacy',
      'footer.terms':     'terms',
      'footer.contact':   'kontakt',

      // STUDIOS — veřejná stránka studia
      'studio.pageTitle':   'Studio — InkLink',
      'studio.metaDesc':    'Tetovací studio na InkLinku',
      'studio.loading':     'Načítám studio…',
      'studio.notFound':    'Studio nenalezeno.',
      'studio.loadError':   'Nepodařilo se načíst studio.',
      'studio.address':     'Adresa:',
      'studio.city':        'Město:',
      'studio.website':     'Web:',
      'studio.phone':       'Tel:',
      'studio.photos':      'Galerie',
      'studio.team':        'Tým',
      'studio.noMembers':   'Studio nemá zatím žádné členy.',
      'studio.viewProfile': 'Profil →',
      'studio.adminBadge':  'Admin',
      'studio.memberBadge': 'Člen',

      // STUDIO ADMIN
      'studioAdmin.pageTitle':       'Moje studio — InkLink',
      'studioAdmin.loading':         'Načítám…',
      'studioAdmin.loadError':       'Nepodařilo se načíst studio.',
      'studioAdmin.subtitle':        'Správa studia — členové, pozvánky a profil',
      'studioAdmin.viewPublic':      'Zobrazit veřejnou stránku',
      'studioAdmin.team':            'Tým',
      'studioAdmin.makeAdmin':       'Předat admin',
      'studioAdmin.remove':          'Odebrat',
      'studioAdmin.pendingInvites':  'Čekající pozvánky',
      'studioAdmin.noPending':       'Žádné čekající pozvánky.',
      'studioAdmin.expires':         'Vyprší',
      'studioAdmin.cancel':          'Zrušit',
      'studioAdmin.inviteHeading':   'Pozvat nového tatéra',
      'studioAdmin.inviteEmail':     'E-mail tatéra',
      'studioAdmin.sendInvite':      'Odeslat',
      'studioAdmin.profile':         'Profil studia',
      'studioAdmin.name':            'Název',
      'studioAdmin.description':     'Popis',
      'studioAdmin.address':         'Adresa',
      'studioAdmin.city':            'Město',
      'studioAdmin.country':         'Země',
      'studioAdmin.website':         'Web',
      'studioAdmin.phone':           'Telefon',
      'studioAdmin.logo':            'Logo URL',
      'studioAdmin.save':            'Uložit změny',
      'studioAdmin.dangerZone':      'Nebezpečná zóna',
      'studioAdmin.leaveDesc':       'Odejít ze studia. Pokud jsi jediný admin a ve studiu jsou další členové, nejdřív předej admin práva.',
      'studioAdmin.leaveStudio':     'Opustit studio',

      // STUDIO CREATE
      'studioCreate.pageTitle':      'Založit studio — InkLink',
      'studioCreate.loading':        'Načítám…',
      'studioCreate.loadError':      'Něco se pokazilo. Zkus to znovu.',
      'studioCreate.notArtistTitle': 'Nejprve se staň tatérem',
      'studioCreate.notArtistBody':  'Studio může založit jen tatér. Vytvoř si profil tatéra a vrať se sem.',
      'studioCreate.becomeArtist':   'Stát se tatérem',
      'studioCreate.title':          'Založit studio',
      'studioCreate.lede':           'Studio seskupuje tvůj tým tatérů pod jednu značku. Jako zakladatel(ka) jsi admin — pozveš ostatní e-mailem.',
      'studioCreate.infoTitle':      'Co se nemění: ',
      'studioCreate.infoBody':       'Stripe účet, payouts a rezervace zůstávají u každého tatéra zvlášť. Studio jen seskupuje portfolio a má veřejnou stránku.',
      'studioCreate.name':           'Název studia *',
      'studioCreate.description':    'Krátký popis',
      'studioCreate.city':           'Město',
      'studioCreate.country':        'Země',
      'studioCreate.address':        'Adresa',
      'studioCreate.website':        'Web',
      'studioCreate.submit':         'Založit studio',

      // INVITE LANDING
      'invite.pageTitle':    'Pozvánka do studia — InkLink',
      'invite.loading':      'Načítám pozvánku…',
      'invite.notFound':     'Pozvánka nenalezena.',
      'invite.loadError':    'Nepodařilo se načíst pozvánku.',
      'invite.heading':      'Pozvánka do studia',
      'invite.invitesYou':   'tě zve, ať se přidáš do týmu.',
      'invite.emailTarget':  'Adresát:',
      'invite.accept':       'Přijmout',
      'invite.decline':      'Odmítnout',
      'invite.note':         'Tvůj Stripe účet, payouts a rezervace zůstávají tvé. Studio jen seskupuje portfolio a má veřejnou stránku.',
      'invite.declined':     'Pozvánka odmítnuta.',
      'invite.goHome':       'Domů',
    },

    en: {
      // NAV
      'nav.howItWorks': 'How it works',
      'nav.features':   'Features',
      'nav.pricing':    'Pricing',
      'nav.faq':        'FAQ',
      'nav.signIn':     'sign in',
      'nav.start':      'get started',

      // HERO — editorial manifesto
      'hero.eyebrow':   'tattoo · booking · ink that lasts',
      'hero.line1':     'Browse sketches,',
      'hero.line2':     'find your artist.',
      'hero.line3':     '',
      'hero.lede':      "Tattoo marketplace for Czechia. Browse sketches or discover an artist by style — and book safely via Stripe. No DM ping-pong, no guesswork.",
      'hero.ctaFeed':   'browse sketches',
      'hero.ctaMap':    'artist map',
      // (legacy)
      'hero.tag':       'CZ · Tattoo Marketplace · Live',
      'hero.title1':    'BROWSE SKETCHES.',
      'hero.title2':    'FIND YOUR ARTIST.',
      'hero.sub':       'Pick a sketch you love or discover an artist by style and city. Book with a deposit or pay the full amount upfront.',
      'hero.ctaPrimary':'Browse sketches',

      // MANIFESTO
      'manifesto.eyebrow': 'manifesto',
      'manifesto.title':   'why InkLink',
      'manifesto.s1':      'Ink lasts <b>forever</b>.<br>Trust should too.',
      'manifesto.s2':      'A tattoo is a lifelong decision.<br>Booking should not be a <b>lottery</b>.',
      'manifesto.s3':      'The artist gets paid. The client pays just once.<br>Funds wait until it is <b>done</b>.',
      'manifesto.s4':      'No DMs. No cash deposits.<br>No <b>stress</b>.',

      // STATS
      'stats.artists':       'Artists',
      'stats.artistsSub':    'active in CZ',
      'stats.sketches':      'Sketches',
      'stats.sketchesSub':   'available to book',
      'stats.commission':    'Commission',
      'stats.commissionSub': 'on booking only',
      'stats.reminder':      'Reminder',
      'stats.reminderSub':   'before appointment',

      // HOW IT WORKS — 3 kroky (new landing)
      'how.eyebrow':   'how it works',
      'how.title':     'three steps',
      'how.lead':      'No scrolling through Instagrams. No confusing DM threads. Booking a tattoo as easy as buying a movie ticket.',
      'how.s1Title':   'pick a sketch',
      'how.s1Body':    'From the feed, the map, or an artist profile. Filter by style, city, price. Like your favorites.',
      'how.s1Desc':    'Browse the sketches feed, the map, or artist profiles. Filter by style, city, price. Like favorites or follow artists for later.',
      'how.s2Title':   'book a slot',
      'how.s2Body':    'Deposit or full payment via Stripe. Open the artist calendar, pick a free block, pay.',
      'how.s2Desc':    "Open the artist's calendar, choose a free block and a size (Mini 1 h · Large 5 h · Full day 8 h). Price is calculated automatically.",
      'how.s3Title':   'get inked',
      'how.s3Body':    "The artist's money waits at Stripe — released once you both confirm completion. Safety for both sides.",
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
      'final.eyebrow':     'ready?',
      'final.claim':       'begin.',
      'final.lede':        'You can browse the feed without an account. An account is only required to book.',
      'final.ctaFeed':     'browse feed',
      'final.ctaArtist':   'become an artist',

      // FOOTER
      'footer.tag':       'tattoo booking · prague · 2026',
      'footer.feed':      'feed',
      'footer.map':       'map',
      'footer.signIn':    'sign in',
      'footer.privacy':   'privacy',
      'footer.terms':     'terms',
      'footer.contact':   'contact',

      // STUDIOS — public studio page
      'studio.pageTitle':   'Studio — InkLink',
      'studio.metaDesc':    'Tattoo studio on InkLink',
      'studio.loading':     'Loading studio…',
      'studio.notFound':    'Studio not found.',
      'studio.loadError':   'Could not load studio.',
      'studio.address':     'Address:',
      'studio.city':        'City:',
      'studio.website':     'Web:',
      'studio.phone':       'Phone:',
      'studio.photos':      'Gallery',
      'studio.team':        'Team',
      'studio.noMembers':   'Studio has no members yet.',
      'studio.viewProfile': 'Profile →',
      'studio.adminBadge':  'Admin',
      'studio.memberBadge': 'Member',

      // STUDIO ADMIN
      'studioAdmin.pageTitle':       'My studio — InkLink',
      'studioAdmin.loading':         'Loading…',
      'studioAdmin.loadError':       'Could not load studio.',
      'studioAdmin.subtitle':        'Studio management — members, invites and profile',
      'studioAdmin.viewPublic':      'View public page',
      'studioAdmin.team':            'Team',
      'studioAdmin.makeAdmin':       'Transfer admin',
      'studioAdmin.remove':          'Remove',
      'studioAdmin.pendingInvites':  'Pending invites',
      'studioAdmin.noPending':       'No pending invites.',
      'studioAdmin.expires':         'Expires',
      'studioAdmin.cancel':          'Cancel',
      'studioAdmin.inviteHeading':   'Invite a new artist',
      'studioAdmin.inviteEmail':     'Artist email',
      'studioAdmin.sendInvite':      'Send',
      'studioAdmin.profile':         'Studio profile',
      'studioAdmin.name':            'Name',
      'studioAdmin.description':     'Description',
      'studioAdmin.address':         'Address',
      'studioAdmin.city':            'City',
      'studioAdmin.country':         'Country',
      'studioAdmin.website':         'Website',
      'studioAdmin.phone':           'Phone',
      'studioAdmin.logo':            'Logo URL',
      'studioAdmin.save':            'Save changes',
      'studioAdmin.dangerZone':      'Danger zone',
      'studioAdmin.leaveDesc':       'Leave the studio. If you are the only admin and other members remain, transfer admin role first.',
      'studioAdmin.leaveStudio':     'Leave studio',

      // STUDIO CREATE
      'studioCreate.pageTitle':      'Create studio — InkLink',
      'studioCreate.loading':        'Loading…',
      'studioCreate.loadError':      'Something went wrong. Try again.',
      'studioCreate.notArtistTitle': 'Become an artist first',
      'studioCreate.notArtistBody':  'Only artists can create a studio. Set up your artist profile and come back.',
      'studioCreate.becomeArtist':   'Become an artist',
      'studioCreate.title':          'Create studio',
      'studioCreate.lede':           'A studio groups your tattoo team under one brand. As the founder you become admin — invite others by email.',
      'studioCreate.infoTitle':      'What stays unchanged: ',
      'studioCreate.infoBody':       'Each artist keeps their own Stripe account, payouts and bookings. The studio only groups portfolios and has a public page.',
      'studioCreate.name':           'Studio name *',
      'studioCreate.description':    'Short description',
      'studioCreate.city':           'City',
      'studioCreate.country':        'Country',
      'studioCreate.address':        'Address',
      'studioCreate.website':        'Website',
      'studioCreate.submit':         'Create studio',

      // INVITE LANDING
      'invite.pageTitle':    'Studio invite — InkLink',
      'invite.loading':      'Loading invite…',
      'invite.notFound':     'Invite not found.',
      'invite.loadError':    'Could not load invite.',
      'invite.heading':      'Studio invite',
      'invite.invitesYou':   'is inviting you to join the team.',
      'invite.emailTarget':  'Sent to:',
      'invite.accept':       'Accept',
      'invite.decline':      'Decline',
      'invite.note':         'Your Stripe account, payouts and bookings stay yours. The studio just groups portfolios and has a public page.',
      'invite.declined':     'Invite declined.',
      'invite.goHome':       'Home',
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
