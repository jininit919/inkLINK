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
  `;

  const HTML = `
<div id="addPortfolioModal" onclick="if(event.target===this)closeAddPortfolio()">
  <div class="addp-card">
    <div class="addp-head">
      <h3 id="addpTitle" data-i18n="fd.addSketch">Add sketch</h3>
      <button class="addp-close" type="button" onclick="closeAddPortfolio()" aria-label="Close"><svg class="icon"><use href="#i-x"/></svg></button>
    </div>
    <form id="addPortfolioForm" class="addp-body">
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
    if (window.InkLinkI18N && window.InkLinkI18N.apply) window.InkLinkI18N.apply();
  }

  let _addpStylesLoaded = false;

  async function openAddPortfolio(kind) {
    ensureMounted();
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

  window.InkLinkAddPortfolio = {
    open: openAddPortfolio,
    close: closeAddPortfolio,
    onDone: fn => { onDone = fn; },
  };
  // Stránky volají tyhle názvy z inline onclick.
  window.openAddPortfolio = openAddPortfolio;
  window.closeAddPortfolio = closeAddPortfolio;
})();
