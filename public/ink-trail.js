/* InkLink — inkoustová stopa za kurzorem
 * Při mousemove vytváří dropy inkoustu, které jemně doznívají.
 * Lehký na výkon: throttle ~80ms, recycle pool, autoremove po animaci.
 * Disabled na touch zařízeních (CSS @media block + JS check).
 */
(function () {
  // Skip on touch / coarse pointer devices
  if (window.matchMedia && window.matchMedia('(hover: none), (pointer: coarse)').matches) return;
  // Respect reduced motion
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;

  const THROTTLE_MS = 70;        // jak hustá stopa
  const LIFETIME_MS = 1400;      // jak dlouho dot žije (musí matchovat CSS animaci)
  const MIN_DIST_PX = 14;        // minimální vzdálenost mezi dropy (skip když se kurzor nehne)
  const SIZE_MIN = 3.5;
  const SIZE_MAX = 9;

  let lastTime = 0;
  let lastX = -9999, lastY = -9999;
  let dropCount = 0;
  const MAX_LIVE_DROPS = 60;     // hard cap pro bezpečnost
  let liveDrops = 0;

  function spawnDrop(x, y) {
    if (liveDrops >= MAX_LIVE_DROPS) return;
    const el = document.createElement('span');
    el.className = 'il-ink-trail';
    // Drobné jitter — ať se to nepokládá rovně do linie
    const jitterX = (Math.random() - 0.5) * 8;
    const jitterY = (Math.random() - 0.5) * 8;
    // Velikost klesá s pohybem — pomalý kurzor = větší dropy, rychlý = menší
    const size = SIZE_MIN + Math.random() * (SIZE_MAX - SIZE_MIN);
    el.style.left = (x + jitterX - size / 2) + 'px';
    el.style.top  = (y + jitterY - size / 2) + 'px';
    el.style.width  = size + 'px';
    el.style.height = size + 'px';
    // Lehká variabilita opacity start
    el.style.setProperty('animation-duration', (LIFETIME_MS + (Math.random() * 200 - 100)) + 'ms');
    document.body.appendChild(el);
    liveDrops++;
    dropCount++;
    setTimeout(() => {
      el.remove();
      liveDrops--;
    }, LIFETIME_MS + 200);
  }

  document.addEventListener('mousemove', function (e) {
    const now = performance.now();
    if (now - lastTime < THROTTLE_MS) return;

    // Skip když se kurzor v podstatě nehnul
    const dx = e.pageX - lastX;
    const dy = e.pageY - lastY;
    if ((dx * dx + dy * dy) < (MIN_DIST_PX * MIN_DIST_PX)) return;

    lastTime = now;
    lastX = e.pageX;
    lastY = e.pageY;
    spawnDrop(e.pageX, e.pageY);

    // Občas (10 %) přidej druhý drobnější satelit — víc organic feel
    if (Math.random() < 0.10) {
      setTimeout(() => spawnDrop(e.pageX + (Math.random() - 0.5) * 18, e.pageY + (Math.random() - 0.5) * 18), 40);
    }
  }, { passive: true });

  // Body potřebuje position:relative aby absolute drops byly v document space
  // Většina layoutů ji už má, ale safeguard:
  if (getComputedStyle(document.body).position === 'static') {
    document.body.style.position = 'relative';
  }
})();
