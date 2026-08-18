/* InkLink — Capacitor native bridge
 *
 * Loaduje se na všech stránkách. Detekuje, jestli běžíme uvnitř Capacitor
 * WebView (= nativní iOS / Android shell). Pokud ano:
 *   - Přidá class .capacitor-app na <html> (CSS hooks)
 *   - Konfiguruje status bar (paper background, dark text)
 *   - Skryje splash screen po loadu
 *   - Vystaví globální helper window.InkLinkNative s metodami:
 *       .haptic('light' | 'medium' | 'heavy' | 'success' | 'warning' | 'error')
 *       .share({ title, text, url })
 *       .requestPushPermission() → Promise<boolean>
 *       .pickImage() → Promise<{ dataUrl, format } | null>
 *
 * Pokud běžíme jen na webu (no Capacitor), všechny metody jsou no-op nebo
 * graceful fallback na web API (navigator.share, file input, žádné haptics).
 */
(function () {
  const isCapacitor = !!(window.Capacitor && window.Capacitor.isNativePlatform && window.Capacitor.isNativePlatform());
  const platform = isCapacitor ? (window.Capacitor.getPlatform ? window.Capacitor.getPlatform() : 'unknown') : 'web';

  if (isCapacitor) {
    document.documentElement.classList.add('capacitor-app', 'capacitor-' + platform);
  }

  // ── Brand overlay ─────────────────────────────────────────────────────────
  // Full-screen paper + centered inklink logo, matches native splash.
  // When native splash lifts, this overlay is already on top of page content
  // so there's no visible "logo jump". After a short beat, we fade it out
  // to reveal the actual page.
  // Only in Capacitor context; only on first entry of the session, so
  // client-side navigation between pages doesn't re-flash the splash.
  function injectBrandOverlay() {
    if (!isCapacitor) return;
    if (window.sessionStorage && sessionStorage.getItem('il-splash-shown') === '1') return;
    try { sessionStorage.setItem('il-splash-shown', '1'); } catch {}

    const style = document.createElement('style');
    style.textContent = `
      #il-brand-splash{position:fixed;inset:0;background:#faf8f3;z-index:2147483000;
        display:flex;align-items:flex-start;justify-content:center;
        padding-top:34vh;
        pointer-events:none;transition:opacity .3s ease}
      #il-brand-splash img{width:40vmin;max-width:280px;max-height:30vh;object-fit:contain}
      #il-brand-splash.hide{opacity:0}
    `;
    const div = document.createElement('div');
    div.id = 'il-brand-splash';
    div.innerHTML = '<img src="/img/ink-logo.png" alt="">';
    // Prepend so it's above page content immediately
    (document.head || document.documentElement).appendChild(style);
    (document.body || document.documentElement).appendChild(div);
  }
  function hideBrandOverlay(delayMs) {
    const el = document.getElementById('il-brand-splash');
    if (!el) return;
    setTimeout(() => {
      el.classList.add('hide');
      setTimeout(() => el.remove(), 350);
    }, delayMs || 0);
  }
  // Run immediately — as soon as native.js parses, the overlay covers the page
  injectBrandOverlay();

  // Lazy-load Capacitor plugins (pouze pokud běžíme v native shellu)
  function plugin(name) {
    if (!isCapacitor) return null;
    return window.Capacitor.Plugins && window.Capacitor.Plugins[name];
  }

  // ── Status bar ─────────────────────────────────────────────────────────────
  async function setupStatusBar() {
    const StatusBar = plugin('StatusBar');
    if (!StatusBar) return;
    try {
      // Dark text na paper bg
      await StatusBar.setStyle({ style: 'DARK' });
      // iOS bývá overlaying — set explicit non-overlay
      if (StatusBar.setOverlaysWebView) {
        await StatusBar.setOverlaysWebView({ overlay: false });
      }
      if (StatusBar.setBackgroundColor) {
        await StatusBar.setBackgroundColor({ color: '#faf8f3' });
      }
    } catch (e) { console.warn('StatusBar setup failed', e); }
  }

  // ── Splash hide ────────────────────────────────────────────────────────────
  async function hideSplash() {
    const SplashScreen = plugin('SplashScreen');
    if (SplashScreen) {
      try {
        // Instantní schování — web overlay pod tím už drží stejný vizuál.
        await SplashScreen.hide({ fadeOutDuration: 0 });
      } catch (e) { /* nic */ }
    }
    // Fade out the web-side brand overlay after native splash is truly gone
    // + small hold so uživatel má chvíli na vjem loga.
    hideBrandOverlay(300);
  }

  // ── Haptics ────────────────────────────────────────────────────────────────
  async function haptic(kind) {
    const Haptics = plugin('Haptics');
    if (!Haptics) return;
    try {
      switch (kind) {
        case 'light':
          await Haptics.impact({ style: 'LIGHT' }); break;
        case 'medium':
          await Haptics.impact({ style: 'MEDIUM' }); break;
        case 'heavy':
          await Haptics.impact({ style: 'HEAVY' }); break;
        case 'success':
          await Haptics.notification({ type: 'SUCCESS' }); break;
        case 'warning':
          await Haptics.notification({ type: 'WARNING' }); break;
        case 'error':
          await Haptics.notification({ type: 'ERROR' }); break;
        default:
          await Haptics.impact({ style: 'LIGHT' });
      }
    } catch (e) { /* nic */ }
  }

  // ── Share ──────────────────────────────────────────────────────────────────
  async function share(opts) {
    const Share = plugin('Share');
    if (Share) {
      try { await Share.share(opts); return true; } catch (e) {
        if (e && e.message && /cancel/i.test(e.message)) return false;
        console.warn('Share native failed', e);
      }
    }
    // Fallback na Web Share API
    if (navigator.share) {
      try { await navigator.share(opts); return true; } catch (e) {
        if (e && e.name === 'AbortError') return false;
      }
    }
    // Last resort: copy to clipboard
    if (opts.url && navigator.clipboard) {
      try { await navigator.clipboard.writeText(opts.url); return 'copied'; } catch (e) {}
    }
    return false;
  }

  // ── Push notifications ────────────────────────────────────────────────────
  async function requestPushPermission() {
    const Push = plugin('PushNotifications');
    if (!Push) return false;
    try {
      const perm = await Push.requestPermissions();
      if (perm && perm.receive === 'granted') {
        await Push.register();
        return true;
      }
    } catch (e) { console.warn('Push request failed', e); }
    return false;
  }

  // Setup token listener — posílá APNs/FCM token na backend
  function setupPushListeners() {
    const Push = plugin('PushNotifications');
    if (!Push) return;
    Push.addListener('registration', async (token) => {
      try {
        await fetch('/api/native/register-push', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            token: token.value,
            provider: platform === 'ios' ? 'apns' : 'fcm',
            platform: platform,
          })
        });
      } catch (e) { console.warn('Token register failed', e); }
    });
    Push.addListener('registrationError', (err) => {
      console.warn('Push registration error', err);
    });
    Push.addListener('pushNotificationReceived', (notif) => {
      console.log('Push received foreground:', notif);
    });
    Push.addListener('pushNotificationActionPerformed', (action) => {
      // Tap na notifikaci — pokud má URL, navigate
      const data = action && action.notification && action.notification.data;
      if (data && data.url) {
        location.href = data.url;
      }
    });
  }

  // ── Image picker ───────────────────────────────────────────────────────────
  async function pickImage() {
    const Camera = plugin('Camera');
    if (!Camera) return null;
    try {
      const photo = await Camera.getPhoto({
        quality: 88,
        allowEditing: false,
        resultType: 'dataUrl',
        source: 'PROMPT',  // user vybere kamera vs. galerie
        saveToGallery: false,
      });
      return { dataUrl: photo.dataUrl, format: photo.format };
    } catch (e) {
      if (e && /cancel/i.test(String(e))) return null;
      console.warn('Camera pickImage failed', e);
      return null;
    }
  }

  // ── Init ──────────────────────────────────────────────────────────────────
  window.InkLinkNative = {
    isNative: isCapacitor,
    platform: platform,
    haptic, share,
    requestPushPermission, pickImage,
  };

  if (isCapacitor) {
    document.addEventListener('DOMContentLoaded', () => {
      setupStatusBar();
      setupPushListeners();
      // Push permission se ptáme až po prvním user interaction — ne hned na boot.
    });

    // Splash schováme až po `window.load` — čekáme na všechny obrázky a fonty,
    // aby po zmizení splashe nedošlo k layout shiftu (Bristol swap, logo image).
    // Requestem animation frame navíc necháme browser dorender.
    function hideAfterPaint() {
      requestAnimationFrame(() => requestAnimationFrame(hideSplash));
    }
    if (document.readyState === 'complete') hideAfterPaint();
    else window.addEventListener('load', hideAfterPaint, { once: true });
  }
})();
