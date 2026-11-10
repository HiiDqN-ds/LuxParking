(function () {
  "use strict";

  const labels = {
    front: ppT("front"),
    back: ppT("back"),
    right: ppT("right side"),
    left: ppT("left side"),
    corner_front_left: ppT("front-left corner"),
    corner_front_right: ppT("front-right corner"),
    corner_back_left: ppT("rear-left corner"),
    corner_back_right: ppT("rear-right corner"),
    roof: ppT("roof"),
    windshield: ppT("windshield"),
    odometer: ppT("odometer"),
    fuel_level: ppT("fuel level"),
    signature: ppT("customer signature"),
  };

  function compressImage(file) {
    if (!file || !file.type.startsWith("image/") || !("createImageBitmap" in window)) {
      return Promise.resolve(file);
    }
    return createImageBitmap(file).then((bitmap) => {
      const scale = Math.min(1, 1600 / bitmap.width, 1200 / bitmap.height);
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      bitmap.close();
      return new Promise((resolve) => {
        canvas.toBlob(
          (blob) => resolve(blob && blob.size < file.size ? blob : file),
          "image/webp",
          0.82
        );
      });
    }).catch(() => file);
  }

  function formFields(form, category) {
    const fields = {};
    for (const element of form.elements) {
      if (element.name && element.type !== "file" && element.name !== "photo") {
        fields[element.name] = element.value;
      }
    }
    if (category) fields.category = category;
    fields.captured_at = new Date().toISOString();
    return fields;
  }

  async function uploadEvidence({ form, file, category, url, csrfToken, status, reload = true }) {
    const blob = await compressImage(file);
    const fields = form ? formFields(form, category) : {
      csrf_token: csrfToken,
      category,
      captured_at: new Date().toISOString(),
    };
    const id = await window.ParkPilotInspectionQueue.enqueue({
      url,
      fields,
      blob,
      filename: category === "signature" ? "customer-signature.png" : `${category || "damage"}.webp`,
    });
    if (status) status.textContent = navigator.onLine ? ppT("Uploading…") : ppT("Saved on this device; will upload when online.");
    if ("serviceWorker" in navigator) {
      try {
        // Background sync is only a bonus for flaky networks. Browsers may disable it (it then throws);
        // the upload below must never depend on it.
        const registration = await navigator.serviceWorker.ready;
        if (registration.sync) {
          await registration.sync.register("inspection-upload");
        }
      } catch (error) {
        /* continue with the normal upload */
      }
    }
    if (navigator.onLine) {
      const outcomes = await window.ParkPilotInspectionQueue.flush();
      const result = outcomes.find((outcome) => outcome.id === id);
      if (result && result.ok) {
        if (status) status.textContent = ppT("%(label)s saved.", { label: labels[category] || ppT("Damage photo") });
        if (reload) reloadAfterDraft();
        return true;
      }
      if (status && result) status.textContent = result.message;
    }
    updateQueueCount();
    return "queued";
  }

  async function uploadVideoCapture(videoForm, file, status) {
    const category = videoForm.querySelector('[name="category"]')?.value || "";
    if (!category) throw new Error(ppT("Could not prepare this video for upload."));
    await validateVideoDuration(file);
    const csrfToken = videoForm.querySelector('[name="csrf_token"]').value;
    const videoId = await window.ParkPilotInspectionQueue.enqueue({
      url: videoForm.action,
      fields: {
        csrf_token: csrfToken,
        category,
        captured_at: new Date().toISOString(),
      },
      blob: file,
      filename: file.name || `${category}.video`,
      fileField: "video",
    });
    let videoResult = null;
    if (navigator.onLine) {
      const outcomes = await window.ParkPilotInspectionQueue.flush();
      videoResult = outcomes.find((outcome) => outcome.id === videoId);
      if (videoResult && !videoResult.ok) {
        if (status) status.textContent = videoResult.message;
        return false;
      }
    }
    await updateQueueCount();
    if (videoResult && videoResult.ok) {
      if (status) status.textContent = ppT("Video saved.");
      reloadAfterDraft();
      return true;
    }
    if (status && !videoResult) status.textContent = ppT("Saved on this device; will upload when online.");
    return "queued";
  }

  function validateVideoDuration(file) {
    return new Promise((resolve, reject) => {
      const source = URL.createObjectURL(file);
      const player = document.createElement("video");
      player.preload = "metadata";
      const cleanUp = () => URL.revokeObjectURL(source);
      player.onerror = () => { cleanUp(); reject(new Error(ppT("Could not read this video."))); };
      player.onloadedmetadata = () => {
        cleanUp();
        if (!Number.isFinite(player.duration) || player.duration <= 0 || player.duration > 30) {
          reject(new Error(ppT("Choose a video that is 30 seconds or shorter.")));
          return;
        }
        resolve();
      };
      player.src = source;
    });
  }

  async function updateQueueCount() {
    const element = document.getElementById("upload-queue-count");
    if (!element || !window.ParkPilotInspectionQueue) return;
    try {
      element.textContent = ppT("Offline queue: %(count)s", { count: await window.ParkPilotInspectionQueue.count() });
    } catch (error) {
      element.textContent = ppT("Offline queue unavailable in this browser.");
    }
  }

  let damagePositioned = false;  // an automatic damage save needs a deliberate tap on the vehicle first

  document.querySelectorAll("[data-evidence-form]").forEach((form) => {
    const fileInput = form.querySelector('input[type="file"][name="photo"]');
    const saveButton = form.querySelector('button[type="submit"]');
    if (saveButton) saveButton.hidden = true;  // nothing to press: choosing or taking the photo saves it
    if (fileInput) {
      fileInput.addEventListener("change", () => {
        if (!fileInput.files || !fileInput.files[0]) return;
        if (form.classList.contains("damage-form") && !damagePositioned) {
          const hintBox = document.getElementById("diagram-hint");
          if (hintBox) { hintBox.textContent = ppT("Tap the vehicle to set the location for the next damage mark."); hintBox.classList.add("is-active"); }
          fileInput.value = "";
          return;
        }
        form.requestSubmit();
      });
    }
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const file = form.querySelector('input[type="file"][name="photo"]')?.files?.[0];
      if (!file) {
        form.reportValidity();
        return;
      }
      const status = form.querySelector(".upload-state");
      const category = form.querySelector('[name="category"]')?.value || "";
      try {
        await uploadEvidence({
          form,
          file,
          category,
          url: form.action,
          csrfToken: form.querySelector('[name="csrf_token"]').value,
          status,
        });
      } catch (error) {
        if (status) status.textContent = ppT("Could not save this image to the offline queue.");
      }
    });
  });

  document.querySelectorAll("[data-video-form]").forEach((form) => {
    const input = form.querySelector('input[type="file"][name="video"]');
    const status = form.querySelector(".upload-state");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      if (status) status.textContent = ppT("Preparing video…");
      try {
        await uploadVideoCapture(form, file, status);
      } catch (error) {
        if (status) status.textContent = error.message || ppT("Could not save this video. Try again.");
      } finally {
        input.value = "";
      }
    });
  });

  const diagram = document.getElementById("vehicle-diagram");
  if (diagram) {
    const SVG_NS = "http://www.w3.org/2000/svg";
    const form = document.querySelector(".damage-form");
    const hint = document.getElementById("diagram-hint");
    const defaultHint = hint ? hint.textContent : "";
    let pending = null;

    function showPending(x, y) {
      if (!pending) {
        pending = document.createElementNS(SVG_NS, "g");
        pending.setAttribute("class", "damage-pending");
        pending.setAttribute("pointer-events", "none");
        const ring = document.createElementNS(SVG_NS, "circle");
        ring.setAttribute("class", "damage-pending-ring");
        ring.setAttribute("r", "6");
        const dot = document.createElementNS(SVG_NS, "circle");
        dot.setAttribute("class", "damage-pending-dot");
        dot.setAttribute("r", "2.5");
        pending.append(ring, dot);
        diagram.appendChild(pending);
      }
      pending.setAttribute("transform", `translate(${x} ${y})`);
      if (hint) {
        hint.textContent = form
          ? ppT("Mark set at %(x)s, %(y)s. Choose the damage type and add a photo below; it saves automatically.", { x: Math.round(x), y: Math.round(y) })
          : ppT("Mark at %(x)s, %(y)s.", { x: Math.round(x), y: Math.round(y) });
        hint.classList.add("is-active");
      }
    }

    function pointFromEvent(event) {
      const ctm = diagram.getScreenCTM();
      if (ctm && diagram.createSVGPoint) {
        const point = diagram.createSVGPoint();
        point.x = event.clientX;
        point.y = event.clientY;
        const local = point.matrixTransform(ctm.inverse());
        return { x: local.x, y: local.y };
      }
      const bounds = diagram.getBoundingClientRect();
      return {
        x: ((event.clientX - bounds.left) / bounds.width) * 100,
        y: ((event.clientY - bounds.top) / bounds.height) * 100,
      };
    }

    diagram.addEventListener("click", (event) => {
      const raw = pointFromEvent(event);
      const x = Math.min(100, Math.max(0, raw.x));
      const y = Math.min(100, Math.max(0, raw.y));
      if (form) {
        form.elements.x.value = String(Math.round(x));
        form.elements.y.value = String(Math.round(y));
      }
      damagePositioned = true;
      showPending(Math.round(x), Math.round(y));
    });

    // Typing coordinates by hand moves the marker too.
    if (form) {
      const syncFromFields = () => {
        const x = Number(form.elements.x.value);
        const y = Number(form.elements.y.value);
        if (Number.isFinite(x) && Number.isFinite(y)) {
          damagePositioned = true;
          showPending(Math.min(100, Math.max(0, x)), Math.min(100, Math.max(0, y)));
        }
      };
      form.elements.x.addEventListener("input", syncFromFields);
      form.elements.y.addEventListener("input", syncFromFields);
    } else if (hint) {
      hint.textContent = defaultHint;
    }
  }

  const signature = document.getElementById("customer-signature");
  if (signature) {
    const context = signature.getContext("2d");
    const status = document.getElementById("signature-state");
    let drawing = false;
    let inkLength = 0;
    let lastPoint = null;
    let saveTimer = null;
    let saving = false;
    context.lineWidth = 4;
    context.lineCap = "round";
    context.strokeStyle = "#14212B";
    const pointOf = (event) => {
      const rect = signature.getBoundingClientRect();
      return [((event.clientX - rect.left) / rect.width) * signature.width, ((event.clientY - rect.top) / rect.height) * signature.height];
    };
    const saveSignature = () => {
      if (saving || inkLength < 120) return;
      saving = true;
      signature.toBlob(async (blob) => {
        try {
          await uploadEvidence({ file: blob, category: "signature", url: signature.dataset.uploadUrl, csrfToken: signature.dataset.csrf, status });
        } catch (error) {
          if (status) status.textContent = ppT("Could not save the signature to the offline queue.");
        } finally {
          saving = false;
        }
      }, "image/png");
    };
    signature.addEventListener("pointerdown", (event) => {
      drawing = true;
      clearTimeout(saveTimer);
      signature.setPointerCapture(event.pointerId);
      lastPoint = pointOf(event);
      context.beginPath();
      context.moveTo(lastPoint[0], lastPoint[1]);
    });
    signature.addEventListener("pointermove", (event) => {
      if (!drawing) return;
      const next = pointOf(event);
      context.lineTo(next[0], next[1]);
      context.stroke();
      inkLength += Math.hypot(next[0] - lastPoint[0], next[1] - lastPoint[1]);
      lastPoint = next;
    });
    const lift = () => {
      if (!drawing) return;
      drawing = false;
      if (inkLength >= 120) {  // a real signature rather than an accidental touch; saved after a short pause
        if (status) status.textContent = ppT("Uploading…");
        saveTimer = setTimeout(saveSignature, 1200);
      }
    };
    signature.addEventListener("pointerup", lift);
    signature.addEventListener("pointercancel", lift);
    document.getElementById("clear-signature").addEventListener("click", () => {
      clearTimeout(saveTimer);
      context.clearRect(0, 0, signature.width, signature.height);
      inkLength = 0;
      if (status) status.textContent = "";
    });
  }

  // Handover form: saved on the server while typing (and on this device while offline), so a reload or a lost
  // connection never loses what the employee entered.
  const draftForm = document.querySelector(".inspection-complete-form");
  const draftKey = draftForm ? `ppdraft:${draftForm.dataset.inspectionId}` : "";
  const checklist = document.getElementById("complete-checklist");
  let draftDirty = false;
  let draftTimer = null;

  function draftValues() {
    const data = {};
    for (const element of draftForm.elements) {
      if (!element.name || element.name === "csrf_token" || element.name === "then") continue;
      data[element.name] = element.type === "checkbox" ? (element.checked ? "yes" : "") : element.value;
    }
    return data;
  }

  function keepDraftOnDevice(clear) {
    try {
      if (clear) localStorage.removeItem(draftKey);
      else localStorage.setItem(draftKey, JSON.stringify(draftValues()));
    } catch (error) { /* private browsing: the server copy still works */ }
  }

  function showDraftState(text) {
    const element = document.getElementById("draft-state");
    if (element) element.textContent = text;
  }

  async function flushDraft() {
    if (!draftForm || !draftDirty) return;
    clearTimeout(draftTimer);
    draftDirty = false;
    try {
      const response = await fetch(draftForm.dataset.draftUrl, {
        method: "POST",
        credentials: "same-origin",
        keepalive: true,
        headers: { "Content-Type": "application/json", Accept: "application/json", "X-CSRFToken": draftForm.querySelector('[name="csrf_token"]').value },
        body: JSON.stringify(draftValues()),
      });
      if (!response.ok) throw new Error("draft");
      keepDraftOnDevice(true);
      showDraftState(ppT("Draft saved automatically."));
    } catch (error) {
      draftDirty = true;
      showDraftState(ppT("Saved on this device; will upload when online."));
    }
  }

  function reloadAfterDraft() {
    flushDraft().then(() => window.location.reload(), () => window.location.reload());
  }

  function updateChecklist() {
    if (!checklist || !draftForm) return;
    const marks = Number(checklist.dataset.marks) || 0;
    const videoCount = Number(checklist.dataset.videoCount) || 0;
    const requiredVideos = Number(checklist.dataset.videoRequired) || 0;
    const confirmed = draftForm.elements.no_damage_confirmed.checked;
    const mark = (key, ok) => { const item = checklist.querySelector(`[data-key="${key}"]`); if (item) item.classList.toggle("is-done", ok); };
    mark("videos", videoCount >= requiredVideos);
    mark("odometer", draftForm.elements.odometer_km.value.trim() !== "");
    mark("damage", marks > 0 ? !confirmed : confirmed);  // marks present: do not confirm "no damage"; none: confirm it
    const missing = [...checklist.querySelectorAll("li:not(.is-done)")].map((item) => item.textContent.trim());
    const note = document.getElementById("complete-missing");
    if (note) note.textContent = missing.length ? checklist.dataset.missing.replace("{items}", missing.join(", ")) : "";
    const button = document.getElementById("complete-button");
    if (button) button.disabled = missing.length > 0;
  }

  if (draftForm) {
    try {  // anything that could not be sent last time comes back first
      const saved = JSON.parse(localStorage.getItem(draftKey) || "null");
      if (saved && typeof saved === "object") {
        for (const element of draftForm.elements) {
          if (!element.name || !(element.name in saved)) continue;
          if (element.type === "checkbox") element.checked = saved[element.name] === "yes";
          else element.value = saved[element.name];
        }
        draftDirty = true;
        draftTimer = setTimeout(flushDraft, 400);
      }
    } catch (error) { /* ignore a damaged local copy */ }
    const changed = () => {
      draftDirty = true;
      keepDraftOnDevice(false);
      updateChecklist();
      clearTimeout(draftTimer);
      draftTimer = setTimeout(flushDraft, 700);
    };
    draftForm.addEventListener("input", changed);
    draftForm.addEventListener("change", changed);
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") flushDraft(); });
    updateChecklist();
  }

  const completeForm = document.querySelector(".inspection-complete-form");
  if (completeForm) {
    completeForm.addEventListener("submit", async (event) => {
      try {
        const pending = await window.ParkPilotInspectionQueue.count();
        if (pending > 0) {
          event.preventDefault();
          const message = document.getElementById("offline-status");
          message.textContent = ppT("Upload all queued photos before completing this inspection.");
        }
      } catch (error) {
        event.preventDefault();
        document.getElementById("offline-status").textContent = ppT("The offline upload queue could not be checked.");
      }
    });
  }

  window.addEventListener("online", async () => {
    flushDraft();
    const message = document.getElementById("offline-status");
    if (message) message.textContent = ppT("Connection restored. Uploading saved inspection photos…");
    const outcomes = await window.ParkPilotInspectionQueue.flush();
    await updateQueueCount();
    if (outcomes.some((outcome) => outcome.ok)) reloadAfterDraft();
  });

  updateQueueCount();
})();
