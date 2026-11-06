(function () {
  'use strict';
  var root = document.querySelector('[data-owner-search]');
  if (!root) return;

  var input = root.querySelector('input');
  var results = root.querySelector('.owner-search-results');
  var status = root.querySelector('.owner-search-status');
  var timer = null;
  var controller = null;

  function closeResults() {
    results.hidden = true;
    input.setAttribute('aria-expanded', 'false');
  }

  function showMessage(message) {
    results.replaceChildren();
    var item = document.createElement('li');
    item.className = 'owner-search-message';
    item.textContent = message;
    results.appendChild(item);
    results.hidden = false;
    input.setAttribute('aria-expanded', 'true');
  }

  function showResults(items) {
    results.replaceChildren();
    items.forEach(function (item) {
      var row = document.createElement('li');
      var link = document.createElement('a');
      link.href = item.url;
      link.className = 'owner-search-result';

      var heading = document.createElement('strong');
      heading.textContent = item.reference;
      var detail = document.createElement('span');
      detail.textContent = [item.customer, item.plate, item.lot, item.pickup, item.status].filter(Boolean).join(' · ');
      link.appendChild(heading);
      link.appendChild(detail);
      row.appendChild(link);
      results.appendChild(row);
    });
    results.hidden = !items.length;
    input.setAttribute('aria-expanded', items.length ? 'true' : 'false');
  }

  function search(query) {
    if (controller) controller.abort();
    controller = new AbortController();
    status.textContent = root.dataset.loading;
    fetch(root.dataset.url + '?q=' + encodeURIComponent(query), {
      credentials: 'same-origin',
      headers: { Accept: 'application/json' },
      signal: controller.signal
    })
      .then(function (response) {
        if (!response.ok) throw new Error('Search request failed');
        return response.json();
      })
      .then(function (data) {
        if (input.value.trim() !== query) return;
        var matches = data.results || [];
        showResults(matches);
        status.textContent = matches.length ? root.dataset.results.replace('{count}', matches.length) : root.dataset.empty;
        if (!matches.length) showMessage(root.dataset.empty);
      })
      .catch(function (error) {
        if (error.name === 'AbortError') return;
        closeResults();
        status.textContent = root.dataset.error;
      });
  }

  input.addEventListener('input', function () {
    var query = input.value.trim();
    clearTimeout(timer);
    if (controller) controller.abort();
    if (query.length < 2 && !/^\d+$/.test(query)) {
      closeResults();
      status.textContent = '';
      return;
    }
    timer = setTimeout(function () { search(query); }, 250);
  });

  input.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') closeResults();
  });

  document.addEventListener('click', function (event) {
    if (!root.contains(event.target)) closeResults();
  });
})();
