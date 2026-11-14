/* FaceFind admin: page changes without reloads, live album cards, toasts, dialogs. */
(() => {
  "use strict";
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];
  const view = () => $("#view");
  let lastNav = { server: null, total: null, at: Date.now() };

  // ------------------------------------------------------------------ theme
  const setTheme = (t) => {
    document.documentElement.dataset.theme = t;
    try { localStorage.setItem("ff_theme", t); } catch (e) { /* private mode */ }
    $$("#theme use, #theme2 use").forEach((u) => u.setAttribute("href", t === "dark" ? "#i-sun" : "#i-moon"));
  };
  const flip = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  setTheme(document.documentElement.dataset.theme || "light");

  // ------------------------------------------------------------------ top bar and toasts
  const bar = { el: $("#bar"), t: null, p: 0,
    start() { clearTimeout(this.t); this.p = 8; this.el.classList.add("on"); this.el.style.width = "8%";
      this.t = setInterval(() => { this.p += (92 - this.p) * 0.12; this.el.style.width = this.p + "%"; }, 180); },
    done() { clearInterval(this.t); this.el.style.width = "100%"; setTimeout(() => { this.el.classList.remove("on"); this.el.style.width = "0"; }, 260); } };
  function toast(text, kind) {
    if (!text) return;
    const t = document.createElement("div");
    t.className = "toast" + (kind === "bad" ? " bad" : "");
    t.textContent = text;
    $("#toasts").append(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; setTimeout(() => t.remove(), 320); }, kind === "bad" ? 7000 : 4200);
  }
  const bad = (m) => /^(not |could not|wrong|too many|please|that |no )/i.test(m) || /fail|error|damaged|refus/i.test(m);

  function confirmBox(title, text, yes) {
    const d = $("#confirm");
    $("#c-title").textContent = title || "Are you sure?";
    $("#c-text").textContent = text || "";
    $("#c-yes").textContent = yes || "Yes";
    return new Promise((res) => {
      const done = (v) => { d.close(); $("#c-yes").onclick = $("#c-no").onclick = null; res(v); };
      $("#c-yes").onclick = () => done(true);
      $("#c-no").onclick = () => done(false);
      d.addEventListener("cancel", () => done(false), { once: true });
      d.showModal();
    });
  }

  // ------------------------------------------------------------------ page changes without a reload
  async function load(url, opts = {}, mode = "push") {          // mode: push | replace | quiet
    if (mode !== "quiet") bar.start();
    const t0 = performance.now();
    try {
      const r = await fetch(url, { credentials: "same-origin", redirect: "follow", cache: "no-store",
        ...opts, headers: { "X-PJAX": "1", "X-Requested-With": "fetch", ...(opts.headers || {}) } });
      const path = new URL(r.url).pathname;
      const type = r.headers.get("content-type") || "";
      if (path.startsWith("/admin/login") || !type.includes("text/html")) { location.href = r.url; return false; }
      const html = await r.text();
      const st = /dur=(\d+)/.exec(r.headers.get("Server-Timing") || "");
      lastNav = { server: st ? +st[1] : null, total: Math.round(performance.now() - t0), at: Date.now() };
      return swap(html, r.url, mode);
    } catch (e) {
      if (mode !== "quiet") toast("No connection to the app. Check that it is running, then try again.", "bad");
      return false;
    } finally { if (mode !== "quiet") bar.done(); }
  }

  function swap(html, url, mode) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    const u = new URL(url);
    const msg = (doc.querySelector("#flash") || {}).dataset ? doc.querySelector("#flash").dataset.msg : "";
    const hadMsg = u.searchParams.has("msg");
    u.searchParams.delete("msg");
    const clean = u.pathname + (u.search || "");
    const here = location.pathname + location.search;
    const box = view();
    const page = (doc.body.querySelector("[data-page]") || {}).dataset;
    const nodes = [...doc.body.childNodes].filter((n) => !(n.id === "flash"));
    const keepScroll = mode === "quiet" ? window.scrollY : 0;
    const open = mode === "quiet" ? $$("details[open]", box).map((d) => d.closest(".album-card") ? d.closest(".album-card").dataset.eid + "|" + (d.className || "x") : "") : [];
    $$("#flash", box).forEach((n) => n.remove());
    box.replaceChildren(...nodes.filter((n) => n.nodeName !== "TITLE"));
    const title = doc.querySelector("title");
    if (title) document.title = title.textContent;
    if (mode === "push" && clean !== here) history.pushState({}, "", clean);
    else history.replaceState({}, "", clean);
    if (mode !== "quiet") window.scrollTo({ top: 0 });
    else { window.scrollTo(0, keepScroll); reopen(open); }
    $$(".nav").forEach((a) => a.classList.toggle("on", new URL(a.href).pathname === u.pathname ||
      (a.getAttribute("href") === "/admin/guests" && u.pathname.startsWith("/admin/guest"))));
    boot();
    const fresh = $(".album-card.is-new");                      // a new album: bring it into view
    if (fresh && mode !== "quiet") setTimeout(() => fresh.scrollIntoView({ block: "center", behavior: "smooth" }), 120);
    if (msg && mode !== "quiet") {
      toast(msg, bad(msg) ? "bad" : "");
      if (/^Not created/i.test(msg)) reopenNew();
    }
    return true;
  }
  function reopen(list) {
    list.forEach((k) => {
      const [id, cls] = k.split("|");
      const card = $(`.album-card[data-eid="${id}"]`);
      const d = card && (cls === "more" ? $("details.more", card) : null);
      if (d) d.open = true;
    });
  }

  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    if (a.target === "_blank" || a.hasAttribute("download")) return;
    const u = new URL(a.href, location.href);
    if (u.origin !== location.origin || !u.pathname.startsWith("/admin")) return;
    if (/\.csv$|\/logout|\/selfie\/|\/photo\//.test(u.pathname)) return;
    e.preventDefault();
    load(u.pathname + u.search);
  });
  addEventListener("popstate", () => load(location.pathname + location.search, {}, "replace"));
  // hovering a menu link warms the page
  document.addEventListener("pointerover", (e) => {
    const a = e.target.closest(".nav");
    if (a && !a.dataset.warm) { a.dataset.warm = 1; fetch(a.href, { headers: { "X-PJAX": "1" }, credentials: "same-origin" }).catch(() => {}); }
  });

  document.addEventListener("submit", async (e) => {
    const f = e.target;
    if (!(f instanceof HTMLFormElement) || f.hasAttribute("data-plain") || f.dataset.sent) return;
    const action = new URL(f.getAttribute("action") || location.href, location.href);
    if (action.origin !== location.origin || !action.pathname.startsWith("/admin")) return;
    e.preventDefault();
    if (f.dataset.confirm) {
      const ok = await confirmBox(f.dataset.confirm, f.dataset.text, f.dataset.yes);
      if (!ok) return;
    }
    const btns = $$("button[type=submit],button:not([type])", f);
    btns.forEach((b) => { b.disabled = true; });
    const sheet = f.closest("dialog");
    if (f.id === "new-sheet-form" || (sheet && sheet.id === "new-sheet")) rememberNew(f);
    const method = (f.method || "get").toUpperCase();
    try {
      if (method === "GET") {
        const q = new URLSearchParams(new FormData(f)); [...q].forEach(([k, v]) => { if (!v) q.delete(k); });
        const live = f.hasAttribute("data-live");
        await load(action.pathname + (q.toString() ? "?" + q : ""), {}, live ? "replace" : "push");
        if (live) { const s = $("input[type=search]"); if (s) { s.focus(); s.setSelectionRange(s.value.length, s.value.length); } }
      } else {
        await load(action.pathname + action.search, { method: "POST", body: new FormData(f) }, "push");
      }
    } finally { btns.forEach((b) => { if (b.isConnected) b.disabled = false; }); }
  });

  // live search while typing
  document.addEventListener("input", (e) => {
    const f = e.target.closest("form[data-live]");
    if (!f || e.target.type !== "search") return;
    clearTimeout(f._t);
    f._t = setTimeout(() => f.requestSubmit(), 320);
  });

  // ------------------------------------------------------------------ the new-album sheet
  let draft = null;
  const rememberNew = (f) => { draft = Object.fromEntries(new FormData(f)); };
  function openNew() { const d = $("#new-sheet"); if (d && !d.open) { d.showModal(); setTimeout(() => $("#name", d) && $("#name", d).focus(), 60); } }
  function reopenNew() {
    openNew();
    if (draft) Object.entries(draft).forEach(([k, v]) => { const el = $(`#new-sheet [name="${k}"]`); if (el && el.type !== "hidden" && el.type !== "checkbox") el.value = v; });
  }

  // ------------------------------------------------------------------ clicks
  document.addEventListener("click", async (e) => {
    const t = e.target;
    if (t.closest("#theme, #theme2")) return flip();
    if (t.closest("#new-open, [data-open-new]")) return openNew();
    const close = t.closest("[data-close]");
    if (close) { const d = close.closest("dialog"); if (d) d.close(); return; }
    if (t.matches("dialog") && t.open && t.id !== "confirm") { t.close(); return; }   // click on the dim area
    const cp = t.closest("[data-copy]");
    if (cp) {
      const text = cp.dataset.copy;
      try { await navigator.clipboard.writeText(text); } catch (err) {
        const ta = document.createElement("textarea"); ta.value = text; document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
      }
      toast("Link copied. Send it to your guests.");
      return;
    }
    const tb = t.closest("[data-test]");
    if (tb) return openTester(tb.dataset.test, tb.dataset.name);
    if (t.closest("#browse-open")) { $("#browser").hidden = false; return browse(""); }
    if (t.closest("#b-close")) { $("#browser").hidden = true; return; }
    if (t.closest("#b-up")) return browse(browser.parent || "");
    if (t.closest("#b-pick")) {
      const ta = $("#folders"); const lines = ta.value.split("\n").map((x) => x.trim()).filter(Boolean);
      if (!lines.includes(browser.current)) lines.push(browser.current);
      ta.value = lines.join("\n"); $("#browser").hidden = true; return;
    }
    const d = t.closest("[data-path]"); if (d) return browse(d.dataset.path);
    if (!t.closest(".menu")) $$("details.menu[open]").forEach((m) => { m.open = false; });
    if (t.closest("#speed-run")) return runSpeed();
    if (t.closest("#speed-copy")) { try { await navigator.clipboard.writeText(speedText); toast("Report copied."); } catch (er) { toast("Select the text and copy it.", "bad"); } }
  });
  document.addEventListener("keydown", (e) => {
    const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName);
    if (e.key === "/" && !typing) { const s = $("input[type=search]"); if (s) { e.preventDefault(); s.focus(); } }
    if (e.key === "n" && !typing && !e.ctrlKey && !e.metaKey && $("#new-sheet")) { e.preventDefault(); openNew(); }
  });

  // ------------------------------------------------------------------ choose a folder on this computer
  const browser = { current: "", parent: null };
  async function browse(path) {
    const r = await fetch("/admin/browse?path=" + encodeURIComponent(path), { credentials: "same-origin" });
    if (!r.ok) return;
    const j = await r.json();
    browser.current = j.path; browser.parent = j.parent;
    $("#b-path").textContent = j.path || "Choose a drive";
    $("#b-up").hidden = j.parent === null;
    $("#b-pick").hidden = !j.path;
    $("#b-count").textContent = j.path ? j.photos + " photos directly in this folder (sub-folders are included too)" : "";
    const list = $("#b-list"); list.textContent = "";
    j.dirs.forEach((d) => {
      const li = document.createElement("li"), b = document.createElement("button");
      b.type = "button"; b.className = "link"; b.textContent = d.name; b.dataset.path = d.path;
      li.append(b); list.append(li);
    });
    if (!j.dirs.length) { const li = document.createElement("li"); li.textContent = "No folders inside."; list.append(li); }
  }

  // ------------------------------------------------------------------ test an album with a selfie
  const tester = { eid: null, stream: null };
  function openTester(eid, name) {
    const d = $("#tester"); if (!d) return;
    tester.eid = eid; $("#t-title", d).textContent = "Test: " + name;
    $(".t-result", d).hidden = true; $(".t-grid", d).textContent = "";
    d.showModal();
  }
  function stopCam() {
    const d = $("#tester"); if (!d) return;
    if (tester.stream) { tester.stream.getTracks().forEach((t) => t.stop()); tester.stream = null; }
    $(".cam", d).hidden = true; $(".t-snap", d).hidden = true; $(".t-open", d).hidden = false;
  }
  async function testWith(source, w, h) {
    const d = $("#tester"), out = $(".t-result", d), grid = $(".t-grid", d), snap = $(".t-snap", d);
    const tell = (x) => { out.textContent = x; out.hidden = false; };
    const c = document.createElement("canvas"), k = Math.min(1, 1280 / Math.max(w, h));
    c.width = Math.round(w * k); c.height = Math.round(h * k);
    c.getContext("2d").drawImage(source, 0, 0, c.width, c.height);
    snap.disabled = true; tell("Testing ..."); grid.textContent = "";
    try {
      const r = await fetch("/admin/events/" + tester.eid + "/test-selfie", { method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF": $(".sheet", d).dataset.csrf },
        body: JSON.stringify({ image: c.toDataURL("image/jpeg", 0.92) }) });
      if (!r.ok) tell("Please sign in again.");
      else {
        const j = await r.json(); tell(j.text);
        (j.photos || []).forEach((ph) => {
          const fig = document.createElement("figure"), img = document.createElement("img"), cap = document.createElement("figcaption");
          img.loading = "lazy"; img.alt = "Matched album photo"; img.src = "/admin/photo/" + ph.id;
          img.addEventListener("error", async () => {
            try { cap.textContent = "score " + ph.score.toFixed(2) + " - cannot show: " + (await (await fetch(img.src)).text()).slice(0, 200); } catch (e) { /* offline */ }
            img.remove();
          }, { once: true });
          cap.textContent = "score " + ph.score.toFixed(2);
          fig.append(img, cap); grid.append(fig);
        });
      }
    } catch (e) { tell("No connection to the app."); }
    snap.disabled = false;
  }
  document.addEventListener("click", async (e) => {
    const d = $("#tester"); if (!d || !d.open) return;
    if (e.target.closest(".t-open")) {
      try {
        tester.stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 } }, audio: false });
        $("video", d).srcObject = tester.stream; $(".cam", d).hidden = false; $(".t-snap", d).hidden = false; $(".t-open", d).hidden = true;
      } catch (er) { const o = $(".t-result", d); o.textContent = "Camera could not be opened. Allow camera access, or choose a photo below."; o.hidden = false; }
    }
    if (e.target.closest(".t-snap")) {
      const v = $("video", d);
      if (!v.videoWidth) { const o = $(".t-result", d); o.textContent = "Camera is not ready yet."; o.hidden = false; return; }
      testWith(v, v.videoWidth, v.videoHeight);
    }
  });
  document.addEventListener("change", (e) => {
    if (!e.target.matches(".t-file")) return;
    const f = e.target.files[0]; if (!f) return;
    const img = new Image();
    img.onload = () => { testWith(img, img.naturalWidth, img.naturalHeight); URL.revokeObjectURL(img.src); };
    img.onerror = () => { const o = $(".t-result"); o.textContent = "That file is not a picture. Choose a JPG or PNG photo."; o.hidden = false; };
    img.src = URL.createObjectURL(f); e.target.value = "";
  });
  document.addEventListener("close", (e) => { if (e.target.id === "tester") stopCam(); }, true);

  // ------------------------------------------------------------------ live album cards
  let pollTimer = null;
  const LABEL = { running: "Scanning", ready: "Ready", failed: "Failed", empty: "Not scanned" };
  const num = (n) => Number(n).toLocaleString("en-IN");
  function paint(card, s) {
    const was = card.dataset.state;
    card.dataset.state = s.state;
    const pill = $('[data-k="pill"]', card);
    if (pill) { pill.className = "pill " + s.state; $('[data-k="pilltext"]', pill).textContent = LABEL[s.state] || s.state; }
    const st = $(".status", card); st.textContent = s.text; st.className = "status " + s.state; st.dataset.state = s.state;
    const bar = $('[data-k="bar"]', card);
    if (bar) {
      bar.hidden = !s.running;
      const i = $("i", bar); if (i) i.style.width = (s.running ? Math.max(s.pct || 0, s.listing ? 4 : 2) : 0) + "%";
    }
    $('[data-k="photos"]', card).textContent = num(s.photos);
    $('[data-k="faces"]', card).textContent = num(s.faces);
    return was === "running" && s.state !== "running";
  }
  async function poll() {
    clearTimeout(pollTimer);
    const cards = $$(".album-card[data-eid]");
    if (!cards.length) return;
    const running = cards.some((c) => c.dataset.state === "running");
    if (!document.hidden) {
      try {
        const ids = cards.map((c) => c.dataset.eid).join(",");
        const r = await fetch("/admin/status?ids=" + ids, { credentials: "same-origin", cache: "no-store" });
        if (r.status === 403) { location.href = "/admin/login"; return; }
        const j = await r.json();
        let finished = false;
        cards.forEach((c) => { const s = j[c.dataset.eid]; if (s && paint(c, s)) finished = true; });
        if (finished) { await load(location.pathname + location.search, {}, "quiet"); toast("A scan finished."); }
      } catch (e) { /* the app is restarting: try again */ }
    }
    pollTimer = setTimeout(poll, running ? 1500 : 8000);
  }

  // ------------------------------------------------------------------ speed page
  let speedText = "";
  function pageMeter() {
    const box = $("#page-meter"); if (!box) return;
    const nav = performance.getEntriesByType("navigation")[0];
    const m = [];
    if (lastNav.total && Date.now() - lastNav.at < 4000) {
      m.push(["Server", (lastNav.server ?? "?") + " ms", lastNav.server < 150 ? "good" : lastNav.server < 500 ? "mid" : "slow"]);
      m.push(["Whole page", lastNav.total + " ms", lastNav.total < 400 ? "good" : lastNav.total < 1200 ? "mid" : "slow"]);
      if (lastNav.server != null) m.push(["Network", Math.max(0, lastNav.total - lastNav.server) + " ms", lastNav.total - lastNav.server < 300 ? "good" : "slow"]);
    } else if (nav) {
      const total = Math.round(nav.loadEventEnd || nav.domContentLoadedEventEnd || nav.responseEnd);
      m.push(["Page loaded in", total + " ms", total < 800 ? "good" : total < 2000 ? "mid" : "slow"]);
      m.push(["Server answered in", Math.round(nav.responseStart - nav.requestStart) + " ms", "good"]);
    }
    paintMeter(box, m);
    (async () => {                                   // five tiny requests: the line's own delay
      const t = [], s = [];
      for (let i = 0; i < 5; i++) {
        const a = performance.now();
        try { const r = await fetch("/admin/ping", { cache: "no-store", credentials: "same-origin" }); await r.text();
          t.push(performance.now() - a); const d = /dur=(\d+)/.exec(r.headers.get("Server-Timing") || ""); if (d) s.push(+d[1]); } catch (e) { /* */ }
      }
      if (t.length) { t.sort((x, y) => x - y); const med = Math.round(t[Math.floor(t.length / 2)]);
        m.push(["Round trip to the app", med + " ms", med < 120 ? "good" : med < 400 ? "mid" : "slow"]);
        if (s.length) m.push(["Server for that request", Math.round(s.reduce((x, y) => x + y) / s.length) + " ms", "good"]);
        paintMeter(box, m); }
    })();
  }
  function paintMeter(box, rows) {
    box.textContent = "";
    rows.forEach(([k, v, c]) => { const d = document.createElement("div"); d.className = "m " + (c || ""); const b = document.createElement("b"); b.textContent = v; const s = document.createElement("span"); s.textContent = k; d.append(b, s); box.append(d); });
  }
  async function runSpeed() {
    const btn = $("#speed-run"); btn.disabled = true; btn.lastChild.textContent = " Measuring ...";
    try {
      const j = await (await fetch("/admin/speed/run", { credentials: "same-origin", cache: "no-store" })).json();
      const lvl = (v, g, m) => v == null ? "" : v <= g ? "good" : v <= m ? "mid" : "slow";
      const lvlHi = (v, g, m) => v == null ? "" : v >= g ? "good" : v >= m ? "mid" : "slow";
      const rows = [
        ["Database: open and read", j.db_open_read_ms + " ms", lvl(j.db_open_read_ms, 6, 25)],
        ["Database: save (one commit)", j.db_commit_ms == null ? "-" : j.db_commit_ms + " ms", lvl(j.db_commit_ms, 15, 40)],
        ["Disk write", j.disk_write_mbs + " MB/s", lvlHi(j.disk_write_mbs, 100, 40)],
        ["Disk read", j.disk_read_mbs + " MB/s", lvlHi(j.disk_read_mbs, 150, 60)],
        ["Processor test", j.cpu_burn_ms + " ms", lvl(j.cpu_burn_ms, 60, 120)],
        ["Free disk space", j.disk_free_gb + " GB", lvlHi(j.disk_free_gb, 20, 5)],
        ["Database size", j.db_size_mb + " MB", ""], ["Database mode", j.journal, ""],
        ["Scans running", String(j.scans_running), ""], ["Processors", String(j.cpus), ""],
        ["Album folders checked", j.folders_checked + " (" + j.folders_slow.length + " slow)", j.folders_slow.length ? "slow" : "good"],
      ];
      if (j.db_disk) rows.push(["Data folder is on", j.db_disk, /NETWORK|USB/.test(j.db_disk) ? "slow" : "good"]);
      if (j.drive_token_ms != null) rows.push(["Google answers in", j.drive_token_ms + " ms", lvl(j.drive_token_ms, 800, 1500)]);
      paintMeter($("#speed-meter"), rows);
      const tips = $("#speed-tips"); tips.textContent = "";
      j.tips.forEach((t) => { const li = document.createElement("li"); li.textContent = t; tips.append(li); });
      const body = $("#speed-routes"); body.textContent = "";
      j.routes.forEach((r) => { const tr = document.createElement("tr"); [r.route, r.n, r.avg + " ms", r.p95 + " ms", r.worst + " ms"].forEach((v) => { const td = document.createElement("td"); td.textContent = v; tr.append(td); }); body.append(tr); });
      $("#speed-out").hidden = false;
      speedText = "FaceFind speed report\n" + rows.map((r) => r[0] + ": " + r[1]).join("\n") + "\n\n" + j.tips.join("\n") + "\n\nSlowest pages:\n" + j.routes.map((r) => r.route + "  avg " + r.avg + "  p95 " + r.p95 + "  worst " + r.worst).join("\n");
    } catch (e) { toast("The check could not run. Sign in again.", "bad"); }
    btn.disabled = false; btn.lastChild.textContent = " Run the check again";
  }

  // ------------------------------------------------------------------ every page, after it appears
  function boot() {
    $$(".progress i[data-pct]").forEach((i) => { i.style.width = Math.max(+i.dataset.pct, 2) + "%"; });
    const f = $("#flash");
    if (f && f.dataset.msg && !boot.first) { toast(f.dataset.msg, bad(f.dataset.msg) ? "bad" : ""); }
    boot.first = true;
    if (f) f.dataset.msg = "";
    clearTimeout(pollTimer); pollTimer = setTimeout(poll, 1200);
    if (view() && view().dataset.page === "speed") pageMeter();
    $$("#view .view").forEach((v) => { v.style.animation = "none"; void v.offsetWidth; v.style.animation = ""; });
  }
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  // the flash message of the very first page load (after a normal redirect)
  const first = $("#flash");
  if (first && first.dataset.msg) { const m = first.dataset.msg; first.dataset.msg = ""; setTimeout(() => toast(m, bad(m) ? "bad" : ""), 300); }
  boot.first = true;
  boot();
})();
