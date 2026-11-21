// Sections refresh themselves through htmx (hx-trigger="every 15s [quiet(this)]").
// This says when it is safe: not while someone is typing in the section or has
// something ticked in it, and not while the tab is hidden.
function quiet(section) {
  return !document.hidden && !section.contains(document.activeElement) &&
    !section.querySelector('input:checked, textarea:not(:placeholder-shown)');
}
