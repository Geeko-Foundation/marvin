// A form sends once. A second press while the first is on its way would
// repeat the action, such as starting a second payment, so it is ignored.
// The pressed button is only styled as busy, not disabled, so its name and
// value still go with the form.
(function () {
  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (event.defaultPrevented || form.method.toLowerCase() === 'get' || form.hasAttribute('data-download')) return;
    if (form.dataset.sending) {
      event.preventDefault();
      return;
    }
    form.dataset.sending = 'yes';
    var button = event.submitter;
    if (button) {
      button.classList.add('btn-disabled');
      button.setAttribute('aria-disabled', 'true');
    }
  });

  // Coming back with the browser's back button shows the page as it was left.
  window.addEventListener('pageshow', function (event) {
    if (!event.persisted) return;
    document.querySelectorAll('form[data-sending]').forEach(function (form) {
      delete form.dataset.sending;
      form.querySelectorAll('[aria-disabled="true"]').forEach(function (button) {
        button.classList.remove('btn-disabled');
        button.removeAttribute('aria-disabled');
      });
    });
  });
})();
