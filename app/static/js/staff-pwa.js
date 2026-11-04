(function () {
  "use strict";

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => {
      navigator.serviceWorker.register("/staff/service-worker.js", { scope: "/staff/" })
        .catch(() => {
          const status = document.getElementById("offline-status");
          if (status) status.textContent = ppT("Offline app setup failed; inspection uploads still work while this page remains open.");
        });
    });
  }

  const video = document.getElementById("qr-camera");
  const start = document.getElementById("start-scan");
  const stop = document.getElementById("stop-scan");
  const message = document.getElementById("scan-message");
  const lookup = document.getElementById("booking-lookup");
  if (!video || !start || !lookup) return;

  let stream;
  let scanning = false;
  start.addEventListener("click", async () => {
    if (!("BarcodeDetector" in window)) {
      message.textContent = ppT("QR camera scanning is not supported here. Enter the booking number instead.");
      return;
    }
    try {
      const detector = new BarcodeDetector({ formats: ["qr_code"] });
      stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment" } });
      video.srcObject = stream;
      video.hidden = false;
      stop.hidden = false;
      start.hidden = true;
      scanning = true;
      await video.play();
      message.textContent = ppT("Point the camera at the customer's booking QR code.");
      const scanFrame = async () => {
        if (!scanning) return;
        try {
          const codes = await detector.detect(video);
          if (codes.length) {
            document.getElementById("booking-code").value = codes[0].rawValue;
            stopCamera();
            lookup.requestSubmit();
            return;
          }
        } catch (error) {
          message.textContent = ppT("Could not read that QR code. Try again or enter the booking number.");
        }
        requestAnimationFrame(scanFrame);
      };
      requestAnimationFrame(scanFrame);
    } catch (error) {
      message.textContent = ppT("Camera access was unavailable. Enter the booking number instead.");
    }
  });

  function stopCamera() {
    scanning = false;
    if (stream) stream.getTracks().forEach((track) => track.stop());
    video.srcObject = null;
    video.hidden = true;
    stop.hidden = true;
    start.hidden = false;
  }
  stop.addEventListener("click", stopCamera);
})();
