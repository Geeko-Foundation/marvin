// Sections refresh themselves through htmx (hx-trigger="every 15s [quiet(this)]").
// This says when it is safe: not while someone is typing in the section or has
// something ticked in it, and not while the tab is hidden.
function quiet(section) {
  return !document.hidden && !section.contains(document.activeElement) &&
    !section.querySelector('input:checked, textarea:not(:placeholder-shown)');
}

// One polite announcer for the page, which htmx never replaces. A status
// inside a swapped section (marked data-announce) is read out only when its
// text changes, so polling every few seconds stays silent.
(function () {
  var spoken = {};
  function key(el) { var owner = el.closest('[id]'); return owner ? owner.id : ''; }
  document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-announce]').forEach(function (el) { spoken[key(el)] = el.textContent.trim(); });
  });
  document.addEventListener('htmx:afterSettle', function (event) {
    var announcer = document.getElementById('announcer');
    // Look across the page: after an outerHTML swap the event's element can be the old one.
    document.querySelectorAll('[data-announce]').forEach(function (el) {
      var text = el.textContent.trim();
      if (announcer && text && spoken[key(el)] !== text) {
        spoken[key(el)] = text;
        announcer.textContent = text;
      }
    });
  });
})();
