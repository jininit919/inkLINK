/**
 * InkLink — přidání práce do portfolia. Sdílená komponenta.
 *
 * Formulář byl jen v index.html, takže „přidat práci" odjinud znamenalo
 * skok na feed s `?add=1`. Z profilu, kde portfolio bydlí, to bylo
 * obzvlášť divné. Druhá kopie v HTML by se ale dřív nebo později
 * rozešla s tou první, tak je to komponenta — jako notifs.js.
 *
 * Použití:
 *   <script src="/add-portfolio.js"></script>
 *   InkLinkAddPortfolio.onDone(() => location.reload());
 *   InkLinkAddPortfolio.open('sketch');
 *
 * Markup ani CSS stránka nedodává, vloží se samy při prvním otevření.
 */
(() => {
  if (window.InkLinkAddPortfolio) return;

  const CSS = `
#addPortfolioModal{position:fixed;inset:0;background:rgba(20,16,8,0.55);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);display:none;align-items:flex-start;justify-content:center;z-index:9999;padding:60px 16px 16px;-webkit-tap-highlight-color:transparent}
#addPortfolioModal.show{display:flex}
/* Mobile: reserve space for fixed bottom-nav (74px) + iOS home indicator */
@media(max-width:768px){#addPortfolioModal{padding:20px 12px calc(90px + env(safe-area-inset-bottom))}}
.addp-card{width:100%;max-width:640px;background:var(--bg2);border:1px solid var(--border2);border-radius:12px;max-height:calc(100dvh - 76px);overflow-y:auto;box-shadow:0 24px 60px rgba(20,16,8,0.18);display:flex;flex-direction:column}
@media(max-width:768px){.addp-card{max-height:calc(100dvh - 110px - env(safe-area-inset-bottom))}}
.addp-head{display:flex;align-items:center;justify-content:space-between;padding:16px 22px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);z-index:5}
.addp-head h3{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:20px;letter-spacing:0.16em;color:var(--txt);margin:0;font-weight:400}
.addp-close{background:transparent;border:none;color:var(--txt3);cursor:pointer;padding:6px;display:flex;align-items:center;justify-content:center;border-radius:6px;transition:all 0.12s}
.addp-close:hover{background:var(--bg3);color:var(--txt)}
.addp-body{padding:20px 22px 22px;display:flex;flex-direction:column;gap:16px}
.addp-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:520px){.addp-row{grid-template-columns:1fr}}
.addp-field{display:flex;flex-direction:column;gap:6px}
.addp-field label{font-size:10px;letter-spacing:0.14em;color:var(--txt3);text-transform:uppercase;display:flex;align-items:center;gap:6px}
.addp-field label .hint{font-size:9px;color:var(--txt3);text-transform:none;letter-spacing:0.04em;font-weight:300}
.addp-field input[type="text"],.addp-field input[type="number"],.addp-field select{width:100%;background:var(--bg3);border:1px solid var(--border2);color:var(--txt);font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:13px;padding:10px 14px;border-radius:6px;outline:none;transition:all 0.12s;letter-spacing:0.02em}
.addp-field input:focus,.addp-field select:focus{border-color:var(--txt);background:var(--bg4);box-shadow:0 0 0 3px rgba(255,255,255,0.05)}
.addp-field input[type="file"]{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:12px;color:var(--txt2);padding:8px 0}
.addp-field input[type="file"]::-webkit-file-upload-button{background:var(--bg3);border:1px solid var(--border2);color:var(--txt);font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;padding:7px 12px;border-radius:6px;cursor:pointer;letter-spacing:0.06em;text-transform:uppercase;margin-right:10px}
.addp-field input[type="file"]::-webkit-file-upload-button:hover{background:var(--bg4)}
#addpPreview{display:none;grid-template-columns:repeat(4,1fr);gap:6px;max-width:340px;margin-top:8px}
#addpPreview.show{display:grid}
#addpPreview img{width:100%;aspect-ratio:1;object-fit:cover;border-radius:6px;border:1px solid var(--border2)}
.addp-styles{display:flex;flex-wrap:wrap;gap:6px}
.addp-style-chip{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:10px;letter-spacing:0.08em;padding:6px 11px;background:var(--bg3);border:1px solid var(--border2);color:var(--txt2);border-radius:999px;cursor:pointer;text-transform:uppercase;transition:all 0.12s;-webkit-tap-highlight-color:transparent;display:inline-flex;align-items:center;gap:5px}
.addp-style-chip:hover{border-color:var(--txt3);color:var(--txt)}
.addp-style-chip.on{background:var(--txt);color:var(--bg);border-color:var(--txt)}
.addp-style-chip.on .icon{display:inline-block;width:11px;height:11px}
.addp-hint{font-size:11px;color:var(--txt3);line-height:1.6;letter-spacing:0.02em;padding:10px 12px;background:var(--bg3);border-radius:6px;border-left:2px solid var(--border2)}
.addp-hint b{color:var(--txt2);font-weight:400}
.addp-foot{display:flex;gap:10px;justify-content:flex-end;padding-top:6px}
.addp-foot .btn{flex:0 0 auto;padding:11px 22px;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;cursor:pointer;border-radius:6px;background:var(--bg3);border:1px solid var(--border2);color:var(--txt);transition:all 0.12s}
.addp-foot .btn:hover{background:var(--bg4)}
.addp-foot .btn.btn-primary{background:var(--txt);color:var(--bg);border-color:var(--txt)}
.addp-foot .btn.btn-primary:hover{background:#fff;border-color:#fff}
.addp-foot .btn[disabled]{opacity:0.4;cursor:not-allowed}
.addp-flash{padding:10px 12px;border-radius:6px;font-size:12px;letter-spacing:0.04em}
.addp-flash.ok{background:rgba(48,209,88,0.12);color:#4dd964;border:1px solid rgba(48,209,88,0.25)}
.addp-flash.err{background:rgba(255,69,58,0.12);color:#ff453a;border:1px solid rgba(255,69,58,0.25)}

/* Záložky: nahrát ze souboru vs. vytáhnout z Instagramu. Import bydlel
   v nastavení profilu, což je divné místo — přidává se práce, ne mění
   nastavení. Připojení účtu zůstalo v nastavení, protože to nastavení je. */
.addp-tabs{display:flex;gap:2px;padding:0 22px;border-bottom:1px solid var(--border);
  background:var(--bg2);position:sticky;top:57px;z-index:4}
.addp-tab{background:none;border:none;font-family:inherit;font-size:11px;
  letter-spacing:0.12em;text-transform:uppercase;color:var(--txt3);cursor:pointer;
  padding:12px 14px;border-bottom:2px solid transparent;margin-bottom:-1px;
  transition:color .14s,border-color .14s}
.addp-tab:hover{color:var(--txt2)}
.addp-tab.on{color:var(--txt);border-bottom-color:var(--txt)}
.addp-pane{display:none}
.addp-pane.on{display:flex;flex-direction:column;gap:16px}
#addpIgPane{padding:20px 22px 22px}

.ig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(96px,1fr));gap:8px;margin-top:14px;max-height:340px;overflow-y:auto}
.ig-tile{position:relative;aspect-ratio:1;border:2px solid transparent;border-radius:6px;overflow:hidden;cursor:pointer;background:var(--bg3)}
.ig-tile img{width:100%;height:100%;object-fit:cover;display:block}
.ig-tile.on{border-color:var(--red2)}
.ig-tile.done{opacity:0.4;cursor:default}
.ig-tile .mark{position:absolute;top:4px;right:4px;width:18px;height:18px;border-radius:50%;background:var(--red2);color:var(--bg);font-size:11px;display:none;align-items:center;justify-content:center;line-height:1}
.ig-tile.on .mark{display:flex}
.ig-tile .lbl{position:absolute;left:0;right:0;bottom:0;background:rgba(0,0,0,0.6);color:#fff;font-size:9px;letter-spacing:0.04em;padding:2px 4px;text-align:center}
.ig-actions{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:14px}
.ig-user{display:inline-flex;align-items:center;gap:8px;font-size:13px;color:var(--txt2)}
.ig-user svg{width:15px;height:15px;stroke:currentColor;fill:none;stroke-width:1.6}
/* Tlačítka v panelu Instagramu: styl .btn je v komponentě navázaný na
   patičku formuláře, takže sem nedosáhne a braly by se ze stránky. */
.ig-actions .btn{flex:0 0 auto;padding:11px 22px;font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;letter-spacing:0.12em;text-transform:uppercase;cursor:pointer;border-radius:6px;background:var(--bg3);border:1px solid var(--border2);color:var(--txt);transition:all 0.12s;text-decoration:none;display:inline-flex;align-items:center}
.ig-actions .btn:hover{background:var(--bg4)}
.ig-actions .btn.btn-primary{background:var(--txt);color:var(--bg);border-color:var(--txt)}
.ig-actions .btn.btn-primary:hover{background:var(--txt2);border-color:var(--txt2)}
.ig-actions .btn[disabled]{opacity:0.4;cursor:not-allowed}
.ig-actions select{background:var(--bg3);border:1px solid var(--border2);color:var(--txt);font-family:inherit;font-size:13px;padding:9px 12px;border-radius:6px}
  `;

  const HTML = `
<div id="addPortfolioModal" onclick="if(event.target===this)closeAddPortfolio()">
  <div class="addp-card">
    <div class="addp-head">
      <h3 id="addpTitle" data-i18n="fd.addSketch">Add sketch</h3>
      <button class="addp-close" type="button" onclick="closeAddPortfolio()" aria-label="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <div class="addp-tabs" id="addpTabs">
      <button type="button" class="addp-tab on" data-pane="file" data-i18n="fd.tabFile">From file</button>
      <button type="button" class="addp-tab" data-pane="ig" data-i18n="fd.tabInstagram">From Instagram</button>
    </div>
    <div class="addp-pane on" id="addpIgPane" style="display:none"></div>
    <form id="addPortfolioForm" class="addp-body addp-pane on" data-pane="file">
      <div class="addp-row">
        <div class="addp-field">
          <label data-i18n="fd.type">Type</label>
          <select name="kind" id="addpKind" required>
            <option value="sketch" data-i18n="fd.sketchDesign">Sketch / tattoo design</option>
            <option value="done" data-i18n="fd.healedWork">Finished work</option>
          </select>
        </div>
        <div class="addp-field">
          <label>Price (CZK) <span class="hint">— optional</span></label>
          <input type="number" name="price_kc" min="0" placeholder="4500">
        </div>
      </div>

      <div class="addp-field">
        <label data-i18n="fd.caption">Caption / title</label>
        <input type="text" name="caption" maxlength="500" data-i18n-attr="fd.captionPh:placeholder" placeholder="Wolf on forearm, realism">
      </div>

      <div class="addp-field">
        <label>Images <span class="hint">(1-4, JPG/PNG/WEBP, up to 10 MB)</span></label>
        <input type="file" id="addpImage" accept="image/jpeg,image/png,image/webp" multiple required>
        <div id="addpPreview"></div>
      </div>

      <div class="addp-row">
        <div class="addp-field">
          <label>Estimated hours <span class="hint">— optional</span></label>
          <input type="number" name="estimated_hours" min="0.5" max="24" step="0.5" placeholder="3">
        </div>
        <div class="addp-field">
          <label>Styles <span class="hint">— up to 4</span></label>
          <div class="addp-styles" id="addpStylesGrid"></div>
        </div>
      </div>

      <p class="addp-hint">
        If you fill in <b>price + hours for a sketch</b>, clients can book this design directly at the fixed price.
      </p>

      <div id="addpFlash"></div>

      <div class="addp-foot">
        <button type="button" class="btn" onclick="closeAddPortfolio()" data-i18n="bk.cancel">Cancel</button>
        <button type="submit" class="btn btn-primary" id="addpSubmit" data-i18n="fd.upload">Upload</button>
      </div>
    </form>
  </div>
</div>
  `;

  let onDone = null;
  let mounted = false;
  let cachedMe;

  const t = k => (window.InkLinkI18N ? window.InkLinkI18N.t(k) : k);
  const escapeHtml = s => String(s ?? '').replace(/[&<>"']/g,
    m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));

  function showToast(msg) {
    if (typeof window.showToast === 'function') return window.showToast(msg);
    console.warn('[addp]', msg);
  }

  async function isLoggedIn() {
    if (cachedMe !== undefined) return cachedMe;
    if (window.me) { cachedMe = true; return true; }
    try {
      const r = await fetch('/api/me');
      cachedMe = r.ok;
    } catch { cachedMe = false; }
    return cachedMe;
  }


  // ── Instagram ─────────────────────────────────────────────────────────
  // Picker se přestěhoval z nastavení profilu sem. Přidává se práce, ne
  // mění nastavení — a formulář je jediné místo, kde se práce přidává.
  let igMedia = [], igPicked = new Set(), igLoaded = false;

  function showPane(name) {
    document.querySelectorAll('#addpTabs .addp-tab').forEach(b =>
      b.classList.toggle('on', b.dataset.pane === name));
    const form = document.getElementById('addPortfolioForm');
    const ig = document.getElementById('addpIgPane');
    form.style.display = name === 'file' ? '' : 'none';
    ig.style.display = name === 'ig' ? '' : 'none';
    if (name === 'ig' && !igLoaded) { igLoaded = true; loadInstagram(); }
  }

  async function loadInstagram() {
    const box = document.getElementById('addpIgPane');
    if (!box) return;
    box.innerHTML = `<div class="addp-hint">${escapeHtml(t('ig.loadingMedia'))}</div>`;

    let st;
    try { st = await fetch('/api/instagram/status').then(r => r.json()); }
    catch (_) {
      box.innerHTML = `<div class="addp-hint">${escapeHtml(t('ig.error'))}</div>`;
      return;
    }

    // Server bez nakonfigurované Mety — tlačítko, které skončí chybou,
    // je horší než poctivé vysvětlení.
    if (!st.available) {
      box.innerHTML = `<div class="addp-hint">${escapeHtml(t('ig.unavailable'))}</div>`;
      return;
    }

    if (!st.connected) {
      // Po propojení se chceme vrátit sem, ne do nastavení.
      const back = encodeURIComponent(location.pathname + location.search);
      box.innerHTML =
        `<div class="addp-hint">${escapeHtml(t('ig.hint'))}</div>` +
        `<div class="addp-hint" style="margin-top:10px">${escapeHtml(t('ig.needPro'))}</div>` +
        `<div class="ig-actions"><a class="btn btn-primary" ` +
        `href="/api/instagram/connect?return=${back}">${escapeHtml(t('ig.connect'))}</a></div>`;
      return;
    }

    box.innerHTML = `
      <div class="ig-actions" style="margin-top:0">
        <span class="ig-user"><svg><use href="#i-instagram"/></svg>
          ${escapeHtml(t('ig.connectedAs'))} <b>@${escapeHtml(st.username || '')}</b></span>
      </div>
      <div class="addp-hint" style="margin-top:10px">${escapeHtml(t('ig.importedCount').replace('{n}', st.imported_count))}</div>
      <div id="igPicker" style="margin-top:14px">
        <div class="addp-hint">${escapeHtml(t('ig.loadingMedia'))}</div>
      </div>`;
    loadInstagramMedia();
  }

  async function loadInstagramMedia() {
    const box = document.getElementById('igPicker');
    if (!box) return;
    let d;
    try { d = await fetch('/api/instagram/media').then(r => r.json().then(j => ({ok: r.ok, j}))); }
    catch (_) { box.innerHTML = `<div class="addp-hint">${escapeHtml(t('ig.error'))}</div>`; return; }
    if (!d.ok) {
      // Vypršelý token není chyba serveru — je to výzva k novému propojení.
      const msg = d.j && d.j.reconnect ? t('ig.reconnect') : t('ig.error');
      const back = encodeURIComponent(location.pathname + location.search);
      box.innerHTML = `<div class="addp-hint">${escapeHtml(msg)}</div>
        <div class="ig-actions"><a class="btn" href="/api/instagram/connect?return=${back}">${escapeHtml(t('ig.connect'))}</a></div>`;
      return;
    }
    igMedia = d.j.media || [];
    igPicked = new Set();
    if (!igMedia.length) { box.innerHTML = `<div class="addp-hint">${escapeHtml(t('ig.noMedia'))}</div>`; return; }

    box.innerHTML = `
      <div class="addp-hint">${escapeHtml(t('ig.pickPhotos'))}</div>
      <div class="ig-grid">${igMedia.map(m => `
        <div class="ig-tile${m.imported ? ' done' : ''}" data-id="${escapeHtml(m.id)}"
             title="${escapeHtml(m.caption || '')}">
          <img src="${escapeHtml(m.thumb || '')}" alt="" loading="lazy">
          <span class="mark">✓</span>
          ${m.imported ? `<span class="lbl">${escapeHtml(t('ig.alreadyIn'))}</span>` : ''}
        </div>`).join('')}</div>
      <div class="ig-actions">
        <label class="addp-hint" style="margin:0;padding:0;background:none;border:none">${escapeHtml(t('ig.importAs'))}</label>
        <select id="igKind">
          <option value="done">${escapeHtml(t('fd.healedWork'))}</option>
          <option value="sketch">${escapeHtml(t('fd.sketchDesign'))}</option>
        </select>
        <button class="btn btn-primary" type="button" id="igImportBtn">${escapeHtml(t('ig.importBtn'))}</button>
      </div>
      <div id="igFlash" class="addp-hint" style="margin-top:10px;display:none"></div>`;

    box.querySelectorAll('.ig-tile:not(.done)').forEach(el => {
      el.addEventListener('click', () => {
        const id = el.dataset.id;
        if (igPicked.has(id)) { igPicked.delete(id); el.classList.remove('on'); }
        else { igPicked.add(id); el.classList.add('on'); }
      });
    });
    document.getElementById('igImportBtn').addEventListener('click', runIgImport);
  }

  async function runIgImport() {
    const flash = document.getElementById('igFlash');
    const say = msg => { flash.textContent = msg; flash.style.display = ''; };
    if (!igPicked.size) { say(t('ig.selectSome')); return; }
    const btn = document.getElementById('igImportBtn');
    btn.disabled = true;
    const label = btn.textContent;
    btn.textContent = t('ig.importing');
    flash.style.display = 'none';
    try {
      const r = await fetch('/api/instagram/import', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ids: [...igPicked], kind: document.getElementById('igKind').value})
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || 'failed');
      // Hlásíme i přeskočené a nepovedené — „naimportováno 3" u výběru pěti
      // fotek by tatéra nechalo hádat, co se stalo se zbytkem.
      const parts = [t('ig.importDone').replace('{n}', j.imported)];
      if (j.skipped) parts.push(t('ig.importSkipped').replace('{n}', j.skipped));
      if (j.failed)  parts.push(t('ig.importFailed').replace('{n}', j.failed));
      say(parts.join(' · '));
      if (j.imported && typeof onDone === 'function') {
        setTimeout(() => { closeAddPortfolio(); onDone(); }, 1200);
      } else {
        await loadInstagram();
      }
    } catch (e) {
      say(e.message || t('ig.error'));
    } finally {
      btn.disabled = false;
      btn.textContent = label;
    }
  }

  function ensureMounted() {
    if (mounted) return;
    mounted = true;
    const st = document.createElement('style');
    st.id = 'addp-css';
    st.textContent = CSS;
    document.head.appendChild(st);
    const wrap = document.createElement('div');
    wrap.innerHTML = HTML;
    while (wrap.firstChild) document.body.appendChild(wrap.firstChild);
    wire();
    document.querySelectorAll('#addpTabs .addp-tab').forEach(b =>
      b.addEventListener('click', () => showPane(b.dataset.pane)));
    if (window.InkLinkI18N && window.InkLinkI18N.apply) window.InkLinkI18N.apply();
  }

  let _addpStylesLoaded = false;

  async function openAddPortfolio(kind, pane) {
    ensureMounted();
    showPane(pane === 'ig' ? 'ig' : 'file');
    if (!(await isLoggedIn())) { location.href = '/login'; return; }
    // Nemusíš už být is_artist=1 — POST /api/portfolio tě na server sám
    // promuje při prvním uploadu. Dřívější gate sem pustil jen existující
    // tatéry, což byl slepý kruh: bez portfolia se is_artist nikdy nenastavilo.
    const modal = document.getElementById('addPortfolioModal');
    if (!modal) return;
    modal.classList.add('show');
    // Předvyplnit typ: podle aktivní záložky feedu, nebo podle toho, co
    // volající řekl. Profil žádné záložky feedu nemá.
    const activeTab = document.querySelector('.feed-tab.active');
    const kindEl = document.getElementById('addpKind');
    if (kindEl && kind) kindEl.value = kind === 'done' ? 'done' : 'sketch';
    else if (kindEl && activeTab) kindEl.value = activeTab.dataset.kind === 'done' ? 'done' : 'sketch';
    updateAddpTitle();
    // Lazy-load styly při prvním otevření
    if (!_addpStylesLoaded) await loadAddpStyles();
  }

  function closeAddPortfolio() {
    const modal = document.getElementById('addPortfolioModal');
    if (modal) modal.classList.remove('show');
  }

  function updateAddpTitle() {
    const k = document.getElementById('addpKind')?.value;
    const el = document.getElementById('addpTitle');
    if (el) el.textContent = k === 'done' ? t('fd.addHealed') : t('fd.addSketch');
  }

  async function loadAddpStyles() {
    const grid = document.getElementById('addpStylesGrid');
    if (!grid) return;
    try {
      const styles = await fetch('/api/styles').then(r => r.json()).catch(() => []);
      grid.innerHTML = styles.map(s =>
        `<button class="addp-style-chip" type="button" data-style="${escapeHtml(s)}"><svg class="icon" style="display:none"><use href="#i-check"/></svg>${escapeHtml(s)}</button>`
      ).join('');
      grid.querySelectorAll('.addp-style-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          const onCount = grid.querySelectorAll('.addp-style-chip.on').length;
          if (!chip.classList.contains('on') && onCount >= 4) return;
          chip.classList.toggle('on');
          const ico = chip.querySelector('.icon');
          if (ico) ico.style.display = chip.classList.contains('on') ? 'inline-block' : 'none';
        });
      });
      _addpStylesLoaded = true;
    } catch {}
  }

  function readAddpStyles() {
    return Array.from(document.querySelectorAll('#addpStylesGrid .addp-style-chip.on'))
      .map(c => c.dataset.style).join(',');
  }

  // Handlery se nedrátují na DOMContentLoaded, ale hned po vložení markupu —
  // modal do stránky přidáváme až při prvním otevření.
  function wire() {
    const imageInput = document.getElementById('addpImage');
    const preview = document.getElementById('addpPreview');
    if (imageInput && preview) {
      imageInput.addEventListener('change', () => {
        const files = Array.from(imageInput.files || []).slice(0, 4);
        preview.innerHTML = files.map(f => `<img src="${URL.createObjectURL(f)}" alt="">`).join('');
        preview.classList.toggle('show', files.length > 0);
      });
    }
    const kindEl = document.getElementById('addpKind');
    if (kindEl) kindEl.addEventListener('change', updateAddpTitle);
    const form = document.getElementById('addPortfolioForm');
    if (form) form.addEventListener('submit', submitAddPortfolio);
  }

  async function submitAddPortfolio(e) {
    e.preventDefault();
    const flash = document.getElementById('addpFlash');
    const submitBtn = document.getElementById('addpSubmit');
    const imageInput = document.getElementById('addpImage');
    const files = Array.from(imageInput.files || []).slice(0, 4);
    if (!files.length) {
      flash.innerHTML = `<div class="addp-flash err">${t('fd.pickImage')}</div>`;
      return;
    }
    submitBtn.disabled = true;
    submitBtn.textContent = t('fd.uploading');

    const fd = new FormData();
    fd.set('image', files[0]);
    if (files[1]) fd.set('image2', files[1]);
    if (files[2]) fd.set('image3', files[2]);
    if (files[3]) fd.set('image4', files[3]);
    for (const el of e.target.elements) {
      if (el.name && el.type !== 'file') fd.set(el.name, el.value);
    }
    fd.set('styles', readAddpStyles());

    try {
      const r = await fetch('/api/portfolio', { method: 'POST', body: fd });
      if (r.ok) {
        flash.innerHTML = `<div class="addp-flash ok">${t('fd.itemAdded')}</div>`;
        e.target.reset();
        document.getElementById('addpPreview').innerHTML = '';
        document.getElementById('addpPreview').classList.remove('show');
        document.querySelectorAll('#addpStylesGrid .addp-style-chip.on').forEach(c => {
          c.classList.remove('on');
          const ico = c.querySelector('.icon');
          if (ico) ico.style.display = 'none';
        });
        setTimeout(() => {
          closeAddPortfolio();
          flash.innerHTML = '';
          // Feed se překreslí, profil se načte znovu — každá stránka si
          // řekne sama, ať komponenta o žádné z nich nemusí vědět.
          if (typeof onDone === 'function') onDone();
        }, 800);
      } else {
        const j = await r.json().catch(() => ({}));
        flash.innerHTML = `<div class="addp-flash err">${escapeHtml(j.error || t('fd.uploadFailed'))}</div>`;
      }
    } catch {
      flash.innerHTML = `<div class="addp-flash err">${t('fd.netErrorRetry')}</div>`;
    }
    submitBtn.disabled = false;
    submitBtn.textContent = t('fd.upload');
  }

  // Návrat z propojení Instagramu. Server nás pošle tam, odkud jsme vyšli,
  // takže formulář otevřeme rovnou na té záložce a řekneme, jak to dopadlo.
  (function () {
    const st = new URLSearchParams(location.search).get('ig');
    if (!st) return;
    const run = async () => {
      await openAddPortfolio(null, 'ig');
      if (st !== 'ok') {
        const map = {state: 'ig.errState', denied: 'ig.errDenied',
                     error: 'ig.errGeneric', unconfigured: 'ig.errGeneric'};
        const box = document.getElementById('addpIgPane');
        if (box && map[st]) {
          box.insertAdjacentHTML('afterbegin',
            `<div class="addp-flash err">${escapeHtml(t(map[st]))}</div>`);
        }
      }
      // Stav z adresy pryč, ať se to neopakuje při obnovení stránky.
      history.replaceState({}, '', location.pathname + location.hash);
    };
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', run);
    } else { run(); }
  })();

  window.InkLinkAddPortfolio = {
    open: openAddPortfolio,
    close: closeAddPortfolio,
    onDone: fn => { onDone = fn; },
  };
  // Stránky volají tyhle názvy z inline onclick.
  window.openAddPortfolio = openAddPortfolio;
  window.closeAddPortfolio = closeAddPortfolio;
})();
