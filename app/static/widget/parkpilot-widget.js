/*!
 * ParkPilot booking widget. Embed:
 *   <div id="parkpilot-widget"></div>
 *   <script async src="https://YOUR-PARKPILOT-HOST/widget.js"
 *           data-key="pk_..." data-lot="1" data-tenant="your-slug" data-lot-slug="your-lot"
 *           data-color="#14385C" data-lang="de"></script>
 *
 * It asks the public API (with your public pk_ key) what is free and from what price, then sends the visitor to the
 * hosted booking page with the dates filled in. Card data and personal data never touch your site.
 * The page origin must be listed in the key's allowed origins (Admin > API access).
 */
(function () {
  "use strict";
  var script = document.currentScript || document.querySelector("script[data-key][src*='widget.js']");
  if (!script) return;
  var host = new URL(script.src).origin;
  var cfg = {
    key: script.dataset.key, lot: script.dataset.lot, tenant: script.dataset.tenant, lotSlug: script.dataset.lotSlug,
    color: /^#[0-9a-fA-F]{6}$/.test(script.dataset.color || "") ? script.dataset.color : "#14385C",
    target: script.dataset.target || "parkpilot-widget",
    lang: (script.dataset.lang || navigator.language || "en").slice(0, 2).toLowerCase()
  };
  var T = {
    en: { title: "Book your parking", from: "Drop-off", to: "Pick-up", check: "Check price", book: "Book now", free: "spaces available", price: "from", none: "No space is available for these dates.", err: "Prices are unavailable right now. Please try again.", dates: "Choose a drop-off before the pick-up, in the future.", wait: "Checking…" },
    de: { title: "Parkplatz buchen", from: "Abgabe", to: "Abholung", check: "Preis prüfen", book: "Jetzt buchen", free: "Plätze frei", price: "ab", none: "Für diesen Zeitraum ist kein Platz frei.", err: "Preise sind gerade nicht verfügbar. Bitte erneut versuchen.", dates: "Abgabe muss in der Zukunft und vor der Abholung liegen.", wait: "Prüfe …" },
    nl: { title: "Boek uw parkeerplaats", from: "Inleveren", to: "Ophalen", check: "Prijs bekijken", book: "Nu boeken", free: "plaatsen vrij", price: "vanaf", none: "Geen plaats beschikbaar voor deze data.", err: "Prijzen zijn nu niet beschikbaar. Probeer het opnieuw.", dates: "Kies een inlevermoment in de toekomst, vóór het ophalen.", wait: "Controleren…" },
    fr: { title: "Réservez votre parking", from: "Dépôt", to: "Récupération", check: "Voir le prix", book: "Réserver", free: "places libres", price: "dès", none: "Aucune place disponible pour ces dates.", err: "Les prix sont indisponibles. Réessayez.", dates: "Choisissez un dépôt dans le futur, avant la récupération.", wait: "Vérification…" },
    es: { title: "Reserve su aparcamiento", from: "Entrega", to: "Recogida", check: "Ver precio", book: "Reservar", free: "plazas libres", price: "desde", none: "No hay plazas para esas fechas.", err: "Los precios no están disponibles. Inténtelo de nuevo.", dates: "Elija una entrega futura anterior a la recogida.", wait: "Comprobando…" },
    ar: { title: "احجز موقف سيارتك", from: "التسليم", to: "الاستلام", check: "عرض السعر", book: "احجز الآن", free: "مكان متاح", price: "ابتداءً من", none: "لا يوجد مكان متاح في هذه التواريخ.", err: "الأسعار غير متاحة حالياً. حاول مجدداً.", dates: "اختر موعد تسليم مستقبلياً قبل الاستلام.", wait: "جارٍ التحقق…" }
  };
  var t = T[cfg.lang] || T.en;
  var rtl = cfg.lang === "ar";
  var mount = document.getElementById(cfg.target);
  if (!mount || !cfg.key || !cfg.lot) { console.warn("ParkPilot widget: missing container, data-key or data-lot"); return; }
  var root = mount.attachShadow ? mount.attachShadow({ mode: "open" }) : mount;
  var esc = function (s) { return String(s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); };
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  var local = function (d) { return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes()); };
  var start = new Date(Date.now() + 24 * 3600e3); start.setMinutes(0, 0, 0);
  var end = new Date(start.getTime() + 7 * 24 * 3600e3);
  root.innerHTML = '<style>:host{display:block;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}' +
    '.box{box-sizing:border-box;max-width:460px;padding:20px;border:1px solid #d9e0e6;border-radius:14px;background:#fff;color:#14212b}' +
    'h3{margin:0 0 12px;font-size:1.15rem}label{display:block;margin:10px 0 4px;font-size:.85rem;color:#5b6b78}' +
    'input{box-sizing:border-box;width:100%;padding:11px;border:1px solid #b9c4cd;border-radius:8px;font:inherit}' +
    'button,a.go{display:block;box-sizing:border-box;width:100%;margin-top:14px;padding:12px;border:0;border-radius:8px;background:' + cfg.color + ';color:#fff;font:inherit;font-weight:600;text-align:center;text-decoration:none;cursor:pointer}' +
    'button:disabled{opacity:.6}.out{margin-top:14px;min-height:1.4em}.price{font-size:1.5rem;font-weight:700}.err{color:#b3261e}</style>' +
    '<div class="box" dir="' + (rtl ? "rtl" : "ltr") + '"><h3>' + esc(t.title) + '</h3>' +
    '<label for="f">' + esc(t.from) + '</label><input id="f" type="datetime-local" value="' + local(start) + '">' +
    '<label for="t">' + esc(t.to) + '</label><input id="t" type="datetime-local" value="' + local(end) + '">' +
    '<button id="check" type="button">' + esc(t.check) + '</button><div class="out" id="out" aria-live="polite"></div></div>';
  var $ = function (id) { return root.getElementById ? root.getElementById(id) : root.querySelector("#" + id); };
  $("check").addEventListener("click", function () {
    var from = $("f").value, to = $("t").value, out = $("out"), btn = $("check");
    if (!from || !to || new Date(from) <= new Date() || new Date(from) >= new Date(to)) { out.innerHTML = '<span class="err">' + esc(t.dates) + "</span>"; return; }
    btn.disabled = true; out.textContent = t.wait;
    var url = host + "/v1/availability?lot_id=" + encodeURIComponent(cfg.lot) + "&from=" + encodeURIComponent(new Date(from).toISOString()) + "&to=" + encodeURIComponent(new Date(to).toISOString());
    fetch(url, { headers: { Authorization: "Bearer " + cfg.key } }).then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); }).then(function (data) {
      var free = (data.spaces || []).filter(function (s) { return s.selectable; });
      if (!free.length) { out.textContent = t.none; return; }
      var lowest = Math.min.apply(null, free.map(function (s) { return parseFloat(s.price); }));
      var cur = free[0].currency === "EUR" ? "€" : free[0].currency;
      var link = host + "/" + (T[cfg.lang] ? cfg.lang : "en") + "/reserve/" + encodeURIComponent(cfg.tenant || "") + "/" + encodeURIComponent(cfg.lotSlug || "") + "?pickup_local=" + encodeURIComponent(from) + "&return_local=" + encodeURIComponent(to);
      out.innerHTML = esc(t.price) + ' <span class="price">' + esc(lowest.toFixed(2)) + " " + esc(cur) + "</span><br><small>" + free.length + " " + esc(t.free) + '</small><a class="go" href="' + esc(link) + '" target="_blank" rel="noopener">' + esc(t.book) + "</a>";
    }).catch(function () { out.innerHTML = '<span class="err">' + esc(t.err) + "</span>"; }).then(function () { btn.disabled = false; });
  });
})();
