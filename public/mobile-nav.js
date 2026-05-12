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
  .il-mnav{position:fixed;bottom:0;left:0;right:0;z-index:90;background:rgba(0,0,0,0.96);backdrop-filter:blur(12px);border-top:1px solid var(--border,#1a1a1a);display:none;height:62px;font-family:'DM Mono',monospace;-webkit-tap-highlight-color:transparent}
  .il-mnav-grid{display:grid;grid-template-columns:repeat(5,1fr);height:100%}
  .il-mnav-item{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;color:var(--txt3,#777);text-decoration:none;cursor:pointer;font-size:10px;letter-spacing:0.05em;text-transform:uppercase;background:none;border:none;font-family:inherit;position:relative;padding:8px 4px}
  .il-mnav-item .ico{font-size:20px;line-height:1;display:flex;align-items:center;justify-content:center}
  .il-mnav-item .lbl{font-size:9px;letter-spacing:0.06em;color:var(--txt3,#777);white-space:nowrap}
  .il-mnav-item.active{color:var(--red2,#e8e8e8)}
  .il-mnav-item.active .lbl{color:var(--red2,#e8e8e8)}
  .il-mnav-item:active .ico{transform:scale(0.92)}
  .il-mnav-item.primary{justify-content:flex-end;padding-bottom:8px}
  .il-mnav-item.primary .ico-circle{position:absolute;top:-14px;left:50%;margin-left:-23px;width:46px;height:46px;border-radius:50%;background:var(--red2,#e8e8e8);color:var(--bg,#000);display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:300;line-height:1;box-shadow:0 4px 14px rgba(232,232,232,0.18);transition:transform 0.15s,box-shadow 0.15s}
  .il-mnav-item.primary:active .ico-circle{transform:scale(0.92);box-shadow:0 2px 8px rgba(232,232,232,0.12)}
  .il-mnav-item.primary .lbl{color:var(--red2,#e8e8e8);font-weight:500}
  .il-mnav-badge{position:absolute;top:6px;right:calc(50% - 18px);min-width:14px;height:14px;border-radius:7px;background:var(--red2,#e8e8e8);border:1.5px solid var(--bg,#000);font-size:9px;color:var(--bg,#000);display:none;align-items:center;justify-content:center;padding:0 3px;font-weight:700;line-height:1}
  .il-mnav-badge.show{display:flex}
  .il-mnav-dot{position:absolute;top:8px;right:calc(50% - 14px);width:8px;height:8px;border-radius:50%;background:var(--red2,#e8e8e8);border:1.5px solid var(--bg,#000);display:none}
  .il-mnav-dot.show{display:block}

  @media(max-width:768px){
    .il-mnav{display:block}
    body{padding-bottom:68px}
    nav .nav-icons,
    nav .nav-links{display:none !important}
    nav > .nav-icon{display:none !important}
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

  function svgEnvelope() {
    return `<svg width="20" height="20" viewBox="0 0 17 17" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M2 4.5 L10.5 4.5 M8 2 L10.5 4.5 L8 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M15 12.5 L6.5 12.5 M9 10 L6.5 12.5 L9 15" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
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
    const active  = isActive(it.href) ? ' active' : '';
    const primary = it.primary ? ' primary' : '';
    const ariaLbl = it.aria || it.lbl;
    if (it.primary) {
      // Primary má kruhový vystrčený ikon (.ico-circle) — absolutně pozicovaný
      return `<a class="il-mnav-item${active} primary" href="${it.href}" aria-label="${ariaLbl}">
        <span class="ico-circle">${it.ico}</span>
        <span class="lbl">${it.lbl}</span>
      </a>`;
    }
    const ico = it.ico === 'ENV'
      ? svgEnvelope()
      : `<span style="font-size:20px;line-height:1">${it.ico}</span>`;
    const badge = it.badgeId ? `<span class="il-mnav-badge" id="${it.badgeId}"></span>` : '';
    const dot   = it.dotId   ? `<span class="il-mnav-dot" id="${it.dotId}"></span>` : '';
    return `<a class="il-mnav-item${active}" href="${it.href}" aria-label="${ariaLbl}">
      <span class="ico">${ico}</span>
      <span class="lbl">${it.lbl}</span>
      ${badge}${dot}
    </a>`;
  }

  async function mount() {
    injectCSS();
    const me = await getMe();
    const isArtist = !!(me && me.is_artist);
    const profileHref = me ? `/profile/${me.username}` : '/login';

    const centerItem = isArtist
      ? { href: '/artist-setup#portfolio', ico: '+', lbl: 'Přidat', primary: true, aria: 'Přidat skicu nebo práci' }
      : { href: '/my-bookings', ico: '◷', lbl: 'Rezervace' };

    const items = [
      { href: '/',         ico: '⌂',   lbl: 'Feed' },
      { href: '/liked',    ico: '♥',   lbl: 'Lajknuté' },
      centerItem,
      { href: '/messages', ico: 'ENV', lbl: 'Zprávy', badgeId: 'il-mnav-msg-badge' },
      { href: profileHref, ico: '◉',   lbl: me ? 'Profil' : 'Sign In', dotId: 'il-mnav-profile-dot' },
    ];

    // Diagnostic — pomáhá debugovat když user nahlásí prázdnou navigaci.
    if (window.console && console.log) {
      console.log('[il-mnav] mounting', { items: items.length, isArtist, hasMe: !!me });
    }

    const nav = document.createElement('nav');
    nav.className = 'il-mnav';
    nav.setAttribute('role', 'navigation');
    nav.setAttribute('aria-label', 'Hlavní navigace');
    nav.innerHTML = `<div class="il-mnav-grid">${items.map(renderItem).join('')}</div>`;
    document.body.appendChild(nav);

    if (me) {
      await refreshBadge();
      setInterval(refreshBadge, 60_000);
      window.addEventListener('focus', refreshBadge);
    }
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
