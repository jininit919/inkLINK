/**
 * Rezervace — klientské i tatérské. Bydlí jako záložka na profilu; vlastní
 * stránka /my-bookings zůstala jen jako přesměrování, aby odkazy z e-mailů
 * a notifikací dál fungovaly.
 *
 * Použití:  InkLinkBookings.mount(document.getElementById('tab-bookings'))
 *
 * Styly jsou zaprefixované .il-bookings, protože profil má vlastní .tabs,
 * .tab i .empty — bez prefixu by je panel přebarvil. Ze stejného důvodu
 * je všechno uvnitř IIFE: profil má vlastní `me`, `fmtDate` i `escapeHtml`
 * a druhá deklarace v globálu by stránku shodila.
 */
window.InkLinkBookings = (function () {
  const CSS = ".il-bookings .cal-tools{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}\n.il-bookings .cal-tools .btn{text-decoration:none;display:inline-flex;align-items:center;gap:6px}\n.il-bookings .cal-sync-head{margin-top:40px;padding-top:20px;border-top:1px solid var(--border);\n  font-size:11px;letter-spacing:0.1em;text-transform:uppercase;color:var(--txt3);margin-bottom:10px}\n.il-bookings .btn.muted{border-color:var(--border2);color:var(--txt2)}\n.il-bookings .btn.muted:hover{background:transparent;color:var(--red2);border-color:var(--red2)}\n.il-bookings .tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:24px;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}\n.il-bookings .tabs::-webkit-scrollbar{display:none}\n.il-bookings .tab{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:16px;letter-spacing:0.18em;padding:12px 18px;cursor:pointer;color:var(--txt3);border-bottom:2px solid transparent;margin-bottom:-1px;white-space:nowrap;flex-shrink:0}\n.il-bookings .tab.active{color:var(--red2);border-bottom-color:var(--red2)}\n.il-bookings .tab .count{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:12px;letter-spacing:0.05em;color:var(--txt3);margin-left:6px}\n@media(max-width:560px){}\n.il-bookings .empty{text-align:center;padding:80px 20px;color:var(--txt3);font-size:13px;letter-spacing:0.04em;line-height:1.6}\n.il-bookings .empty .b{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:22px;letter-spacing:0.18em;color:var(--red2);margin-bottom:12px}\n.il-bookings .list{display:flex;flex-direction:column;gap:10px}\n.il-bookings .b-row{display:grid;grid-template-columns:140px 1fr auto;gap:14px;padding:16px;background:var(--bg2);border:1px solid var(--border)}\n@media(max-width:560px){.il-bookings .b-row{grid-template-columns:1fr}\n}\n.il-bookings .b-when{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:18px;letter-spacing:0.06em}\n.il-bookings .b-mid{display:flex;flex-direction:column;gap:5px;min-width:0}\n.il-bookings .b-who{display:flex;align-items:center;gap:8px;font-size:13px}\n.il-bookings .b-avatar{width:28px;height:28px;border-radius:50%;background:var(--bg4);border:1px solid var(--red);display:flex;align-items:center;justify-content:center;font-size:11px;color:var(--red2);overflow:hidden;flex-shrink:0}\n.il-bookings .b-avatar img{width:100%;height:100%;object-fit:cover}\n.il-bookings .b-note{font-size:12px;color:var(--txt2);line-height:1.5;white-space:pre-wrap;word-break:break-word}\n.il-bookings .b-money{font-size:11px;color:var(--txt3);letter-spacing:0.04em}\n.il-bookings .b-money b{color:var(--txt);font-weight:400}\n.il-bookings .b-status{padding:3px 10px;border:1px solid var(--border2);font-size:10px;letter-spacing:0.12em;color:var(--txt3);align-self:flex-start;white-space:nowrap}\n.il-bookings .b-status.confirmed{color:var(--ok);border-color:rgba(127,211,145,0.3)}\n.il-bookings .b-status.pending_payment{color:var(--warn);border-color:rgba(232,200,127,0.3)}\n.il-bookings .b-status.completed{color:var(--txt2);border-color:var(--border2)}\n.il-bookings .b-status.cancelled_client, .il-bookings .b-status.cancelled_artist{color:var(--err);border-color:rgba(229,135,135,0.3)}\n.il-bookings .b-actions{display:flex;flex-direction:column;gap:6px;align-items:flex-end;justify-content:center}\n@media(max-width:560px){.il-bookings .b-actions{align-items:flex-start;flex-direction:row;flex-wrap:wrap}\n}\n.il-bookings .btn{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;letter-spacing:0.08em;padding:6px 12px;background:transparent;border:1px solid var(--red2);color:var(--red2);cursor:pointer;text-transform:uppercase}\n.il-bookings .btn:hover{background:var(--red2);color:var(--bg)}\n.il-bookings .btn.danger{border-color:var(--err);color:var(--err)}\n.il-bookings .btn.danger:hover{background:var(--err);color:#fff}\n.il-bookings .btn.muted{border-color:var(--border2);color:var(--txt3)}\n.il-bookings .btn.muted:hover{border-color:var(--txt3);color:var(--txt2);background:transparent}\n.il-bookings .demo-banner{background:rgba(232,200,127,0.08);border:1px solid rgba(232,200,127,0.3);padding:10px 14px;color:#e8c87f;font-size:12px;letter-spacing:0.04em;margin-bottom:18px;line-height:1.5}\n.il-bookings #reviewModal, .il-bookings #respondModal, .il-bookings #refundModal, .il-bookings #editBookModal, .il-bookings #rescheduleModal{position:fixed;inset:0;background:rgba(20,16,8,0.55);display:none;align-items:center;justify-content:center;z-index:200;padding:20px}\n.il-bookings #reviewModal.show, .il-bookings #respondModal.show, .il-bookings #refundModal.show, .il-bookings #editBookModal.show, .il-bookings #rescheduleModal.show{display:flex}\n.il-bookings .cm-card{max-width:480px;width:100%;background:var(--bg2);border:1px solid var(--border);padding:24px;max-height:90vh;overflow-y:auto}\n.il-bookings .cm-card h3{font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:22px;letter-spacing:0.12em;margin-bottom:6px}\n.il-bookings .cm-card p{color:var(--txt2);font-size:12px;line-height:1.5;margin-bottom:14px;letter-spacing:0.04em}\n.il-bookings .cm-card label{display:block;font-size:11px;letter-spacing:0.1em;color:var(--txt3);margin-bottom:6px;text-transform:uppercase;margin-top:14px}\n.il-bookings .cm-card label:first-of-type{margin-top:0}\n.il-bookings .cm-card input, .il-bookings .cm-card textarea{width:100%;background:var(--bg3);border:1px solid var(--border2);color:var(--txt);font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:13px;padding:9px 12px;outline:none}\n.il-bookings .cm-card textarea{min-height:90px;resize:vertical}\n.il-bookings .cm-card input:focus, .il-bookings .cm-card textarea:focus{border-color:var(--red2)}\n.il-bookings .cm-card .actions{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}\n.il-bookings .star-pick{display:flex;gap:6px;font-size:32px;line-height:1;margin:6px 0 4px;cursor:pointer}\n.il-bookings .star-pick span{color:var(--txt3);transition:color 0.1s;user-select:none}\n.il-bookings .star-pick span.on{color:var(--warn)}\n.il-bookings .star-pick span:hover{color:var(--warn)}\n.il-bookings .star-static{display:inline-flex;gap:2px;font-size:14px;color:var(--warn);letter-spacing:0.02em;line-height:1}\n.il-bookings .star-static .empty{color:var(--border2)}\n.il-bookings .b-review{margin-top:8px;padding:10px 12px;background:var(--bg3);border-left:2px solid var(--warn);font-size:11px;color:var(--txt2);line-height:1.5;letter-spacing:0.03em;word-break:break-word}\n.il-bookings .b-review b{color:var(--txt);font-weight:400}\n.il-bookings .b-review .resp{margin-top:8px;padding-top:8px;border-top:1px solid var(--border2);color:var(--txt3)}\n.il-bookings .b-review .resp b{color:var(--txt2)}\n";
  const HTML = "  <div id=\"content\" style=\"display:none\">\n    <div id=\"calSubBox\" style=\"display:none;margin:14px 0;padding:14px;background:var(--bg2);border:1px solid var(--border2);font-size:11px;letter-spacing:0.04em;color:var(--txt2);line-height:1.7\">\n      <div style=\"margin-bottom:8px;color:var(--txt)\"><span data-i18n=\"bk.subscribeHint\">Continuous sync URL \u2014 paste into Apple/Google Calendar (Subscribe to calendar):</span></div>\n      <input id=\"calSubUrl\" readonly style=\"width:100%;background:var(--bg3);border:1px solid var(--border);color:var(--txt2);font-family:'Helvetica Neue','Helvetica','Arial',sans-serif;font-size:11px;padding:8px 10px;letter-spacing:0.02em\" onclick=\"this.select()\">\n      <div style=\"margin-top:10px;display:flex;gap:8px\">\n        <button class=\"btn muted\" onclick=\"InkLinkBookings.copyCalSubUrl()\" style=\"font-size:10px\" data-i18n=\"bk.copy\">Copy</button>\n        <button class=\"btn muted\" onclick=\"InkLinkBookings.regenerateCalToken()\" style=\"font-size:10px;color:var(--bad)\" data-i18n=\"bk.regenerate\">Regenerate (old URL stops working)</button>\n      </div>\n    </div>\n\n    <div class=\"tabs\">\n      <div class=\"tab active\" data-tab=\"client\" id=\"tabClient\"><span data-i18n=\"bk.asClient\">As a client</span><span class=\"count\" id=\"cntClient\"></span></div>\n      <div class=\"tab\" data-tab=\"artist\" id=\"tabArtist\"><span data-i18n=\"bk.asArtist\">As an artist</span><span class=\"count\" id=\"cntArtist\"></span></div>\n    </div>\n\n    <div id=\"tab-client\">\n      <div class=\"list\" id=\"clientList\"></div>\n      <div class=\"empty\" id=\"clientEmpty\" style=\"display:none\">\n        <div class=\"b\" data-i18n=\"bk.noneYet\">No bookings yet</div>\n        <span data-i18n=\"bk.clientEmpty\">Find an artist in the feed and pick an open slot.</span>\n        <div style=\"margin-top:16px\"><a class=\"btn\" href=\"/\" data-i18n=\"bk.browseFeed\">Browse the feed</a></div>\n      </div>\n    </div>\n\n    <div id=\"tab-artist\" style=\"display:none\">\n      <div class=\"list\" id=\"artistList\"></div>\n      <div class=\"empty\" id=\"artistEmpty\" style=\"display:none\">\n        <div class=\"b\" data-i18n=\"bk.none\">No bookings</div>\n        <span data-i18n=\"bk.artistEmpty\">Nobody has booked with you yet.</span><br>\n        <span data-i18n=\"bk.artistEmptyCta\">Publish open slots in your calendar so clients can find you.</span>\n        <!-- M\u00ed\u0159\u00ed na /calendar: term\u00edny se od Sprintu 2 spravuj\u00ed tam,\n             v artist-setup ta sekce u\u017e nen\u00ed. -->\n        <div style=\"margin-top:16px\"><a class=\"btn\" href=\"/calendar\" data-i18n=\"bk.goToCalendar\">Open the calendar</a></div>\n      </div>\n    </div>\n\n    <!-- Napojen\u00ed na Apple/Google kalend\u00e1\u0159 se nastav\u00ed jednou. Bylo to nad\n         rezervacemi, tedy na nejcenn\u011bj\u0161\u00edm m\u00edst\u011b str\u00e1nky. -->\n    <div class=\"cal-sync-head\" data-i18n=\"bk.calSync\">Sync with your calendar app</div>\n    <div class=\"cal-tools\" id=\"calTools\">\n      <a class=\"btn muted\" href=\"/api/me/calendar.ics\" download=\"inklink.ics\" data-i18n=\"bk.downloadIcs\">\u21e9 Download .ics</a>\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.showCalendarSubscribe()\" data-i18n=\"bk.subscribeUrl\">\u2337 Subscribe URL</button>\n    </div>\n\n<div id=\"reviewModal\" onclick=\"if(event.target===this)InkLinkBookings.closeReview()\">\n  <div class=\"cm-card\">\n    <h3 data-i18n=\"bk.mRate\">Rate the tattoo</h3>\n    <p id=\"rmHint\"><span data-i18n=\"bk.mRateHint\">Your rating will be visible to other clients on the artist's profile. You can edit it anytime.</span></p>\n    <label data-i18n=\"bk.mStars\">Stars *</label>\n    <div class=\"star-pick\" id=\"rmStars\" data-rating=\"0\">\n      <span data-v=\"1\">\u2605</span><span data-v=\"2\">\u2605</span><span data-v=\"3\">\u2605</span>\n      <span data-v=\"4\">\u2605</span><span data-v=\"5\">\u2605</span>\n    </div>\n    <label data-i18n=\"bk.mComment\">Comment (optional)</label>\n    <textarea id=\"rmText\" maxlength=\"1000\" data-i18n-attr=\"bk.mCommentPh:placeholder\" placeholder=\"How was the session? Communication, hygiene, result\u2026\"></textarea>\n    <div id=\"rmFlash\" style=\"margin-top:10px\"></div>\n    <div class=\"actions\">\n      <button class=\"btn danger\" id=\"rmDelete\" onclick=\"InkLinkBookings.deleteReview()\" style=\"margin-right:auto;display:none\" data-i18n=\"bk.deleteRating\">Delete rating</button>\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.closeReview()\">${t('bk.cancel')}</button>\n      <button class=\"btn\" onclick=\"InkLinkBookings.submitReview()\" data-i18n=\"bk.save\">Save</button>\n    </div>\n  </div>\n</div>\n\n<div id=\"respondModal\" onclick=\"if(event.target===this)InkLinkBookings.closeRespond()\">\n  <div class=\"cm-card\">\n    <h3 data-i18n=\"bk.mReply\">Reply to review</h3>\n    <p><span data-i18n=\"bk.mReplyHint\">Your reply will appear under the client's review on your profile.</span></p>\n    <div id=\"respClientReview\" style=\"margin-bottom:12px\"></div>\n    <label data-i18n=\"bk.mYourReply\">Your reply</label>\n    <textarea id=\"respText\" maxlength=\"500\" data-i18n-attr=\"bk.mReplyPh:placeholder\" placeholder=\"Thanks for the review!\"></textarea>\n    <div id=\"respFlash\" style=\"margin-top:10px\"></div>\n    <div class=\"actions\">\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.closeRespond()\">${t('bk.cancel')}</button>\n      <button class=\"btn\" onclick=\"InkLinkBookings.submitResponse()\" data-i18n=\"bk.mSendReply\">Send reply</button>\n    </div>\n  </div>\n</div>\n\n<div id=\"editBookModal\" onclick=\"if(event.target===this)InkLinkBookings.closeEditBook()\">\n  <div class=\"cm-card\">\n    <h3 id=\"ebmTitle\" data-i18n=\"bk.mEdit\">Edit booking</h3>\n    <p id=\"ebmHint\" class=\"hint\" style=\"font-size:11px;color:var(--txt3);margin-bottom:14px\"><span data-i18n=\"bk.mEditHint\">Edit the tattoo description. Use \u201cReschedule\u201d to move the appointment.</span></p>\n    <label data-i18n=\"bk.mDesc\">Tattoo description</label>\n    <textarea id=\"ebmNote\" maxlength=\"1000\"></textarea>\n    <div id=\"ebmFlash\" style=\"margin-top:10px\"></div>\n    <div class=\"actions\">\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.closeEditBook()\">${t('bk.cancel')}</button>\n      <button class=\"btn\" onclick=\"InkLinkBookings.submitEditBook()\" data-i18n=\"bk.saveChanges\">Save changes</button>\n    </div>\n  </div>\n</div>\n\n<!-- P\u0159esun rezervace: klient \u226548 h p\u0159edem hned, jinak \u017e\u00e1dost tat\u00e9rovi -->\n<div id=\"rescheduleModal\" onclick=\"if(event.target===this)InkLinkBookings.closeReschedule()\">\n  <div class=\"cm-card\">\n    <h3 id=\"rsTitle\" data-i18n=\"bk.mMove\">Reschedule booking</h3>\n    <p id=\"rsHint\" class=\"hint\" style=\"font-size:11px;color:var(--txt3);margin-bottom:14px\"></p>\n    <label data-i18n=\"bk.mArtistSlot\">Artist's open slot</label>\n    <select id=\"rsSlot\" onchange=\"InkLinkBookings.renderRescheduleStarts()\"></select>\n    <label data-i18n=\"bk.startAt\">Start</label>\n    <select id=\"rsStart\"></select>\n    <div id=\"rsFlash\" style=\"margin-top:10px\"></div>\n    <div class=\"actions\">\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.closeReschedule()\" data-i18n=\"bk.back\">Back</button>\n      <button class=\"btn\" id=\"rsSubmit\" onclick=\"InkLinkBookings.submitReschedule()\">${t('bk.reschedule')}</button>\n    </div>\n  </div>\n</div>\n\n<div id=\"refundModal\" onclick=\"if(event.target===this)InkLinkBookings.closeRefund()\">\n  <div class=\"cm-card\">\n    <h3 id=\"rfTitle\" data-i18n=\"bk.mRefund\">Refund request</h3>\n    <p id=\"rfHint\" style=\"font-size:13px;color:var(--txt3);letter-spacing:0\">The artist will review your request \u2014 if approved, the money will be refunded to your card within 5\u201310 days.</p>\n    <div id=\"rfMaxLine\" style=\"font-size:11px;color:var(--txt3);letter-spacing:0.04em;margin:6px 0 4px\"></div>\n    <label>\u010c\u00e1stka (K\u010d) <span style=\"color:var(--txt3);text-transform:none;letter-spacing:0\" data-i18n=\"bk.amountBlank\">\u2014 leave blank = full paid amount</span></label>\n    <input type=\"number\" id=\"rfAmount\" min=\"1\" data-i18n-attr=\"bk.mAmountPh:placeholder\" placeholder=\"full paid amount\">\n    <label style=\"margin-top:14px\">Reason (min 10 chars)</label>\n    <textarea id=\"rfReason\" rows=\"4\" style=\"width:100%;background:var(--bg2);border:1px solid var(--border2);color:var(--txt);padding:10px;font-family:inherit;font-size:14px;resize:vertical\" data-i18n-attr=\"bk.reasonPh:placeholder\" placeholder=\"Briefly what happened\u2026\"></textarea>\n    <div id=\"rfFlash\" style=\"margin-top:10px\"></div>\n    <div class=\"actions\">\n      <button class=\"btn muted\" onclick=\"InkLinkBookings.closeRefund()\" data-i18n=\"bk.close\">Close</button>\n      <button class=\"btn\" onclick=\"InkLinkBookings.submitRefund()\" data-i18n=\"bk.mSendRequest\">Send request</button>\n    </div>\n  </div>\n</div>\n\n";

  let mounted = false;
  let root = null;   // kořen panelu — profil má vlastní .tabs a .tab

  function mount(container) {
    if (!container || mounted) return;
    mounted = true;
    if (!document.getElementById('il-bookings-css')) {
      const s = document.createElement('style');
      s.id = 'il-bookings-css';
      s.textContent = CSS;
      document.head.appendChild(s);
    }
    root = container;
    container.classList.add('il-bookings');
    container.innerHTML = HTML;
    if (window.InkLinkI18N) InkLinkI18N.apply(container);
    return init();
  }

let me = null, demoMode = false;

const t = k => window.InkLinkI18N ? window.InkLinkI18N.t(k) : k;
// Seznamy se skládají v JS, takže je po přepnutí jazyka musíme překreslit.
document.addEventListener('il-i18n-applied', () => { if (me) { loadClient(); loadArtist(); } });
let refundByBooking = {}; // booking_id → latest refund_request (or null)
let refundingBookingId = null, refundingMaxKc = 0;
let rescheduleByBooking = {}; // booking_id → latest pending reschedule request
let rsBooking = null, rsSlots = [];   // stav modalu pro přesun

function escapeHtml(s){return(s||'').replace(/[&<>"']/g, m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));}
// Formát data a čísel se řídí jazykem UI; měna zůstává CZK, ta se s jazykem
// nemění (platforma je česká).
function locale(){ return (window.InkLinkI18N ? InkLinkI18N.get() : 'en') === 'cs' ? 'cs-CZ' : 'en-GB'; }
function fmtDate(iso){try{const d=new Date(iso);return d.toLocaleDateString(locale(),{day:'numeric',month:'short',year:'numeric'})+' · '+d.toLocaleTimeString(locale(),{hour:'2-digit',minute:'2-digit'});}catch{return iso;}}
function fmtKc(cents, cur){return cents ? InkLinkI18N.money(cents/100, cur || 'CZK') : '—';}
function statusLabel(s){return ({
  pending_payment: t('bk.stPending'),
  confirmed:       t('bk.stConfirmed'),
  completed:       t('bk.stCompleted'),
  cancelled_client:t('bk.stCancelledC'),
  cancelled_artist:t('bk.stCancelledA'),
  no_show:         t('bk.stNoShow'),
})[s] || (s||'').toUpperCase();}

function showTab(which) {
  root.querySelectorAll('.tab').forEach(x =>
    x.classList.toggle('active', x.dataset.tab === which));
  document.getElementById('tab-client').style.display = which === 'client' ? 'block' : 'none';
  document.getElementById('tab-artist').style.display = which === 'artist' ? 'block' : 'none';
}

async function init() {
  // Panel se montuje jen vlastníkovi profilu, ale role rozhoduje o výchozí
  // záložce a demo režimu — proto se na sebe pořád ptáme.
  me = await fetch('/api/me').then(r => r.json()).catch(() => null);
  if (!me) return;

  root.querySelectorAll('.tab').forEach(el => el.addEventListener('click', () => {
    showTab(el.dataset.tab);
  }));
  bindStars();

  document.getElementById('content').style.display = 'block';
  // Tatér sem chodí kvůli SVÝM rezervacím. Výchozí klientská záložka mu
  // ukazovala "žádné rezervace", i když jako tatér nějaké měl.
  if (me.is_artist) showTab('artist');
  demoMode = !me.can_accept_bookings;
  if (demoMode) {
    const banner = document.createElement('div');
    banner.className = 'demo-banner';
    banner.textContent = t('bk.demoBanner');
    const content = document.getElementById('content');
    content.insertBefore(banner, content.querySelector('.tabs'));
  }
  await Promise.all([loadRefundRequests(), loadRescheduleRequests()]);
  await Promise.all([loadClient(), loadArtist()]); syncTabs();
}

async function loadRescheduleRequests() {
  rescheduleByBooking = {};
  try {
    const rows = await fetch('/api/reschedule-requests').then(r => r.ok ? r.json() : []);
    for (const rr of rows) {
      if (rr.status !== 'pending') continue;   // zajímá nás jen čekající
      const prev = rescheduleByBooking[rr.booking_id];
      if (!prev || rr.id > prev.id) rescheduleByBooking[rr.booking_id] = rr;
    }
  } catch {}
}

async function loadRefundRequests() {
  refundByBooking = {};
  try {
    const list = await fetch('/api/refund-requests').then(r => r.json());
    if (!Array.isArray(list)) return;
    // Keep the most recent request per booking
    list.forEach(rr => {
      const prev = refundByBooking[rr.booking_id];
      if (!prev || new Date(rr.created_at) > new Date(prev.created_at)) {
        refundByBooking[rr.booking_id] = rr;
      }
    });
  } catch (e) { console.warn('refund load failed', e); }
}

let nClient = 0, nArtist = 0;

// Záložky mají smysl jen tehdy, když je opravdu kam přepínat. Tatér
// prakticky nikdy nemá vlastní rezervace jako klient, a přesto na tu
// poloprázdnou záložku koukal při každé návštěvě.
function syncTabs() {
  const both = nClient > 0 && nArtist > 0;
  root.querySelector('.tabs').style.display = both ? '' : 'none';
  if (both) return;
  // Při nule na obou stranách rozhoduje role — tatér chce vidět tu svoji.
  showTab(nArtist > 0 ? 'artist' : nClient > 0 ? 'client' : (me.is_artist ? 'artist' : 'client'));
}

async function loadClient() {
  const items = await fetch('/api/me/bookings/client').then(r => r.json()).catch(() => []);
  nClient = items.length;
  document.getElementById('cntClient').textContent = items.length ? `(${items.length})` : '';
  if (!items.length) { document.getElementById('clientEmpty').style.display='block'; return; }
  document.getElementById('clientEmpty').style.display='none';
  document.getElementById('clientList').innerHTML = items.map(b => renderRow(b, 'client')).join('');
}

async function loadArtist() {
  const items = await fetch('/api/me/bookings/artist').then(r => r.json()).catch(() => []);
  nArtist = items.length;
  document.getElementById('cntArtist').textContent = items.length ? `(${items.length})` : '';
  if (!items.length) { document.getElementById('artistEmpty').style.display='block'; return; }
  document.getElementById('artistEmpty').style.display='none';
  document.getElementById('artistList').innerHTML = items.map(b => renderRow(b, 'artist')).join('');
}

function renderRow(b, view) {
  const other = view === 'client' ? b.artist : b.client;
  const ava = other.avatar_url ? `<img src="${escapeHtml(other.avatar_url)}" alt="">`
                                : escapeHtml((other.display_name || '?').slice(0,2).toUpperCase());
  const profileLink = `/profile/${escapeHtml(other.username)}`;
  // primárně používáme konkrétní booking_start_at, fallback na slot.start_at (legacy)
  const startIso = b.booking_start_at || b.slot.start_at;
  const start = new Date(startIso);
  const hoursBefore = (start - new Date()) / 36e5;
  const isPast = hoursBefore < 0;
  const isCancellable = ['confirmed','pending_payment'].includes(b.status) && !isPast;
  const isArtistView = view === 'artist';
  const canComplete = isArtistView && ['confirmed','pending_payment'].includes(b.status);
  // Editovatelnost: confirmed/pending, ne v minulosti. Klient může vždy popis,
  // tatér i termín — backend permission to enforces.
  const isEditable  = ['confirmed','pending_payment'].includes(b.status) && !isPast;

  // refund tier display for cancellable
  let refundHint = '';
  if (isCancellable && view === 'client') {
    const pct = hoursBefore >= 96 ? 100 : hoursBefore >= 48 ? 50 : 0;
    refundHint = `<div class="b-money" style="margin-top:4px">Cancel now: refund ${pct}% (${pct === 100 ? 'free' : pct === 50 ? '50% back' : 'deposit forfeited'})</div>`;
  }

  let cancelInfo = '';
  if (b.status === 'cancelled_client' || b.status === 'cancelled_artist') {
    const pct = b.deposit_cents ? Math.round(b.refund_cents / b.deposit_cents * 100) : 0;
    cancelInfo = `<div class="b-money">Cancelled by ${b.cancellation_actor === 'artist' ? 'artist' : 'client'} — refund ${pct}% (${fmtKc(b.refund_cents)})</div>`;
  }

  let onsiteInfo = '';
  if (b.status === 'completed') {
    const onsiteKc = b.onsite_amount_cents || 0;
    const balKc    = b.balance_paid_cents || 0;
    if (onsiteKc || balKc) {
      const parts = [];
      if (onsiteKc) parts.push(`on-site <b>${fmtKc(onsiteKc)}</b>`);
      if (balKc)    parts.push(`via InkLink <b>${fmtKc(balKc)}</b>`);
      const totalKc = (b.deposit_cents || 0) + onsiteKc + balKc;
      onsiteInfo = `<div class="b-money">Doplatek: ${parts.join(' · ')} · celkem <b>${fmtKc(totalKc)}</b></div>`;
    }
  }
  // ── REVIEWS ──────────────────────────────────────────────────────────
  // U completed bookingu: klient může napsat/upravit, tatér může odpovědět.
  let reviewCta = '';
  let reviewBlock = '';
  if (b.status === 'completed') {
    if (b.review) {
      // ukázat existing review v řádku
      reviewBlock = renderReviewBlock(b.review, view, b.id);
      if (view === 'client') {
        reviewCta = `<button class="btn muted" onclick="InkLinkBookings.openReview(${b.id}, ${b.review.id}, ${b.review.rating}, ${JSON.stringify(b.review.text).replace(/"/g,'&quot;')})">Edit ★</button>`;
      } else if (view === 'artist' && !b.review.response) {
        reviewCta = `<button class="btn" onclick="InkLinkBookings.openRespond(${b.review.id}, ${b.review.rating}, ${JSON.stringify(b.review.text).replace(/"/g,'&quot;')})">${t('bk.reply')}</button>`;
      }
    } else if (view === 'client') {
      reviewCta = `<button class="btn" onclick="InkLinkBookings.openReview(${b.id}, null, 0, '')">★ Write a review</button>`;
    }
  }

  // platební režim badge
  let modeBadge = '';
  if (b.payment_mode === 'full') {
    modeBadge = `<div class="b-money" style="color:#7fd391">✓ Paid in full upfront (${fmtKc(b.deposit_cents)})</div>`;
  } else if (b.balance_due_cents > 0 && b.balance_paid_cents < b.balance_due_cents && b.status === 'completed') {
    const remaining = b.balance_due_cents - (b.balance_paid_cents || 0) - (b.onsite_amount_cents || 0);
    if (remaining > 0) modeBadge = `<div class="b-money" style="color:#e8c87f">⚠ ${fmtKc(remaining)} remaining</div>`;
  }

  // Klient: pokud tatér vystavil balance přes platformu a ještě není zaplaceno → CTA
  let payCta = '';
  const charge = b.balance_charge_cents || 0;
  const paid   = b.balance_paid_cents   || 0;
  if (view === 'client' && b.balance_payment_intent_id) {
    if (charge > paid) {
      // čeká na zaplacení
      const remaining = charge - paid;
      payCta = `<a class="btn" href="/balance-pay/${b.id}" style="background:var(--red2);color:var(--bg);text-decoration:none">Zaplatit doplatek ${fmtKc(remaining)}</a>`;
      modeBadge += `<div class="b-money" style="color:#e8c87f">⚠ Artist sent you a ${fmtKc(remaining)} balance via InkLink</div>`;
    } else if (paid > 0) {
      modeBadge += `<div class="b-money" style="color:#7fd391">✓ InkLink balance paid (${fmtKc(paid)})</div>`;
    }
  }

  // Klient: pending_payment booking se Stripe PI → Pay deposit
  if (view === 'client' && b.status === 'pending_payment' && b.stripe_payment_intent_id) {
    const depAmount = b.payment_mode === 'full' ? (b.total_price_cents || b.deposit_cents) : b.deposit_cents;
    payCta = `<a class="btn" href="/pay/${b.id}" style="background:var(--red2);color:var(--bg);text-decoration:none">Pay deposit ${fmtKc(depAmount)}</a>` + (payCta ? ' ' + payCta : '');
  }

  // ── ŽÁDOST O PŘESUN (čeká na tatéra) ──────────────────────────────────
  const rs = rescheduleByBooking[b.id];
  let rescheduleHint = '';
  if (rs && rs.status === 'pending') {
    const when = fmtDate(rs.new_booking_start_at);
    rescheduleHint = `<div class="b-money" style="color:#e8c87f">${t('bk.rescheduleAsk').replace('{when}', escapeHtml(when))}${
      view === 'artist'
        ? ` <button class="btn" style="margin-left:8px" onclick="InkLinkBookings.decideReschedule(${rs.id},'approve')">${t('bk.approve')}</button>
            <button class="btn danger" onclick="InkLinkBookings.decideReschedule(${rs.id},'reject')">${t('bk.reject')}</button>`
        : t('bk.waitingArtist')}</div>`;
  }

  // ── REFUND REQUEST block + button ─────────────────────────────────────
  const rr = refundByBooking[b.id];
  let refundBlock = '';
  let refundCta = '';
  if (rr) {
    const amt = fmtKc(rr.amount_cents);
    if (rr.status === 'pending') {
      refundBlock = `<div class="b-money" style="color:#e8c87f">⏳ Refund request ${amt} — awaiting artist decision</div>`;
      if (view === 'artist') {
        refundCta = `
          <button class="btn" onclick="InkLinkBookings.decideRefund(${rr.id}, 'approve')">${t('bk.approveRefund')}</button>
          <button class="btn danger" onclick="InkLinkBookings.decideRefund(${rr.id}, 'reject')">${t('bk.reject')}</button>`;
      }
    } else if (rr.status === 'approved') {
      refundBlock = `<div class="b-money" style="color:#7fd391">✓ Refund ${amt} approved${rr.decision_note ? ' — ' + escapeHtml(rr.decision_note) : ''}</div>`;
    } else if (rr.status === 'rejected') {
      refundBlock = `<div class="b-money" style="color:#e58787">${t('bk.refundRejected')}${rr.decision_note ? ' — ' + escapeHtml(rr.decision_note) : ''}</div>`;
    }
  }
  // Client can request refund only if booking is past-completion or already cancelled
  // (cancellation flow handles refund automatically — request is for everything else).
  const wasPaid = (b.deposit_cents || 0) > 0;
  const refundable = wasPaid && !rr && view === 'client'
    && (b.status === 'completed' || (isPast && b.status !== 'pending_payment'));
  if (refundable) {
    const maxKc = Math.max(0, Math.floor(((b.deposit_cents || 0) + (b.balance_paid_cents || 0) - (b.refund_cents || 0)) / 100));
    if (maxKc > 0) {
      refundCta = `<button class="btn muted" onclick="InkLinkBookings.openRefund(${b.id}, ${maxKc}, '${b.currency || 'CZK'}')">${t('bk.mRefund')}</button>`;
    }
  }

  // Tatér má na rezervaci jediné tlačítko — otevře ji v kalendáři, kde je
  // poznámka, přesun, další sezení, dokončení i zrušení pohromadě.
  // Dvě místa se stejnými akcemi znamenala dvě místa k opravě.
  const calendarCta = view === 'artist'
    ? `<a class="btn muted" href="/calendar#b${b.id}@${escapeHtml(String(startIso).slice(0, 10))}">${t('bk.openInCalendar')}</a>`
    : '';

  // duration badge
  const durTxt = b.duration_hours
    ? `${(+b.duration_hours).toFixed(1).replace(/\.0$/,'')} h${b.size_label ? ` · ${b.size_label}` : ''}`
    : '';

  return `
    <div class="b-row">
      <div class="b-when">${fmtDate(startIso)}${durTxt?`<div style="font-size:10px;color:var(--txt3);letter-spacing:0.1em;margin-top:3px">${escapeHtml(durTxt.toUpperCase())}</div>`:''}</div>
      <div class="b-mid">
        <a class="b-who" href="${profileLink}">
          <span class="b-avatar">${ava}</span>
          <span><b>${escapeHtml(other.display_name)}</b><span style="color:var(--txt3)"> · @${escapeHtml(other.username)}</span></span>
        </a>
        ${b.design_note ? `<div class="b-note">${escapeHtml(b.design_note)}</div>` : ''}
        ${(b.session_number || 1) > 1 ? `<div style="font-size:11px;color:var(--txt3);letter-spacing:0.06em;margin-top:4px">${t('bk.session').replace('{n}', b.session_number)}${b.parent_booking_id?` · ${t('bk.followsOn').replace('{id}', b.parent_booking_id)}`:''}</div>` : ''}
        ${rescheduleHint}
        <div class="b-money">${b.payment_mode === 'full' ? t('bk.fullPayment') : t('bk.deposit')} <b>${fmtKc(b.deposit_cents, b.currency)}</b>${b.platform_fee_cents?` · ${t('bk.commission')} ${fmtKc(b.platform_fee_cents, b.currency)}`:''}</div>
        ${modeBadge}${refundHint}${cancelInfo}${onsiteInfo}${refundBlock}${reviewBlock}
      </div>
      <div class="b-actions">
        <span class="b-status ${b.status}">${statusLabel(b.status)}</span>
        ${payCta}
        ${view === 'artist' ? calendarCta : `
        ${isEditable ? `<button class="btn muted" onclick='InkLinkBookings.openEditBook(${JSON.stringify(b).replace(/'/g, "&#39;")}, "${view}")'>${t('bk.edit')}</button>` : ''}
        ${isCancellable ? `<button class="btn muted" onclick='InkLinkBookings.openReschedule(${JSON.stringify(b).replace(/'/g, "&#39;")}, "${view}")'>${t('bk.reschedule')}</button>` : ''}
        ${isCancellable ? `<button class="btn danger" onclick="InkLinkBookings.cancelBooking(${b.id})">${t('bk.cancel')}</button>` : ''}`}
        ${reviewCta}
        ${refundCta}
      </div>
    </div>`;
}

function openRefund(bookingId, maxKc) {
  refundingBookingId = bookingId;
  refundingMaxKc = maxKc;
  document.getElementById('rfAmount').value = '';
  document.getElementById('rfAmount').max = maxKc;
  document.getElementById('rfReason').value = '';
  document.getElementById('rfFlash').innerHTML = '';
  document.getElementById('rfMaxLine').textContent = `${t('bk.maxRefundable')} ${InkLinkI18N.money(maxKc, rfCurrency)}`;
  document.getElementById('refundModal').classList.add('show');
  setTimeout(() => document.getElementById('rfReason').focus(), 30);
}

function closeRefund() {
  document.getElementById('refundModal').classList.remove('show');
  refundingBookingId = null;
  refundingMaxKc = 0;
}

async function submitRefund() {
  if (!refundingBookingId) return;
  const flash = document.getElementById('rfFlash');
  const reason = document.getElementById('rfReason').value.trim();
  if (reason.length < 10) {
    flash.innerHTML = '<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">' + t('bk.needReason') + '</div>';
    return;
  }
  const amountStr = document.getElementById('rfAmount').value.trim();
  const body = { reason };
  if (amountStr) {
    const v = parseFloat(amountStr);
    if (!v || v <= 0 || v > refundingMaxKc) {
      flash.innerHTML = `<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">Amount out of range (1–${refundingMaxKc} CZK).</div>`;
      return;
    }
    body.amount_kc = v;
  }
  const r = await fetch(`/api/bookings/${refundingBookingId}/refund-request`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    flash.innerHTML = `<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">${j.error || t('bk.errRequest')}</div>`;
    return;
  }
  closeRefund();
  await loadRefundRequests();
  await Promise.all([loadClient(), loadArtist()]); syncTabs();
}

async function decideRefund(rid, decision) {
  let note = '';
  if (decision === 'reject') {
    note = prompt(t('bk.rejectReason')) || '';
  } else {
    if (!confirm('Approve refund? Stripe will return the money to the client and this action cannot be undone.')) return;
  }
  const r = await fetch(`/api/refund-requests/${rid}/decide`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ decision, note }),
  });
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    alert(j.error || t('bk.errGeneric'));
    return;
  }
  await loadRefundRequests();
  await Promise.all([loadClient(), loadArtist()]); syncTabs();
}

async function cancelBooking(id) {
  if (!confirm(t('bk.confirmCancel'))) return;
  const r = await fetch(`/api/bookings/${id}/cancel`, {method:'POST'});
  if (r.ok) {
    const j = await r.json();
    alert(`Booking cancelled. ${j.hours_before > 0 ? `${Math.round(j.hours_before)} h before appointment` : 'after appointment'} → refund ${j.refund_pct}%.`);
    await Promise.all([loadClient(), loadArtist()]); syncTabs();
  } else {
    const j = await r.json().catch(() => ({}));
    alert(j.error || t('bk.errCancel'));
  }
}

// ── EDIT BOOKING ─────────────────────────────────────────────────────────
let editingBookingId = null;
let editingView = 'client';

function openEditBook(booking, view) {
  editingBookingId = booking.id;
  editingView = view;
  document.getElementById('ebmTitle').textContent = t('bk.editDesc');
  document.getElementById('ebmNote').value = booking.design_note || '';
  document.getElementById('ebmFlash').innerHTML = '';
  document.getElementById('editBookModal').classList.add('show');
}

function closeEditBook() {
  document.getElementById('editBookModal').classList.remove('show');
  editingBookingId = null;
}

async function submitEditBook() {
  if (!editingBookingId) return;
  const r = await fetch(`/api/bookings/${editingBookingId}`, {
    method: 'PATCH',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ design_note: document.getElementById('ebmNote').value }),
  });
  if (r.ok) {
    closeEditBook();
    if (editingView === 'client') await loadClient();
    else await loadArtist();
  } else {
    document.getElementById('ebmFlash').innerHTML =
      `<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">${(await r.json()).error || 'Selhalo'}</div>`;
  }
}

// ── PŘESUN REZERVACE ─────────────────────────────────────────────────────
// Volné bloky tatéra bereme z jeho veřejného profilu — ten už vrací sloty
// i s obsazenými sub-rangy, takže není potřeba nový read endpoint.

async function artistFreeSlots(username) {
  try {
    const p = await fetch('/api/profile/' + encodeURIComponent(username)).then(r => r.json());
    return (p.slots || []).filter(s => s.status !== 'booked');
  } catch { return []; }
}

function slotOptionLabel(s) {
  const d = new Date(s.start_at);
  return `${d.toLocaleDateString(locale(),{day:'numeric',month:'short'})} · `
       + `${s.start_at.slice(11,16)}–${s.end_at.slice(11,16)}`;
}

function startOptionsFor(slot, durationH) {
  // Půlhodinové kroky od začátku bloku, poslední možný start = konec − délka.
  const out = [];
  const start = new Date(slot.start_at), end = new Date(slot.end_at);
  const last = new Date(end.getTime() - durationH * 3600 * 1000);
  for (let t = new Date(start); t <= last; t = new Date(t.getTime() + 30*60*1000)) {
    const iso = new Date(t.getTime() - t.getTimezoneOffset()*60000).toISOString().slice(0,19);
    out.push({ iso, label: iso.slice(11,16) });
  }
  return out;
}

async function openReschedule(booking, view) {
  rsBooking = booking;
  const other = view === 'client' ? booking.artist : booking.client;
  const username = (view === 'client' ? booking.artist : booking.client).username;
  document.getElementById('rsFlash').innerHTML = '';
  document.getElementById('rsHint').textContent = view === 'artist'
    ? t('bk.moveArtist')
    : t('bk.moveClient');
  rsSlots = await artistFreeSlots(view === 'client' ? username : me.username);
  const sel = document.getElementById('rsSlot');
  sel.innerHTML = rsSlots.map((s,i) => `<option value="${i}">${escapeHtml(slotOptionLabel(s))}</option>`).join('')
                  || `<option value="">${t('bk.noFreeSlots')}</option>`;
  renderRescheduleStarts();
  document.getElementById('rescheduleModal').classList.add('show');
}

function renderRescheduleStarts() {
  const idx = document.getElementById('rsSlot').value;
  const slot = rsSlots[idx];
  const sel = document.getElementById('rsStart');
  if (!slot) { sel.innerHTML = ''; return; }
  const dur = rsBooking?.duration_hours || 1;
  sel.innerHTML = startOptionsFor(slot, dur)
    .map(o => `<option value="${o.iso}">${o.label}</option>`).join('')
    || `<option value="">${t('bk.slotTooShort')}</option>`;
}

function closeReschedule() {
  document.getElementById('rescheduleModal').classList.remove('show');
  rsBooking = null;
}

async function submitReschedule() {
  if (!rsBooking) return;
  const idx = document.getElementById('rsSlot').value;
  const slot = rsSlots[idx];
  const start = document.getElementById('rsStart').value;
  if (!slot || !start) {
    document.getElementById('rsFlash').innerHTML =
      `<div style="color:#e58787;font-size:11px">${t('bk.mPickSlot')}</div>`; return;
  }
  const btn = document.getElementById('rsSubmit');
  btn.disabled = true; btn.textContent = t('bk.sending');
  const r = await fetch(`/api/bookings/${rsBooking.id}/reschedule`, {
    method: 'PATCH', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ new_slot_id: slot.id, booking_start_at: start }),
  });
  const j = await r.json().catch(() => ({}));
  if (r.ok) {
    document.getElementById('rsFlash').innerHTML = j.applied
      ? `<div style="color:#7fd391;font-size:11px">${t('bk.rescheduled')}</div>`
      : `<div style="color:#e8c87f;font-size:11px">${t('bk.requestSent')}</div>`;
    setTimeout(async () => {
      closeReschedule();
      await loadRescheduleRequests();
      await Promise.all([loadClient(), loadArtist()]); syncTabs();
    }, 900);
  } else {
    document.getElementById('rsFlash').innerHTML =
      `<div style="color:#e58787;font-size:11px">${escapeHtml(j.error || t('bk.errMove'))}</div>`;
  }
  btn.disabled = false; btn.textContent = t('bk.reschedule');
}

async function decideReschedule(rid, decision) {
  const r = await fetch(`/api/reschedule-requests/${rid}/decide`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ decision }),
  });
  if (!r.ok) { alert((await r.json().catch(() => ({}))).error || t('bk.errFailed')); return; }
  await loadRescheduleRequests();
  await Promise.all([loadClient(), loadArtist()]); syncTabs();
}

// ── REVIEWS ──────────────────────────────────────────────────────────────
function starsHtml(rating) {
  const r = Math.round(rating);
  let s = '<span class="star-static">';
  for (let i = 1; i <= 5; i++) {
    s += `<span${i > r ? ' class="empty"' : ''}>★</span>`;
  }
  return s + '</span>';
}

function renderReviewBlock(review, view, bookingId) {
  const date = review.created_at ? new Date(review.created_at).toLocaleDateString(locale(), {day:'numeric',month:'short',year:'numeric'}) : '';
  let html = `<div class="b-review">
    <div>${starsHtml(review.rating)} <b>${review.rating}/5</b> <span style="color:var(--txt3);font-size:10px;letter-spacing:0.05em">${escapeHtml(date)}</span></div>`;
  if (review.text) html += `<div style="margin-top:6px">${escapeHtml(review.text)}</div>`;
  if (review.response) {
    html += `<div class="resp"><b>Artist reply:</b> ${escapeHtml(review.response)}</div>`;
  }
  html += '</div>';
  return html;
}

let reviewBookingId = null, reviewExistingId = null, reviewRating = 0;

function openReview(bookingId, existingId, rating, text) {
  reviewBookingId = bookingId;
  reviewExistingId = existingId;
  reviewRating = rating || 0;
  document.getElementById('rmText').value = text || '';
  setStarPick(reviewRating);
  document.getElementById('rmFlash').innerHTML = '';
  document.getElementById('rmDelete').style.display = existingId ? 'inline-block' : 'none';
  document.getElementById('reviewModal').classList.add('show');
}

function setStarPick(r) {
  reviewRating = r;
  const el = document.getElementById('rmStars');
  el.dataset.rating = r;
  [...el.querySelectorAll('span')].forEach(s => {
    s.classList.toggle('on', parseInt(s.dataset.v) <= r);
  });
}

// Hvězdičky vznikají až s panelem, takže se nedají navěsit při načtení
// skriptu — na samostatné stránce markup existoval hned, tady ne.
function bindStars() {
  const stars = document.getElementById('rmStars');
  if (!stars) return;
  stars.addEventListener('click', e => {
    if (e.target.dataset && e.target.dataset.v) setStarPick(parseInt(e.target.dataset.v));
  });
  stars.addEventListener('mouseover', e => {
    if (e.target.dataset && e.target.dataset.v) {
      const v = parseInt(e.target.dataset.v);
      [...e.currentTarget.querySelectorAll('span')].forEach(s => {
        s.classList.toggle('on', parseInt(s.dataset.v) <= v);
      });
    }
  });
  stars.addEventListener('mouseleave', () => setStarPick(reviewRating));
}

function closeReview() {
  document.getElementById('reviewModal').classList.remove('show');
  reviewBookingId = reviewExistingId = null; reviewRating = 0;
}

async function submitReview() {
  if (reviewRating < 1) {
    document.getElementById('rmFlash').innerHTML = '<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">' + t('bk.needStars') + '</div>';
    return;
  }
  const text = document.getElementById('rmText').value.trim();
  const r = await fetch(`/api/bookings/${reviewBookingId}/review`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({rating: reviewRating, text}),
  });
  if (r.ok) { closeReview(); await loadClient(); }
  else {
    document.getElementById('rmFlash').innerHTML =
      `<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">${(await r.json()).error || 'Selhalo'}</div>`;
  }
}

async function deleteReview() {
  if (!reviewExistingId) return;
  if (!confirm('Delete your rating?')) return;
  const r = await fetch(`/api/reviews/${reviewExistingId}`, {method:'DELETE'});
  if (r.ok) { closeReview(); await loadClient(); }
  else alert((await r.json()).error || 'Selhalo');
}

let respondReviewId = null;
function openRespond(reviewId, rating, text) {
  respondReviewId = reviewId;
  document.getElementById('respText').value = '';
  document.getElementById('respFlash').innerHTML = '';
  document.getElementById('respClientReview').innerHTML =
    `<div class="b-review" style="margin:0">${starsHtml(rating)} <b>${rating}/5</b>${text?'<div style="margin-top:6px">'+escapeHtml(text)+'</div>':''}</div>`;
  document.getElementById('respondModal').classList.add('show');
}
function closeRespond() {
  document.getElementById('respondModal').classList.remove('show');
  respondReviewId = null;
}
async function submitResponse() {
  const txt = document.getElementById('respText').value.trim();
  if (!txt) {
    document.getElementById('respFlash').innerHTML = '<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">' + t('bk.needReply') + '</div>';
    return;
  }
  const r = await fetch(`/api/reviews/${respondReviewId}/respond`, {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({response: txt}),
  });
  if (r.ok) { closeRespond(); await loadArtist(); }
  else {
    document.getElementById('respFlash').innerHTML =
      `<div style="color:#e58787;font-size:11px;letter-spacing:0.04em">${(await r.json()).error || 'Selhalo'}</div>`;
  }
}

async function showCalendarSubscribe() {
  const box = document.getElementById('calSubBox');
  if (box.style.display === 'block') { box.style.display = 'none'; return; }
  try {
    const r = await fetch('/api/me/calendar-token');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    document.getElementById('calSubUrl').value = d.subscribe_url;
    box.style.display = 'block';
  } catch (e) {
    alert(t('bk.errIcsUrl'));
  }
}

function copyCalSubUrl() {
  const el = document.getElementById('calSubUrl');
  el.select();
  try {
    navigator.clipboard.writeText(el.value);
    alert(t('bk.okCopied'));
  } catch {}
}

async function regenerateCalToken() {
  if (!confirm('Generate a new token? Old subscribe URL will stop working — you will need to replace it in your calendar.')) return;
  try {
    const r = await fetch('/api/me/calendar-token', {method:'POST'});
    if (!r.ok) throw new Error();
    const d = await r.json();
    document.getElementById('calSubUrl').value = d.subscribe_url;
    alert(t('bk.okRegen'));
  } catch {
    alert(t('bk.errRegen'));
  }
}

  return {mount, cancelBooking, closeEditBook, closeRefund, closeReschedule, closeRespond, closeReview, copyCalSubUrl, decideRefund, decideReschedule, deleteReview, openEditBook, openRefund, openReschedule, openRespond, openReview, regenerateCalToken, renderRescheduleStarts, showCalendarSubscribe, submitEditBook, submitRefund, submitReschedule, submitResponse, submitReview};
})();
