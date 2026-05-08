/**
 * InkLink mobile bottom-nav (Instagram-style).
 *
 * Auto-mountuje fixní spodní lištu na mobilech (<768px). Top `.nav-icons`
 * se na mobilu schovají, logo a další top elementy zůstanou.
 *
 * Použití na stránce:
 *   <script src="/mobile-nav.js"></script>
 *   <script>InkLinkMobileNav.init();</script>
 *
 * Ikony:
 *   ⌂ Feed (/)
 *   ◷ Rezervace (/my-bookings)
 *   ⚙ Setup (/artist-setup) — vstupní bod pro tatéry
 *   🔔 Notif (otevře notif panel přes InkLinkNotifs)
 *   ◉ Profil (/profile/<me>)
 */

(() => {
  if (window.InkLinkMobileNav) return;

  const CSS = `
  .il-mnav{position:fixed;bottom:0;left:0;right:0;z-index:90;background:rgba(0,0,0,0.96);backdrop-filter:blur(12px);border-top:1px solid var(--border,#1a1a1a);display:none;align-items:stretch;justify-content:space-around;padding:0;height:58px;font-family:'DM Mono',monospace;-webkit-tap-highlight-color:transparent}
  .il-mnav-item{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;color:var(--txt3,#777);text-decoration:none;cursor:pointer;font-size:10px;letter-spacing:0.05em;text-transform:uppercase;background:none;border:none;font-family:inherit;position:relative;padding:8px 4px}
  .il-mnav-item .ico{font-size:20px;line-height:1}
  .il-mnav-item .lbl{font-size:9px;letter-spacing:0.06em;color:var(--txt3,#777);white-space:nowrap}
  .il-mnav-item.active{color:var(--red2,#e8e8e8)}
  .il-mnav-item.active .lbl{color:var(--red2,#e8e8e8)}
  .il-mnav-item:active .ico{transform:scale(0.92)}
  .il-mnav-badge{position:absolute;top:6px;right:calc(50% - 18px);min-width:14px;height:14px;border-radius:7px;background:var(--red2,#e8e8e8);border:1.5px solid var(--bg,#000);font-size:9px;color:var(--bg,#000);display:none;align-items:center;justify-content:center;padding:0 3px;font-weight:700;line-height:1}
  .il-mnav-badge.show{display:flex}

  @media(max-width:768px){
    .il-mnav{display:flex}
    body{padding-bottom:62px}
    /* Schováme původní top nav-icons / nav-links (různý markup napříč stránkami) */
    nav .nav-icons,
    nav .nav-links{display:none !important}
    /* Také schováme samotné top-nav .nav-icon ikony, pokud nejsou v kontaineru */
    nav > .nav-icon{display:none !important}
    /* Logo nahoře zůstává — užitečné pro orientaci a click → home */
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

  function svgBell() {
    return `<svg width="20" height="20" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M8 2.5a3.5 3.5 0 0 0-3.5 3.5v2.8L3 11h10l-1.5-2.2V6A3.5 3.5 0 0 0 8 2.5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="M6.5 11a1.5 1.5 0 0 0 3 0" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </svg>`;
  }
  function svgEnvelope() {
    return `<svg width="20" height="20" viewBox="0 0 17 17" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
      <path d="M2 4.5 L10.5 4.5 M8 2 L10.5 4.5 L8 7" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M15 12.5 L6.5 12.5 M9 10 L6.5 12.5 L9 15" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>`;
  }

  async function fetchUnread() {
    try {
      const r = await fetch('/api/notifications/count');
      if (!r.ok) return 0;
      return (await r.json()).count || 0;
    } catch { return 0; }
  }

  async function refreshBadge() {
    const badge = document.getElementById('il-mnav-notif-badge');
    if (!badge) return;
    const c = await fetchUnread();
    if (c > 0) {
      badge.classList.add('show');
      badge.textContent = c > 99 ? '99+' : String(c);
    } else {
      badge.classList.remove('show');
    }
  }

  async function mount() {
    injectCSS();
    const me = await getMe();
    const profileHref = me ? `/profile/${me.username}` : '/login';

    const items = [
      { href: '/',               ico: '⌂',         lbl: 'Feed' },
      { href: '/my-bookings',    ico: '◷',         lbl: 'Rezervace' },
      { href: '/artist-setup',   ico: '⚙',         lbl: 'Setup' },
      { id: 'notif',             ico: 'BELL',      lbl: 'Notif' },
      { href: profileHref,       ico: '◉',         lbl: 'Profil' },
    ];

    const html = items.map(it => {
      if (it.id === 'notif') {
        return `<button class="il-mnav-item" id="il-mnav-notif" aria-label="Notifikace">
          <span class="ico">${svgBell()}</span>
          <span class="lbl">${it.lbl}</span>
          <span class="il-mnav-badge" id="il-mnav-notif-badge"></span>
        </button>`;
      }
      const active = isActive(it.href) ? ' active' : '';
      const ico = it.ico === 'ENV' ? svgEnvelope() : `<span style="font-size:20px;line-height:1">${it.ico}</span>`;
      return `<a class="il-mnav-item${active}" href="${it.href}">
        <span class="ico">${ico}</span>
        <span class="lbl">${it.lbl}</span>
      </a>`;
    }).join('');

    const nav = document.createElement('nav');
    nav.className = 'il-mnav';
    nav.setAttribute('role', 'navigation');
    nav.setAttribute('aria-label', 'Hlavní navigace');
    nav.innerHTML = html;
    document.body.appendChild(nav);

    document.getElementById('il-mnav-notif').addEventListener('click', () => {
      // Pokud je na stránce InkLinkNotifs, deleguj — otevře sdílený panel
      if (window.InkLinkNotifs && typeof window.InkLinkNotifs.toggle === 'function') {
        window.InkLinkNotifs.toggle();
      } else {
        // fallback — klik na neviditelný bell btn
        const btn = document.getElementById('il-notif-btn');
        if (btn) btn.click();
        else location.href = '/'; // poslední fallback
      }
    });

    await refreshBadge();
    setInterval(refreshBadge, 60_000);
    window.addEventListener('focus', refreshBadge);
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
