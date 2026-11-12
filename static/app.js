(() => {
  const app = document.getElementById("app");
  if (!app) return;
  const code = app.dataset.code, portal = app.dataset.portal === "1";
  if (!code && !portal) return;
  const $ = (id) => document.getElementById(id);
  const sections = ["step-email", "step-otp", "step-selfie", "step-pending", "step-gallery"];
  let email = "", stream = null, pollTimer = null;

  async function api(path, body) {
    const opt = body === undefined ? {} : {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "facefind" },
      body: JSON.stringify(body),
    };
    try {
      const r = await fetch(path, { credentials: "same-origin", ...opt });
      const j = await r.json().catch(() => ({ ok: false, error: "Something went wrong." }));
      j.status_code = r.status;
      return j;
    } catch (e) {
      return { ok: false, error: "No connection. Please try again." };
    }
  }

  function say(text) {
    const m = $("msg");
    m.textContent = text || "";
    m.hidden = !text;
  }

  function show(id, stepNo) {
    sections.forEach((s) => { $(s).hidden = s !== id; });
    [1, 2, 3, 4].forEach((n) => $("s" + n).classList.toggle("on", n <= stepNo));
    if (id !== "step-selfie") stopCam();
    clearTimeout(pollTimer);
  }

  function busy(btn, on) { btn.disabled = on; }

  // ---- step 1: email
  $("f-email").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    busy(btn, true); say("");
    email = $("email").value.trim();
    const r = await api("/api/otp/send", { code, email, consent: $("consent").checked });
    busy(btn, false);
    if (!r.ok) return say(r.error);
    $("otp-email").textContent = email;
    $("otp").value = "";
    show("step-otp", 2);
    $("otp").focus();
  });

  // ---- step 2: OTP
  $("f-otp").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    busy(btn, true); say("");
    const r = await api("/api/otp/verify", { code, email, otp: $("otp").value.trim() });
    busy(btn, false);
    if (!r.ok) return say(r.error);
    startSelfie();
  });
  $("otp-back").addEventListener("click", () => { say(""); show("step-email", 1); });

  // ---- step 3: selfie (camera only - no gallery upload)
  async function startSelfie() {
    show("step-selfie", 3);
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 1280 } }, audio: false });
      $("video").srcObject = stream;
    } catch (e) {
      say("Please allow camera access to take your selfie.");
    }
  }
  function stopCam() {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  }
  // draws a camera frame or an uploaded picture (max 1280 px) and sends it
  async function sendFace(source, w, h) {
    const c = $("canvas");
    const scale = Math.min(1, 1280 / Math.max(w, h));
    c.width = Math.round(w * scale);
    c.height = Math.round(h * scale);
    c.getContext("2d").drawImage(source, 0, 0, c.width, c.height);
    busy($("snap"), true); say("Looking for your photos...");
    const body = { image: c.toDataURL("image/jpeg", 0.92) };
    if (portal) { body.date_from = $("d-from").value; body.date_to = $("d-to").value; }
    const r = await api("/api/selfie", body);
    busy($("snap"), false);
    if (!r.ok) {
      if (r.status_code === 401) return show("step-email", 1), say(r.error);
      return say(r.error);
    }
    say("");
    render(r);
  }
  $("snap").addEventListener("click", () => {
    const v = $("video");
    if (!stream || !v.videoWidth) return say("Camera is not ready yet. You can upload a photo instead.");
    sendFace(v, v.videoWidth, v.videoHeight);
  });
  $("upload").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const img = new Image();
    img.onload = () => { sendFace(img, img.naturalWidth, img.naturalHeight); URL.revokeObjectURL(img.src); };
    img.onerror = () => say("That file is not a picture. Please choose a JPG or PNG photo.");
    img.src = URL.createObjectURL(file);
    e.target.value = "";
  });

  // ---- step 4: photos
  function render(st) {
    if (st.status === "pending") {
      show("step-pending", 3);
      pollTimer = setTimeout(refresh, 10000);
      return;
    }
    if (st.status === "rejected") {
      show("step-email", 1);
      return say("This request was not approved. Please contact the photographer.");
    }
    if (st.status === "verified") return startSelfie();
    show("step-gallery", 4);
    const n = st.photos.length;
    $("g-title").textContent = n === 1 ? "1 photo of you" : n + " photos of you";
    $("g-empty").hidden = n > 0;
    $("dl-all").hidden = n === 0;
    const grid = $("grid");
    grid.textContent = "";
    const groups = st.groups || [];
    const named = portal || groups.length > 1;      // several events: one heading each
    if (portal && st.date_from) { $("d-from").value = st.date_from; $("d-to").value = st.date_to; }
    groups.forEach((grp) => {
      if (named) {
        const h = document.createElement("h2");
        h.className = "group";
        h.textContent = grp.name + (grp.date ? " - " + grp.date.split("-").reverse().join("-") : "") +
          " (" + grp.photos.length + ")";
        grid.append(h);
      }
      grp.photos.forEach(addPhoto);
    });
    function addPhoto(id) {
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.loading = "lazy";
      img.alt = "Preview of your photo";
      img.src = "/api/preview/" + id;
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = "Download original";
      b.addEventListener("click", () => download(id, b));
      fig.append(img, b);
      grid.append(fig);
    }
  }

  async function download(what, btn) {
    busy(btn, true); say("");
    const r = await api("/api/download-link/" + what, {});
    busy(btn, false);
    if (!r.ok) {
      if (r.status_code === 401) show("step-email", 1);
      return say(r.error || "Could not start the download.");
    }
    window.location.assign(r.url);
  }
  $("dl-all").addEventListener("click", (e) => download("all", e.target));
  $("retry").addEventListener("click", startSelfie);
  $("forget").addEventListener("click", async () => {
    await api("/api/forget", {});
    show("step-email", 1);
    say("You are signed out and your matches were erased.");
  });

  async function refresh() {
    const st = await api("/api/me");
    if (st.ok && st.event_code === code) { email = st.email; render(st); }
    else show("step-email", 1);
  }
  refresh();
})();
