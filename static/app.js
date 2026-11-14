(() => {
  const tb = document.getElementById("gtheme");
  if (tb) tb.addEventListener("click", () => {
    const dark = document.documentElement.dataset.theme !== "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    try { localStorage.setItem("ff_theme", dark ? "dark" : "light"); } catch (e) { /* private mode */ }
  });
  const app = document.getElementById("app");
  if (!app) return;
  const code = app.dataset.code, portal = app.dataset.portal === "1", lookFirst = app.dataset.look === "1";
  if (!code && !portal) return;
  const $ = (id) => document.getElementById(id);
  const views = ["home", "signin", "otp", "events", "selfie", "pending", "gallery"];
  const start = lookFirst ? "home" : "signin";
  let me = null, email = "", stream = null, pollTimer = null, chosen = null, last = null, current = "";
  let afterSignin = null;                 // what the guest wanted when we asked them to sign in
  const saved = new Set(), picked = new Set();
  const store = {                         // remembers name / phone / email on this device
    get(k) { try { return localStorage.getItem("ff_" + k) || ""; } catch (e) { return ""; } },
    set(k, v) { try { localStorage.setItem("ff_" + k, v); } catch (e) { /* private mode */ } },
  };

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
    function play() {            // next events turn by themselves; hovering or reduced motion stops it
      clearInterval(timer);
      if (calm || cards.length < 2) return;
      timer = setInterval(() => { if (!document.hidden) step(1); }, 3500);
    }
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
      const j = await r.json().catch(() => ({ ok: false, error: "Something went wrong. Please try again." }));
      j.status_code = r.status;
      return j;
    } catch (e) {
      return { ok: false, error: "No connection. Check the internet and try again." };
    }
  }

  function say(text) {
    const m = $("msg");
    m.textContent = text || "";
    m.hidden = !text;
    if (text) m.scrollIntoView({ block: "nearest" });
  }
  function busy(btn, on) {
    btn.disabled = on;
    btn.classList.toggle("working", on);
  }
  const niceDate = (d) => (d ? d.split("-").reverse().join("-") : "");

  // ---- moving between screens; the browser's Back button works too
  function show(view, push) {
    views.forEach((v) => { $("v-" + v).hidden = v !== view; });
    if (view !== "selfie") stopCam();
    clearTimeout(pollTimer);
    const inside = ["events", "selfie", "pending", "gallery"].includes(view);
    document.body.classList.toggle("in", inside);
    $("after").hidden = !inside;
    const stepOf = { home: 1, signin: 1, otp: 1, events: 2, selfie: 3, pending: 3, gallery: 4 }[view] || 1;
    document.body.dataset.step = stepOf;
    document.querySelectorAll("#steps li").forEach((li) => {
      const n = Number(li.dataset.s);
      li.classList.toggle("on", n === stepOf); li.classList.toggle("done", n < stepOf);
    });
    if (view !== "gallery") { picked.clear(); paintTray(); }
    if (push !== false && view !== current) history.pushState({ v: view }, "");
    current = view;
    window.scrollTo(0, 0);
  }
  function landing() { return me ? (me.status === "verified" ? (portal ? "events" : "selfie") : "gallery") : start; }
  window.addEventListener("popstate", (e) => {
    if (viewer && !viewer.hidden) { closeViewer(true); return; }
    let v = (e.state && e.state.v) || landing();
    say("");
    // never land on a screen that cannot work in the current state
    if (["events", "selfie", "gallery", "pending"].includes(v) && !me) v = start;
    if (v === "otp" && !email) v = "signin";
    if (v === "events" && !portal) v = "selfie";
    if (["home", "signin", "otp"].includes(v) && me && !me.looking) v = landing();
    if (v === "home" && me) v = landing();
    if (v === "gallery") { if (last) gallery(last.groups || [], "found", false); else v = landing(); }
    if (v === "selfie") return startSelfie(false);
    if (v === "events") return showEvents(false);
    if (v !== "gallery") show(v, false);
  });
  document.querySelectorAll("[data-back]").forEach((b) => b.addEventListener("click", () => history.back()));

  // ---- first screen: look without a code, or sign in
  $("look").addEventListener("click", async (e) => {
    if (!$("consent").checked) return say("Please tick the box to agree, then continue.");
    busy(e.target, true); say("");
    const r = await api("/api/look", { code, consent: true });
    busy(e.target, false);
    if (!r.ok) { say(r.error); if (r.status_code === 404) show("signin"); return; }
    refresh();
  });
  $("to-signin").addEventListener("click", () => { say(""); afterSignin = null; openSignin(""); });
  function openSignin(why) {
    $("si-why").textContent = why || "We email you a one-time code, so your photos reach only you.";
    $("gname").value = $("gname").value || store.get("name");
    $("phone").value = $("phone").value || store.get("phone");
    $("email").value = $("email").value || store.get("email");
    if ($("consent").checked || (me && me.looking)) $("consent2").checked = true;
    show("signin");
  }

  // ---- sign in: name, phone, email -> code
  async function sendCode(btn) {
    busy(btn, true); say("");
    email = $("email").value.trim();
    const r = await api("/api/otp/send", {
      code, email, name: $("gname").value.trim(), phone: $("phone").value.trim(),
      consent: $("consent2").checked });
    busy(btn, false);
    if (!r.ok) { say(r.error); return false; }
    store.set("name", $("gname").value.trim()); store.set("phone", $("phone").value.trim()); store.set("email", email);
    return true;
  }
  $("f-email").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!(await sendCode(e.target.querySelector("button[type=submit]")))) return;
    $("otp-email").textContent = email;
    $("otp").value = "";
    show("otp");
    $("otp").focus();
    holdResend();
  });
  let resendTimer = null;
  function holdResend() {                  // a new code can be asked for after 30 seconds
    const b = $("resend");
    let left = 30;
    clearInterval(resendTimer);
    b.disabled = true;
    const tick = () => {
      b.textContent = left > 0 ? "Send a new code (in " + left + " s)" : "Send a new code";
      if (left-- <= 0) { clearInterval(resendTimer); b.disabled = false; }
    };
    tick(); resendTimer = setInterval(tick, 1000);
  }
  $("resend").addEventListener("click", async (e) => {
    if (await sendCode(e.target)) { say(""); $("otp").value = ""; $("otp").focus(); holdResend(); }
  });
  async function verify() {
    const btn = $("f-otp").querySelector("button[type=submit]");
    if (btn.disabled) return;
    busy(btn, true); say("");
    const r = await api("/api/otp/verify", { code, email, otp: $("otp").value.trim() });
    busy(btn, false);
    if (!r.ok) { $("otp").value = ""; $("otp").focus(); return say(r.error); }
    await refresh();
    const todo = afterSignin; afterSignin = null;
    if (todo) todo();                      // carry on with the download / save they asked for
  }
  $("f-otp").addEventListener("submit", (e) => { e.preventDefault(); verify(); });
  $("otp").addEventListener("input", (e) => {
    e.target.value = e.target.value.replace(/\D/g, "").slice(0, 6);
    if (e.target.value.length === 6) verify();      // no need to press the button
  });

  // downloading and saving need a verified email
  function needSignin(then) {
    afterSignin = then;
    openSignin("Sign in once to download or save. Your photos stay on the screen.");
  }

  // ---- general link: dates, then events
  function showEvents(push) {
    show("events", push);
    if (me && me.name) $("ev-hello").textContent = "Hello " + me.name + " - choose your events";
    $("ev-saved").hidden = saved.size === 0;
    $("ev-saved").textContent = "My saved photos (" + saved.size + ")";
  }
  async function loadEvents() {
    say(""); busy($("ev-load"), true);
    const r = await api("/api/events?from=" + encodeURIComponent($("d-from").value) +
                        "&to=" + encodeURIComponent($("d-to").value));
    busy($("ev-load"), false);
    if (!r.ok) {
      if (r.status_code === 401) { me = null; show(start); }
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
    const many = r.events.length > 8;
    $("ev-filter").hidden = !many; $("ev-filter-label").hidden = !many; $("ev-filter").value = "";
    $("ev-next").hidden = r.events.length === 0;
    if (!r.events.length) say("There is no event in these dates. Change the dates, or ask the photographer.");
  }
  if (portal) {
    $("ev-load").addEventListener("click", loadEvents);
    const reset = () => { $("ev-box").hidden = true; $("ev-next").hidden = true; };
    $("d-from").addEventListener("change", reset);
    $("d-to").addEventListener("change", reset);
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

  // ---- selfie
  async function startSelfie(push) {
    show("selfie", push);
    $("cam").classList.remove("scanning");
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "user", width: { ideal: 1280 } }, audio: false });
      $("video").srcObject = stream;
    } catch (e) {
      say("The camera could not be opened. Allow camera access, or upload a photo of your face below.");
    }
  }
  function stopCam() {
    if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
  }
  async function sendFace(source, w, h) {
    const c = $("canvas");
    const scale = Math.min(1, 1280 / Math.max(w, h));
    c.width = Math.round(w * scale);
    c.height = Math.round(h * scale);
    c.getContext("2d").drawImage(source, 0, 0, c.width, c.height);
    busy($("snap"), true); say("");
    $("video").pause();                                 // the picture stands still while we search
    $("cam").classList.add("scanning");
    $("snap").textContent = "Looking for your photos ...";
    const body = { image: c.toDataURL("image/jpeg", 0.92) };
    if (portal) { body.date_from = $("d-from").value; body.date_to = $("d-to").value; body.events = chosen; }
    const r = await api("/api/selfie", body);
    busy($("snap"), false);
    $("snap").textContent = "Take selfie and find my photos";
    $("cam").classList.remove("scanning");
    if (!r.ok) {
      if (r.status_code === 401) { me = null; show(start); return say(r.error); }
      if (stream) $("video").play();
      return say(r.error + " You can try again.");
    }
    route(r);
  }
  $("snap").addEventListener("click", () => {
    const v = $("video");
    if (!stream || !v.videoWidth) return say("The camera is not ready yet. You can upload a photo instead.");
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

  // ---- where to go for the state the server reports
  function route(st, push) {
    me = st;
    saved.clear();
    (st.saved || []).forEach((id) => saved.add(id));
    if (st.status === "pending") {
      show("pending", push);
      pollTimer = setTimeout(refresh, 10000);
      return;
    }
    if (st.status === "rejected") {
      show(start, push);
      return say("This request was not approved. Please contact the photographer.");
    }
    if (st.status === "verified") return portal ? showEvents(push) : startSelfie(push);
    last = st;
    gallery(st.groups || [], "found", push);
  }

  // ---- photos (found by the selfie, or saved earlier). Tiles have a fixed
  // shape, so nothing jumps while pictures load.
  function gallery(groups, mode, push) {
    show("gallery", push);
    const n = groups.reduce((a, grp) => a + grp.photos.length, 0);
    $("g-title").textContent = mode === "saved"
      ? (n === 1 ? "1 saved photo" : n + " saved photos")
      : (n === 1 ? "1 photo of you" : n + " photos of you");
    $("g-empty").hidden = n > 0;
    $("g-empty").textContent = mode === "saved"
      ? "You have not saved any photo yet. Press Save under a photo to keep it for your next visit."
      : "No photos matched this selfie. Press New search and try a clearer selfie in good light, or check that you chose the right events.";
    $("dl-all").hidden = n === 0;
    $("dl-all").textContent = mode === "saved" ? "Download all saved" : "Download all";
    $("dl-all").dataset.what = mode === "saved" ? "saved" : "all";
    $("show-saved").hidden = mode === "saved" || (me && me.looking);
    $("show-saved").textContent = "My saved photos (" + saved.size + ")";
    $("show-found").hidden = !(mode === "saved" && last);
    $("g-hint").hidden = n === 0;
    $("g-signin").hidden = !(me && me.looking);
    $("forget").textContent = me && me.looking ? "Leave" : "Sign out";
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
    fig.dataset.id = id;
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = "Preview of your photo";
    img.decoding = "async";
    img.src = "/api/preview/" + id + "?s=t";            // small copy for the tile; the viewer loads the large one
    img.addEventListener("load", () => fig.classList.add("ready"), { once: true });
    img.addEventListener("error", () => { img.alt = "This photo cannot be shown right now"; fig.classList.add("ready"); }, { once: true });
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
    keep.type = "button"; keep.className = "ghost keep";
    const paint = () => {
      keep.textContent = saved.has(id) ? "Saved" : "Save";
      keep.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    };
    paint();
    keep.addEventListener("click", () => toggleSave(id, keep, paint));
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = "Download";
    b.addEventListener("click", () => download(id, b));
    row.append(keep, b);
    fig.append(img, pick, row);
    return fig;
  }
  async function toggleSave(id, btn, paint) {
    if (me && me.looking) return needSignin(() => toggleSave(id, btn, paint));
    busy(btn, true);
    const r = await api("/api/bookmark/" + id, { on: !saved.has(id) });
    busy(btn, false);
    if (!r.ok) return say(r.error || "Could not save this photo.");
    if (r.on) saved.add(id); else saved.delete(id);
    const card = $("grid").querySelector('figure[data-id="' + id + '"] .keep');
    if (card) { card.textContent = r.on ? "Saved" : "Save"; card.setAttribute("aria-pressed", r.on ? "true" : "false"); }
    if (paint) paint();
    $("show-saved").textContent = "My saved photos (" + saved.size + ")";
    if (viewer && !viewer.hidden) paintViewer();
  }

  // ---- one photo, full page: arrows / swipe to move, Esc or Back to close
  let viewer = null, viewing = 0, shown = [];
  function openViewer(id) {
    shown = [...$("grid").querySelectorAll("figure")].map((f) => Number(f.dataset.id));
    viewing = Math.max(0, shown.indexOf(id));
    if (!viewer) buildViewer();
    viewer.hidden = false;
    document.body.classList.add("viewing");
    history.pushState({ v: "gallery", viewer: true }, "");
    paintViewer();
    viewer.querySelector(".v-close").focus();
  }
  function closeViewer(fromBack) {
    viewer.hidden = true;
    document.body.classList.remove("viewing");
    const fig = $("grid").querySelector('figure[data-id="' + shown[viewing] + '"] img');
    if (fig) fig.focus();
    if (!fromBack) history.back();
  }
  function move(n) { if (shown.length) { viewing = (viewing + n + shown.length) % shown.length; paintViewer(); } }
  function paintViewer() {
    const id = shown[viewing];
    const big = viewer.querySelector("img");
    big.src = "/api/preview/" + id + "?s=t";            // shows at once, then sharpens
    const sharp = new Image();
    sharp.onload = () => { if (shown[viewing] === id) big.src = sharp.src; };
    sharp.src = "/api/preview/" + id;
    if (shown.length > 1) { new Image().src = "/api/preview/" + shown[(viewing + 1) % shown.length]; }   // next one, ahead of time
    viewer.querySelector(".v-count").textContent = (viewing + 1) + " of " + shown.length;
    const keep = viewer.querySelector(".v-save"), tick = viewer.querySelector(".v-pick");
    keep.textContent = saved.has(id) ? "Saved" : "Save";
    keep.setAttribute("aria-pressed", saved.has(id) ? "true" : "false");
    tick.textContent = picked.has(id) ? "Selected" : "Select";
    tick.setAttribute("aria-pressed", picked.has(id) ? "true" : "false");
    viewer.querySelectorAll(".nav").forEach((b) => { b.hidden = shown.length < 2; });
  }
  function buildViewer() {
    const el = (tag, cls, text) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text) x.textContent = text; x.type = tag === "button" ? "button" : undefined; return x; };
    viewer = el("div", "viewer");
    viewer.setAttribute("role", "dialog"); viewer.setAttribute("aria-modal", "true"); viewer.setAttribute("aria-label", "Photo");
    const top = el("div", "v-top"), stage = el("div", "v-stage"), bottom = el("div", "v-bottom");
    const close = el("button", "ghost small v-close", "Close");
    top.append(el("span", "v-count"), close);
    const img = el("img"); img.alt = "Your photo, large";
    const prev = el("button", "ghost nav prev", "‹"), next = el("button", "ghost nav next", "›");
    prev.setAttribute("aria-label", "Previous photo"); next.setAttribute("aria-label", "Next photo");
    stage.append(prev, img, next);
    const wrong = el("button", "ghost v-wrong", "This is not me"), tick = el("button", "ghost v-pick"), keep = el("button", "ghost v-save"), dl = el("button", "", "Download");
    bottom.append(wrong, tick, keep, dl);
    viewer.append(top, stage, bottom);
    document.body.append(viewer);
    close.addEventListener("click", () => closeViewer());
    prev.addEventListener("click", () => move(-1)); next.addEventListener("click", () => move(1));
    stage.addEventListener("click", (e) => { if (e.target === stage) closeViewer(); });
    dl.addEventListener("click", () => { const id = shown[viewing]; if (me && me.looking) closeViewer(); download(id, dl); });
    keep.addEventListener("click", () => { const id = shown[viewing]; if (me && me.looking) closeViewer(); toggleSave(id, keep); });
    tick.addEventListener("click", () => { $("grid").querySelector('figure[data-id="' + shown[viewing] + '"] .pick').click(); paintViewer(); });
    wrong.addEventListener("click", async () => {
      const id = shown[viewing];
      busy(wrong, true);
      const r = await api("/api/not-me/" + id, {});
      busy(wrong, false);
      if (!r.ok) return say(r.error || "Could not remove this photo.");
      const card = $("grid").querySelector('figure[data-id="' + id + '"]');
      if (card) card.remove();
      picked.delete(id); saved.delete(id); paintTray();
      if (last) last.groups.forEach((g) => { g.photos = g.photos.filter((p) => p !== id); });
      shown.splice(viewing, 1);
      const left = $("grid").querySelectorAll("figure").length;
      $("g-title").textContent = left === 1 ? "1 photo of you" : left + " photos of you";
      if (!shown.length) return closeViewer();
      viewing = viewing % shown.length;
      paintViewer();
    });
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
  $("tray-dl").addEventListener("click", (e) => download("selected", e.target));

  async function showSaved() {
    say("");
    const r = await api("/api/bookmarks");
    if (!r.ok) { me = null; show(start); return say(r.error); }
    gallery(r.groups, "saved");
  }

  async function download(what, btn) {
    if (me && me.looking) return needSignin(() => download(what, btn));
    busy(btn, true); say("");
    const r = await api("/api/download-link/" + what, what === "selected" ? { ids: [...picked] } : {});
    busy(btn, false);
    if (!r.ok) {
      if (r.signin) return needSignin(() => download(what, btn));
      if (r.status_code === 401) { me = null; show(start); }
      return say(r.error || "The download could not start. Please try again.");
    }
    window.location.assign(r.url);
  }
  $("dl-all").addEventListener("click", (e) => download(e.target.dataset.what || "all", e.target));
  $("show-saved").addEventListener("click", showSaved);
  $("show-found").addEventListener("click", () => gallery(last.groups || [], "found"));
  $("g-signin-btn").addEventListener("click", () => needSignin(null));
  $("retry").addEventListener("click", () => { say(""); if (portal) showEvents(); else startSelfie(); });
  $("forget").addEventListener("click", async () => {
    const was = me && me.looking;
    await api("/api/forget", {});
    me = null; last = null; saved.clear();
    show(start);
    say(was ? "" : "You are signed out. Your saved photos are kept for your next visit.");
  });

  async function refresh() {
    const st = await api("/api/me");
    if (st.ok && st.event_code === code) { email = st.email || email; route(st, current ? undefined : false); }
    else { me = null; show(start, false); }
  }
  history.replaceState({ v: start }, "");
  refresh();
})();
