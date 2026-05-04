/* Cookie consent + footer — vkládáno do všech stránek */

(function () {
  'use strict';

  /* ── Cookie banner ───────────────────────────────────────────────────────── */
  const CONSENT_KEY = 'hmo_cookie_consent';

  function injectBanner() {
    if (localStorage.getItem(CONSENT_KEY)) return;

    const banner = document.createElement('div');
    banner.id = 'cookieBanner';
    banner.setAttribute('role', 'dialog');
    banner.setAttribute('aria-label', 'Souhlas s cookies');
    banner.innerHTML = `
      <div class="cb-text">
        <strong>Cookies a soukromí</strong>
        <p>Používáme pouze nezbytné cookies pro provoz služby (přihlášení, session). Žádné marketingové ani sledovací cookies nepoužíváme bez vašeho souhlasu.
        <a href="/privacy">Zásady ochrany osobních údajů</a></p>
      </div>
      <div class="cb-actions">
        <button id="cbReject" aria-label="Odmítnout volitelné cookies">Pouze nezbytné</button>
        <button id="cbAccept" aria-label="Přijmout všechny cookies">Přijmout vše</button>
      </div>`;
    document.body.appendChild(banner);

    document.getElementById('cbAccept').addEventListener('click', () => {
      localStorage.setItem(CONSENT_KEY, 'all');
      banner.remove();
    });
    document.getElementById('cbReject').addEventListener('click', () => {
      localStorage.setItem(CONSENT_KEY, 'essential');
      banner.remove();
    });
  }

  /* ── Footer ──────────────────────────────────────────────────────────────── */
  function injectFooter() {
    const existing = document.getElementById('legalFooter');
    if (existing) return;
    const footer = document.createElement('footer');
    footer.id = 'legalFooter';
    footer.setAttribute('role', 'contentinfo');
    footer.innerHTML = `
      <div class="lf-inner">
        <div class="lf-brand">INKLINK</div>
        <div class="lf-divider"></div>
        <nav class="lf-links" aria-label="Právní dokumenty">
          <a href="/privacy">Ochrana osobních údajů</a>
          <a href="/terms">Obchodní podmínky</a>
          <a href="mailto:info@inklink.cz">Kontakt</a>
        </nav>
        <div class="lf-legal">
          InkLink s.r.o. · IČO: 00000000 · Praha, Česká republika ·
          © ${new Date().getFullYear()}
        </div>
      </div>`;
    document.body.appendChild(footer);
  }

  /* ── Styles ──────────────────────────────────────────────────────────────── */
  function injectStyles() {
    const s = document.createElement('style');
    s.textContent = `
      /* Cookie banner */
      #cookieBanner{position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#0e0e0e;border-top:1px solid #2a2a2a;padding:16px 24px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}
      #cookieBanner .cb-text{flex:1;min-width:220px;font-family:'DM Mono',monospace;font-size:11px;color:#aaa;line-height:1.6}
      #cookieBanner .cb-text strong{display:block;color:#eee;margin-bottom:4px;font-size:12px}
      #cookieBanner .cb-text a{color:#c62828;text-decoration:none}
      #cookieBanner .cb-text a:hover{text-decoration:underline}
      #cookieBanner .cb-actions{display:flex;gap:10px;flex-shrink:0}
      #cookieBanner .cb-actions button{font-family:'Bebas Neue',sans-serif;font-size:13px;letter-spacing:0.1em;padding:10px 20px;border:1px solid #444;cursor:pointer;transition:all 0.15s;min-width:140px;text-align:center}
      #cbReject{background:transparent;color:#aaa}
      #cbReject:hover{border-color:#aaa;color:#eee}
      #cbAccept{background:#8b0000;color:#fff;border-color:#8b0000}
      #cbAccept:hover{background:#c62828;border-color:#c62828}
      /* Footer */
      #legalFooter{background:#050505;border-top:1px solid #1a1a1a;padding:32px 24px 24px;margin-top:40px}
      .lf-inner{max-width:900px;margin:0 auto;font-family:'DM Mono',monospace;font-size:10px;color:#555;line-height:1.8;text-align:center;display:flex;flex-direction:column;align-items:center;gap:14px}
      .lf-brand{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:0.25em;color:#222}
      .lf-divider{width:40px;height:1px;background:#1a1a1a}
      .lf-links{display:flex;justify-content:center;gap:24px;flex-wrap:wrap}
      .lf-links a{color:#444;text-decoration:none;letter-spacing:0.08em;font-size:10px;transition:color 0.15s}
      .lf-links a:hover{color:#c62828}
      .lf-legal{font-size:9px;color:#2a2a2a;letter-spacing:0.04em}
    `;
    document.head.appendChild(s);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => { injectStyles(); injectBanner(); injectFooter(); });
  } else {
    injectStyles(); injectBanner(); injectFooter();
  }
})();
