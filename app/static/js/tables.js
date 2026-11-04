/* Responsive tables: wrap every table so it scrolls instead of overflowing, and label cells so rows stack as cards on phones. */
(function () {
  'use strict';
  document.querySelectorAll('main table').forEach(function (table) {
    if (!table.parentElement.classList.contains('table-wrap')) {
      var wrap = document.createElement('div');
      wrap.className = 'table-wrap';
      table.parentNode.insertBefore(wrap, table);
      wrap.appendChild(table);
    }
    var heads = Array.prototype.map.call(table.querySelectorAll('thead th'), function (th) { return th.textContent.trim(); });
    table.querySelectorAll('tbody tr').forEach(function (row) {
      Array.prototype.forEach.call(row.children, function (cell, i) { if (heads[i] && !cell.dataset.label) cell.dataset.label = heads[i]; });
    });
    table.classList.add('stackable');
  });
})();
