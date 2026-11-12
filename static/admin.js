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
