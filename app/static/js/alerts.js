/* Notification bell: polls for new alerts, shows a toast and plays a short chime when a booking arrives. */
(function () {
  'use strict';
  var root = document.querySelector('[data-alerts]');
  if (!root) return;
  var bell = root.querySelector('.alerts-bell'), badge = root.querySelector('.alerts-badge');
  var panel = root.querySelector('.alerts-panel'), list = root.querySelector('.alerts-list');
  var toasts = root.querySelector('.alerts-toasts'), soundBtn = root.querySelector('.alerts-sound');
  var lastId = parseInt(root.dataset.latest || '0', 10) || 0;
  var KEY = 'pp_alert_sound', ctx = null;
  function soundOn() { try { return localStorage.getItem(KEY) !== 'off'; } catch (e) { return true; } }
  function setSound(on) {
    try { localStorage.setItem(KEY, on ? 'on' : 'off'); } catch (e) { /* private mode: keep the session value */ }
    soundBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    soundBtn.textContent = (on ? '🔊 ' + soundBtn.dataset.on : '🔇 ' + soundBtn.dataset.off);
  }
  var unlocked = false;  // true once a real user gesture has started the AudioContext
  function audio() {
    if (!unlocked) return null;  // never create/resume before a gesture: Chrome logs "AudioContext was not allowed to start"
    if (ctx.state === 'suspended') { try { ctx.resume().catch(function () {}); } catch (e) { /* ignore */ } }
    return ctx;
  }
  function chime() {  // two soft notes, generated in the browser (no sound file to load)
    var c = audio();
    if (!c) return;
    [[880, 0], [1318.5, 0.18]].forEach(function (note) {
      var osc = c.createOscillator(), gain = c.createGain(), t0 = c.currentTime + note[1];
      osc.type = 'sine'; osc.frequency.value = note[0];
      gain.gain.setValueAtTime(0.0001, t0);
      gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.03);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.55);
      osc.connect(gain); gain.connect(c.destination); osc.start(t0); osc.stop(t0 + 0.6);
    });
  }
  // Browsers only allow sound after a real gesture (click, key press, touch END; touchstart does not count).
  var GESTURES = ['pointerup', 'click', 'keydown', 'touchend'];
  function unlock() {
    if (unlocked || !soundOn()) return;
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return;
    try {
      ctx = ctx || new Ctor();
      var p = ctx.resume();
      var done = function () {
        if (ctx.state === 'running') {
          unlocked = true;
          GESTURES.forEach(function (n) { document.removeEventListener(n, unlock, true); });
        }
      };
      if (p && p.then) p.then(done, function () {}); else done();
    } catch (e) { /* no audio available: stay silent */ }
  }
  GESTURES.forEach(function (n) { document.addEventListener(n, unlock, { capture: true, passive: true }); });
  function setBadge(n) { badge.textContent = n > 99 ? '99+' : String(n); badge.hidden = !n; }
  function itemHtml(a) {
    var li = document.createElement('li'); li.className = 'alert-item ' + a.colour;
    var link = document.createElement('a'); link.href = a.url || '#';
    var ico = document.createElement('span'); ico.className = 'alert-ico'; ico.setAttribute('aria-hidden', 'true'); ico.textContent = a.icon;
    var body = document.createElement('span'); body.className = 'alert-body'; body.textContent = a.text;
    var small = document.createElement('small'); small.textContent = a.ago; body.appendChild(small);
    link.appendChild(ico); link.appendChild(body); li.appendChild(link); return li;
  }
  function toast(a) {
    var el = itemHtml(a); el.classList.add('alert-toast');
    toasts.appendChild(el);
    setTimeout(function () { el.classList.add('leaving'); setTimeout(function () { el.remove(); }, 400); }, 7000);
  }
  function poll() {
    fetch(root.dataset.feed + '?after=' + lastId, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) return;
        var fresh = data.alerts || [], newBooking = false;
        fresh.forEach(function (a) {
          var empty = list.querySelector('.alerts-empty'); if (empty) empty.remove();
          list.insertBefore(itemHtml(a), list.firstChild);
          while (list.children.length > 6) list.removeChild(list.lastChild);
          toast(a);
          if (a.kind === 'booking_created') newBooking = true;
        });
        if (data.latest > lastId) lastId = data.latest;
        setBadge(data.unread);
        if (newBooking && soundOn()) chime();
      })
      .catch(function () { /* offline: try again on the next tick */ });
  }
  bell.addEventListener('click', function () {
    var open = panel.hidden;
    panel.hidden = !open; bell.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && badge && !badge.hidden) {
      fetch(root.dataset.read, { method: 'POST', credentials: 'same-origin', headers: { 'X-CSRFToken': root.dataset.csrf } })
        .then(function () { setBadge(0); }).catch(function () {});
    }
  });
  document.addEventListener('click', function (e) { if (!root.contains(e.target)) { panel.hidden = true; bell.setAttribute('aria-expanded', 'false'); } });
  soundBtn.addEventListener('click', function () { var on = !soundOn(); setSound(on); if (on) { unlock(); setTimeout(chime, 150); } });
  setSound(soundOn());
  setInterval(function () { if (!document.hidden) poll(); }, 8000);
  document.addEventListener('visibilitychange', function () { if (!document.hidden) poll(); });
})();
