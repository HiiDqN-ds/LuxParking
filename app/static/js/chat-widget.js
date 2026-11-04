/* Chat assistant widget. All text is inserted with textContent (never innerHTML); links are built as DOM nodes. */
(function () {
  'use strict';
  var root = document.getElementById('chat');
  if (!root) return;
  var T = {};
  try { T = JSON.parse(root.dataset.i18n || '{}'); } catch (e) { T = {}; }
  var launcher = root.querySelector('.chat-launcher');
  var panel = root.querySelector('.chat-panel');
  var log = root.querySelector('.chat-log');
  var form = root.querySelector('.chat-form');
  var input = form.querySelector('input');
  var sendBtn = form.querySelector('button[type=submit]');
  var chips = root.querySelector('.chat-suggestions');
  var storageKey = root.dataset.storage || 'pp-chat';
  var history = [];
  var busy = false;

  function load() {
    try { var raw = sessionStorage.getItem(storageKey); if (raw) history = JSON.parse(raw) || []; } catch (e) { history = []; }
    if (!Array.isArray(history)) history = [];
  }
  function save() { try { sessionStorage.setItem(storageKey, JSON.stringify(history.slice(-30))); } catch (e) { /* private mode: ignore */ } }

  function linkify(parent, text) {
    var re = /(https?:\/\/[^\s<>"')]+)/g, last = 0, m;
    while ((m = re.exec(text)) !== null) {
      if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
      var url = m[1].replace(/[.,;:!?]+$/, '');
      var a = document.createElement('a');
      a.href = url; a.textContent = url; a.target = '_blank'; a.rel = 'noopener noreferrer';
      parent.appendChild(a);
      last = m.index + url.length; re.lastIndex = last;
    }
    if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
  }

  function bubble(role, text) {
    var row = document.createElement('div');
    row.className = 'chat-msg chat-' + role;
    var who = document.createElement('span');
    who.className = 'sr-only';
    who.textContent = (role === 'user' ? T.you : T.assistant) + ': ';
    var body = document.createElement('div');
    body.className = 'chat-bubble';
    row.appendChild(who);
    linkify(body, text);
    row.appendChild(body);
    log.appendChild(row);
    log.scrollTop = log.scrollHeight;
    return row;
  }

  function render() {
    log.textContent = '';
    bubble('assistant', T.welcome || '');
    history.forEach(function (m) { bubble(m.role, m.content); });
    chips.hidden = history.length > 0;
  }

  function buildChips() {
    chips.textContent = '';
    (T.suggestions || []).forEach(function (q) {
      var b = document.createElement('button');
      b.type = 'button'; b.className = 'chat-chip'; b.textContent = q;
      b.addEventListener('click', function () { send(q); });
      chips.appendChild(b);
    });
  }

  function setBusy(on) { busy = on; sendBtn.disabled = on; input.disabled = on; }

  function send(text) {
    text = (text || '').trim();
    if (!text || busy) return;
    history.push({ role: 'user', content: text.slice(0, 500) });
    save(); chips.hidden = true;
    bubble('user', text);
    input.value = '';
    setBusy(true);
    var typing = bubble('assistant', T.typing || '…');
    typing.classList.add('chat-typing');
    fetch(root.dataset.endpoint, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json', 'X-CSRFToken': root.dataset.csrf },
      body: JSON.stringify({ messages: history.slice(-12), locale: root.dataset.locale })
    }).then(function (r) { return r.json().then(function (d) { return { ok: r.ok, data: d }; }); })
      .then(function (res) {
        typing.remove();
        if (!res.ok || !res.data.reply) throw new Error('chat');
        history.push({ role: 'assistant', content: res.data.reply });
        save(); bubble('assistant', res.data.reply);
      })
      .catch(function () { typing.remove(); history.pop(); save(); bubble('assistant', T.error || ''); })
      .then(function () { setBusy(false); input.focus(); });
  }

  function open() {
    panel.hidden = false; launcher.setAttribute('aria-expanded', 'true'); launcher.hidden = true;
    input.focus(); log.scrollTop = log.scrollHeight;
  }
  function close() {
    panel.hidden = true; launcher.hidden = false; launcher.setAttribute('aria-expanded', 'false'); launcher.focus();
  }

  launcher.addEventListener('click', open);
  root.querySelector('.chat-close').addEventListener('click', close);
  root.querySelector('.chat-reset').addEventListener('click', function () { history = []; save(); render(); });
  form.addEventListener('submit', function (e) { e.preventDefault(); send(input.value); });
  root.addEventListener('keydown', function (e) { if (e.key === 'Escape' && !panel.hidden) close(); });

  load(); buildChips(); render();
})();
