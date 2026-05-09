/**
 * InkLink cookie / privacy banner.
 *
 * InkLink používá pouze nezbytné session cookies (login). Žádné analytics,
 * žádný tracking. Banner je informativní — odsouhlasení uloží localStorage
 * flag a banner se příště neukáže.
 *
 * Použití:
 *   <script src="/cookie-banner.js"></script>
 *   <script>InkLinkCookieBanner.init();</script>
 */

(() => {
  if (window.InkLinkCookieBanner) return;
  const KEY = 'inklink_cookie_ack_v1';

  const CSS = `
  .il-cb{position:fixed;bottom:14px;left:14px;right:14px;max-width:520px;margin:0 auto;background:rgba(0,0,0,0.96);backdrop-filter:blur(12px);border:1px solid #1a1a1a;border-left:3px solid #e8e8e8;padding:14px 16px;z-index:120;font-family:'DM Mono',monospace;font-size:12px;color:#bbb;line-height:1.5;letter-spacing:0.02em;display:none;box-shadow:0 8px 24px rgba(0,0,0,0.6)}
  .il-cb.show{display:block}
  .il-cb .il-cb-title{font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:0.18em;color:#e8e8e8;margin-bottom:6px}
  .il-cb a{color:#e8e8e8;text-decoration:underline}
  .il-cb-row{display:flex;align-items:center;gap:10px;margin-top:10px;flex-wrap:wrap}
  .il-cb-btn{font-family:'DM Mono',monospace;font-size:11px;letter-spacing:0.08em;padding:7px 14px;background:#e8e8e8;color:#000;border:none;cursor:pointer;text-transform:uppercase}
  .il-cb-btn:hover{background:#fff}
  .il-cb-link{font-size:11px;color:#777;letter-spacing:0.04em}
  .il-cb-link:hover{color:#bbb}
  @media(max-width:560px){.il-cb{bottom:70px;left:8px;right:8px}}
  `;

  function injectCSS() {
    if (document.getElementById('il-cb-css')) return;
    const s = document.createElement('style');
    s.id = 'il-cb-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function show() {
    injectCSS();
    if (document.getElementById('il-cb')) return;
    const el = document.createElement('div');
    el.id = 'il-cb';
    el.className = 'il-cb show';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Cookies a soukromí');
    el.innerHTML = `
      <div class="il-cb-title">COOKIES &amp; SOUKROMÍ</div>
      <div>
        InkLink používá <b>pouze nezbytné session cookies</b> pro přihlášení.
        Žádné analytics, žádný tracking, žádná reklama.
        Detaily v <a href="/privacy">Zásadách ochrany osobních údajů</a>.
      </div>
      <div class="il-cb-row">
        <button class="il-cb-btn" id="il-cb-ok" type="button">Rozumím</button>
        <a class="il-cb-link" href="/privacy">Zásady</a>
        <a class="il-cb-link" href="/terms">Podmínky</a>
      </div>
    `;
    document.body.appendChild(el);
    document.getElementById('il-cb-ok').addEventListener('click', dismiss);
  }

  function dismiss() {
    try { localStorage.setItem(KEY, '1'); } catch {}
    const el = document.getElementById('il-cb');
    if (el) el.remove();
  }

  function init() {
    try { if (localStorage.getItem(KEY) === '1') return; } catch {}
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', show);
    } else {
      show();
    }
  }

  window.InkLinkCookieBanner = { init, dismiss };
})();
