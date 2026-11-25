// Ask before a destructive form submits, in the page's own dialog rather
// than the browser's. A form opts in with data-confirm="Question?" and may
// name its button with data-confirm-action and add data-confirm-detail.
(function () {
  var dialog = document.getElementById('confirm-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') return;

  var title = dialog.querySelector('[data-confirm-title]');
  var detail = dialog.querySelector('[data-confirm-detail]');
  var accept = dialog.querySelector('[data-confirm-accept]');
  var pending = null;

  document.addEventListener('submit', function (event) {
    var form = event.target;
    if (!form.matches('form[data-confirm]') || form.dataset.confirmed === 'yes') return;
    event.preventDefault();
    pending = form;
    title.textContent = form.dataset.confirm;
    detail.textContent = form.dataset.confirmDetail || '';
    detail.hidden = !form.dataset.confirmDetail;
    accept.textContent = form.dataset.confirmAction || 'Confirm';
    dialog.showModal();
  });

  accept.addEventListener('click', function () {
    if (!pending) return;
    var form = pending;
    pending = null;
    dialog.close();
    form.dataset.confirmed = 'yes';
    form.requestSubmit ? form.requestSubmit() : form.submit();
  });

  dialog.addEventListener('close', function () { pending = null; });

  // A click on the dimmed area closes it. The .modal dialog fills the screen,
  // so the overlay is the dialog itself and closedby="any" never sees an
  // outside click. A press that starts inside the box (selecting text and
  // letting go outside) does not count.
  var pressedOverlay = false;
  dialog.addEventListener('pointerdown', function (event) { pressedOverlay = event.target === dialog; });
  dialog.addEventListener('click', function (event) {
    if (pressedOverlay && event.target === dialog) dialog.close();
  });
})();
