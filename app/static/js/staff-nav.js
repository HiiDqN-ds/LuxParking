/* Staff app: the Back button returns to the previous staff page (works inside the installed app, which has no browser back button). */
(function () {
  'use strict';
  var back = document.getElementById('staff-back');
  if (!back) return;
  back.addEventListener('click', function (event) {
    var ref = document.referrer;
    if (history.length > 1 && ref && ref.indexOf(location.origin + '/staff') === 0) { event.preventDefault(); history.back(); }
  });
})();
