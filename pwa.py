"""Install-as-an-app support (PWA): a web app manifest, round app icons made
from the studio logo, and a very small service worker. With these, Android
(Chrome) and iPhone (Safari, Add to Home Screen) install FaceFind like an app,
for guests and for the admin, with no app store."""

import functools
import io

from PIL import Image, ImageDraw, ImageFont

import brand
import config

KINDS = {
    # kind: (start page, scope, icon background, theme colour, extra name)
    "guest": ("/", "/", "#0a1020", "#0a1020", ""),
    "admin": ("/admin", "/admin/", "#5b5ce6", "#5b5ce6", " Admin"),
}
SIZES = {"180": (180, False), "192": (192, False), "512": (512, False), "maskable-512": (512, True)}


def _hex(h):
    return tuple(int(h[i:i + 2], 16) for i in (1, 3, 5))


@functools.lru_cache(maxsize=32)
def _icon(kind, name, stamp, brand_name):
    size, maskable = SIZES[name]
    bg = _hex(KINDS[kind][2])
    im = Image.new("RGB", (size, size), bg)
    inner = 0.56 if maskable else 0.66                  # a maskable icon keeps its picture inside the safe middle
    box = int(size * inner)
    if stamp:
        logo = brand.logo_image()
        logo.thumbnail((box, box), Image.LANCZOS)
        im.paste(logo, ((size - logo.width) // 2, (size - logo.height) // 2), logo)
    else:
        letter = (brand_name.strip()[:1] or "F").upper()
        font = ImageFont.load_default(size=int(size * 0.5))
        ImageDraw.Draw(im).text((size / 2, size / 2), letter, fill="white", font=font, anchor="mm")
    out = io.BytesIO()
    im.save(out, "PNG", optimize=True)
    return out.getvalue()


def icon_png(kind, name):
    stamp = brand.LOGO.stat().st_mtime_ns if brand.LOGO.exists() else 0
    return _icon(kind, name, stamp, config.BRAND)


def manifest(kind, base):
    start, scope, bg, theme, extra = KINDS[kind]
    name = config.BRAND + extra
    icons = [{"src": f"{base}/pwa/icon/{kind}/{n}", "sizes": f"{s[0]}x{s[0]}", "type": "image/png",
              **({"purpose": "maskable"} if s[1] else {})}
             for n, s in SIZES.items() if n != "180"]
    return {"id": start, "name": name, "short_name": name[:12], "start_url": start, "scope": scope,
            "display": "standalone", "orientation": "any", "background_color": bg,
            "theme_color": theme, "lang": "en", "icons": icons}


SERVICE_WORKER = """// FaceFind service worker: keeps the app files quick and shows a friendly page when offline.
// It never stores photos, pages or anything personal.
const V = "ff-app-1";
self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(V).then((c) => c.add("/pwa/offline")).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(ks.filter((k) => k !== V).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== "GET" || u.origin !== location.origin) return;
  if (r.mode === "navigate") {
    e.respondWith(fetch(r).catch(() => caches.match("/pwa/offline")));
    return;
  }
  if (u.pathname.startsWith("/static/")) {
    e.respondWith(caches.open(V).then((c) => c.match(r).then((hit) => {
      const net = fetch(r).then((res) => { if (res.ok) c.put(r, res.clone()); return res; }).catch(() => hit);
      return hit || net;
    })));
  }
});
"""
