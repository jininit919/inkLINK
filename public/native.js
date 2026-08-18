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
    if (!SplashScreen) return;
    try {
      // Instantní schování bez fade-out — logo staticky sedí a pak zmizí.
      await SplashScreen.hide({ fadeOutDuration: 0 });
    } catch (e) { /* nic */ }
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
