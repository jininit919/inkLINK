/**
 * InkLink notifikace — sdílená komponenta.
 *
 * Použití:
 *   <div id="notifMount"></div>           ← kam se mountuje bell + panel
 *   <script src="/notifs.js"></script>
 *   <script>InkLinkNotifs.init();</script>
 *
 * Komponenta sama:
 *   - Inject CSS jednou (idempotentně)
 *   - Vykreslí bell ikonu s badge
 *   - Otevírá dropdown panel s posledními 50 notifikacemi
 *   - Polluje /api/notifications/count každých 60s + při focusu okna
 *   - Klik na notif → naviguje podle ref_type
 *   - "Označit vše přečtené" → POST /api/notifications/read-all
 */

(() => {
  if (window.InkLinkNotifs) return;  // idempotent

  const CSS = `
  .il-notif-wrap{position:relative;display:inline-block}
  .il-notif-btn{width:34px;height:34px;display:flex;align-items:center;justify-content:center;cursor:pointer;color:var(--txt3,#777);background:none;border:none;font-family:inherit;position:relative;border-radius:2px}
  .il-notif-btn:hover{color:var(--red2,#e8e8e8);background:var(--bg3,#101010)}
  .il-notif-btn.has-unread{color:var(--red2,#e8e8e8)}
  .il-notif-badge{position:absolute;top:4px;right:4px;min-width:14px;height:14px;border-radius:7px;background:var(--red2,#e8e8e8);border:1.5px solid var(--bg,#000);font-size:10px;color:var(--bg,#000);display:none;align-items:center;justify-content:center;padding:0 3px;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-weight:700;line-height:1}
  .il-notif-badge.show{display:flex}

  .il-notif-panel{position:fixed;top:54px;right:14px;width:360px;max-width:calc(100vw - 28px);max-height:70vh;background:var(--bg2,#080808);border:1px solid var(--border,#1a1a1a);box-shadow:0 8px 32px rgba(0,0,0,0.6);z-index:1000;display:none;flex-direction:column;overflow:hidden;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif}
  .il-notif-panel.open{display:flex}
  .il-notif-head{display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border,#1a1a1a)}
  .il-notif-head h3{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:14px;letter-spacing:0.18em;color:var(--red2,#e8e8e8);margin:0;font-weight:normal}
  .il-notif-mark{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:10px;letter-spacing:0.08em;color:var(--txt3,#777);background:none;border:none;cursor:pointer;text-transform:uppercase;padding:4px 8px}
  .il-notif-mark:hover{color:var(--red2,#e8e8e8)}
  .il-notif-mark:disabled{opacity:0.4;cursor:not-allowed}

  .il-notif-list{overflow-y:auto;flex:1}
  .il-notif-item{display:flex;align-items:flex-start;gap:10px;padding:12px 16px;border-bottom:1px solid var(--border,#1a1a1a);cursor:pointer;transition:background 0.1s;text-decoration:none;color:inherit}
  .il-notif-item:hover{background:var(--bg3,#101010)}
  .il-notif-item.unread{background:rgba(232,232,232,0.04)}
  .il-notif-item.unread:hover{background:rgba(232,232,232,0.07)}
  .il-notif-icon{width:30px;height:30px;border-radius:50%;background:var(--bg3,#101010);border:1px solid var(--border2,#222);display:flex;align-items:center;justify-content:center;font-size:13px;color:var(--red2,#e8e8e8);flex-shrink:0;overflow:hidden}
  .il-notif-icon img{width:100%;height:100%;object-fit:cover}
  .il-notif-body{flex:1;min-width:0}
  .il-notif-msg{font-size:12px;color:var(--txt,#e8e8e8);line-height:1.4;letter-spacing:0.02em;word-break:break-word}
  .il-notif-time{font-size:10px;color:var(--txt3,#777);margin-top:3px;letter-spacing:0.04em}
  .il-notif-dot{width:6px;height:6px;border-radius:50%;background:var(--red2,#e8e8e8);margin-top:4px;flex-shrink:0;opacity:0}
  .il-notif-item.unread .il-notif-dot{opacity:1}

  .il-notif-empty{padding:40px 24px;text-align:center;font-size:12px;color:var(--txt3,#777);letter-spacing:0.04em;line-height:1.7}
  .il-notif-empty .b{display:block;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:18px;letter-spacing:0.18em;color:var(--red2,#e8e8e8);margin-bottom:8px}

  .il-notif-push{border-top:1px solid var(--border,#1a1a1a);padding:12px 16px;display:none;flex-direction:column;gap:8px}
  .il-notif-push.show{display:flex}
  .il-notif-push-text{font-size:11px;color:var(--txt3,#777);letter-spacing:0.04em;line-height:1.5}
  .il-notif-push-btn{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;letter-spacing:0.08em;padding:8px 14px;background:var(--red2,#e8e8e8);color:var(--bg,#000);border:none;cursor:pointer;text-transform:uppercase;text-align:center}
  .il-notif-push-btn:hover{background:var(--red3,#fff)}
  .il-notif-push-btn.muted{background:transparent;color:var(--txt3,#777);border:1px solid var(--border2,#222)}
  .il-notif-push-btn.muted:hover{color:var(--red2,#e8e8e8);border-color:var(--red2,#e8e8e8)}

  @media(max-width:560px){
    .il-notif-panel{right:8px;left:8px;width:auto;top:60px}
  }
  `;

  function injectCSS() {
    if (document.getElementById('il-notif-css')) return;
    const s = document.createElement('style');
    s.id = 'il-notif-css';
    s.textContent = CSS;
    document.head.appendChild(s);
  }

  function escapeHtml(s) {
    return (s||'').replace(/[&<>"']/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
  }

  function notifHref(n) {
    // navigace podle ref_type
    if (n.ref_type === 'booking') return '/my-bookings';
    if (n.ref_type === 'user' && n.actor_username) return '/profile/' + n.actor_username;
    return '/';
  }

  let notifs = [];
  let unread = 0;
  let pollTimer = null;

  async function fetchCount() {
    try {
      const r = await fetch('/api/notifications/count');
      if (!r.ok) return;
      const d = await r.json();
      unread = d.count || 0;
      renderBadge();
    } catch (e) { /* offline ok */ }
  }

  async function fetchAll() {
    try {
      const r = await fetch('/api/notifications');
      if (!r.ok) { notifs = []; return; }
      notifs = await r.json();
      unread = notifs.filter(n => !n.read).length;
      renderPanel();
      renderBadge();
    } catch (e) { notifs = []; renderPanel(); }
  }

  function renderBadge() {
    const btn = document.getElementById('il-notif-btn');
    const badge = document.getElementById('il-notif-badge');
    if (!btn || !badge) return;
    if (unread > 0) {
      btn.classList.add('has-unread');
      badge.classList.add('show');
      badge.textContent = unread > 99 ? '99+' : String(unread);
    } else {
      btn.classList.remove('has-unread');
      badge.classList.remove('show');
    }
  }

  function renderPanel() {
    const list = document.getElementById('il-notif-list');
    const mark = document.getElementById('il-notif-mark');
    if (!list || !mark) return;
    mark.disabled = unread === 0;

    if (!notifs.length) {
      list.innerHTML = `<div class="il-notif-empty">
        <span class="b">PRÁZDNÉ</span>
        Tady se ti budou objevovat upozornění o rezervacích, doplatcích a aktivitě tatérů, které sleduješ.
      </div>`;
      return;
    }

    list.innerHTML = notifs.map(n => {
      const icon = n.actor_avatar
        ? `<img src="${escapeHtml(n.actor_avatar)}" alt="">`
        : (n.icon || '●');
      return `
        <a class="il-notif-item ${n.read ? '' : 'unread'}" href="${notifHref(n)}">
          <div class="il-notif-icon">${icon}</div>
          <div class="il-notif-body">
            <div class="il-notif-msg">${escapeHtml(n.message || '')}</div>
            <div class="il-notif-time">${escapeHtml(n.created_at || '')}</div>
          </div>
          <div class="il-notif-dot"></div>
        </a>
      `;
    }).join('');
  }

  async function openPanel() {
    const panel = document.getElementById('il-notif-panel');
    if (!panel) return;
    panel.classList.add('open');
    checkPushState();
    await fetchAll();
  }

  function closePanel() {
    const panel = document.getElementById('il-notif-panel');
    if (panel) panel.classList.remove('open');
  }

  function togglePanel() {
    const panel = document.getElementById('il-notif-panel');
    if (!panel) return;
    if (panel.classList.contains('open')) closePanel();
    else openPanel();
  }

  async function markAllRead() {
    const r = await fetch('/api/notifications/read-all', {method:'POST'});
    if (r.ok) {
      notifs.forEach(n => n.read = true);
      unread = 0;
      renderBadge();
      renderPanel();
    }
  }

  // ── Web Push subscribe / unsubscribe ─────────────────────────────────
  let pushAvailable = false;
  let pushSubscribed = false;

  function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - base64String.length % 4) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    const out = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  async function checkPushState() {
    const wrap = document.getElementById('il-notif-push');
    const text = document.getElementById('il-notif-push-text');
    const btn  = document.getElementById('il-notif-push-btn');
    if (!wrap || !text || !btn) return;

    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
      wrap.classList.remove('show');
      return;
    }

    try {
      const me = await fetch('/api/me').then(r => r.json());
      if (!me) { wrap.classList.remove('show'); return; }
      pushAvailable = !!me.push_available;
      if (!pushAvailable) { wrap.classList.remove('show'); return; }
    } catch { wrap.classList.remove('show'); return; }

    let perm = Notification.permission;
    let sub = null;
    try {
      const reg = await navigator.serviceWorker.getRegistration() || await navigator.serviceWorker.register('/sw.js');
      sub = await reg.pushManager.getSubscription();
    } catch {}
    pushSubscribed = !!sub;

    wrap.classList.add('show');
    if (perm === 'denied') {
      text.textContent = 'Notifikace zablokované — povol je v nastavení prohlížeče.';
      btn.style.display = 'none';
      return;
    }
    btn.style.display = '';
    if (pushSubscribed) {
      text.textContent = 'Push notifikace zapnuté ✓';
      btn.textContent = 'Vypnout push';
      btn.classList.add('muted');
    } else {
      text.textContent = 'Dostávej notifikace i když nemáš otevřený InkLink.';
      btn.textContent = 'Zapnout push notifikace';
      btn.classList.remove('muted');
    }
  }

  async function togglePush() {
    const btn = document.getElementById('il-notif-push-btn');
    btn.disabled = true;
    try {
      if (pushSubscribed) {
        await disablePush();
      } else {
        await enablePush();
      }
    } finally {
      btn.disabled = false;
      await checkPushState();
    }
  }

  async function enablePush() {
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') return;
    const reg = await navigator.serviceWorker.getRegistration() || await navigator.serviceWorker.register('/sw.js');
    const keyResp = await fetch('/api/push/vapid-key').then(r => r.json());
    if (!keyResp.publicKey) return;
    const sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(keyResp.publicKey),
    });
    await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(sub),
    });
  }

  async function disablePush() {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg && await reg.pushManager.getSubscription();
    if (sub) {
      await fetch('/api/push/unsubscribe', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({endpoint: sub.endpoint}),
      });
      await sub.unsubscribe();
    }
  }

  function mount() {
    const root = document.getElementById('notifMount');
    if (!root) return;
    injectCSS();
    root.innerHTML = `
      <div class="il-notif-wrap">
        <button class="il-notif-btn" id="il-notif-btn" aria-label="Notifikace" title="Notifikace">
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
            <path d="M8 2.5a3.5 3.5 0 0 0-3.5 3.5v2.8L3 11h10l-1.5-2.2V6A3.5 3.5 0 0 0 8 2.5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
            <path d="M6.5 11a1.5 1.5 0 0 0 3 0" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          <span class="il-notif-badge" id="il-notif-badge"></span>
        </button>
      </div>
      <div class="il-notif-panel" id="il-notif-panel" role="dialog" aria-label="Notifikace">
        <div class="il-notif-head">
          <h3>NOTIFIKACE</h3>
          <button class="il-notif-mark" id="il-notif-mark">Označit vše přečtené</button>
        </div>
        <div class="il-notif-list" id="il-notif-list"></div>
        <div class="il-notif-push" id="il-notif-push">
          <div class="il-notif-push-text" id="il-notif-push-text"></div>
          <button class="il-notif-push-btn" id="il-notif-push-btn"></button>
        </div>
      </div>
    `;
    document.getElementById('il-notif-btn').addEventListener('click', e => { e.stopPropagation(); togglePanel(); });
    document.getElementById('il-notif-mark').addEventListener('click', markAllRead);
    document.getElementById('il-notif-push-btn').addEventListener('click', togglePush);
    document.addEventListener('click', e => {
      const panel = document.getElementById('il-notif-panel');
      const btn   = document.getElementById('il-notif-btn');
      if (!panel || !panel.classList.contains('open')) return;
      if (panel.contains(e.target) || btn.contains(e.target)) return;
      closePanel();
    });
    document.addEventListener('keydown', e => { if (e.key === 'Escape') closePanel(); });
  }

  function init() {
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', initInternal);
    } else {
      initInternal();
    }
  }

  async function initInternal() {
    mount();
    // počáteční fetch — když user není přihlášený, vrátí 0 / [] = ok
    await fetchCount();
    // poll
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(fetchCount, 60_000);
    // refresh při focusu okna (po návratu z tabu)
    window.addEventListener('focus', fetchCount);
  }

  window.InkLinkNotifs = { init, refresh: fetchCount, toggle: togglePanel, open: openPanel, close: closePanel };
})();
