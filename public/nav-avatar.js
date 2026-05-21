// InkLink avatar dropdown — auto-wires every page that has #navAvatar
// Usage: include after page sets up `me` (or just include — it'll fetch /api/me itself)
//        <script src="/nav-avatar.js?v=1"></script>
//        <script>InkLinkNavAvatar.init();</script>
(function () {
  const STYLE = `
    .nav-avatar-wrap{position:relative;display:inline-flex;align-items:center}
    .avatar-menu{position:absolute;top:calc(100% + 8px);right:0;background:var(--bg2);border:1px solid var(--border2);border-radius:10px;min-width:220px;padding:8px;display:none;flex-direction:column;gap:1px;box-shadow:0 16px 40px rgba(0,0,0,0.7);z-index:1000;backdrop-filter:blur(20px);-webkit-backdrop-filter:blur(20px)}
    .avatar-menu.open{display:flex}
    .avatar-menu a,.avatar-menu button{padding:9px 12px;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:12px;letter-spacing:0.04em;color:var(--txt2);border-radius:6px;display:flex;align-items:center;gap:10px;cursor:pointer;background:none;border:none;text-align:left;width:100%;transition:background 0.12s,color 0.12s;text-decoration:none}
    .avatar-menu a:hover,.avatar-menu button:hover{background:var(--bg3);color:var(--txt)}
    .avatar-menu .icon{width:15px;height:15px;color:var(--txt3)}
    .avatar-menu a:hover .icon,.avatar-menu button:hover .icon{color:var(--txt)}
    .avatar-menu hr.divider{border:none;border-top:1px solid var(--border);margin:4px 2px}
  `;

  function injectStyle() {
    if (document.getElementById('il-avatar-style')) return;
    const s = document.createElement('style');
    s.id = 'il-avatar-style';
    s.textContent = STYLE;
    document.head.appendChild(s);
  }

  async function init() {
    injectStyle();

    const avatar = document.getElementById('navAvatar');
    if (!avatar) return;

    let me = null;
    try {
      const r = await fetch('/api/me', { cache: 'no-store' });
      if (r.ok) me = await r.json();
    } catch {}

    if (!me) {
      // Host: nahraď avatar za Přihlásit button (idempotentně — neudělej duplicitně)
      if (!avatar.dataset.replaced) {
        avatar.dataset.replaced = '1';
        const btn = document.createElement('a');
        btn.href = '/login';
        btn.className = 'btn';
        btn.style.marginLeft = '6px';
        btn.textContent = 'Přihlásit';
        avatar.parentNode.replaceChild(btn, avatar);
      }
      return;
    }

    // Pro tatéry přesměrovat kalendář na /calendar (slot management)
    if (me.is_artist) {
      const cal = document.getElementById('navCalendar');
      if (cal) { cal.href = '/calendar'; cal.title = 'Kalendář — moje termíny'; }
    }

    // Wrap avatar v .nav-avatar-wrap (pokud ještě není)
    let wrap = avatar.closest('.nav-avatar-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'nav-avatar-wrap';
      avatar.parentNode.insertBefore(wrap, avatar);
      wrap.appendChild(avatar);
    }

    // Odstraň onclick na avataru (současné stránky mají onclick="goToProfile()")
    avatar.onclick = (e) => { e.stopPropagation(); toggle(); };
    avatar.style.cursor = 'pointer';
    avatar.title = 'Účet';

    // Build menu (pouze pokud ještě není)
    if (wrap.querySelector('.avatar-menu')) return;
    const menu = document.createElement('div');
    menu.className = 'avatar-menu';
    menu.id = 'avatarMenu';
    menu.onclick = (e) => e.stopPropagation();

    const username = me.username || '';
    const isArtist = !!me.is_artist;

    menu.innerHTML = `
      <a href="/profile/${encodeURIComponent(username)}"><svg class="icon"><use href="#i-user"/></svg> Můj profil</a>
      ${isArtist ? `<a href="/artist-setup"><svg class="icon"><use href="#i-settings"/></svg> Nastavení tatéra</a>` : ''}
      ${isArtist ? `<a href="/earnings"><svg class="icon"><use href="#i-trending"/></svg> Výdělky</a>` : ''}
      <hr class="divider">
      <button type="button" data-action="logout"><svg class="icon"><use href="#i-log-out"/></svg> Odhlásit</button>
    `;
    wrap.appendChild(menu);

    // Wire up actions
    menu.querySelector('[data-action="logout"]').addEventListener('click', async () => {
      if (typeof window.doLogout === 'function') { window.doLogout(); return; }
      // Fallback POST /api/logout
      try { await fetch('/api/logout', { method: 'POST' }); } catch {}
      location.href = '/login';
    });

    function toggle() { menu.classList.toggle('open'); }

    // Close on outside click
    document.addEventListener('click', () => menu.classList.remove('open'));
    // Close on escape
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') menu.classList.remove('open'); });
  }

  window.InkLinkNavAvatar = { init };
})();
