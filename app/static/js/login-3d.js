/* Login card: 3D tilt that follows the pointer, and a light that tracks it. Off for touch and reduced motion. */
(function () {
  'use strict';
  var card = document.querySelector('[data-tilt]');
  if (!card) return;
  if (matchMedia('(prefers-reduced-motion: reduce)').matches || matchMedia('(pointer: coarse)').matches) return;
  var glow = card.querySelector('.login-glow'), raf = 0, tx = 0, ty = 0, gx = 50, gy = 30;
  function apply() {
    raf = 0;
    card.style.transform = 'perspective(900px) rotateX(' + ty.toFixed(2) + 'deg) rotateY(' + tx.toFixed(2) + 'deg)';
    if (glow) glow.style.background = 'radial-gradient(18rem circle at ' + gx + '% ' + gy + '%, rgba(255,255,255,.14), transparent 60%)';
  }
  function queue() { if (!raf) raf = requestAnimationFrame(apply); }
  document.addEventListener('pointermove', function (e) {
    var r = card.getBoundingClientRect();
    var px = (e.clientX - (r.left + r.width / 2)) / innerWidth, py = (e.clientY - (r.top + r.height / 2)) / innerHeight;
    tx = Math.max(-1, Math.min(1, px * 2)) * 9;
    ty = Math.max(-1, Math.min(1, py * 2)) * -7;
    gx = Math.max(0, Math.min(100, (e.clientX - r.left) / r.width * 100));
    gy = Math.max(0, Math.min(100, (e.clientY - r.top) / r.height * 100));
    queue();
  }, { passive: true });
  document.addEventListener('pointerleave', function () { tx = ty = 0; queue(); });
})();
