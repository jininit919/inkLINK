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

  function injectCSS() {
    if (document.getElementById('il-mnav-css')) return;
    const s = document.createElement('style');
    s.id = 'il-mnav-css';
    s.textContent = CSS;
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
    { href: '/calendar',     ico: 'i-calendar', lbl: T('anav.calendar', 'Calendar') },
    { href: '/earnings',     ico: 'i-trending', lbl: T('anav.earnings', 'Earnings') },
    { href: '/artist-setup', ico: 'i-settings', lbl: T('anav.profile',  'Profile & portfolio') },
  ];

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
    var dupes = nav.querySelectorAll('#navCalendar, #amSetup, #amEarnings');
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
        if (me && me.is_artist) injectArtistBar();
        // Přepnutí jazyka musí přepsat i navigaci, ne jen obsah stránky.
        document.addEventListener('il-i18n-applied', () => {
          const bar = document.getElementById('il-abar');
          if (bar) bar.remove();
          renderNav(buildItems(me));
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

  window.InkLinkMobileNav = { init, refreshBadge };
})();
