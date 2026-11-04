// Replaces the inline onclick/onsubmit/onchange handlers so the Content-Security-Policy can forbid inline scripts.
(function () {
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form && form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  }, true);

  document.addEventListener("change", function (event) {
    var el = event.target;
    if (el && el.hasAttribute && el.hasAttribute("data-autosubmit") && el.form) el.form.submit();
  });

  document.addEventListener("click", function (event) {
    var el = event.target;
    if (!el || !el.closest) return;
    var all = el.closest("[data-select-all]");
    if (all) {
      var name = all.getAttribute("data-select-all");
      document.querySelectorAll('[name="' + name + '"]').forEach(function (box) { box.checked = all.checked; });
      return;
    }
    var area = el.closest("[data-select-on-click]");
    if (area && area.select) { area.select(); return; }
    var copy = el.closest("[data-copy-previous]");
    if (copy && copy.parentNode.previousElementSibling && navigator.clipboard) {
      navigator.clipboard.writeText(copy.parentNode.previousElementSibling.value);
    }
  });
})();
