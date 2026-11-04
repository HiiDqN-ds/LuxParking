/* Developer portal: code tabs (remembers the language), copy buttons, endpoint filter, active endpoint in the side list. */
(function () {
  'use strict';
  var KEY = 'pp-dev-lang';
  function remembered() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function remember(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } }

  function select(group, lang) {
    var buttons = group.querySelectorAll('[role=tab]');
    var has = Array.prototype.some.call(buttons, function (b) { return b.dataset.lang === lang; });
    if (!has) return;
    buttons.forEach(function (b) { b.setAttribute('aria-selected', b.dataset.lang === lang ? 'true' : 'false'); });
    group.querySelectorAll('.panel[data-lang]').forEach(function (p) { p.hidden = p.dataset.lang !== lang; });
  }
  document.querySelectorAll('.codetabs[data-tabs]').forEach(function (group) {
    var saved = remembered();
    if (saved) select(group, saved);
    group.querySelectorAll('[role=tab]').forEach(function (b) {
      b.addEventListener('click', function () {
        remember(b.dataset.lang);
        document.querySelectorAll('.codetabs[data-tabs]').forEach(function (g) { select(g, b.dataset.lang); });
      });
    });
  });

  document.querySelectorAll('[data-copy]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var box = btn.closest('.codetabs');
      var panel = box.querySelector('.panel:not([hidden]) code');
      if (!panel || !navigator.clipboard) return;
      var old = btn.textContent;
      navigator.clipboard.writeText(panel.textContent).then(function () {
        btn.classList.add('done'); btn.textContent = '✓';
        setTimeout(function () { btn.classList.remove('done'); btn.textContent = old; }, 1400);
      });
    });
  });

  var search = document.getElementById('ref-search');
  if (search) {
    search.addEventListener('input', function () {
      var q = search.value.trim().toLowerCase();
      document.querySelectorAll('.ref-nav li[data-filter], .op[data-filter]').forEach(function (el) {
        el.hidden = q && el.dataset.filter.toLowerCase().indexOf(q) === -1;
      });
    });
  }

  function openFromHash() {
    var id = location.hash.slice(1);
    var op = id && document.getElementById(id);
    if (op && op.tagName === 'DETAILS') op.open = true;
  }
  window.addEventListener('hashchange', openFromHash); openFromHash();

  if ('IntersectionObserver' in window) {
    var items = {};
    document.querySelectorAll('.ref-nav li[data-filter] a').forEach(function (a) { items[a.getAttribute('href').slice(1)] = a.parentNode; });
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting && items[e.target.id]) {
          Object.keys(items).forEach(function (k) { items[k].classList.remove('active'); });
          items[e.target.id].classList.add('active');
        }
      });
    }, { rootMargin: '-20% 0px -70% 0px' });
    Object.keys(items).forEach(function (id) { var el = document.getElementById(id); if (el) seen.observe(el); });
  }
})();
