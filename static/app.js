(() => {
  const app = document.getElementById("app");
  if (!app) return;
  const code = app.dataset.code, portal = app.dataset.portal === "1";
  if (!code && !portal) return;
  const $ = (id) => document.getElementById(id);
  const sections = ["step-email", "step-otp", "step-events", "step-selfie", "step-pending", "step-gallery"];
  let email = "", stream = null, pollTimer = null, chosen = null, last = null;
  const saved = new Set(), picked = new Set();

  const calm = window.matchMedia("(prefers-reduced-motion:reduce)").matches;
  const hover = window.matchMedia("(hover:hover)").matches;

  // ---- the sign-in card leans towards the pointer and catches the light
  if (hover && !calm) {
    document.querySelectorAll(".tilt").forEach((card) => {
      card.addEventListener("pointermove", (e) => {
        const r = card.getBoundingClientRect();
        const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
        card.style.setProperty("--ry", ((x - 0.5) * 7).toFixed(2) + "deg");
        card.style.setProperty("--rx", ((0.5 - y) * 6).toFixed(2) + "deg");
        card.style.setProperty("--mx", (x * 100).toFixed(1) + "%");
        card.style.setProperty("--my", (y * 100).toFixed(1) + "%");
      });
      card.addEventListener("pointerleave", () => {
        card.style.setProperty("--rx", "0deg"); card.style.setProperty("--ry", "0deg");
      });
    });
  }

  // ---- offers and next events stand in a 3D row; one is in front
  document.querySelectorAll(".flow").forEach((flow) => {
    const cards = [...flow.querySelectorAll(".promo")];
    let front = 0, timer = null;
    function place() {
      cards.forEach((c, i) => {
        let pos = i - front;
        if (pos > cards.length / 2) pos -= cards.length;
        if (pos < -cards.length / 2) pos += cards.length;
        if (cards.length === 2 && pos === -1) pos = 1;
        c.dataset.pos = pos;
        c.setAttribute("aria-hidden", pos === 0 ? "false" : "true");
        c.tabIndex = pos === 0 && c.tagName === "A" ? 0 : -1;
      });
    }
    function step(n) { front = (front + n + cards.length) % cards.length; place(); }
    function play() { if (!calm && cards.length > 1) timer = setInterval(() => step(1), 5000); }
    cards.forEach((c, i) => c.addEventListener("click", (e) => {
      if (i !== front) { e.preventDefault(); front = i; place(); }      // a side card comes to the front first
    }));
    const nav = document.querySelector('.flow-nav[data-for="' + flow.id + '"]');
    if (nav) nav.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      clearInterval(timer); step(Number(b.dataset.step));
    }));
    flow.addEventListener("pointerenter", () => clearInterval(timer));
    flow.addEventListener("pointerleave", () => { clearInterval(timer); play(); });
    place(); play();
  });

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
    if (text) m.scrollIntoView({ block: "nearest" });
  }

  function show(id, stepNo) {
    sections.forEach((s) => { const el = $(s); if (el) el.hidden = s !== id; });
    [1, 2, 3, 4].forEach((n) => $("s" + n).classList.toggle("on", n <= stepNo));
    if (id !== "step-selfie") stopCam();
    clearTimeout(pollTimer);
    // signed in: the big sign-in stage folds away, offers appear under the content
    const inside = stepNo >= 3;
    document.body.classList.toggle("in", inside);
    $("after").hidden = !inside;
    if (id !== "step-gallery") { picked.clear(); paintTray(); }
  }

  function busy(btn, on) { btn.disabled = on; }
  const niceDate = (d) => (d ? d.split("-").reverse().join("-") : "");

  // ---- step 1: name, phone, email
  $("f-email").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    busy(btn, true); say("");
    email = $("email").value.trim();
    const r = await api("/api/otp/send", {
      code, email, name: $("gname").value.trim(), phone: $("phone").value.trim(),
      consent: $("consent").checked });
    busy(btn, false);
    if (!r.ok) return say(r.error);
    $("otp-email").textContent = email;
    $("otp").value = "";
    show("step-otp", 2);
    $("otp").focus();
  });

  // ---- step 2: code from the email
  $("f-otp").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector("button");
    busy(btn, true); say("");
    const r = await api("/api/otp/verify", { code, email, otp: $("otp").value.trim() });
    busy(btn, false);
    if (!r.ok) return say(r.error);
    refresh();
  });
  $("otp-back").addEventListener("click", () => { say(""); show("step-email", 1); });

  // ---- step 3a (general link): dates, then events
  function showEvents(st) {
    show("step-events", 3);
    if (st && st.name) $("ev-hello").textContent = "Hello " + st.name + " - choose your events";
    $("ev-saved").hidden = saved.size === 0;
    $("ev-saved").textContent = "My saved photos (" + saved.size + ")";
  }
  async function loadEvents() {
    say("");
    const r = await api("/api/events?from=" + encodeURIComponent($("d-from").value) +
                        "&to=" + encodeURIComponent($("d-to").value));
    if (!r.ok) {
      if (r.status_code === 401) show("step-email", 1);
      return say(r.error);
    }
    const list = $("ev-list");
    list.textContent = "";
    r.events.forEach((ev) => {
      const label = document.createElement("label");
      label.className = "check";
      const box = document.createElement("input");
      box.type = "checkbox"; box.checked = true; box.value = ev.id;
      const span = document.createElement("span");
      span.textContent = ev.name + " - " + niceDate(ev.date);
      label.append(box, span);
      list.append(label);
    });
    $("ev-box").hidden = r.events.length === 0;
    $("ev-count").textContent = r.events.length + (r.events.length === 1 ? " event" : " events") + " in these dates";
    const many = r.events.length > 8;          // long lists get a search box
    $("ev-filter").hidden = !many; $("ev-filter-label").hidden = !many; $("ev-filter").value = "";
    $("ev-next").hidden = r.events.length === 0;
    if (!r.events.length) say("There is no event in these dates. Please check the dates, or ask the photographer.");
  }
  if (portal) {
    $("ev-load").addEventListener("click", loadEvents);
    $("d-from").addEventListener("change", () => { $("ev-box").hidden = true; $("ev-next").hidden = true; });
    $("d-to").addEventListener("change", () => { $("ev-box").hidden = true; $("ev-next").hidden = true; });
    $("ev-next").addEventListener("click", () => {
      chosen = [...$("ev-list").querySelectorAll("label:not([hidden]) input:checked")].map((b) => Number(b.value));
      if (!chosen.length) return say("Please tick at least one event.");
      say("");
      startSelfie();
    });
    $("ev-saved").addEventListener("click", showSaved);
    $("ev-filter").addEventListener("input", (e) => {
      const q = e.target.value.trim().toLowerCase();
      $("ev-list").querySelectorAll("label").forEach((l) => { l.hidden = !!q && !l.textContent.toLowerCase().includes(q); });
    });
  }

  // ---- step 3b: selfie
  async function startSelfie() {
    show("step-selfie", 3);
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 1280 } }, audio: false });
      $("video").srcObject = stream;
    } catch (e) {
      say("Please allow camera access to take your selfie, or upload a photo of your face below.");
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
    if (portal) { body.date_from = $("d-from").value; body.date_to = $("d-to").value; body.events = chosen; }
    const r = await api("/api/selfie", body);
    busy($("snap"), false);
    if (!r.ok) {
      if (r.status_code === 401) return show("step-email", 1), say(r.error);
      return say(r.error);
    }
    say("");
    render(r, true);
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

  // ---- step 4: photos (found by the selfie, or saved earlier)
  function render(st, fresh) {
    saved.clear();
    (st.saved || []).forEach((id) => saved.add(id));
    if (st.status === "pending") {
      show("step-pending", 3);
      pollTimer = setTimeout(refresh, 10000);
      return;
    }
    if (st.status === "rejected") {
      show("step-email", 1);
      return say("This request was not approved. Please contact the photographer.");
    }
    if (st.status === "verified") return portal ? showEvents(st) : startSelfie();
    last = st;
    gallery(st.groups || [], "found");
  }

  function gallery(groups, mode) {
    show("step-gallery", 4);
    const n = groups.reduce((a, grp) => a + grp.photos.length, 0);
    $("g-title").textContent = mode === "saved"
      ? (n === 1 ? "1 saved photo" : n + " saved photos")
      : (n === 1 ? "1 photo of you" : n + " photos of you");
    $("g-empty").hidden = n > 0;
    $("g-empty").textContent = mode === "saved"
      ? "You have not saved any photo yet. Press Save under a photo to keep it for your next visit."
      : "No photos matched this selfie. Try a clearer selfie in good light, or check that you chose the right events.";
    $("dl-all").hidden = n === 0;
    $("dl-all").textContent = mode === "saved" ? "Download all saved" : "Download all";
    $("dl-all").dataset.what = mode === "saved" ? "saved" : "all";
    $("show-saved").hidden = mode === "saved";
    $("show-saved").textContent = "My saved photos (" + saved.size + ")";
    $("show-found").hidden = !(mode === "saved" && last);
    $("g-hint").hidden = n === 0;
    picked.clear(); paintTray();
    const grid = $("grid");
    grid.textContent = "";
    const named = portal || groups.length > 1 || mode === "saved";
    groups.forEach((grp) => {
      if (named) {
        const h = document.createElement("h2");
        h.className = "group";
        h.textContent = grp.name + (grp.date ? " - " + niceDate(grp.date) : "") + " (" + grp.photos.length + ")";
        grid.append(h);
      }
      grp.photos.forEach((id) => grid.append(photoCard(id)));
    });
  }

  function photoCard(id) {
    const fig = document.createElement("figure");
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "Preview of your photo";
    img.src = "/api/preview/" + id;
    img.addEventListener("error", () => { img.alt = "This photo cannot be shown right now"; }, { once: true });
    img.tabIndex = 0;
    img.setAttribute("role", "button");
    img.setAttribute("aria-label", "Open this photo full page");
    img.addEventListener("click", () => openViewer(id));
    img.addEventListener("keydown", (e) => { if (e.key === "Enter") openViewer(id); });
    const pick = document.createElement("input");
    pick.type = "checkbox"; pick.className = "pick";
    pick.setAttribute("aria-label", "Select this photo for download");
    pick.addEventListener("change", () => {
      if (pick.checked) picked.add(id); else picked.delete(id);
      fig.classList.toggle("picked", pick.checked);
      paintTray();
    });
    const row = document.createElement("div");
    row.className = "acts";
    const keep = document.createElement("button");
    keep.type = "button"; keep.className = "ghost";
    const paint = () => {
      keep.textContent = saved.has(id) ? "Saved" : "Save";
      keep.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    };
    paint();
    keep.addEventListener("click", async () => {
      busy(keep, true);
      const r = await api("/api/bookmark/" + id, { on: !saved.has(id) });
      busy(keep, false);
      if (!r.ok) return say(r.error || "Could not save this photo.");
      if (r.on) saved.add(id); else saved.delete(id);
      paint();
      $("show-saved").textContent = "My saved photos (" + saved.size + ")";
    });
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "Download";
    b.addEventListener("click", () => download(id, b));
    row.append(keep, b);
    fig.dataset.id = id;
    fig.append(img, pick, row);
    return fig;
  }

  // ---- one photo, full page: arrows / swipe to move, Esc to close
  let viewer = null, viewing = 0, shown = [];
  function openViewer(id) {
    shown = [...$("grid").querySelectorAll("figure")].map((f) => Number(f.dataset.id));
    viewing = Math.max(0, shown.indexOf(id));
    if (!viewer) buildViewer();
    viewer.hidden = false;
    document.body.classList.add("viewing");
    paintViewer();
    viewer.querySelector(".v-close").focus();
  }
  function closeViewer() {
    viewer.hidden = true;
    document.body.classList.remove("viewing");
    const fig = $("grid").querySelector('figure[data-id="' + shown[viewing] + '"] img');
    if (fig) fig.focus();
  }
  function move(n) { viewing = (viewing + n + shown.length) % shown.length; paintViewer(); }
  function paintViewer() {
    const id = shown[viewing];
    viewer.querySelector("img").src = "/api/preview/" + id;
    viewer.querySelector(".v-count").textContent = (viewing + 1) + " of " + shown.length;
    const keep = viewer.querySelector(".v-save"), tick = viewer.querySelector(".v-pick");
    keep.textContent = saved.has(id) ? "Saved" : "Save";
    keep.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    tick.textContent = picked.has(id) ? "Selected" : "Select";
    tick.setAttribute("aria-pressed", picked.has(id) ? "true" : "false");
    viewer.querySelectorAll(".nav").forEach((b) => { b.hidden = shown.length < 2; });
  }
  function buildViewer() {
    const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text) x.textContent = text; return x; };
    viewer = el("div", "viewer");
    viewer.setAttribute("role", "dialog"); viewer.setAttribute("aria-modal", "true"); viewer.setAttribute("aria-label", "Photo");
    const top = el("div", "v-top"), stage = el("div", "v-stage"), bottom = el("div", "v-bottom");
    const close = el("button", "ghost small v-close", "Close"); close.type = "button";
    top.append(el("span", "v-count"), close);
    const img = el("img"); img.alt = "Your photo, large";
    const prev = el("button", "ghost nav prev", "\u2039"), next = el("button", "ghost nav next", "\u203A");
    prev.type = next.type = "button"; prev.setAttribute("aria-label", "Previous photo"); next.setAttribute("aria-label", "Next photo");
    stage.append(prev, img, next);
    const tick = el("button", "ghost v-pick"), keep = el("button", "ghost v-save"), dl = el("button", "", "Download");
    tick.type = keep.type = dl.type = "button";
    bottom.append(tick, keep, dl);
    viewer.append(top, stage, bottom);
    document.body.append(viewer);
    close.addEventListener("click", closeViewer);
    prev.addEventListener("click", () => move(-1)); next.addEventListener("click", () => move(1));
    stage.addEventListener("click", (e) => { if (e.target === stage) closeViewer(); });
    dl.addEventListener("click", () => download(shown[viewing], dl));
    const cardOf = (id) => $("grid").querySelector('figure[data-id="' + id + '"]');
    keep.addEventListener("click", () => { cardOf(shown[viewing]).querySelector(".acts button").click(); setTimeout(paintViewer, 400); });
    tick.addEventListener("click", () => { cardOf(shown[viewing]).querySelector(".pick").click(); paintViewer(); });
    document.addEventListener("keydown", (e) => {
      if (!viewer || viewer.hidden) return;
      if (e.key === "Escape") closeViewer();
      if (e.key === "ArrowLeft") move(-1);
      if (e.key === "ArrowRight") move(1);
    });
    let startX = null;
    stage.addEventListener("touchstart", (e) => { startX = e.touches[0].clientX; }, { passive: true });
    stage.addEventListener("touchend", (e) => {
      if (startX === null) return;
      const dx = e.changedTouches[0].clientX - startX; startX = null;
      if (Math.abs(dx) > 50) move(dx < 0 ? 1 : -1);
    });
  }

  // ---- several photos ticked: one download
  function paintTray() {
    $("tray").hidden = picked.size === 0;
    $("tray-count").textContent = picked.size + (picked.size === 1 ? " photo selected" : " photos selected");
  }
  $("tray-clear").addEventListener("click", () => {
    picked.clear();
    $("grid").querySelectorAll(".pick").forEach((b) => { b.checked = false; });
    $("grid").querySelectorAll("figure.picked").forEach((f) => f.classList.remove("picked"));
    paintTray();
  });
  $("tray-dl").addEventListener("click", async (e) => {
    busy(e.target, true); say("");
    const r = await api("/api/download-link/selected", { ids: [...picked] });
    busy(e.target, false);
    if (!r.ok) return say(r.error || "Could not start the download. Please sign in again.");
    window.location.assign(r.url);
  });

  async function showSaved() {
    say("");
    const r = await api("/api/bookmarks");
    if (!r.ok) { show("step-email", 1); return say(r.error); }
    gallery(r.groups, "saved");
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
  $("dl-all").addEventListener("click", (e) => download(e.target.dataset.what || "all", e.target));
  $("show-saved").addEventListener("click", showSaved);
  $("show-found").addEventListener("click", () => gallery(last.groups || [], "found"));
  $("retry").addEventListener("click", () => { say(""); if (portal) showEvents(last); else startSelfie(); });
  $("forget").addEventListener("click", async () => {
    await api("/api/forget", {});
    last = null; saved.clear();
    show("step-email", 1);
    say("You are signed out. Your saved photos are kept for your next visit.");
  });

  async function refresh() {
    const st = await api("/api/me");
    if (st.ok && st.event_code === code) { email = st.email; render(st); }
    else show("step-email", 1);
  }
  refresh();
})();
