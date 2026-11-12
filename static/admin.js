(() => {
  const $ = (id) => document.getElementById(id);

  // ---- copy guest link
  document.querySelectorAll(".copy").forEach((b) => b.addEventListener("click", async () => {
    const inp = $(b.dataset.target);
    inp.select();
    try { await navigator.clipboard.writeText(inp.value); } catch (e) { document.execCommand("copy"); }
    b.textContent = "Copied";
    setTimeout(() => { b.textContent = b.dataset.label || "Copy link"; }, 1500);
  }));

  // ---- folder picker
  const box = $("browser");
  let current = "", parent = null;
  async function go(path) {
    const r = await fetch("/admin/browse?path=" + encodeURIComponent(path));
    if (!r.ok) return;
    const j = await r.json();
    current = j.path; parent = j.parent;
    $("b-path").textContent = current || "Choose a drive";
    $("b-up").hidden = parent === null;
    $("b-pick").hidden = !current;
    $("b-count").textContent = current ? (j.photos + " photos directly in this folder (sub-folders are included too)") : "";
    const list = $("b-list");
    list.textContent = "";
    j.dirs.forEach((d) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button"; btn.className = "link"; btn.textContent = d.name;
      btn.addEventListener("click", () => go(d.path));
      li.append(btn); list.append(li);
    });
    if (!j.dirs.length) {
      const li = document.createElement("li");
      li.textContent = "No folders inside.";
      list.append(li);
    }
  }
  if (box) {
    $("browse-open").addEventListener("click", () => { box.hidden = false; go(""); });
    $("b-close").addEventListener("click", () => { box.hidden = true; });
    $("b-up").addEventListener("click", () => go(parent || ""));
    $("b-pick").addEventListener("click", () => {
      const t = $("folders");
      const lines = t.value.split("\n").map((x) => x.trim()).filter(Boolean);
      if (!lines.includes(current)) lines.push(current);
      t.value = lines.join("\n");
      box.hidden = true;
    });
  }

  // ---- test an album with a live selfie or a chosen photo
  document.querySelectorAll(".tester").forEach((box) => {
    const video = box.querySelector("video"), cam = box.querySelector(".cam");
    const open = box.querySelector(".t-open"), snap = box.querySelector(".t-snap");
    const out = box.querySelector(".t-result"), grid = box.querySelector(".t-grid");
    const file = box.querySelector(".t-file");
    let stream = null;
    const tell = (t) => { out.textContent = t; out.hidden = false; };
    function stop() {
      if (stream) { stream.getTracks().forEach((t) => t.stop()); stream = null; }
      cam.hidden = true; snap.hidden = true; open.hidden = false;
    }
    async function test(source, w, h) {
      const c = document.createElement("canvas");
      const k = Math.min(1, 1280 / Math.max(w, h));
      c.width = Math.round(w * k); c.height = Math.round(h * k);
      c.getContext("2d").drawImage(source, 0, 0, c.width, c.height);
      snap.disabled = true; tell("Testing ..."); grid.textContent = "";
      try {
        const r = await fetch("/admin/events/" + box.dataset.eid + "/test-selfie", {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-CSRF": box.dataset.csrf },
          body: JSON.stringify({ image: c.toDataURL("image/jpeg", 0.92) }) });
        if (!r.ok) { tell("Please sign in again."); }
        else {
          const j = await r.json();
          tell(j.text);
          (j.photos || []).forEach((ph) => {
            const fig = document.createElement("figure");
            const img = document.createElement("img");
            img.loading = "lazy"; img.alt = "Matched album photo";
            img.src = "/admin/photo/" + ph.id;
            img.addEventListener("error", async () => {      // say why it is missing
              try {
                const why = await (await fetch(img.src)).text();
                cap.textContent = "score " + ph.score.toFixed(2) + " - cannot show: " + why.slice(0, 200);
              } catch (e) { /* no connection */ }
              img.remove();
            }, { once: true });
            const cap = document.createElement("figcaption");
            cap.textContent = "score " + ph.score.toFixed(2);
            fig.append(img, cap); grid.append(fig);
          });
        }
      } catch (e) { tell("No connection to the app."); }
      snap.disabled = false;
    }
    open.addEventListener("click", async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1280 } }, audio: false });
        video.srcObject = stream;
        cam.hidden = false; snap.hidden = false; open.hidden = true;
      } catch (e) { tell("Camera could not be opened. Allow camera access, or choose a photo below."); }
    });
    snap.addEventListener("click", () => {
      if (!video.videoWidth) return tell("Camera is not ready yet.");
      test(video, video.videoWidth, video.videoHeight);
    });
    file.addEventListener("change", () => {
      const f = file.files[0];
      if (!f) return;
      const img = new Image();
      img.onload = () => { test(img, img.naturalWidth, img.naturalHeight); URL.revokeObjectURL(img.src); };
      img.onerror = () => tell("That file is not a picture. Choose a JPG or PNG photo.");
      img.src = URL.createObjectURL(f);
      file.value = "";
    });
    box.addEventListener("toggle", () => { if (!box.open) stop(); });
  });

  // ---- live scan status
  const running = () => document.querySelectorAll('.status[data-state="running"]').length > 0;
  async function poll() {
    try {
      const j = await (await fetch("/admin/status")).json();
      let finished = false;
      document.querySelectorAll(".status").forEach((el) => {
        const s = j[el.dataset.eid];
        if (!s) return;
        if (el.dataset.state === "running" && s.state !== "running") finished = true;
        el.textContent = s.text;
        el.dataset.state = s.state;
        el.className = "status " + s.state;
      });
      if (finished) return location.replace("/admin");
    } catch (e) { /* app restarting */ }
    if (running()) setTimeout(poll, 2500);
  }
  if (running()) setTimeout(poll, 1500);
})();
