(function () {
  const STORAGE_KEY = 'marvin-theme';
  const LIGHT = 'corporate';
  const DARK = 'business';

  function bindToggle(toggle) {
    const apply = (theme) => {
      document.documentElement.setAttribute('data-theme', theme);
      localStorage.setItem(STORAGE_KEY, theme);
      toggle.checked = theme === DARK;
      toggle.setAttribute('aria-checked', toggle.checked ? 'true' : 'false');
    };

    const saved = localStorage.getItem(STORAGE_KEY);
    const current = document.documentElement.getAttribute('data-theme') || LIGHT;
    toggle.checked = (saved || current) === DARK;
    toggle.setAttribute('aria-checked', toggle.checked ? 'true' : 'false');

    toggle.addEventListener('change', () => {
      apply(toggle.checked ? DARK : LIGHT);
    });
  }

  document.querySelectorAll('[data-theme-toggle]').forEach(bindToggle);
})();
