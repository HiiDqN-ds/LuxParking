/* Reserve page: quick duration buttons set the return time from the pick-up time. */
(function () {
  'use strict';
  var pick = document.getElementById('pickup-at'), ret = document.getElementById('return-at');
  if (!pick || !ret) return;
  function fmt(d) { var p = function (n) { return String(n).padStart(2, '0'); };
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes()); }
  document.querySelectorAll('[data-days]').forEach(function (b) {
    b.addEventListener('click', function () {
      var start = new Date(pick.value); if (isNaN(start)) return;
      start.setDate(start.getDate() + Number(b.dataset.days));
      ret.value = fmt(start);
      ret.dispatchEvent(new Event('change', { bubbles: true }));
      document.querySelectorAll('[data-days]').forEach(function (x) { x.setAttribute('aria-pressed', String(x === b)); });
    });
  });
})();
