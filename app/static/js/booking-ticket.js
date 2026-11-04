/* Booking page: copy number, full-screen QR (keeps the screen awake while open). */
(function () {
  'use strict';
  var root = document.querySelector('.ticket-page'), dlg = document.getElementById('qr-dialog');
  if (!root) return;
  var lock = null;
  function openQr() {
    if (!dlg) return;
    if (dlg.showModal) dlg.showModal(); else dlg.setAttribute('open', '');
    if (navigator.wakeLock) navigator.wakeLock.request('screen').then(function (l) { lock = l; }).catch(function () {});
  }
  function closeQr() {
    if (dlg.close) dlg.close(); else dlg.removeAttribute('open');
    if (lock) { lock.release().catch(function () {}); lock = null; }
  }
  ['open-qr', 'open-qr-2'].forEach(function (id) { var b = document.getElementById(id); if (b) b.addEventListener('click', openQr); });
  var close = document.getElementById('close-qr');
  if (close) close.addEventListener('click', closeQr);
  if (dlg) {
    dlg.addEventListener('click', function (e) { if (e.target === dlg) closeQr(); });
    dlg.addEventListener('close', function () { if (lock) { lock.release().catch(function () {}); lock = null; } });
  }
  var copy = document.getElementById('copy-ref'), ref = document.getElementById('booking-ref');
  if (copy && ref) copy.addEventListener('click', function () {
    var old = copy.textContent;
    (navigator.clipboard ? navigator.clipboard.writeText(ref.textContent) : Promise.reject()).then(function () {
      copy.textContent = root.dataset.copied; setTimeout(function () { copy.textContent = old; }, 1600);
    }).catch(function () {
      var r = document.createRange(); r.selectNodeContents(ref); var s = getSelection(); s.removeAllRanges(); s.addRange(r);
    });
  });
})();
