/* Dárkové poukazy — nákup a uplatnění.
 *
 * Jeden soubor pro feed i profil tatéra. Poukaz je univerzální kredit:
 * kupuje ho jeden člověk druhému a obdarovaný si vybere kohokoliv. Proto
 * se z profilu tatéra kupuje ten samý poukaz jako z feedu — profil je jen
 * místo, kde člověka nákup napadne, ne příjemce peněz.
 *
 * Limity a částky se tahají z /api/vouchers/options. Natvrdo tu být
 * nesmí: jsou per měnu a měna se odvozuje ze země, ne z volby uživatele.
 */
(function () {
  'use strict';
  if (window.InkLinkVoucher) return;

  var opts = null;          // cache odpovědi /api/vouchers/options
  var esc = function (s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  };
  var t = function (k, f) {
    return (window.InkLinkI18N && window.InkLinkI18N.t) ? window.InkLinkI18N.t(k, f) : f;
  };

  function styles() {
    if (document.getElementById('ilv-css')) return;
    var el = document.createElement('style');
    el.id = 'ilv-css';
    el.textContent = [
      /* Dlaždice do feedu. Vypadá jako lístek, ne jako něčí práce — kdyby
         splynula s fotkami, čte se jako tetování, které nikdo nedělal. */
      '.ilv-tile{break-inside:avoid;-webkit-column-break-inside:avoid;page-break-inside:avoid;',
      'margin-bottom:10px;background:linear-gradient(160deg,#fffdf7,#f2ece0);',
      'border:1px solid var(--border);border-radius:10px;position:relative;overflow:hidden;',
      'cursor:pointer;display:block;padding:20px 18px 16px;',
      'transition:transform .25s cubic-bezier(.32,.72,0,1),box-shadow .25s,border-color .15s}',
      '.ilv-tile:hover{border-color:var(--border2);transform:translateY(-3px);box-shadow:0 14px 32px rgba(20,16,8,.10)}',
      '.ilv-tile .k{font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--txt3)}',
      '.ilv-tile .h{font-family:"Bristol","Caveat",cursive;font-size:34px;line-height:1;margin:10px 0 6px;color:var(--txt)}',
      '.ilv-tile .s{font-size:12px;line-height:1.5;color:var(--txt3)}',
      '.ilv-tile .stub{margin-top:14px;padding-top:12px;border-top:1px dashed var(--border2);',
      'display:flex;align-items:center;justify-content:space-between;gap:8px}',
      '.ilv-tile .amt{font-size:12px;color:var(--txt2);letter-spacing:.02em}',
      /* Šipka nesmí spadnout pod slovo — v úzkém sloupci feedu to jinak dělá. */
      '.ilv-tile .go{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--txt);white-space:nowrap}',
      /* varianta na profil tatéra — na šířku, aby nesoupeřila s portfoliem */
      '.ilv-tile.wide{padding:16px 18px}',
      '.ilv-tile.wide .h{font-size:26px;margin:6px 0 4px}',

      '.ilv-back{position:fixed;inset:0;background:rgba(20,16,8,.55);backdrop-filter:blur(3px);',
      'z-index:400;display:flex;align-items:center;justify-content:center;padding:16px}',
      '.ilv-modal{background:var(--bg);border:1px solid var(--border);border-radius:12px;',
      'max-width:440px;width:100%;max-height:92vh;overflow-y:auto;padding:22px}',
      '.ilv-modal h3{font-size:17px;margin:0 0 4px;letter-spacing:-.01em}',
      '.ilv-modal .lede{font-size:12.5px;line-height:1.55;color:var(--txt3);margin:0 0 16px}',
      '.ilv-lbl{display:block;font-size:10px;letter-spacing:.14em;text-transform:uppercase;',
      'color:var(--txt3);margin:14px 0 6px}',
      '.ilv-amts{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}',
      '.ilv-amts button{padding:11px 4px;border:1px solid var(--border);background:var(--bg2);',
      'border-radius:7px;cursor:pointer;font-size:12.5px;color:var(--txt2);font-family:inherit}',
      '.ilv-amts button:hover{border-color:var(--border2)}',
      '.ilv-amts button.on{background:var(--txt);color:var(--bg);border-color:var(--txt)}',
      '.ilv-modal input,.ilv-modal textarea{width:100%;padding:11px 12px;border:1px solid var(--border);',
      'border-radius:7px;background:var(--bg2);color:var(--txt);font-size:14px;font-family:inherit}',
      '.ilv-modal textarea{resize:vertical;min-height:64px;font-size:13px}',
      '.ilv-modal input:focus,.ilv-modal textarea:focus{outline:none;border-color:var(--txt3)}',
      '#ilvCode{letter-spacing:.22em;text-transform:uppercase;text-align:center;font-size:16px}',
      '.ilv-hint{font-size:11px;color:var(--txt3);margin-top:5px;line-height:1.45}',
      '.ilv-err{font-size:12px;color:#8a2a2a;margin-top:12px;line-height:1.45;display:none}',
      '.ilv-ok{font-size:13px;line-height:1.55;color:var(--txt2);margin-top:12px;display:none}',
      '.ilv-row{display:flex;gap:8px;margin-top:18px}',
      '.ilv-row button{flex:1;padding:12px;border-radius:7px;font-size:13px;cursor:pointer;font-family:inherit}',
      '.ilv-go{background:var(--txt);color:var(--bg);border:1px solid var(--txt)}',
      '.ilv-go:disabled{opacity:.5;cursor:default}',
      '.ilv-cancel{background:none;border:1px solid var(--border);color:var(--txt3)}',
      '.ilv-credit{margin-top:14px;padding:11px 13px;background:var(--bg3);border-radius:7px;',
      'font-size:12.5px;color:var(--txt2);line-height:1.5}',
      '.ilv-vrow{display:flex;align-items:center;gap:10px;padding:11px 0;border-top:1px solid var(--border)}',
      '.ilv-vrow>div{flex:1;min-width:0;font-size:13px}',
      '.ilv-vrow .w{color:var(--txt3);font-size:12px}',
      '.ilv-vrow .c{color:var(--txt3);font-size:11.5px;letter-spacing:.08em;margin-top:3px}',
      '.ilv-vrow .p{font-size:11px;letter-spacing:.1em;text-transform:uppercase;',
      'border:1px solid var(--border);padding:7px 11px;border-radius:6px;color:var(--txt2);white-space:nowrap}',
      '.ilv-vrow .p:hover{border-color:var(--border2)}'
    ].join('');
    document.head.appendChild(el);
  }

  function load() {
    if (opts) return Promise.resolve(opts);
    return fetch('/api/vouchers/options')
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)); })
      .then(function (d) { opts = d; return d; });
  }

  function money(v, cur) {
    if (window.InkLinkI18N && window.InkLinkI18N.money) return window.InkLinkI18N.money(v, cur);
    return v + ' ' + (cur || '');
  }

  function close() {
    var b = document.querySelector('.ilv-back');
    if (b) b.remove();
    document.removeEventListener('keydown', onEsc);
  }
  function onEsc(e) { if (e.key === 'Escape') close(); }

  function shell(html) {
    close();
    styles();
    var back = document.createElement('div');
    back.className = 'ilv-back';
    back.innerHTML = '<div class="ilv-modal" role="dialog" aria-modal="true">' + html + '</div>';
    back.addEventListener('click', function (e) { if (e.target === back) close(); });
    document.body.appendChild(back);
    document.addEventListener('keydown', onEsc);
    return back.querySelector('.ilv-modal');
  }

  /* ── Dlaždice ──────────────────────────────────────────────────────── */

  function tile(o) {
    styles();
    o = o || {};
    var sub = o.artist
      ? t('gv.tileSubArtist', 'Nevíš, co vybrat? Poukaz nechá výběr na tom, kdo ho dostane — u {name} i kdekoliv jinde.')
          .replace('{name}', esc(o.artist))
      : t('gv.tileSub', 'Dárek pro toho, kdo si tetování chce vybrat po svém. Kredit platí u kteréhokoliv tatéra.');
    return '<article class="ilv-tile' + (o.wide ? ' wide' : '') + '" '
      + 'onclick="InkLinkVoucher.openBuy()" role="button" tabindex="0" '
      + 'onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();InkLinkVoucher.openBuy()}">'
      + '<div class="k">' + t('gv.tileKind', 'Dárkový poukaz') + '</div>'
      + '<div class="h">' + t('gv.tileTitle', 'Daruj tetování') + '</div>'
      + '<div class="s">' + sub + '</div>'
      + '<div class="stub"><span class="amt">' + t('gv.tileAmt', 'Libovolná částka') + '</span>'
      + '<span class="go">' + t('gv.tileGo', 'Koupit →') + '</span></div>'
      + '</article>';
  }

  /* ── Nákup ─────────────────────────────────────────────────────────── */

  function openBuy() {
    var m = shell('<h3>' + t('gv.buyTitle', 'Dárkový poukaz') + '</h3>'
      + '<p class="lede">' + t('gv.buyLede', 'Načítám…') + '</p>');
    load().then(function (d) {
      if (!d.logged_in) {
        m.innerHTML = '<h3>' + t('gv.buyTitle', 'Dárkový poukaz') + '</h3>'
          + '<p class="lede">' + t('gv.needLogin',
              'Poukaz se kupuje z účtu, ať se ti kód neztratí a můžeš si ho pak vytisknout.') + '</p>'
          + '<div class="ilv-row"><button class="ilv-cancel" onclick="InkLinkVoucher.close()">'
          + t('gv.cancel', 'Zpět') + '</button>'
          + '<button class="ilv-go" onclick="location.href=\'/login?next=\'+encodeURIComponent(location.pathname)">'
          + t('gv.login', 'Přihlásit se') + '</button></div>';
        return;
      }
      var chips = d.presets.map(function (p) {
        return '<button type="button" data-a="' + p + '">' + esc(money(p, d.currency)) + '</button>';
      }).join('');
      m.innerHTML = '<h3>' + t('gv.buyTitle', 'Dárkový poukaz') + '</h3>'
        + '<p class="lede">' + t('gv.buyLede2',
            'Obdarovaný zadá kód a částka se mu připíše jako kredit. Utratí ji u kohokoliv na InkLinku — '
            + 'za celé tetování i jen za zálohu.') + '</p>'
        + '<span class="ilv-lbl">' + t('gv.amount', 'Částka') + '</span>'
        + '<div class="ilv-amts">' + chips + '</div>'
        + '<input id="ilvAmt" type="number" inputmode="numeric" style="margin-top:8px" '
        + 'min="' + d.min + '" max="' + d.max + '" placeholder="'
        + t('gv.ownAmount', 'Vlastní částka') + '">'
        + '<div class="ilv-hint">' + t('gv.range', 'Od {min} do {max} · platí {months} měsíců')
            .replace('{min}', esc(money(d.min, d.currency)))
            .replace('{max}', esc(money(d.max, d.currency)))
            .replace('{months}', d.valid_months) + '</div>'
        + '<span class="ilv-lbl">' + t('gv.forWhom', 'Pro koho') + '</span>'
        + '<input id="ilvTo" maxlength="80" placeholder="' + t('gv.forWhomPh', 'Jméno') + '">'
        + '<span class="ilv-lbl">' + t('gv.msg', 'Vzkaz na poukaz') + '</span>'
        + '<textarea id="ilvMsg" maxlength="300" placeholder="'
        + t('gv.msgPh', 'Nepovinné') + '"></textarea>'
        + '<div class="ilv-err" id="ilvErr"></div>'
        + '<div class="ilv-row"><button class="ilv-cancel" onclick="InkLinkVoucher.close()">'
        + t('gv.cancel', 'Zpět') + '</button>'
        + '<button class="ilv-go" id="ilvGo">' + t('gv.pay', 'Zaplatit') + '</button></div>';

      var amt = m.querySelector('#ilvAmt');
      m.querySelectorAll('.ilv-amts button').forEach(function (b) {
        b.addEventListener('click', function () {
          m.querySelectorAll('.ilv-amts button').forEach(function (x) { x.classList.remove('on'); });
          b.classList.add('on');
          amt.value = b.dataset.a;
        });
      });
      // Ruční částka přebíjí vybranou dlaždici — jinak by uživatel viděl
      // zvýrazněných 2 000 a platil 3 500.
      amt.addEventListener('input', function () {
        m.querySelectorAll('.ilv-amts button').forEach(function (x) {
          x.classList.toggle('on', x.dataset.a === amt.value);
        });
      });
      m.querySelector('#ilvGo').addEventListener('click', function () { submitBuy(m, d); });
    }, function () {
      m.querySelector('.lede').textContent = t('gv.loadFail', 'Nepovedlo se načíst. Zkus to prosím znovu.');
    });
  }

  function submitBuy(m, d) {
    var err = m.querySelector('#ilvErr');
    var go = m.querySelector('#ilvGo');
    var v = parseInt(m.querySelector('#ilvAmt').value, 10);
    err.style.display = 'none';
    if (!v || v < d.min || v > d.max) {
      err.textContent = t('gv.range', 'Od {min} do {max} · platí {months} měsíců')
        .replace('{min}', money(d.min, d.currency)).replace('{max}', money(d.max, d.currency))
        .replace('{months}', d.valid_months);
      err.style.display = 'block';
      return;
    }
    go.disabled = true;
    go.textContent = t('gv.paying', 'Přesměrovávám…');
    fetch('/api/vouchers', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        amount_kc: v,
        recipient_name: m.querySelector('#ilvTo').value,
        message: m.querySelector('#ilvMsg').value
      })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.j.error || 'Chyba');
        if (res.j.checkout_url) { location.href = res.j.checkout_url; return; }
        // Demo režim bez Stripu vrací poukaz rovnou platný.
        if (res.j.print_url) { location.href = res.j.print_url; return; }
        throw new Error(t('gv.payFail', 'Platbu se nepovedlo založit.'));
      })
      .catch(function (e) {
        err.textContent = e.message;
        err.style.display = 'block';
        go.disabled = false;
        go.textContent = t('gv.pay', 'Zaplatit');
      });
  }

  /* ── Uplatnění ─────────────────────────────────────────────────────── */

  function openRedeem() {
    var m = shell('<h3>' + t('gv.redeemTitle', 'Uplatnit poukaz') + '</h3>'
      + '<p class="lede">' + t('gv.redeemLede',
          'Zadej kód z poukazu. Částka se ti připíše jako kredit a odečte se při další rezervaci — '
          + 'z celé ceny i ze zálohy.') + '</p>'
      + '<input id="ilvCode" maxlength="14" autocomplete="off" placeholder="XXXX-XXXX-XXXX">'
      + '<div class="ilv-err" id="ilvErr"></div><div class="ilv-ok" id="ilvOk"></div>'
      + '<div class="ilv-row"><button class="ilv-cancel" onclick="InkLinkVoucher.close()">'
      + t('gv.cancel', 'Zpět') + '</button>'
      + '<button class="ilv-go" id="ilvGo">' + t('gv.redeem', 'Uplatnit') + '</button></div>');
    var inp = m.querySelector('#ilvCode');
    // Kód se opisuje z papíru: pomlčky doplňujeme sami, ať se nikdo netrefuje.
    inp.addEventListener('input', function () {
      var raw = inp.value.toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12);
      inp.value = raw.replace(/(.{4})(?=.)/g, '$1-');
    });
    inp.focus();
    m.querySelector('#ilvGo').addEventListener('click', function () { submitRedeem(m); });
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') submitRedeem(m); });
  }

  function submitRedeem(m) {
    var err = m.querySelector('#ilvErr'), ok = m.querySelector('#ilvOk');
    var go = m.querySelector('#ilvGo');
    err.style.display = 'none';
    go.disabled = true;
    fetch('/api/vouchers/redeem', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: m.querySelector('#ilvCode').value })
    }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) throw new Error(res.j.error || 'Kód nesedí.');
        var bal = money((res.j.balance_cents || 0) / 100, res.j.currency);
        ok.innerHTML = t('gv.redeemOk', 'Hotovo. Máš kredit <b>{bal}</b> — odečte se sám při rezervaci.')
          .replace('{bal}', esc(bal));
        ok.style.display = 'block';
        m.querySelector('.ilv-row').innerHTML =
          '<button class="ilv-go" onclick="location.reload()">' + t('gv.done', 'Hotovo') + '</button>';
        opts = null;                         // zůstatek v cache je po uplatnění neplatný
        document.dispatchEvent(new CustomEvent('inklink:credit-changed'));
      })
      .catch(function (e) {
        err.textContent = e.message;
        err.style.display = 'block';
        go.disabled = false;
      });
  }

  /* ── Koupené poukazy ───────────────────────────────────────────────── */

  function openMine() {
    var m = shell('<h3>' + t('gv.mineTitle', 'Moje poukazy') + '</h3>'
      + '<p class="lede">' + t('gv.buyLede', 'Načítám…') + '</p>');
    fetch('/api/vouchers/mine')
      .then(function (r) { return r.ok ? r.json() : Promise.reject(new Error('HTTP ' + r.status)); })
      .then(function (list) {
        if (!list.length) {
          m.querySelector('.lede').textContent = t('gv.mineNone', 'Zatím žádný poukaz.');
          return;
        }
        var rows = list.map(function (v) {
          // Bez předložky: „Pro Tereza" je špatně česky a skloňovat jméno
        // automaticky nejde.
        var who = v.recipient_name ? esc(v.recipient_name) : '';
          var st = v.status === 'redeemed' ? t('gv.mineUsed', 'Uplatněný')
                 : v.status === 'active'   ? t('gv.mineReady', 'Připravený')
                 : t('gv.mineUnpaid', 'Nezaplacený');
          return '<div class="ilv-vrow"><div><b>' + esc(money(v.amount_kc, v.currency)) + '</b>'
            + (who ? ' <span class="w">' + who + '</span>' : '')
            + '<div class="c">' + (v.code ? esc(v.code) : '—') + ' · ' + st + '</div></div>'
            + (v.print_url
                ? '<a class="p" href="' + esc(v.print_url) + '" target="_blank" rel="noopener">'
                  + t('gv.minePrint', 'Otevřít') + '</a>'
                : '') + '</div>';
        }).join('');
        m.innerHTML = '<h3>' + t('gv.mineTitle', 'Moje poukazy') + '</h3>'
          + '<p class="lede">' + t('gv.mineLede',
              'Kód pošli obdarovanému, nebo poukaz otevři a vytiskni.') + '</p>'
          + rows
          + '<div class="ilv-row"><button class="ilv-cancel" onclick="InkLinkVoucher.close()">'
          + t('gv.done', 'Hotovo') + '</button></div>';
      })
      .catch(function () {
        m.querySelector('.lede').textContent =
          t('gv.loadFail', 'Nepovedlo se načíst. Zkus to prosím znovu.');
      });
  }

  window.InkLinkVoucher = {
    tile: tile, openBuy: openBuy, openRedeem: openRedeem, openMine: openMine,
    close: close, options: load
  };
})();
