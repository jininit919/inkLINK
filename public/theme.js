(function () {
  const THEME_KEY = 'hmo_theme';
  // Paper = nový default pro redesign. Dark/light/purple zůstávají jako legacy.
  const THEMES = ['paper', 'dark', 'purple', 'light'];
  const ICONS  = { paper: '◇', dark: '●', purple: '◆', light: '○' };
  const TITLES = { paper: 'Paper (ink)', dark: 'Dark', purple: 'Purple', light: 'Light' };

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const btn = document.getElementById('themeToggle');
    if (btn) {
      btn.textContent = ICONS[theme] || '◇';
      btn.title = TITLES[theme] || theme;
    }
  }

  function toggleTheme() {
    const current = document.documentElement.dataset.theme || 'paper';
    const idx  = THEMES.indexOf(current);
    const next = THEMES[(idx + 1) % THEMES.length];
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
  }

  const saved = localStorage.getItem(THEME_KEY) || 'paper';
  applyTheme(saved);

  window.toggleTheme = toggleTheme;

  document.addEventListener('DOMContentLoaded', function () {
    const btn = document.getElementById('themeToggle');
    const theme = document.documentElement.dataset.theme || 'paper';
    if (btn) { btn.textContent = ICONS[theme]; btn.title = TITLES[theme]; }
  });
})();
