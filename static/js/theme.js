(function () {
  const STORAGE_KEY = 'marvin-theme';
  function bindToggle(toggle) {
    const LIGHT = toggle.dataset.light;
    const DARK = toggle.dataset.dark;
    const apply = (theme) => {
      document.documentElement.setAttribute('data-theme', theme);
      // Keep a choice only when it differs from the system setting, so someone
      // who picks what their system already uses keeps following it.
      const system = matchMedia('(prefers-color-scheme: dark)').matches ? DARK : LIGHT;
      if (theme === system) localStorage.removeItem(STORAGE_KEY);
      else localStorage.setItem(STORAGE_KEY, theme);
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
