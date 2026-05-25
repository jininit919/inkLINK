/**
 * InkLink — cookie consent banner.
 *
 * Self-contained, no deps. Renders a bottom banner on first visit + a granular
 * settings modal. Persists choice to localStorage as JSON. Exposes a small API
 * (window.InkLinkCookies) so other code can gate on consent.
 *
 * Categories:
 *   - essential  — always true (session cookie for login etc.)
 *   - analytics  — opt-in; checked before loading any analytics script
 *
 * Marketing cookies are NOT used (privacy.html section 4). If we add them
 * later, add a third toggle here and `marketing` to the consent record.
 */
(function () {
  'use strict';

  var KEY = 'inklink_cookie_consent';
  var VERSION = 1;        // bump when categories change → re-prompt
  var IS_PAPER = document.documentElement.dataset.theme === 'paper' ||
                 document.body && document.body.dataset && document.body.dataset.theme === 'paper';

  // Theme tokens — copied from theme.css so this script is self-contained.
  var T = IS_PAPER
    ? { bg: '#faf8f3', surf: '#f3efe6', border: '#d9d2c4', txt: '#1a1a1a', txt2: '#555', txt3: '#888', accent: '#c62828', btn: '#1a1a1a', btnTxt: '#faf8f3' }
    : { bg: '#0a0a0a', surf: '#0e0e0e', border: '#1f1f1f', txt: '#eee',    txt2: '#bbb', txt3: '#777', accent: '#c62828', btn: '#c62828', btnTxt: '#ffffff' };

  function readConsent() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return null;
      var obj = JSON.parse(raw);
      if (!obj || obj.version !== VERSION) return null;  // version bumped → re-ask
      return obj;
    } catch (e) { return null; }
  }

  function writeConsent(obj) {
    try {
      localStorage.setItem(KEY, JSON.stringify(Object.assign({
        version:  VERSION,
        ts:       new Date().toISOString(),
        essential: true,
      }, obj)));
    } catch (e) { /* private mode / storage full — silently ignore */ }
  }

  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ── Banner ──────────────────────────────────────────────────────────────
  function renderBanner() {
    if (document.getElementById('ink-cookie-banner')) return;
    var el = document.createElement('div');
    el.id = 'ink-cookie-banner';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-label', 'Souhlas s cookies');
    el.style.cssText =
      'position:fixed;left:14px;right:14px;bottom:14px;z-index:9999;' +
      'max-width:540px;margin:0 auto;background:' + T.surf + ';color:' + T.txt + ';' +
      'border:1px solid ' + T.border + ';border-left:3px solid ' + T.accent + ';' +
      'padding:16px 18px;font-family:"Helvetica Neue",Helvetica,Arial,sans-serif;' +
      'font-size:13px;line-height:1.6;letter-spacing:0.02em;box-shadow:0 6px 24px rgba(0,0,0,0.35);' +
      'animation:ink-cookie-in 0.32s ease-out';
    el.innerHTML =
      '<style>@keyframes ink-cookie-in{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}</style>' +
      '<div style="color:' + T.txt3 + ';font-size:10px;letter-spacing:0.18em;margin-bottom:6px">COOKIES</div>' +
      '<p style="margin:0 0 10px;color:' + T.txt2 + '">Používáme nezbytné cookies pro přihlášení a analytické cookies pro zlepšování platformy. Marketing cookies <b>nepoužíváme</b>. Detail v <a href="/privacy" style="color:' + T.accent + ';text-decoration:underline">zásadách</a>.</p>' +
      '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">' +
        '<button type="button" data-act="accept-all" style="background:' + T.btn + ';color:' + T.btnTxt + ';border:none;padding:9px 14px;font-family:inherit;font-size:11px;letter-spacing:0.1em;cursor:pointer">PŘIJMOUT VŠE</button>' +
        '<button type="button" data-act="essential" style="background:transparent;color:' + T.txt + ';border:1px solid ' + T.border + ';padding:9px 14px;font-family:inherit;font-size:11px;letter-spacing:0.1em;cursor:pointer">JEN NEZBYTNÉ</button>' +
        '<button type="button" data-act="settings" style="background:transparent;color:' + T.txt2 + ';border:1px solid ' + T.border + ';padding:9px 14px;font-family:inherit;font-size:11px;letter-spacing:0.1em;cursor:pointer">NASTAVENÍ</button>' +
      '</div>';
    document.body.appendChild(el);

    el.addEventListener('click', function (e) {
      var t = e.target.closest('button[data-act]');
      if (!t) return;
      var act = t.dataset.act;
      if (act === 'accept-all') {
        writeConsent({ analytics: true });
        closeBanner();
      } else if (act === 'essential') {
        writeConsent({ analytics: false });
        closeBanner();
      } else if (act === 'settings') {
        openSettings();
      }
    });
  }

  function closeBanner() {
    var el = document.getElementById('ink-cookie-banner');
    if (el) el.remove();
  }

  // ── Settings modal ──────────────────────────────────────────────────────
  function openSettings() {
    if (document.getElementById('ink-cookie-modal')) return;
    var current = readConsent() || { analytics: false };
    var overlay = document.createElement('div');
    overlay.id = 'ink-cookie-modal';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');
    overlay.style.cssText =
      'position:fixed;inset:0;z-index:10000;background:rgba(0,0,0,0.55);' +
      'display:flex;align-items:center;justify-content:center;padding:20px;' +
      'animation:ink-cookie-in 0.2s ease-out';
    overlay.innerHTML =
      '<div style="background:' + T.surf + ';color:' + T.txt + ';border:1px solid ' + T.border + ';' +
      'max-width:480px;width:100%;padding:24px 26px;font-family:\'Helvetica Neue\',Helvetica,Arial,sans-serif">' +
        '<div style="color:' + T.txt3 + ';font-size:10px;letter-spacing:0.18em;margin-bottom:4px">NASTAVENÍ COOKIES</div>' +
        '<h2 style="margin:0 0 12px;font-size:18px;letter-spacing:0.06em;color:' + T.txt + '">Vyber kategorie</h2>' +
        '<p style="margin:0 0 18px;font-size:13px;color:' + T.txt2 + ';line-height:1.6">Nezbytné cookies jsou vždy aktivní — bez nich služba nefunguje. Ostatní si zapni podle uvážení.</p>' +

        '<div style="border:1px solid ' + T.border + ';padding:14px 16px;margin-bottom:10px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px">' +
            '<div><div style="font-size:13px;color:' + T.txt + ';margin-bottom:3px"><b>Nezbytné cookies</b></div>' +
            '<div style="font-size:11px;color:' + T.txt3 + ';letter-spacing:0.04em">Session pro přihlášení, CSRF, payment flow. Vyžadované zákonem k provozu služby.</div></div>' +
            '<input type="checkbox" checked disabled style="width:18px;height:18px;accent-color:' + T.accent + ';opacity:0.6">' +
          '</div></div>' +

        '<div style="border:1px solid ' + T.border + ';padding:14px 16px;margin-bottom:18px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;gap:12px">' +
            '<div><div style="font-size:13px;color:' + T.txt + ';margin-bottom:3px"><b>Analytické cookies</b></div>' +
            '<div style="font-size:11px;color:' + T.txt3 + ';letter-spacing:0.04em">Anonymní statistiky návštěvnosti, abychom viděli, co opravit / vylepšit. Bez profilování.</div></div>' +
            '<input type="checkbox" id="ink-cc-analytics" ' + (current.analytics ? 'checked' : '') + ' style="width:18px;height:18px;accent-color:' + T.accent + '">' +
          '</div></div>' +

        '<div style="display:flex;gap:8px;justify-content:flex-end;flex-wrap:wrap">' +
          '<button type="button" data-act="cancel" style="background:transparent;color:' + T.txt2 + ';border:1px solid ' + T.border + ';padding:9px 14px;font-family:inherit;font-size:11px;letter-spacing:0.1em;cursor:pointer">ZRUŠIT</button>' +
          '<button type="button" data-act="save" style="background:' + T.btn + ';color:' + T.btnTxt + ';border:none;padding:9px 14px;font-family:inherit;font-size:11px;letter-spacing:0.1em;cursor:pointer">ULOŽIT</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(overlay);

    overlay.addEventListener('click', function (e) {
      if (e.target === overlay) closeSettings();
      var t = e.target.closest('button[data-act]');
      if (!t) return;
      if (t.dataset.act === 'cancel') closeSettings();
      if (t.dataset.act === 'save') {
        var analytics = !!document.getElementById('ink-cc-analytics').checked;
        writeConsent({ analytics: analytics });
        closeSettings();
        closeBanner();
      }
    });
  }

  function closeSettings() {
    var el = document.getElementById('ink-cookie-modal');
    if (el) el.remove();
  }

  // ── Public API ──────────────────────────────────────────────────────────
  window.InkLinkCookies = {
    open:     openSettings,
    current:  readConsent,
    consent:  function (category) {
      var c = readConsent();
      if (!c) return false;
      if (category === 'essential') return true;
      return !!c[category];
    },
    reset:    function () { try { localStorage.removeItem(KEY); } catch (e) {} },
  };

  // ── Init: show banner if no consent yet ─────────────────────────────────
  function init() {
    if (readConsent()) return;
    renderBanner();
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
