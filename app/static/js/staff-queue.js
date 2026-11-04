(function (root) {
  "use strict";

  const DB_NAME = "parkpilot-staff";
  const STORE_NAME = "inspection-uploads";

  function openDatabase() {
    return new Promise((resolve, reject) => {
      const request = indexedDB.open(DB_NAME, 1);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains(STORE_NAME)) {
          request.result.createObjectStore(STORE_NAME, { keyPath: "id" });
        }
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
  }

  async function withStore(mode, operation) {
    const database = await openDatabase();
    return new Promise((resolve, reject) => {
      const transaction = database.transaction(STORE_NAME, mode);
      const store = transaction.objectStore(STORE_NAME);
      const result = operation(store);
      transaction.oncomplete = () => {
        database.close();
        resolve(result && result.result);
      };
      transaction.onerror = () => {
        database.close();
        reject(transaction.error);
      };
      transaction.onabort = () => {
        database.close();
        reject(transaction.error);
      };
    });
  }

  async function enqueue(item) {
    const id = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    await withStore("readwrite", (store) => store.add({ ...item, id, queuedAt: Date.now() }));
    return id;
  }

  async function all() {
    return withStore("readonly", (store) => store.getAll());
  }

  async function remove(id) {
    return withStore("readwrite", (store) => store.delete(id));
  }

  async function count() {
    return withStore("readonly", (store) => store.count());
  }

  async function flush() {
    const items = await all();
    const outcomes = [];
    for (const item of items) {
      const form = new FormData();
      for (const [key, value] of Object.entries(item.fields)) {
        form.append(key, value);
      }
      form.append("photo", item.blob, item.filename);
      try {
        const response = await fetch(item.url, {
          method: "POST",
          body: form,
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          outcomes.push({ id: item.id, ok: false, message: body.error || ppT("Upload failed (%(status)s).", { status: response.status }) });
          break;
        }
        await remove(item.id);
        outcomes.push({ id: item.id, ok: true });
      } catch (error) {
        outcomes.push({ id: item.id, ok: false, message: ppT("Upload is waiting for a network connection.") });
        break;
      }
    }
    return outcomes;
  }

  root.ParkPilotInspectionQueue = { enqueue, all, remove, count, flush };
})(typeof self === "undefined" ? window : self);
