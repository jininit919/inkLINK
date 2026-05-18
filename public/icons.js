// InkLink icon loader — synchronously inject /icons.svg sprite into <body>
// Once loaded, use anywhere: <svg class="icon"><use href="#i-heart"/></svg>
(function () {
  // Run as soon as body exists
  function inject() {
    if (window.__inkLinkIconsLoaded) return;
    window.__inkLinkIconsLoaded = true;
    fetch('/icons.svg', { cache: 'force-cache' })
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
