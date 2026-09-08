/**
 * InkLink mobile bottom-nav (Instagram-style, role-aware).
 *
 * Auto-mountuje fixní spodní lištu na mobilech (<768px). Top `.nav-icons`
 * se na mobilu schovají, logo a další top elementy zůstanou.
 *
 * Použití na stránce:
 *   <script src="/mobile-nav.js"></script>
 *   <script>InkLinkMobileNav.init();</script>
 *
 * Layout podle role (5 slotů, center=primary akce):
 *   Tatér   : ⌂ Feed · ▦ Kalendář · [+ Přidat] · ✉ Zprávy · ◉ Profil
 *   Klient  : ⌂ Feed · ♥ Lajknuté · 🔍 Hledat · ✉ Zprávy · ◉ Profil
 *   Neauth  : stejný layout jako klient; chráněné kliky → /login
 *
 * Notifikační badge je nalepený na profile ikonu (vpravo nahoře).
 * Zprávy mají vlastní badge.
 */

(() => {
  if (window.InkLinkMobileNav) return;

  const CSS = `
  .il-mnav{position:fixed !important;top:auto !important;bottom:0 !important;left:0 !important;right:0 !important;z-index:9999 !important;background:rgba(250,248,243,0.96) !important;backdrop-filter:blur(12px);border-top:1px solid var(--border,#d4cfbf);display:none;height:calc(62px + env(safe-area-inset-bottom) + 12px) !important;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;-webkit-tap-highlight-color:transparent;padding:0 0 calc(env(safe-area-inset-bottom) + 12px) 0 !important;margin:0 !important}
  .il-mnav-grid{display:grid;grid-template-columns:repeat(5,1fr);height:100%}
  .il-mnav-item{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;color:var(--txt3,#5a5a5a);text-decoration:none;cursor:pointer;font-size:10px;letter-spacing:0.05em;text-transform:uppercase;background:none;border:none;font-family:inherit;position:relative;padding:8px 4px}
  .il-mnav-item .ico{line-height:1;display:flex;align-items:center;justify-content:center}
  .il-mnav-item .ico svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
  .il-mnav-item .lbl{font-size:9px;letter-spacing:0.06em;color:var(--txt3,#5a5a5a);white-space:nowrap}
  .il-mnav-item.active{color:var(--txt,#0a0a0a)}
  .il-mnav-item.active .lbl{color:var(--txt,#0a0a0a)}
  .il-mnav-item:active .ico{transform:scale(0.92)}
  .il-mnav-item.primary{justify-content:flex-end;padding-bottom:8px}
  .il-mnav-item.primary .ico-circle{position:absolute;top:-14px;left:50%;margin-left:-23px;width:46px;height:46px;border-radius:50%;background:var(--txt,#0a0a0a);color:var(--bg,#faf8f3);display:flex;align-items:center;justify-content:center;box-shadow:0 4px 14px rgba(20,16,8,0.18);transition:transform 0.15s,box-shadow 0.15s;overflow:hidden}
  .il-mnav-item.primary .ico-circle.bristol-plus{font-family:'Bristol','Caveat',cursive;font-size:42px;line-height:1;padding:0}
  .il-mnav-item.primary .ico-circle.bristol-plus > span{display:block;line-height:1;transform:translateY(0.02em)}
  .il-mnav-item.primary .ico-circle svg{width:24px;height:24px;stroke:var(--bg,#faf8f3);fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
  .il-mnav-item.primary:active .ico-circle{transform:scale(0.92);box-shadow:0 2px 8px rgba(20,16,8,0.10)}
  .il-mnav-item.primary .lbl{color:var(--txt,#0a0a0a);font-weight:500}
  .il-mnav-badge{position:absolute;top:6px;right:calc(50% - 18px);min-width:14px;height:14px;border-radius:7px;background:var(--txt,#0a0a0a);border:1.5px solid var(--bg,#faf8f3);font-size:9px;color:var(--bg,#faf8f3);display:none;align-items:center;justify-content:center;padding:0 3px;font-weight:700;line-height:1}
  .il-mnav-badge.show{display:flex}
  .il-mnav-dot{position:absolute;top:8px;right:calc(50% - 14px);width:8px;height:8px;border-radius:50%;background:var(--txt,#0a0a0a);border:1.5px solid var(--bg,#faf8f3);display:none}
  .il-mnav-dot.show{display:block}

  #il-abar{display:flex;gap:2px;align-items:center;margin-left:18px;
    font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;flex-wrap:nowrap;min-width:0}
  #il-abar a{display:inline-flex;align-items:center;gap:7px;padding:7px 12px;border-radius:7px;
    color:var(--txt3,#5a5a5a);text-decoration:none;font-size:12px;letter-spacing:0.04em;
    white-space:nowrap;transition:background 0.15s,color 0.15s}
  #il-abar a:hover{background:var(--bg3,#ede8db);color:var(--txt,#0a0a0a)}
  #il-abar a.active{background:var(--txt,#0a0a0a);color:var(--bg,#faf8f3)}
  #il-abar svg{width:15px;height:15px;flex-shrink:0}
  nav.il-has-abar > .nav-logo{flex:0 0 auto !important}
  nav.il-has-abar > #il-abar{margin-right:auto}
  /* Úzký desktop: popisky pryč, ikony zůstanou — jinak lišta vytlačí ikony
     vpravo mimo obrazovku. */
  @media(max-width:1180px){
    #il-abar a span{display:none}
    #il-abar a{padding:7px 9px}
  }
  /* Fallback pro stránku bez navu. */
  #il-abar.standalone{position:fixed;top:0;left:0;right:0;z-index:9998;justify-content:center;
    margin-left:0;background:rgba(250,248,243,0.96);backdrop-filter:blur(12px);
    border-bottom:1px solid var(--border,#d4cfbf);padding:8px 16px}
  body.has-abar nav{top:46px !important}
  body.has-abar{padding-top:46px}
  @media(max-width:768px){
    #il-abar{display:none !important}
    body.has-abar{padding-top:0}
    body.has-abar nav{top:0 !important}
    .il-mnav{display:block}
    body{padding-bottom:calc(80px + env(safe-area-inset-bottom))}
    nav .nav-icons,
    nav .nav-links{display:none !important}
    nav > .nav-icon{display:none !important}
    /* Skryjeme "+ Přidat" tlačítka — primary button v bottom navu je nahradí */
    #navAddBtn,
    #mainAddBtn{display:none !important}
  }
  `;

  const HOME_CSS = `
  /* Stránky si .nav-icon většinou stylují samy; messages.html ne. Sázíme
     jen to, bez čeho by ikona nebyla vidět — rozměry nechává na stránce. */
  #il-home{display:inline-flex;align-items:center;justify-content:center;
    color:var(--txt3,#5a5a5a);text-decoration:none;cursor:pointer}
  #il-home:hover{color:var(--txt,#0a0a0a)}
  /* Když ikona sedí přímo v navu, odstrčí se doprava sama — ne každá
     stránka má logo s flex:1 (invite.html ho nemá a ikona se nalepila
     rovnou na něj). Uvnitř .nav-icons to nechceme, tam patří ke svým. */
  nav > #il-home{margin-left:auto}
  #il-home svg{width:18px;height:18px;stroke:currentColor;fill:none;
    stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
  `;

  function injectHomeCSS() {
    if (document.getElementById('il-home-css') ||
        document.getElementById('il-mnav-css')) return;
    const s = document.createElement('style');
    s.id = 'il-home-css';
    s.textContent = HOME_CSS;
    document.head.appendChild(s);
  }


  const NAV_CSS = `
  /* Horní ikony vykresluje jedna definice, ne dvacet ručně psaných.
     Rozešly se: /my-bookings mělo na dvou stránkách zakládací ikonu
     a na třech kalendář, hledání volalo funkci, která existuje jen ve
     feedu, a avatar někde otevíral menu a jinde vedl rovnou na profil. */
  #il-navicons{display:flex;gap:6px;align-items:center;margin-left:auto}
  #il-navicons .nav-icon{width:34px;height:34px;display:flex;align-items:center;
    justify-content:center;cursor:pointer;color:var(--txt3,#5a5a5a);border-radius:8px;
    background:none;border:none;padding:0;transition:color .15s,background .15s;
    text-decoration:none;position:relative}
  #il-navicons .nav-icon:hover{color:var(--txt,#0a0a0a);background:var(--bg3,#ede8db)}
  #il-navicons .nav-icon svg{width:18px;height:18px;stroke:currentColor;fill:none;
    stroke-width:1.6;stroke-linecap:round;stroke-linejoin:round}
  #il-navicons .nav-avatar{width:30px;height:30px;border-radius:50%;
    background:var(--bg4,#e3ddca);border:1.5px solid var(--red,#1a1a1a);display:flex;
    align-items:center;justify-content:center;font-size:11px;color:var(--red2,#0a0a0a);
    cursor:pointer;margin-left:2px;letter-spacing:.05em;overflow:hidden;flex-shrink:0}
  #il-navicons .nav-avatar img{width:100%;height:100%;object-fit:cover}
  #il-account{position:relative}
  #il-menu{position:absolute;top:calc(100% + 8px);right:0;background:var(--bg2,#f5f1e8);
    border:1px solid var(--border2,#a8a399);border-radius:10px;min-width:210px;padding:7px;
    display:none;flex-direction:column;gap:1px;box-shadow:0 16px 40px rgba(20,16,8,.18);
    z-index:300}
  #il-menu.open{display:flex}
  #il-menu a,#il-menu button{padding:9px 11px;font-family:inherit;font-size:12.5px;
    letter-spacing:.02em;color:var(--txt2,#2a2a2a);border-radius:7px;display:flex;
    align-items:center;gap:10px;cursor:pointer;background:none;border:none;
    text-align:left;width:100%;text-decoration:none}
  #il-menu a:hover,#il-menu button:hover{background:var(--bg3,#ede8db);color:var(--txt,#0a0a0a)}
  #il-menu svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.6;
    stroke-linecap:round;stroke-linejoin:round;color:var(--txt3,#5a5a5a);flex-shrink:0}
  #il-menu hr{border:none;border-top:1px solid var(--border,#d4cfbf);margin:4px 2px}
  @media(max-width:768px){ #il-navicons{display:none !important} }
  `;

  function injectCSS() {
    if (document.getElementById('il-mnav-css')) return;
    const s = document.createElement('style');
    s.id = 'il-mnav-css';
    s.textContent = CSS + HOME_CSS + NAV_CSS;
    document.head.appendChild(s);
  }

  function isActive(href) {
    const path = location.pathname;
    if (href === '/') return path === '/' || path === '';
    if (href.startsWith('/profile/')) return path.startsWith('/profile/');
    return path === href || path.startsWith(href + '/');
  }

  let cachedMe = undefined;
  async function getMe() {
    if (cachedMe !== undefined) return cachedMe;
    try {
      const r = await fetch('/api/me');
      cachedMe = r.ok ? await r.json() : null;
    } catch { cachedMe = null; }
    return cachedMe;
  }

  function svgIcon(id) {
    return `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
  }

  async function fetchCount(url) {
    try {
      const r = await fetch(url);
      if (!r.ok) return 0;
      return (await r.json()).count || 0;
    } catch { return 0; }
  }

  function setBadge(id, count) {
    const badge = document.getElementById(id);
    if (!badge) return;
    if (count > 0) {
      badge.classList.add('show');
      badge.textContent = count > 99 ? '99+' : String(count);
    } else {
      badge.classList.remove('show');
    }
  }

  function setDot(id, on) {
    const dot = document.getElementById(id);
    if (!dot) return;
    dot.classList.toggle('show', on);
  }

  async function refreshBadge() {
    const [notifCount, msgCount] = await Promise.all([
      fetchCount('/api/notifications/count'),
      fetchCount('/api/messages/unread'),
    ]);
    setBadge('il-mnav-msg-badge', msgCount);
    setDot('il-mnav-profile-dot', notifCount > 0);
  }

  function renderItem(it) {
    const active  = it.href && isActive(it.href) ? ' active' : '';
    const ariaLbl = it.aria || it.lbl;
    const onclickAttr = it.onclick ? ` onclick="${it.onclick}; return false"` : '';
    const hrefAttr = it.href ? ` href="${it.href}"` : ' href="#"';
    if (it.primary) {
      // Plus pro Add v Bristolu; ostatní ikony jako SVG v černém kruhu
      const isBristolPlus = it.ico === 'i-plus';
      const inner = isBristolPlus ? '<span>+</span>' : svgIcon(it.ico);
      const circleClass = isBristolPlus ? 'ico-circle bristol-plus' : 'ico-circle';
      return `<a class="il-mnav-item${active} primary"${hrefAttr}${onclickAttr} aria-label="${ariaLbl}">
        <span class="${circleClass}">${inner}</span>
        <span class="lbl">${it.lbl}</span>
      </a>`;
    }
    const badge = it.badgeId ? `<span class="il-mnav-badge" id="${it.badgeId}"></span>` : '';
    const dot   = it.dotId   ? `<span class="il-mnav-dot" id="${it.dotId}"></span>` : '';
    return `<a class="il-mnav-item${active}"${hrefAttr}${onclickAttr} aria-label="${ariaLbl}">
      <span class="ico">${svgIcon(it.ico)}</span>
      <span class="lbl">${it.lbl}</span>
      ${badge}${dot}
    </a>`;
  }

  function buildItems(me) {
    const isArtist = !!(me && me.is_artist);
    const profileHref = me ? `/profile/${me.username}` : '/login';
    const onFeed = location.pathname === '/' || location.pathname === '/feed';

    // Střední tlačítko podle role:
    //   tatér:  + Přidat  (modal na feedu, jinak artist-setup)
    //   klient: 🔍 Hledat
    let centerItem;
    if (isArtist) {
      // Střední tlačítko dělá to, co je na dané stránce hlavní akce. Na
      // kalendáři to je "vypsat termín" — jinak by tam byla dvě "+" vedle
      // sebe: jedno v liště a jedno stránkové, každé s jiným významem.
      const onCalendar = location.pathname === '/calendar';
      if (onCalendar && typeof window.openSlotSheet === 'function') {
        centerItem = { onclick: 'window.openSlotSheet()', ico: 'i-plus',
                       lbl: T('mnav.add', 'Add'), primary: true,
                       aria: T('cal.addSlot', 'Add slot') };
      } else {
        const useAddModal = onFeed && typeof window.openAddPortfolio === 'function';
        centerItem = useAddModal
          ? { onclick: 'window.openAddPortfolio()', ico: 'i-plus', lbl: T('mnav.add', 'Add'), primary: true, aria: T('mnav.add', 'Add') }
          : { href: '/?add=1',                     ico: 'i-plus', lbl: T('mnav.add', 'Add'), primary: true, aria: T('mnav.add', 'Add') };
      }
    } else {
      centerItem = onFeed && typeof window.openSearchOverlay === 'function'
        ? { onclick: 'window.openSearchOverlay()', ico: 'i-search', lbl: T('mnav.search', 'Search'), primary: true, aria: T('mnav.search', 'Search') }
        : { href: '/?search=1',                   ico: 'i-search', lbl: T('mnav.search', 'Search'), primary: true, aria: T('mnav.search', 'Search') };
    }

    // Lišta má pět míst a role je vyplňují jinak. Domeček je všude feed —
    // u tatéra pod ním chvíli byly rezervace, což ikona neříkala. Rezervace
    // se přestěhovaly na profil jako záložka, kalendář zabral druhé místo.
    if (isArtist) {
      return [
        { href: '/',            ico: 'i-home',     lbl: T('mnav.feed', 'Feed') },
        { href: '/calendar',    ico: 'i-calendar', lbl: T('anav.calendar', 'Calendar') },
        centerItem,
        { href: '/messages',    ico: 'i-message',  lbl: T('mnav.messages', 'Messages'), badgeId: 'il-mnav-msg-badge' },
        { href: profileHref,    ico: 'i-user',     lbl: T('mnav.profile', 'Profile'), dotId: 'il-mnav-profile-dot' },
      ];
    }

    return [
      { href: '/',         ico: 'i-home',    lbl: T('mnav.feed', 'Feed') },
      { href: '/liked',    ico: 'i-heart',   lbl: T('mnav.liked', 'Liked') },
      centerItem,
      { href: '/messages', ico: 'i-message', lbl: T('mnav.messages', 'Messages'), badgeId: 'il-mnav-msg-badge' },
      { href: profileHref, ico: 'i-user',    lbl: me ? T('mnav.profile', 'Profile') : T('mnav.signIn', 'Sign in'), dotId: 'il-mnav-profile-dot' },
    ];
  }


  // ── Desktopová lišta tatéra ────────────────────────────────────────────────
  // Spodní lišta je jen mobilní (max-width:768px), takže na desktopu se tatér
  // dřív mezi svými stránkami neproklikal: /calendar a /artist-setup měly
  // v horní liště jen "Back" a k penězům se šlo výhradně přes rozbalovací
  // menu pod avatarem. Jedna definice tady místo pěti ručně psaných v HTML.
  // Popisky přes i18n; bez načteného i18n.js padáme na anglický text,
  // ať lišta funguje i na stránce, která překlady nenačítá.
  const T = (key, fallback) =>
    (window.InkLinkI18N && window.InkLinkI18N.t(key) !== key) ? window.InkLinkI18N.t(key) : fallback;

  // Klienti tu nejsou schválně — bydlí jako záložka na profilu tatéra,
  // protože je to jeho pracovní kartotéka, ne další sekce navigace.
  const ARTIST_LINKS = () => [
    { href: '/',             ico: 'i-home',     lbl: T('mnav.feed',     'Feed') },
    { href: '/calendar',     ico: 'i-calendar', lbl: T('anav.calendar', 'Calendar') },
    { href: '/earnings',     ico: 'i-trending', lbl: T('anav.earnings', 'Earnings') },
    { href: '/artist-setup', ico: 'i-grid',     lbl: T('anav.profile',  'Profile & portfolio') },
    { href: '/premium',      ico: 'i-star',     lbl: T('anav.premium',  'Premium') },
  ];

  // Šipka „zpět" byla na dvanácti stránkách a na patnácti chyběla, a
  // všude stejně vedla na `/`. Šipka slibuje krok zpátky v historii, což
  // nedělala — domeček říká, kam vede, a může být všude stejný.
  function ensureHomeIcon() {
    if (isActive('/')) return;               // ve feedu domeček nedává smysl
    var nav = document.querySelector('nav');
    if (!nav || document.getElementById('il-home')) return;

    // Existující „zpět" nepřidáváme podruhé — jen mu vyměníme ikonu.
    var links = nav.querySelectorAll('a[href="/"]');
    for (var i = 0; i < links.length; i++) {
      var use = links[i].querySelector('use');
      if (!use) continue;
      var href = use.getAttribute('href') || use.getAttribute('xlink:href') || '';
      if (href !== '#i-arrow-left' && href !== '#i-home') continue;
      use.setAttribute('href', '#i-home');
      links[i].id = 'il-home';
      links[i].title = T('mnav.feed', 'Feed');
      return;
    }

    var a = document.createElement('a');
    a.id = 'il-home';
    a.className = 'nav-icon';
    a.href = '/';
    a.title = T('mnav.feed', 'Feed');
    a.innerHTML = svgIcon('i-home');

    var icons = nav.querySelector('.nav-icons');
    var logo  = nav.querySelector('.nav-logo');
    if (icons) icons.insertBefore(a, icons.firstChild);
    else if (logo && logo.nextSibling) nav.insertBefore(a, logo.nextSibling);
    else nav.appendChild(a);
  }


  // ── Horní lišta ikon ────────────────────────────────────────────────────
  // Řada je stejná na všech stránkách. Pod avatarem zůstává jen to, co
  // se týká účtu — schovávat tam hledání nebo mapu by znamenalo dvě
  // místa na totéž.
  function navRow(me) {
    var row = [{ id: 'il-home', href: '/', ico: 'i-home', title: T('mnav.feed', 'Feed') },
               { id: 'il-search', search: true, ico: 'i-search', title: T('fd.navSearch', 'Search') }];
    row.push({ id: 'il-map', href: '/map', ico: 'i-map-pin', title: T('fd.navMap', 'Map') });
    if (me) {
      row.push({ id: 'il-liked',    href: '/liked',    ico: 'i-heart',   title: T('fd.navLiked', 'Liked') });
      row.push({ id: 'il-messages', href: '/messages', ico: 'i-message', title: T('fd.navMessages', 'Messages') });
    }
    return row.filter(function (it) { return !(it.href && isActive(it.href)); });
  }

  function menuItems(me) {
    var out = [{ href: '/profile/' + encodeURIComponent(me.username), ico: 'i-user',
                 lbl: T('fd.myProfile', 'My profile') },
               { href: '/my-bookings', ico: 'i-calendar', lbl: T('fd.navBookings', 'Bookings') },
               { href: '/map',         ico: 'i-map-pin',  lbl: T('fd.navMap', 'Map') }];
    // Tatér má Kalendář, Výdělky, Profil a Premium v liště vedle loga —
    // v menu by to bylo podruhé.
    return out;
  }

  function initials(me) {
    var n = (me.display_name || me.username || '?').trim();
    return n.split(/\s+/).slice(0, 2).map(function (w) { return w[0]; }).join('').toUpperCase();
  }

  function renderNavIcons(me) {
    var nav = document.querySelector('nav');
    if (!nav) return;

    var box = document.getElementById('il-navicons');
    if (!box) {
      // Stránky měly vlastní `.nav-icons` s různým obsahem; ta se stává
      // naší, ať se ikony neobjeví dvakrát.
      box = nav.querySelector('.nav-icons');
      if (!box) {
        box = document.createElement('div');
        nav.appendChild(box);
      }
      box.id = 'il-navicons';
    }
    box.className = 'nav-icons';

    // Ikony psané ručně přímo do <nav> (bez kontejneru) by nám zůstaly
    // vedle. Na premium.html bylo „nastavení" dokonce znak ⚙ místo SVG.
    var loose = nav.querySelectorAll(':scope > .nav-icon, :scope > .nav-avatar-wrap, :scope > .nav-avatar');
    for (var i = 0; i < loose.length; i++) {
      if (!box.contains(loose[i])) loose[i].remove();
    }
    box.innerHTML =
      navRow(me).map(function (it) {
        var tag = it.search ? 'button' : 'a';
        return '<' + tag + ' class="nav-icon" id="' + it.id + '"' +
               (it.search ? ' type="button"' : ' href="' + it.href + '"') +
               ' title="' + it.title + '" aria-label="' + it.title + '">' +
               svgIcon(it.ico) + '</' + tag + '>';
      }).join('') +
      (me
        ? '<div id="il-account"><div class="nav-avatar" id="il-avatar" title="' +
          T('fd.navAccount', 'Account') + '">' + initials(me) + '</div>' +
          '<div id="il-menu">' +
          menuItems(me).map(function (m) {
            return '<a href="' + m.href + '">' + svgIcon(m.ico) + '<span>' + m.lbl + '</span></a>';
          }).join('') +
          '<hr><button type="button" id="il-logout">' + svgIcon('i-log-out') +
          '<span>' + T('fd.signOut', 'Sign out') + '</span></button></div></div>'
        : '<a class="nav-icon" id="il-login" href="/login" title="' +
          T('fd.signIn', 'Sign in') + '">' + svgIcon('i-user') + '</a>');

    if (me && me.avatar) {
      document.getElementById('il-avatar').innerHTML =
        '<img src="' + me.avatar + '" alt="">';
    }

    var search = document.getElementById('il-search');
    if (search) {
      search.addEventListener('click', function () {
        // Overlay má jen feed. Jinde by volání spadlo — a spadalo:
        // ikona byla i na výdělcích a skice, kde funkce neexistuje.
        if (typeof window.openSearchOverlay === 'function') window.openSearchOverlay();
        else location.href = '/';
      });
    }

    var av = document.getElementById('il-avatar');
    if (av) {
      av.addEventListener('click', function (e) {
        e.stopPropagation();
        document.getElementById('il-menu').classList.toggle('open');
      });
      document.addEventListener('click', function () {
        var m = document.getElementById('il-menu');
        if (m) m.classList.remove('open');
      });
    }

    var out = document.getElementById('il-logout');
    if (out) {
      out.addEventListener('click', async function () {
        try { await fetch('/api/logout', { method: 'POST' }); } catch (e) {}
        location.href = '/login';
      });
    }

    // Odznak zpráv se věší na `nav a[href="/messages"]`, který jsme právě
    // vyrobili — notifs.js se inicializuje dřív než my.
    try {
      if (window.InkLinkNotifs && window.InkLinkNotifs.mountMsgBadges) {
        window.InkLinkNotifs.mountMsgBadges();
        if (window.InkLinkNotifs.refresh) window.InkLinkNotifs.refresh();
      }
    } catch (e) {}
  }

  function injectArtistBar() {
    if (document.getElementById('il-abar')) return;

    var bar = document.createElement('div');
    bar.id = 'il-abar';
    bar.innerHTML = ARTIST_LINKS().map(function (l) {
      var active = isActive(l.href) ? ' class="active"' : '';
      return '<a href="' + l.href + '"' + active + '>' + svgIcon(l.ico) +
             '<span>' + l.lbl + '</span></a>';
    }).join('');

    // Lišta patří DO stávajícího navu, ne nad něj. První verze byla vlastní
    // pruh nahoře, takže vznikly dva řádky navigace pod sebou a kalendář
    // se objevil dvakrát — jednou jako popisek, jednou jako ikona vpravo.
    var nav  = document.querySelector('nav');
    var logo = nav && nav.querySelector('.nav-logo');
    if (nav && logo && logo.nextSibling) {
      nav.insertBefore(bar, logo.nextSibling);
      // .nav-logo má flex:1 a jinak by lištu vytlačilo doprostřed navu.
      // Značka na navu, ne inline styl — ať to jde přebít v CSS stránky.
      nav.classList.add('il-has-abar');
      hideDuplicateNavItems(nav);
    } else {
      // Stránka bez navu (nebo s jinou strukturou) dostane pruh nahoře.
      bar.classList.add('standalone');
      document.body.appendChild(bar);
      document.body.classList.add('has-abar');
    }
  }

  // Co je v liště, nemá smysl mít vedle ještě jako ikonu nebo položku menu.
  function hideDuplicateNavItems(nav) {
    var dupes = nav.querySelectorAll('#navCalendar, #amSetup, #amEarnings, #il-home');
    for (var i = 0; i < dupes.length; i++) dupes[i].style.display = 'none';
    // Oddělovač v menu pod avatarem má smysl, jen když nad ním něco zbylo.
    var menu = nav.querySelector('.avatar-menu');
    if (menu) {
      var kept = menu.querySelectorAll('a:not([style*="display: none"])');
      var hr = menu.querySelector('hr.divider');
      if (hr && kept.length <= 1) hr.style.display = 'none';
    }
  }

  function renderNav(items) {
    let nav = document.getElementById('il-mnav-root');
    if (!nav) {
      // Použijeme <div> místo <nav> — page-level `nav{...}` CSS na
      // některých stránkách (top nav rules) jinak overridne pozici
      // i height našeho bottom navu.
      nav = document.createElement('div');
      nav.id = 'il-mnav-root';
      nav.className = 'il-mnav';
      nav.setAttribute('role', 'navigation');
      nav.setAttribute('aria-label', 'Main navigation');
      document.body.appendChild(nav);
    }
    nav.innerHTML = `<div class="il-mnav-grid">${items.map(renderItem).join('')}</div>`;
  }

  function mount() {
    try { injectCSS(); } catch (e) { console && console.error && console.error('[il-mnav] css', e); }

    // 1) Render IMMEDIATELY s defaultem (= neauth/klient layout). Tím
    //    se nav objeví i kdyby /api/me selhalo nebo trvalo dlouho.
    try {
      renderNav(buildItems(null));
      renderNavIcons(null);
      console && console.log && console.log('[il-mnav] mounted with default items');
    } catch (e) {
      console && console.error && console.error('[il-mnav] render', e);
      return;
    }

    // Re-render once when icon sprite arrives (icons.js dispatches this)
    if (!document.getElementById('il-icon-sprite')) {
      document.addEventListener('il-icons-ready', () => {
        try { renderNav(buildItems(cachedMe || null)); } catch {}
      }, { once: true });
    }

    // 2) Pak asynchronně načti uživatele a re-renderuj, pokud je tatér
    //    (jiný center item) nebo chceme upravit profile href.
    (async () => {
      try {
        const me = await getMe();
        renderNav(buildItems(me));
        renderNavIcons(me);
        if (me && me.is_artist) injectArtistBar();
        // Přepnutí jazyka musí přepsat i navigaci, ne jen obsah stránky.
        document.addEventListener('il-i18n-applied', () => {
          const bar = document.getElementById('il-abar');
          if (bar) bar.remove();
          renderNav(buildItems(me));
          renderNavIcons(me);
          if (me && me.is_artist) injectArtistBar();
        });
        console && console.log && console.log('[il-mnav] re-rendered', { isArtist: !!(me && me.is_artist), hasMe: !!me });
        if (me) {
          await refreshBadge();
          setInterval(refreshBadge, 60_000);
          window.addEventListener('focus', refreshBadge);
        }
      } catch (e) {
        console && console.error && console.error('[il-mnav] me fetch', e);
      }
    })();
  }

  function init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', mount);
    } else {
      mount();
    }
  }

  // Stránky mimo hlavní procházení (sdílené odkazy, studio) chtějí
  // stejný domeček, ale ne spodní lištu ani lištu tatéra. Pět ručně
  // psaných kopií v HTML by se dřív nebo později rozešlo.
  function homeIcon() {
    var run = function () {
      try { injectHomeCSS(); ensureHomeIcon(); }
      catch (e) { console && console.error && console.error('[il-mnav] home', e); }
    };
    // Ať nezáleží na tom, kde v HTML je <script> vůči <nav>.
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', run);
    } else { run(); }
  }

  window.InkLinkMobileNav = { init, refreshBadge, homeIcon };
})();
