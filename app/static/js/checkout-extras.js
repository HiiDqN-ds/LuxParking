/* Checkout: one shuttle and one cleaning choice, a details field once something is chosen, live total. */
(function () {
  'use strict';
  var box = document.querySelector('.extras-choice');
  if (!box) return;
  var base = Number(box.dataset.basePrice) || 0, currency = box.dataset.currency;
  function money(value) { return value.toFixed(2) + ' ' + currency; }
  function update() {
    var sum = 0;
    box.querySelectorAll('.extra-group').forEach(function (group) {
      var picked = group.querySelector('input[type=radio]:checked');
      var details = group.querySelector('.extra-details');
      if (details) details.hidden = !(picked && picked.value);
      var pax = group.querySelector('input[name=passengers]'), perPerson = picked && picked.dataset.perPerson === '1';
      var field = group.querySelector('.passengers-field');
      if (field) field.hidden = !perPerson;
      var included = picked ? parseInt(picked.dataset.included, 10) || 0 : 0;
      var extraPerson = picked && picked.dataset.extraPerson !== '' ? Number(picked.dataset.extraPerson) : null;
      var seats = picked ? parseInt(picked.dataset.max, 10) || 20 : 20;  // seat limit of the vehicle (shuttle: 2)
      // Bundle: without a price for additional persons only the included persons can be booked.
      var limit = Math.min(seats, included && extraPerson === null ? included : 20);
      if (pax) pax.max = String(limit);
      var label = group.querySelector('label[for=passengers]');
      if (label && label.dataset.labelBundle) {
        var text = included ? label.dataset.labelBundle : (limit < 20 && label.dataset.labelMax ? label.dataset.labelMax : label.dataset.labelSingle);
        label.textContent = text.replace('{included}', included).replace('{max}', limit);
      }
      var typed = pax ? parseInt(pax.value, 10) : NaN;
      if (pax && perPerson && typed > limit) { pax.value = String(limit); typed = limit; }
      var n = perPerson && pax ? Math.max(1, Math.min(limit, typed || 1)) : 1;
      var cost = 0;
      if (picked) {
        var price = Number(picked.dataset.price) || 0;
        cost = included ? price + Math.max(0, n - included) * (extraPerson || 0) : price * n;
        sum += cost;
      }
      var hint = group.querySelector('.passengers-total');
      if (hint) hint.textContent = perPerson && !included ? n + ' \u00d7 ' + money(Number(picked.dataset.price) || 0) + ' = ' + money(cost) : '';
    });
    document.querySelectorAll('[data-local-money]').forEach(function (el) {
      el.dataset.amount = String(base + sum); el.textContent = money(base + sum);
    });
  }
  box.addEventListener('change', update);
  box.addEventListener('input', update);
  update();
})();
