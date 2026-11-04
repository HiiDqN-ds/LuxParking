(() => {
  "use strict";

  const pageLocale = document.documentElement.lang || "en";
  for (const element of document.querySelectorAll("[data-local-datetime]")) {
    const date = new Date(element.dateTime);
    if (Number.isFinite(date.getTime())) {
      element.textContent = new Intl.DateTimeFormat(pageLocale, {
        dateStyle: "medium",
        timeStyle: "short"
      }).format(date);
    }
  }
  for (const element of document.querySelectorAll("[data-local-money]")) {
    const amount = Number(element.dataset.amount);
    try {
      if (Number.isFinite(amount)) {
        element.textContent = new Intl.NumberFormat(pageLocale, {
          style: "currency",
          currency: element.dataset.currency
        }).format(amount);
      }
    } catch (error) {
      if (!(error instanceof RangeError)) throw error;
      element.textContent = `${new Intl.NumberFormat(pageLocale).format(amount)} ${element.dataset.currency}`;
    }
  }

  const shell = document.querySelector(".booking-shell");
  const countdown = document.getElementById("hold-countdown");
  if (countdown) {
    const expiresAt = new Date(countdown.dataset.expires).getTime();
    const progress = document.getElementById("hold-progress");
    const timer = document.getElementById("hold-timer");
    const totalSeconds = Math.max(600, Math.ceil((expiresAt - Date.now()) / 1000));
    const updateCountdown = () => {
      const secondsLeft = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
      countdown.textContent = `${Math.floor(secondsLeft / 60)}:${String(secondsLeft % 60).padStart(2, "0")}`;
      if (progress) progress.style.width = `${Math.min(100, (secondsLeft / totalSeconds) * 100)}%`;
      if (timer) timer.classList.toggle("is-urgent", secondsLeft <= 120);
      if (secondsLeft === 0) {
        window.location.reload();
      }
    };
    updateCountdown();
    window.setInterval(updateCountdown, 1000);
    return;
  }
  if (!shell) return;

  const pickupInput = document.getElementById("pickup-at");
  const returnInput = document.getElementById("return-at");
  const emailInput = document.getElementById("customer-email");
  const holdForm = document.getElementById("hold-form");
  const holdButton = document.getElementById("hold-submit");
  const selectedIdInput = document.getElementById("selected-space-id");
  const pickupUtcInput = document.getElementById("pickup-at-utc");
  const returnUtcInput = document.getElementById("return-at-utc");
  const availabilityMessage = document.getElementById("availability-message");
  const spaceCount = document.getElementById("space-count");
  const spaceList = document.getElementById("space-list");
  const noSpaces = document.getElementById("no-spaces");
  const map = document.getElementById("booking-map");
  const bestAvailableButton = document.getElementById("best-available");
  const selectionMessage = document.getElementById("selection-message");
  const selectionDetails = document.getElementById("selection-details");
  const selectedName = document.getElementById("selected-space-name");
  const selectedType = document.getElementById("selected-space-type");
  const selectedDistance = document.getElementById("selected-space-distance");
  const selectedPrice = document.getElementById("selected-space-price");
  const tripPickup = document.getElementById("trip-pickup");
  const tripReturn = document.getElementById("trip-return");
  const tripDuration = document.getElementById("trip-duration");
  const selectionPanel = document.getElementById("selection-panel");
  const stickyCta = document.getElementById("sticky-cta");
  const stickyName = document.getElementById("sticky-name");
  const stickyPrice = document.getElementById("sticky-price");
  const stickyGo = document.getElementById("sticky-go");
  const flowSteps = document.querySelectorAll("#flow-steps li");
  const datePanel = document.getElementById("date-panel");
  const searchButton = document.getElementById("search-spaces");
  const changeDates = document.getElementById("change-dates");
  const dateSummaryText = document.getElementById("date-summary-text");
  const viewList = document.getElementById("view-list");
  const viewMap = document.getElementById("view-map");
  const flowBody = document.querySelector(".flow-body");
  const spacesHeading = document.getElementById("spaces-heading");
  const skeleton = document.getElementById("space-skeleton");
  const showAll = document.getElementById("show-all");
  const mapViewport = document.querySelector(".map-viewport");
  const filterButtons = { covered: document.getElementById("filter-covered"), ev: document.getElementById("filter-ev") };
  const TOP_N = 3;
  const filters = { covered: false, ev: false };
  let expanded = false;
  let animateCards = true;
  let loadedOnce = false;
  let lastSpaces = [];
  let lastFlags = {};
  const spaceById = new Map();
  let selectedSpaceId = null;
  let requestController = null;
  let refreshTimer = null;
  const locale = shell.dataset.locale || document.documentElement.lang || "en";
  const messages = shell.dataset.messages ? JSON.parse(shell.dataset.messages) : {};
  const message = (key, values = {}) => {
    let text = messages[key] || key;
    for (const [name, value] of Object.entries(values)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
    return text;
  };
  for (const element of document.querySelectorAll("[data-available-from]")) {
    const date = new Date(element.dataset.availableFrom);
    if (Number.isFinite(date.getTime())) {
      element.textContent = message("available_from", {
        time: new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(date)
      });
    }
  }

  const labels = {
    available: message("available"),
    reserved: message("reserved"),
    soon: message("soon"),
    optional: message("optional"),
    blocked: message("blocked")
  };

  const localInputValue = (date) => {
    const pad = (value) => String(value).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
  };

  const numeric = (value) => (value === null || value === undefined || value === "" ? NaN : Number(value));
  const cheapestOf = (spaces) => spaces
    .filter((space) => space.selectable && Number.isFinite(numeric(space.price)))
    .sort((left, right) => numeric(left.price) - numeric(right.price))[0];

  const formatUnit = (value, unit) => {
    try {
      return new Intl.NumberFormat(locale, { style: "unit", unit, unitDisplay: "long" }).format(value);
    } catch {
      return `${value} ${unit}${value === 1 ? "" : "s"}`;
    }
  };
  const formatDuration = (milliseconds) => {
    const hours = Math.max(1, Math.round(milliseconds / 3_600_000));
    if (hours < 24) return formatUnit(hours, "hour");
    const days = Math.floor(hours / 24);
    const rest = hours % 24;
    return rest ? `${formatUnit(days, "day")} ${formatUnit(rest, "hour")}` : formatUnit(days, "day");
  };
  const setFlowStep = (current) => {
    flowSteps.forEach((item, index) => {
      item.classList.toggle("done", index < current - 1);
      if (index === current - 1) item.setAttribute("aria-current", "step");
      else item.removeAttribute("aria-current");
    });
  };
  const updateTripSummary = () => {
    const interval = selectedInterval();
    if (!interval) {
      tripPickup.textContent = tripReturn.textContent = tripDuration.textContent = dateSummaryText.textContent = "–";
      return;
    }
    const format = new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" });
    tripPickup.textContent = format.format(interval.start);
    tripReturn.textContent = format.format(interval.end);
    tripDuration.textContent = formatDuration(interval.end - interval.start);
    dateSummaryText.textContent = `${format.format(interval.start)} → ${format.format(interval.end)} · ${tripDuration.textContent}`;
  };

  const setStage = (name) => {
    shell.dataset.stage = name;
    setFlowStep(name === "spaces" ? 2 : 1);
    if (name === "spaces") {
      animateCards = true;
      renderList();
    }
    flowBody.scrollIntoView({ block: "start" });
    if (name === "spaces") spacesHeading.focus({ preventScroll: true });
  };
  const setView = (name) => {
    shell.dataset.view = name;
    viewList.setAttribute("aria-pressed", String(name === "list"));
    viewMap.setAttribute("aria-pressed", String(name === "map"));
  };

  const selectedInterval = () => {
    if (!pickupInput.value || !returnInput.value) return null;
    const start = new Date(pickupInput.value);
    const end = new Date(returnInput.value);
    if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime()) || start <= new Date() || start >= end) {
      return null;
    }
    return { start, end };
  };

  const setSelectedSpace = (spaceId) => {
    const next = spaceById.get(String(spaceId));
    if (!next || !next.selectable) return;
    selectedSpaceId = String(spaceId);
    selectedIdInput.value = selectedSpaceId;
    for (const item of map.querySelectorAll(".map-space")) {
      item.setAttribute("aria-pressed", item.dataset.spaceId === selectedSpaceId ? "true" : "false");
    }
    for (const card of spaceList.querySelectorAll(".space-card")) {
      card.classList.toggle("is-selected", card.dataset.spaceId === selectedSpaceId);
    }
    selectedName.textContent = message("space", { name: next.name });
    selectedType.textContent = `${message(`kind.${next.kind}`)} · ${next.covered ? message("covered") : message("open_air")}${next.ev_charging ? ` · ${message("ev")}` : ""}`;
    selectedDistance.textContent = next.bus_distance_m === null ? message("shuttle_unknown") : message("shuttle_distance", { distance: next.bus_distance_m });
    selectedPrice.textContent = formatPrice(next.price, next.currency);
    selectionDetails.hidden = false;
    selectionMessage.textContent = message("selected");
    stickyName.textContent = selectedName.textContent;
    stickyPrice.textContent = selectedPrice.textContent;
    stickyCta.hidden = false;
    shell.dataset.selected = "true";
    updateHoldButton();
  };

  const formatPrice = (amount, currency) => {
    if (amount === null || amount === undefined || currency === null) return message("price_missing");
    const numeric = Number(amount);
    if (!Number.isFinite(numeric)) return message("price_missing");
    try {
      return new Intl.NumberFormat(locale, { style: "currency", currency }).format(numeric);
    } catch {
      return `${amount} ${currency}`;
    }
  };

  const updateHoldButton = () => {
    updateTripSummary();
    searchButton.disabled = !selectedInterval();
    const space = selectedSpaceId ? spaceById.get(selectedSpaceId) : null;
    const interval = selectedInterval();
    const emailValid = emailInput.checkValidity();
    const enabled = Boolean(space && space.selectable && interval && emailValid);
    holdButton.disabled = !enabled;
    if (interval) {
      pickupUtcInput.value = interval.start.toISOString();
      returnUtcInput.value = interval.end.toISOString();
    }
  };

  const svgElement = (name, attributes = {}) => {
    const element = document.createElementNS("http://www.w3.org/2000/svg", name);
    for (const [key, value] of Object.entries(attributes)) element.setAttribute(key, String(value));
    return element;
  };

  const safeImageUrl = (value) => {
    if (!value) return null;
    try {
      const url = new URL(value, window.location.origin);
      return ["https:", "http:"].includes(url.protocol) ? url.href : null;
    } catch {
      return null;
    }
  };

  // Customers only see spaces they can book; reserved, soon-free and blocked spaces are left off the map and list.
  const isShown = (space) => space.status === "available" || space.status === "optional";
  let totalSpaces = 0;

  const renderMapSpace = (space) => {
    let group = map.querySelector(`[data-space-id="${CSS.escape(String(space.id))}"]`);
    if (!isShown(space)) {
      if (group) group.remove();
      return;
    }
    if (!group) {
      group = svgElement("g", { "data-space-id": space.id });
      const rect = svgElement("rect", { rx: 8 });
      const text = svgElement("text", { "text-anchor": "middle", "dominant-baseline": "middle" });
      text.textContent = space.name;
      group.append(rect, text);
      map.append(group);
    }
    for (const state of Object.keys(labels)) group.classList.remove(`s-${state}`);
    group.classList.add("map-space", `s-${space.status}`);
    group.dataset.selectable = String(space.selectable);
    group.setAttribute("role", "button");
    group.setAttribute("tabindex", space.selectable ? "0" : "-1");
    group.setAttribute("aria-disabled", String(!space.selectable));
    group.setAttribute("aria-pressed", String(space.id) === selectedSpaceId ? "true" : "false");
    group.setAttribute("aria-label", `${message("space", { name: space.name })}, ${labels[space.status] || message("blocked")}${space.available_from ? `, ${message("available_from", { time: new Date(space.available_from).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) })}` : ""}`);
    const rect = group.querySelector("rect");
    rect.setAttribute("x", space.x);
    rect.setAttribute("y", space.y);
    rect.setAttribute("width", space.width);
    rect.setAttribute("height", space.height);
    const text = group.querySelector("text");
    text.setAttribute("x", Number(space.x) + Number(space.width) / 2);
    text.setAttribute("y", Number(space.y) + Number(space.height) / 2);
    text.textContent = space.name;
    for (const tag of group.querySelectorAll(".tag")) tag.remove();
    if (space.ev_charging) {
      const tag = svgElement("text", { class: "tag", x: Number(space.x) + Number(space.width) - 8, y: Number(space.y) + 16, "text-anchor": "end" });
      tag.textContent = "EV";
      group.append(tag);
    }
    if (space.covered) {
      const tag = svgElement("text", { class: "tag", x: Number(space.x) + 8, y: Number(space.y) + 16 });
      tag.textContent = "C";
      group.append(tag);
    }
  };

  const BUS_ICON = '<svg class="ico" viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="4" y="4" width="16" height="13" rx="3"/><path d="M4 11h16M7 20v-3M17 20v-3"/></svg>';

  const renderCard = (space, flags = {}) => {
    const item = document.createElement("li");
    item.className = `space-card${space.selectable ? "" : " is-disabled"}${String(space.id) === selectedSpaceId ? " is-selected" : ""}`;
    item.dataset.spaceId = space.id;
    item.dataset.selectable = String(space.selectable);
    item.dataset.price = space.price || "";

    const button = document.createElement("button");
    button.className = "space-card-button";
    button.type = "button";
    button.disabled = !space.selectable;
    button.setAttribute("aria-label", `Space ${space.name}, ${labels[space.status] || "Unavailable"}`);
    const imageUrl = safeImageUrl(space.photo_url);
    if (imageUrl) {
      const image = document.createElement("img");
      image.className = "space-thumb";
      image.src = imageUrl;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      button.append(image);
    } else {
      const tile = document.createElement("span");
      tile.className = "space-thumb space-thumb-tile";
      tile.setAttribute("aria-hidden", "true");
      tile.textContent = space.name;
      button.append(tile);
    }

    const copy = document.createElement("span");
    copy.className = "space-card-copy";
    const name = document.createElement("strong");
    name.textContent = message("space", { name: space.name });
    copy.append(name);
    if (flags.cheapest || flags.closest) {
      const badges = document.createElement("span");
      badges.className = "badges";
      for (const [on, key] of [[flags.cheapest, "badge.price"], [flags.closest, "badge.shuttle"]]) {
        if (!on) continue;
        const badge = document.createElement("span");
        badge.className = "badge-pill";
        badge.textContent = message(key);
        badges.append(badge);
      }
      copy.append(badges);
    }
    const chips = document.createElement("span");
    chips.className = "chips";
    const chipTexts = [message(`kind.${space.kind}`), space.covered ? message("covered") : message("open_air")];
    if (space.ev_charging) chipTexts.push(message("ev"));
    chipTexts.forEach((text, index) => {
      const chip = document.createElement("span");
      chip.className = `chip${space.ev_charging && index === 2 ? " chip-ev" : ""}`;
      chip.textContent = text;
      chips.append(chip);
    });
    copy.append(chips);
    const distance = document.createElement("span");
    distance.className = "space-meta";
    distance.insertAdjacentHTML("afterbegin", BUS_ICON);
    distance.append(space.bus_distance_m === null ? message("shuttle_unknown") : message("shuttle_distance", { distance: space.bus_distance_m }));
    copy.append(distance);
    button.append(copy);

    const price = document.createElement("span");
    price.className = "space-price";
    price.textContent = formatPrice(space.price, space.currency);
    const interval = selectedInterval();
    const days = interval ? Math.max(1, Math.ceil((interval.end - interval.start) / 86_400_000)) : 1;
    if (days > 1 && Number.isFinite(numeric(space.price)) && space.currency) {
      const perDay = document.createElement("small");
      perDay.className = "per-day";
      perDay.textContent = message("per_day", { price: formatPrice(numeric(space.price) / days, space.currency) });
      price.append(perDay);
    }
    button.append(price);
    item.append(button);

    if (space.status === "soon" && space.available_from) {
      const note = document.createElement("small");
      note.className = "availability-note";
      note.textContent = message("available_from", { time: new Date(space.available_from).toLocaleTimeString(locale, { hour: "2-digit", minute: "2-digit" }) });
      item.append(note);
    }
    return item;
  };

  const renderList = () => {
    const passes = (space) => (!filters.covered || space.covered) && (!filters.ev || space.ev_charging);
    const priceOf = (space) => (Number.isFinite(numeric(space.price)) ? numeric(space.price) : Infinity);
    const open = lastSpaces.filter((space) => space.selectable && passes(space)).sort((a, b) => priceOf(a) - priceOf(b));
    const closed = lastSpaces.filter((space) => !space.selectable && passes(space));
    const ordered = [...open, ...closed];
    let shown = expanded ? ordered : (open.length ? open : closed).slice(0, TOP_N);
    if (!expanded && selectedSpaceId && !shown.some((space) => String(space.id) === selectedSpaceId)) {
      const chosen = spaceById.get(selectedSpaceId);
      if (chosen) shown = [chosen, ...shown.slice(0, TOP_N - 1)];
    }
    spaceList.replaceChildren(...shown.map((space, index) => {
      const card = renderCard(space, {
        cheapest: String(lastFlags.cheapest) === String(space.id),
        closest: String(lastFlags.closest) === String(space.id)
      });
      if (animateCards) {
        card.classList.add("enter");
        card.style.setProperty("--i", index);
      }
      return card;
    }));
    animateCards = false;
    spaceCount.textContent = message("space_count", { count: open.length });
    showAll.hidden = ordered.length <= TOP_N && !expanded;
    showAll.textContent = expanded ? message("show_less") : message("show_all", { count: ordered.length });
    noSpaces.hidden = lastSpaces.length > 0;
    noSpaces.textContent = message(totalSpaces > 0 ? "none_free" : "none_configured");
  };

  const setLoading = (on) => {
    skeleton.hidden = !on;
    spaceList.hidden = on;
    if (on) showAll.hidden = true;
    datePanel.classList.toggle("is-loading", on);
    availabilityMessage.classList.toggle("is-loading", on);
    availabilityMessage.setAttribute("aria-busy", String(on));
    mapViewport.classList.toggle("is-loading", on);
    datePanel.classList.toggle("has-note", on || availabilityMessage.textContent !== message("up_to_date"));
  };

  const updateFromData = (spaces) => {
    spaceById.clear();
    for (const space of spaces) {
      space.selectable = Boolean(space.selectable);
      spaceById.set(String(space.id), space);
      renderMapSpace(space);
    }
    const open = spaces.filter((space) => space.selectable);
    const cheapest = open.length > 1 ? cheapestOf(open) : null;
    const closest = open.length > 1
      ? open.filter((space) => Number.isFinite(numeric(space.bus_distance_m)))
          .sort((left, right) => numeric(left.bus_distance_m) - numeric(right.bus_distance_m))[0]
      : null;
    totalSpaces = spaces.length;
    lastSpaces = spaces.filter(isShown);
    lastFlags = { cheapest: cheapest ? cheapest.id : null, closest: closest ? closest.id : null };
    renderList();
    const best = cheapestOf(spaces);
    bestAvailableButton.disabled = !best;
    if (selectedSpaceId && !spaceById.get(selectedSpaceId)?.selectable) {
      selectedSpaceId = null;
      selectedIdInput.value = "";
      selectionDetails.hidden = true;
      stickyCta.hidden = true;
      shell.dataset.selected = "false";
      selectionMessage.textContent = message("selection_lost");
      renderList();
    } else if (selectedSpaceId) {
      setSelectedSpace(selectedSpaceId);
    }
    updateHoldButton();
  };

  const refreshAvailability = async ({ clearSelection = false } = {}) => {
    const interval = selectedInterval();
    if (clearSelection) {
      selectedSpaceId = null;
      selectedIdInput.value = "";
      selectionDetails.hidden = true;
      stickyCta.hidden = true;
      shell.dataset.selected = "false";
      selectionMessage.textContent = message("choose_continue");
      holdButton.disabled = true;
    }
    if (!interval) {
      availabilityMessage.textContent = message("future_times");
      updateHoldButton();
      return;
    }
    if (requestController) requestController.abort();
    requestController = new AbortController();
    if (clearSelection || !loadedOnce) {
      animateCards = true;
      setLoading(true);
    }
    availabilityMessage.textContent = message("updating");
    const url = new URL(shell.dataset.availabilityUrl, window.location.origin);
    url.searchParams.set("pickup_at", interval.start.toISOString());
    url.searchParams.set("return_at", interval.end.toISOString());
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" }, signal: requestController.signal });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || message("unavailable"));
      updateFromData(data.spaces);
      availabilityMessage.textContent = message("up_to_date");
    } catch (error) {
      if (error.name === "AbortError") return;
      availabilityMessage.textContent = error.message || message("unavailable");
    }
    loadedOnce = true;
    setLoading(false);
  };

  const updateFromUtcDefaults = () => {
    const pickup = new Date(shell.dataset.pickupUtc);
    const returned = new Date(shell.dataset.returnUtc);
    pickupInput.value = localInputValue(pickup);
    returnInput.value = localInputValue(returned);
    const minValue = localInputValue(new Date(Date.now() + 60_000));
    pickupInput.min = minValue;
    returnInput.min = minValue;
    // Dates chosen in the embeddable widget arrive as ?pickup_local=YYYY-MM-DDTHH:MM&return_local=...
    const params = new URLSearchParams(window.location.search);
    const fromWidget = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
    const wantedPickup = params.get("pickup_local");
    const wantedReturn = params.get("return_local");
    if (fromWidget.test(wantedPickup || "") && fromWidget.test(wantedReturn || "") && wantedPickup < wantedReturn && wantedPickup >= minValue) {
      pickupInput.value = wantedPickup;
      returnInput.value = wantedReturn;
    }
  };

  map.addEventListener("click", (event) => {
    const target = event.target.closest(".map-space");
    if (target?.dataset.selectable === "true") setSelectedSpace(target.dataset.spaceId);
  });
  map.addEventListener("keydown", (event) => {
    const target = event.target.closest(".map-space");
    if (target && ["Enter", " "].includes(event.key)) {
      event.preventDefault();
      if (target.dataset.selectable === "true") setSelectedSpace(target.dataset.spaceId);
    }
  });
  spaceList.addEventListener("click", (event) => {
    const card = event.target.closest(".space-card");
    if (card?.dataset.selectable === "true") setSelectedSpace(card.dataset.spaceId);
  });
  pickupInput.addEventListener("change", () => refreshAvailability({ clearSelection: true }));
  returnInput.addEventListener("change", () => refreshAvailability({ clearSelection: true }));
  emailInput.addEventListener("input", updateHoldButton);
  holdForm.addEventListener("submit", (event) => {
    updateHoldButton();
    if (holdButton.disabled) event.preventDefault();
  });
  bestAvailableButton.addEventListener("click", () => {
    const best = cheapestOf([...spaceById.values()]);
    if (best) setSelectedSpace(best.id);
  });

  stickyGo.addEventListener("click", () => {
    if (!holdButton.disabled) {
      holdForm.requestSubmit();
      return;
    }
    const calm = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    selectionPanel.scrollIntoView({ behavior: calm ? "auto" : "smooth", block: "start" });
    emailInput.focus({ preventScroll: true });
  });
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
      stickyCta.classList.toggle("is-tucked", entry.isIntersecting);
    }, { threshold: 0.25 }).observe(selectionPanel);
  }

  searchButton.addEventListener("click", () => {
    if (selectedInterval()) setStage("spaces");
  });
  changeDates.addEventListener("click", () => {
    setStage("dates");
    pickupInput.focus({ preventScroll: true });
  });
  viewList.addEventListener("click", () => setView("list"));
  viewMap.addEventListener("click", () => setView("map"));
  for (const [key, button] of Object.entries(filterButtons)) {
    button.addEventListener("click", () => {
      filters[key] = !filters[key];
      button.setAttribute("aria-pressed", String(filters[key]));
      animateCards = true;
      renderList();
    });
  }
  showAll.addEventListener("click", () => {
    expanded = !expanded;
    animateCards = true;
    renderList();
  });

  let zoom = 1;
  document.getElementById("zoom-in").addEventListener("click", () => {
    zoom = Math.min(1.8, zoom + 0.2);
    map.style.width = `${zoom * 100}%`;
  });
  document.getElementById("zoom-out").addEventListener("click", () => {
    zoom = Math.max(1, zoom - 0.2);
    map.style.width = `${zoom * 100}%`;
  });

  updateFromUtcDefaults();
  refreshAvailability();
  if (window.io) {
    const socket = window.io("/booking");
    socket.on("connect", () => {
      socket.emit("join_lot", {
        tenant_slug: shell.dataset.tenantSlug,
        lot_slug: shell.dataset.lotSlug
      });
    });
    socket.on("availability_changed", refreshAvailability);
    socket.on("disconnect", () => {
      availabilityMessage.textContent = message("network_wait");
    });
    window.addEventListener("beforeunload", () => {
      socket.emit("leave_lot", {
        tenant_slug: shell.dataset.tenantSlug,
        lot_slug: shell.dataset.lotSlug
      });
    });
  }
  refreshTimer = window.setInterval(refreshAvailability, 20_000);
  window.addEventListener("pagehide", () => {
    if (refreshTimer) window.clearInterval(refreshTimer);
  });
})();
