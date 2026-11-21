// A section marked data-live (with an id) re-reads itself from the server every
// 15 seconds, so new requests and payments appear without a reload. It waits
// while someone is typing or has ticked something in it, so nothing is lost.
document.querySelectorAll('[data-live][id]').forEach(function (section) {
  setInterval(function () {
    if (document.hidden || section.contains(document.activeElement) ||
        section.querySelector('input:checked, textarea:not(:placeholder-shown)')) return;
    fetch(location.href, {headers: {'Accept': 'text/html'}})
      .then(function (response) { return response.ok ? response.text() : null; })
      .then(function (html) {
        if (!html) return;
        var fresh = new DOMParser().parseFromString(html, 'text/html').getElementById(section.id);
        if (fresh && fresh.innerHTML !== section.innerHTML) section.innerHTML = fresh.innerHTML;
      })
      .catch(function () {});
  }, 15000);
});
