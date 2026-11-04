/* My profile page: photo drop zone with live preview, password strength + match, copy button, toast messages,
   active section tracking, count-up completeness ring, loading buttons. No libraries; all DOM text via textContent. */
(function () {
  'use strict';
  var root = document.getElementById('acct');
  if (!root) return;
  var D = root.dataset;
  var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var SVG = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">';
  var ICON = {
    eye: SVG + '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    eyeOff: SVG + '<path d="M17.9 17.9A10.1 10.1 0 0 1 12 19c-6.5 0-10-7-10-7a18.6 18.6 0 0 1 5.1-5.9M9.9 4.2A9.1 9.1 0 0 1 12 5c6.5 0 10 7 10 7a18.5 18.5 0 0 1-2.2 3.2"/><path d="M14.1 14.1a3 3 0 1 1-4.2-4.2"/><path d="m2 2 20 20"/></svg>',
    check: SVG + '<path d="M20 6 9 17l-5-5"/></svg>',
    copy: SVG + '<rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>',
    alert: SVG + '<path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/></svg>',
    x: SVG + '<path d="M18 6 6 18M6 6l12 12"/></svg>'
  };
  // The icon strings above are constants of this file (never user input), so assigning them as HTML is safe.
  function setIcon(el, key) { el.innerHTML = ICON[key]; }

  /* ---------- completeness ring: animate in + count up ---------- */
  var pct = parseInt(D.pct || '0', 10);
  root.style.setProperty('--p', String(pct));
  requestAnimationFrame(function () { requestAnimationFrame(function () { root.classList.add('is-ready'); }); });
  var num = document.getElementById('score-num');
  if (num && !reduce && pct > 0) {
    var t0 = null;
    num.textContent = '0';
    (function tick(ts) {
      if (t0 === null) t0 = ts;
      var k = Math.min(1, (ts - t0) / 1000);
      num.textContent = String(Math.round(pct * (1 - Math.pow(1 - k, 3))));
      if (k < 1) requestAnimationFrame(tick);
    })(performance.now());
  }

  /* ---------- photo: drop zone, checks, live preview ---------- */
  var MAX = 3 * 1024 * 1024, TYPES = ['image/png', 'image/jpeg', 'image/webp'];
  var input = document.getElementById('photo-input');
  var zone = document.getElementById('dropzone');
  var save = document.getElementById('photo-save');
  var preview = document.getElementById('photo-preview');
  var info = document.getElementById('photo-name');
  var idleText = info ? info.textContent : '';

  function human(bytes) { return bytes > 1048576 ? (bytes / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB'; }

  function accept(file) {
    save.disabled = true;
    if (!file) { info.textContent = idleText; return; }
    if (TYPES.indexOf(file.type) === -1) { info.textContent = D.badType; input.value = ''; return; }
    if (file.size > MAX) { info.textContent = D.tooBig; input.value = ''; return; }
    var reader = new FileReader();  // data: URLs are allowed by the Content-Security-Policy (blob: is not)
    reader.onload = function () {
      var img = document.createElement('img');
      img.className = 'avatar avatar-lg pop'; img.alt = ''; img.src = reader.result;
      preview.textContent = ''; preview.appendChild(img);
    };
    reader.readAsDataURL(file);
    info.textContent = file.name + ' · ' + human(file.size);
    save.disabled = false;
    var card = document.getElementById('photo');
    if (card && document.activeElement !== input) card.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'center' });
  }

  if (input && zone) {
    input.addEventListener('change', function () { accept(input.files && input.files[0]); });
    ['dragenter', 'dragover'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.add('is-over'); info.textContent = D.drop; });
    });
    ['dragleave', 'drop'].forEach(function (ev) {
      zone.addEventListener(ev, function (e) { e.preventDefault(); zone.classList.remove('is-over'); if (ev === 'dragleave') info.textContent = idleText; });
    });
    zone.addEventListener('drop', function (e) {
      var files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        try { input.files = files; } catch (err) { /* older browsers: the chooser still works */ }
        accept(files[0]);
      }
    });
  }

  /* ---------- password fields: show/hide, strength, match ---------- */
  function score(value, email) {
    var s = 0;
    if (value.length >= 12) s++;
    if (value.length >= 16) s++;
    if (/[a-z]/.test(value) && /[A-Z]/.test(value)) s++;
    if (/\d/.test(value)) s++;
    if (/[^A-Za-z0-9]/.test(value) || /\s/.test(value)) s++;
    var local = (email || '').split('@')[0].toLowerCase();
    if (local.length > 2 && value.toLowerCase().indexOf(local) !== -1) s = Math.max(0, s - 2);
    if (value.length < 12) s = Math.min(s, 1);
    return s;
  }
  var email = (document.getElementById('current-email') || {}).textContent || '';

  root.querySelectorAll('input[type=password]').forEach(function (field) {
    var box = field.parentNode;
    var toggle = document.createElement('button');
    toggle.type = 'button'; toggle.className = 'pw-toggle'; toggle.setAttribute('aria-label', D.show); toggle.title = D.show;
    setIcon(toggle, 'eye');
    toggle.addEventListener('click', function () {
      var hidden = field.type === 'password';
      field.type = hidden ? 'text' : 'password';
      field.classList.toggle('pw-shown', hidden);
      var label = hidden ? D.hide : D.show;
      toggle.setAttribute('aria-label', label); toggle.title = label;
      setIcon(toggle, hidden ? 'eyeOff' : 'eye');
    });
    box.appendChild(toggle);

    if (field.hasAttribute('data-meter')) {
      var meter = document.createElement('div'); meter.className = 'meter'; meter.hidden = true;
      var bar = document.createElement('span'); bar.className = 'meter-bar'; meter.appendChild(bar);
      var text = document.createElement('small'); text.className = 'meter-text'; text.setAttribute('aria-live', 'polite');
      box.parentNode.insertBefore(meter, box.nextSibling); meter.parentNode.insertBefore(text, meter.nextSibling);
      field.addEventListener('input', function () {
        var v = field.value;
        meter.hidden = !v;
        if (!v) { text.textContent = ''; return; }
        var s = score(v, email), level = s >= 4 ? 3 : (s >= 2 ? 2 : 1);
        meter.dataset.level = String(level);
        bar.style.width = (level === 3 ? 100 : level === 2 ? 62 : 28) + '%';
        text.textContent = level === 3 ? D.strong : level === 2 ? D.good : D.weak;
      });
    }
    if (field.hasAttribute('data-confirm')) {
      var note = document.createElement('small'); note.className = 'match-text'; note.setAttribute('aria-live', 'polite');
      box.parentNode.insertBefore(note, box.nextSibling);
      var main = root.querySelector('#password input[data-meter]');
      var check = function () {
        if (!field.value) { note.textContent = ''; note.className = 'match-text'; return; }
        var ok = main && field.value === main.value;
        note.className = 'match-text ' + (ok ? 'ok' : 'no');
        note.textContent = ok ? D.match : D.nomatch;
      };
      field.addEventListener('input', check);
      if (main) main.addEventListener('input', check);
    }
  });

  /* ---------- copy email ---------- */
  document.querySelectorAll('.copy-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var target = document.getElementById(btn.dataset.copyTarget);
      if (!target || !navigator.clipboard) return;
      navigator.clipboard.writeText(target.textContent.trim()).then(function () {
        btn.classList.add('copied'); setIcon(btn, 'check');
        setTimeout(function () { btn.classList.remove('copied'); setIcon(btn, 'copy'); }, 1600);
      });
    });
  });

  /* ---------- flash messages become toasts ---------- */
  var flashes = document.querySelectorAll('main .flash');
  if (flashes.length) {
    var stack = document.createElement('div'); stack.className = 'toasts';
    stack.setAttribute('role', 'status'); stack.setAttribute('aria-live', 'polite');
    document.body.appendChild(stack);
    flashes.forEach(function (flash, i) {
      var bad = flash.classList.contains('error');
      var toast = document.createElement('div'); toast.className = 'toast ' + (bad ? 'error' : 'ok');
      var ms = bad ? 8000 : 5000; toast.style.setProperty('--t', ms + 'ms'); toast.style.animationDelay = (i * 90) + 'ms';
      var ic = document.createElement('span'); ic.className = 'toast-ic'; setIcon(ic, bad ? 'alert' : 'check');
      var msg = document.createElement('div'); msg.className = 'toast-msg'; msg.textContent = flash.textContent.trim();
      var x = document.createElement('button'); x.type = 'button'; x.className = 'toast-x'; x.setAttribute('aria-label', D.close); setIcon(x, 'x');
      function dismiss() { toast.classList.add('leaving'); setTimeout(function () { toast.remove(); }, 260); }
      x.addEventListener('click', dismiss);
      toast.appendChild(ic); toast.appendChild(msg); toast.appendChild(x); stack.appendChild(toast);
      setTimeout(dismiss, ms + i * 90);
      flash.remove();
    });
  }

  /* ---------- loading state on submit (no double clicks) ---------- */
  root.querySelectorAll('form').forEach(function (form) {
    form.addEventListener('submit', function () {
      var btn = form.querySelector('button:not([type=button])');
      if (btn) setTimeout(function () { btn.classList.add('is-loading'); btn.disabled = true; }, 0);
    });
  });

  /* ---------- active section in the side navigation ---------- */
  var links = {};
  root.querySelectorAll('.acct-nav a').forEach(function (a) { links[a.getAttribute('href').slice(1)] = a; });
  function activate(id) {
    Object.keys(links).forEach(function (k) { links[k].classList.toggle('active', k === id); });
  }
  activate('photo');
  if ('IntersectionObserver' in window) {
    var seen = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { if (e.isIntersecting) activate(e.target.id); });
    }, { rootMargin: '-25% 0px -60% 0px' });
    ['photo', 'profile', 'email', 'password'].forEach(function (id) { var s = document.getElementById(id); if (s) seen.observe(s); });
  }
  root.querySelectorAll('.acct-nav a, .score-list a').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var target = document.getElementById(a.getAttribute('href').slice(1));
      if (!target) return;
      e.preventDefault();
      target.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' });
      if (history.replaceState) history.replaceState(null, '', '#' + target.id);
    });
  });

  /* ---------- jump to the form that came back with errors ---------- */
  var section = D.section && document.getElementById(D.section);
  if (section) {
    section.scrollIntoView({ block: 'start' });
    var bad = section.querySelector('.err');
    if (bad) { var f = bad.closest('.field'); f = f && f.querySelector('input'); if (f) f.focus({ preventScroll: true }); }
  }
})();
