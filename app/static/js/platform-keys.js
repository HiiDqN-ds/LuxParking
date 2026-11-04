(function () {
  var form = document.getElementById("partner-form");
  if (!form) return;
  var cfg = JSON.parse(form.dataset.config);
  var tenant = form.querySelector("#k-tenant");
  var boxes = Array.prototype.slice.call(form.querySelectorAll('input[name="features"]'));
  var byCode = {};
  boxes.forEach(function (b) { byCode[b.value] = b; });

  function dependents(code) {
    return Object.keys(cfg.features).filter(function (c) { return cfg.features[c].requires.indexOf(code) !== -1; });
  }
  function tick(code) {
    var box = byCode[code];
    if (!box || box.checked) return;
    box.checked = true;
    cfg.features[code].requires.forEach(tick);
  }
  function untick(code) {
    var box = byCode[code];
    if (!box || !box.checked) return;
    box.checked = false;
    dependents(code).forEach(untick);
  }
  function showLots() {
    form.querySelectorAll(".lot-group").forEach(function (g) {
      var on = g.dataset.tenant === tenant.value;
      g.hidden = !on;
      g.querySelectorAll("input").forEach(function (i) { i.disabled = !on; });
    });
  }
  function money(v) { return (Math.round(v * 100) / 100).toFixed(2); }
  function recalc() {
    var lines = [], total = parseFloat(cfg.base);
    lines.push(["Base", total]);
    boxes.forEach(function (b) {
      if (b.checked) { var p = parseFloat(cfg.features[b.value].price); total += p; lines.push([cfg.features[b.value].label, p]); }
    });
    var lots = form.querySelectorAll('.lot-group:not([hidden]) input[name="lot_ids"]:checked').length;
    var lp = lots * parseFloat(cfg.lot);
    total += lp; lines.push(["Maps x " + lots, lp]);
    document.getElementById("price-lines").innerHTML = lines.map(function (l) {
      return "<li><span>" + l[0].replace(/</g, "&lt;") + "</span><span>" + money(l[1]) + " EUR</span></li>";
    }).join("");
    document.getElementById("price-total").textContent = money(total);
  }
  boxes.forEach(function (b) {
    b.addEventListener("change", function () {
      if (b.checked) cfg.features[b.value].requires.forEach(tick); else dependents(b.value).forEach(untick);
      recalc();
    });
  });
  form.querySelectorAll("[data-preset]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      boxes.forEach(function (b) { b.checked = false; });
      cfg.presets[btn.dataset.preset].forEach(tick);
      recalc();
    });
  });
  form.addEventListener("change", function (e) { if (e.target.name === "lot_ids") recalc(); });
  tenant.addEventListener("change", function () { showLots(); recalc(); });
  showLots(); recalc();
})();
