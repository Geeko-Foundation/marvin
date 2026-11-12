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
})();
