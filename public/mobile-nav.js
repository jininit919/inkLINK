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
 *   Tatér   : ⌂ Feed · ♥ Lajknuté · [+ Přidat] · ✉ Zprávy · ◉ Profil
 *   Klient  : ⌂ Feed · ♥ Lajknuté · ◷ Rezervace · ✉ Zprávy · ◉ Profil
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

  @media(max-width:768px){
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

    // Center slot — role-aware primary CTA:
    //   Tatér:  + Přidat  (opens add-portfolio modal on feed, else artist-setup)
    //   Klient: 🔍 Hledat (opens search overlay on feed, else deep-link /?search=1)
    let centerItem;
    if (isArtist) {
      const useAddModal = onFeed && typeof window.openAddPortfolio === 'function';
      centerItem = useAddModal
        ? { onclick: 'window.openAddPortfolio()', ico: 'i-plus', lbl: 'Přidat', primary: true, aria: 'Přidat sketch nebo healed' }
        : { href: '/artist-setup#portfolio',     ico: 'i-plus', lbl: 'Přidat', primary: true, aria: 'Přidat sketch nebo healed' };
    } else {
      centerItem = onFeed && typeof window.openSearchOverlay === 'function'
        ? { onclick: 'window.openSearchOverlay()', ico: 'i-search', lbl: 'Hledat', primary: true, aria: 'Hledat tatéra' }
        : { href: '/?search=1',                   ico: 'i-search', lbl: 'Hledat', primary: true, aria: 'Hledat tatéra' };
    }

    return [
      { href: '/',         ico: 'i-home',    lbl: 'Feed' },
      { href: '/liked',    ico: 'i-heart',   lbl: 'Lajknuté' },
      centerItem,
      { href: '/messages', ico: 'i-message', lbl: 'Zprávy', badgeId: 'il-mnav-msg-badge' },
      { href: profileHref, ico: 'i-user',    lbl: me ? 'Profil' : 'Přihlásit', dotId: 'il-mnav-profile-dot' },
    ];
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
      nav.setAttribute('aria-label', 'Hlavní navigace');
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
