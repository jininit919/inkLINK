// InkLink icon loader — synchronously inject /icons.svg sprite into <body>
// Once loaded, use anywhere: <svg class="icon"><use href="#i-heart"/></svg>
(function () {
  var SPRITE_V = 2;   // ↑ při každé změně public/icons.svg
  // Run as soon as body exists
  function inject() {
    if (window.__inkLinkIconsLoaded) return;
    window.__inkLinkIconsLoaded = true;
    // POZOR: force-cache bez verze znamená, že prohlížeč drží starý sprite
    // a nově přidaná ikona se u vracejícího se uživatele NIKDY neobjeví.
    // Při každé změně icons.svg zvedni SPRITE_V.
    fetch('/icons.svg?v=' + SPRITE_V, { cache: 'force-cache' })
      .then(function (r) { return r.text(); })
      .then(function (txt) {
        var div = document.createElement('div');
        div.id = 'il-icon-sprite';
        div.setAttribute('aria-hidden', 'true');
        div.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden';
        div.innerHTML = txt;
        function attach() {
          document.body.prepend(div);
          document.dispatchEvent(new Event('il-icons-ready'));
        }
        if (document.body) attach();
        else document.addEventListener('DOMContentLoaded', attach);
      })
      .catch(function () { /* tichý fail — sprite není kritický */ });
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', inject);
  } else {
    inject();
  }
})();
