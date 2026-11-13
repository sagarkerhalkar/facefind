(() => {
  const app = document.getElementById("app");
  if (!app) return;
  const code = app.dataset.code, portal = app.dataset.portal === "1";
  if (!code && !portal) return;
  const $ = (id) => document.getElementById(id);
  const sections = ["step-email", "step-otp", "step-events", "step-selfie", "step-pending", "step-gallery"];
  let email = "", stream = null, pollTimer = null, chosen = null, last = null;
  const saved = new Set(), picked = new Set();

  const maybe = new Set();
  let order = [], at = -1, mode = "found";      // photos of the page, and the one shown full page

  // depth: the sign-in card leans a little towards the pointer
  const calm = !window.matchMedia("(hover:hover) and (prefers-reduced-motion:no-preference)").matches;
  if (!calm) {
    document.addEventListener("pointermove", (e) => {
      if (document.body.classList.contains("in")) return;
      app.style.setProperty("--tx", (e.clientX / window.innerWidth - 0.5).toFixed(3));
      app.style.setProperty("--ty", (e.clientY / window.innerHeight - 0.5).toFixed(3));
    }, { passive: true });
  }
  // depth: in a row of event / offer cards, each card turns towards the middle
  const reels = [];
  document.querySelectorAll(".reel").forEach((reel) => {
    const turn = () => {
      if (!reel.clientWidth) return;            // not on screen yet
      const mid = reel.getBoundingClientRect().left + reel.clientWidth / 2;
      reel.querySelectorAll(".promo").forEach((card) => {
        const r = card.getBoundingClientRect();
        const off = Math.max(-1, Math.min(1, (r.left + r.width / 2 - mid) / reel.clientWidth));
        card.style.setProperty("--turn", calm ? 0 : off.toFixed(3));
        card.style.setProperty("--near", calm ? 1 : (1 - Math.abs(off)).toFixed(3));
      });
    };
    reel.addEventListener("scroll", turn, { passive: true });
    window.addEventListener("resize", turn);
    reels.push(turn);
    turn();
  });

  const ICON = {
    heart: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.600-7 10-7 10z"/></svg>',
    down: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4v11m0 0l-4.500-4.500M12 15l4.500-4.500M5 20h14"/></svg>',
    close: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    prev: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>',
    next: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5l7 7-7 7"/></svg>',
  };

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
    const before = $("before");
    if (before) before.hidden = inside;
    if (id !== "step-gallery") closeViewer();
    reels.forEach((turn) => turn());
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
    maybe.clear();
    (st.maybe || []).forEach((id) => maybe.add(id));
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

  function gallery(groups, which) {
    mode = which;
    show("step-gallery", 4);
    const n = groups.reduce((a, grp) => a + grp.photos.length, 0);
    $("g-title").textContent = mode === "saved"
      ? (n === 1 ? "1 saved photo" : n + " saved photos")
      : (n === 1 ? "1 photo of you" : n + " photos of you");
    $("g-empty").hidden = n > 0;
    $("g-empty").textContent = mode === "saved"
      ? "You have not saved any photo yet. Press the heart on a photo to keep it for your next visit."
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
    order = [];
    const named = portal || groups.length > 1 || mode === "saved";
    const block = (ids) => {
      const box = document.createElement("div");
      box.className = "tiles";
      ids.forEach((id) => { order.push(id); box.append(photoCard(id)); });
      grid.append(box);
    };
    groups.forEach((grp) => {
      if (named) {
        const h = document.createElement("h2");
        h.textContent = grp.name + (grp.date ? " - " + niceDate(grp.date) : "") + " (" + grp.photos.length + ")";
        grid.append(h);
      }
      // sure matches first; the ones close to the limit apart, so nothing is skipped
      const sure = mode === "saved" ? grp.photos : grp.photos.filter((id) => !maybe.has(id));
      const unsure = mode === "saved" ? [] : grp.photos.filter((id) => maybe.has(id));
      if (sure.length) block(sure);
      if (unsure.length) {
        const h = document.createElement("h3");
        h.textContent = "Possibly you (" + unsure.length + ")";
        const note = document.createElement("p");
        note.className = "hint";
        note.textContent = "These look similar but we are less sure. Open a photo and press \"This is not me\" to remove it.";
        grid.append(h, note);
        block(unsure);
      }
    });
  }

  function iconButton(name, label) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "icon";
    b.setAttribute("aria-label", label);
    b.innerHTML = ICON[name];
    return b;
  }

  async function toggleSave(id) {
    const r = await api("/api/bookmark/" + id, { on: !saved.has(id) });
    if (!r.ok) { say(r.error || "Could not save this photo."); return; }
    if (r.on) saved.add(id); else saved.delete(id);
    $("show-saved").textContent = "My saved photos (" + saved.size + ")";
    const b = document.querySelector('.tile[data-id="' + id + '"] .save');
    if (b) b.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    paintViewer();
  }

  function setPicked(id, on) {
    if (on) picked.add(id); else picked.delete(id);
    const tile = document.querySelector('.tile[data-id="' + id + '"]');
    if (tile) { tile.classList.toggle("picked", on); tile.querySelector(".pick").checked = on; }
    paintTray(); paintViewer();
  }

  function photoCard(id) {
    const fig = document.createElement("figure");
    fig.className = "tile"; fig.dataset.id = id;
    const open = document.createElement("button");
    open.type = "button"; open.className = "open";
    open.setAttribute("aria-label", "Open this photo full page");
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = "";
    img.src = "/api/preview/" + id;
    img.addEventListener("error", () => { img.alt = "This photo cannot be shown right now"; }, { once: true });
    open.append(img);
    open.addEventListener("click", () => openViewer(id));
    const pick = document.createElement("input");
    pick.type = "checkbox"; pick.className = "pick";
    pick.setAttribute("aria-label", "Select this photo for download");
    pick.addEventListener("change", () => setPicked(id, pick.checked));
    const bar = document.createElement("div");
    bar.className = "tile-bar";
    const keep = iconButton("heart", "Save this photo for my next visit");
    keep.classList.add("save");
    keep.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    keep.addEventListener("click", () => toggleSave(id));
    const dl = iconButton("down", "Download this photo");
    dl.addEventListener("click", () => download(id, dl));
    bar.append(keep, dl);
    fig.append(open, pick, bar);
    return fig;
  }

  // ---- one photo, full page: arrows, swipe, Esc
  const viewer = $("viewer");
  $("v-close").innerHTML = ICON.close; $("v-prev").innerHTML = ICON.prev; $("v-next").innerHTML = ICON.next;
  function paintViewer() {
    if (viewer.hidden || at < 0) return;
    const id = order[at];
    $("v-count").textContent = (at + 1) + " of " + order.length;
    $("v-save").textContent = saved.has(id) ? "Saved" : "Save";
    $("v-save").setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    $("v-pick").textContent = picked.has(id) ? "Selected" : "Select";
    $("v-pick").setAttribute("aria-pressed", picked.has(id) ? "true" : "false");
    $("v-notme").hidden = mode === "saved";
    $("v-prev").disabled = at === 0; $("v-next").disabled = at === order.length - 1;
  }
  function openViewer(id) {
    at = order.indexOf(id);
    if (at < 0) return;
    viewer.hidden = false;
    document.body.classList.add("noscroll");
    $("v-img").src = "/api/preview/" + id;
    paintViewer();
    $("v-close").focus();
  }
  function closeViewer() {
    if (!viewer || viewer.hidden) return;
    viewer.hidden = true; at = -1;
    document.body.classList.remove("noscroll");
  }
  function step(by) {
    const to = at + by;
    if (to < 0 || to >= order.length) return;
    at = to;
    const img = $("v-img");
    img.style.animation = "none"; void img.offsetWidth; img.style.animation = "";
    img.src = "/api/preview/" + order[at];
    paintViewer();
  }
  $("v-close").addEventListener("click", closeViewer);
  $("v-prev").addEventListener("click", () => step(-1));
  $("v-next").addEventListener("click", () => step(1));
  $("v-dl").addEventListener("click", (e) => download(order[at], e.target));
  $("v-save").addEventListener("click", () => toggleSave(order[at]));
  $("v-pick").addEventListener("click", () => setPicked(order[at], !picked.has(order[at])));
  $("v-notme").addEventListener("click", async (e) => {
    const id = order[at];
    busy(e.target, true);
    const r = await api("/api/not-me/" + id, {});
    busy(e.target, false);
    if (!r.ok) return;
    closeViewer();
    if (last) {
      last.groups.forEach((grp) => { grp.photos = grp.photos.filter((x) => x !== id); });
      last.groups = last.groups.filter((grp) => grp.photos.length);
      saved.delete(id);
      gallery(last.groups, "found");
    }
  });
  document.addEventListener("keydown", (e) => {
    if (viewer.hidden) return;
    if (e.key === "Escape") closeViewer();
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "ArrowRight") step(1);
  });
  let touchX = null;
  viewer.addEventListener("touchstart", (e) => { touchX = e.touches[0].clientX; }, { passive: true });
  viewer.addEventListener("touchend", (e) => {
    if (touchX === null) return;
    const dx = e.changedTouches[0].clientX - touchX;
    touchX = null;
    if (Math.abs(dx) > 50) step(dx < 0 ? 1 : -1);
  }, { passive: true });

  // ---- several photos ticked: one download
  function paintTray() {
    $("tray").hidden = picked.size === 0;
    $("tray-count").textContent = picked.size + (picked.size === 1 ? " photo selected" : " photos selected");
  }
  $("tray-clear").addEventListener("click", () => {
    [...picked].forEach((id) => setPicked(id, false));
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
