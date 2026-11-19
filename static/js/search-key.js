// "/" jumps to the search box, unless the person is already typing somewhere.
document.addEventListener('keydown', (event) => {
  const box = document.querySelector('[data-search-key]');
  const target = event.target;
  if (event.key !== '/' || !box || event.ctrlKey || event.metaKey || event.altKey) return;
  if (target.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
  event.preventDefault();
  box.focus();
});
