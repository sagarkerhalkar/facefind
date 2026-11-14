"""FaceFind - guests verify their email with an OTP, take a selfie, and get
only the photos they appear in. Run:  python app.py"""
import base64
import collections
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import string
import sys
import threading
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from werkzeug.exceptions import HTTPException

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from PIL import Image, ImageDraw, ImageFont, ImageOps

import autoalbums
import brand
import config
import db
import faces
import gdrive
import indexer
import mailer
import netutil

app = Flask(__name__)
app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=config.COOKIE_SECURE,
    SESSION_COOKIE_NAME="ff_admin",
    PERMANENT_SESSION_LIFETIME=8 * 3600,
)
if config.TRUST_PROXY:
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

db.init()
db.start_backups()


brand.load()
signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="download")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PHONE_RE = re.compile(r"\+?\d{7,15}")
# The general link: not one album, but every open album in the guest's dates.
PORTAL = {"id": 0, "name": "", "code": ""}
COOKIE = "ff_session"


# ---------------------------------------------------------------- helpers
def h(text):
    return hmac.new(config.SECRET_KEY.encode(), text.encode(), hashlib.sha256).hexdigest()


def con():
    if "con" not in g:
        g.con = db.connect()
    return g.con


@app.teardown_appcontext
def _close(_):
    c = g.pop("con", None)
    if c is not None:
        c.close()


def ip():
    a = request.remote_addr or ""
    return a[7:] if a.startswith("::ffff:") else a


def err(msg, status=400):
    return jsonify(ok=False, error=msg), status


import logging
from logging.handlers import RotatingFileHandler

_elog = logging.getLogger("facefind")
try:
    _h = RotatingFileHandler(config.DATA_DIR / "error.log", maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _elog.addHandler(_h)
except OSError:
    pass
_elog.setLevel(logging.INFO)


@app.errorhandler(Exception)
def _crash(e):
    """Never show a bare 'Internal Server Error': write the real reason to
    data/error.log (and the console) and give the visitor a reference."""
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, sqlite3.DatabaseError) and any(
            w in str(e).lower() for w in ("malformed", "not a database", "disk i/o")):
        # The file broke while running. Stop; start.bat / start.sh start the app
        # again at once and the start-up repair rebuilds it from the newest backup.
        _elog.error("database damaged while running - restarting to repair: %s", e)
        threading.Timer(1.0, lambda: os._exit(3)).start()
    import traceback
    ref = secrets.token_hex(3)
    _elog.error("ref %s  %s %s\n%s", ref, request.method, request.full_path,
                traceback.format_exc())
    if request.path.startswith("/api/"):
        return jsonify(ok=False, error=f"Something went wrong on our side (ref {ref}). "
                                       "Please try again."), 500
    return ("<!doctype html><meta name=viewport content='width=device-width'>"
            "<body style='font-family:system-ui;background:#0b0f1a;color:#e8ecf7;"
            "display:grid;place-items:center;min-height:100vh;margin:0'>"
            "<div style='max-width:420px;padding:24px;text-align:center'>"
            "<h2>Something went wrong</h2><p>The page could not load. "
            "Please reload in a moment.</p>"
            f"<p style='opacity:.6'>Reference {ref}</p>"
            "<p><a style='color:#8ab4ff' href='/'>Reload</a></p></div>", 500)


@app.before_request
def _clock():
    g.t0 = time.time()


@app.before_request
def _guests_first():
    # While guests are using the site the scan slows down a little, so pages
    # and photos stay quick (see indexer._embed_file).
    if not request.path.startswith(("/admin", "/static")):
        indexer.guest_seen[0] = time.time()


@app.before_request
def _network_guard():
    # Only the networks / VLANs you list may reach the app; the admin page
    # can be limited further (e.g. to your office VLAN only).
    addr = ip()
    if not netutil.ip_allowed(addr, config.ALLOWED_NETWORKS):
        abort(403)
    if request.path.startswith("/admin") and not netutil.ip_allowed(addr, config.ADMIN_NETWORKS):
        abort(404)


def guest_link(code):
    """Link to share with guests: your domain if set, else this computer's
    LAN address (never 127.0.0.1, which only works on this computer)."""
    path = url_for("event_page", code=code) if code else url_for("home")
    if config.PUBLIC_URL:
        return config.PUBLIC_URL + path
    host = request.host
    name = host.rsplit(":", 1)[0]
    if name in ("127.0.0.1", "localhost"):
        host = host.replace(name, netutil.primary_ip(), 1)
    return f"{request.scheme}://{host}{path}"


@app.before_request
def _csrf_guard():
    # Every state-changing guest call must come from our own page script.
    if request.method == "POST" and request.path.startswith("/api/"):
        if request.headers.get("X-Requested-With") != "facefind":
            abort(403)


_timings = collections.deque(maxlen=3000)        # (time, route, milliseconds) - the Speed page


@app.after_request
def _headers(r):
    took = time.time() - g.get("t0", time.time())
    r.headers["Server-Timing"] = f"app;dur={took * 1000:.0f}"
    if request.endpoint and request.endpoint != "static":
        _timings.append((time.time(), request.endpoint, took * 1000))
    if took > 1.0:                 # anything slower than a second is written down
        _elog.warning("SLOW %.1fs  %s %s -> %s", took, request.method, request.full_path, r.status_code)
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if not config.DEV_MODE:
        r.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.path.startswith("/brand/") and r.status_code == 200:
        r.headers["Cache-Control"] = "public, max-age=86400"      # addresses carry a version
    elif r.headers.pop("X-Keep", None) and r.status_code == 200:
        r.headers["Cache-Control"] = "private, max-age=3600"      # previews: this browser only
    elif request.path.startswith("/static/"):
        if request.args.get("v") and r.status_code == 200:      # the address changes with the file
            r.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        r.headers["Cache-Control"] = "no-store"
    return r


def event_by_code(code):
    if not code or not isinstance(code, str):
        return None
    return con().execute(
        "SELECT * FROM events WHERE code=? AND active=1", (code,)).fetchone()


def event_for(code):
    """An album's own link, or the general link (code "")."""
    if code == "" and config.PORTAL:
        return PORTAL
    return event_by_code(code)


def old_copy(row):
    """A Drive photo scanned by an older version of this app has no Drive id
    yet (the next scan adds it). Until then its old copy on disk is used."""
    if row["ref"] or not gdrive.is_drive(row["root"]):
        return None
    base = (config.DRIVE_DIR / row["root"][7:]).resolve()
    p = (base / row["relpath"]).resolve()
    return p if base in p.parents and p.is_file() else None


def guest():
    """The verified guest session for this browser, or None."""
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    return con().execute(
        "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
        (h(tok), time.time())).fetchone()


def allowed(s, photo_id):
    """A guest may touch a photo only if THEIR selfie matched it now, or if
    they saved it earlier (which was only possible after such a match) and
    are signed in with the same verified email."""
    c = con()
    if s["status"] in ("matched", "approved"):
        row = c.execute(
            "SELECT p.*, e.folders FROM matches m JOIN photos p ON p.id=m.photo_id "
            "JOIN events e ON e.id=p.event_id WHERE m.session_id=? AND m.photo_id=?",
            (s["id"], photo_id)).fetchone()
        if row:
            return row
    if not s["email"]:
        return None
    return c.execute(
        "SELECT p.*, e.folders FROM bookmarks b JOIN photos p ON p.id=b.photo_id "
        "JOIN events e ON e.id=p.event_id WHERE b.email=? AND b.photo_id=? AND e.active=1",
        (s["email"], photo_id)).fetchone()


def signin_needed():
    """Guests who are only looking (no verified email) can see previews;
    downloading and saving need the email code."""
    return jsonify(ok=False, signin=True,
                   error="Sign in with your email to download or save photos."), 403


def record_download(c, s, row):
    """The admin's record: who took which photo, and when."""
    c.execute("INSERT INTO downloads(ts,email,photo_id,event_id,relpath,ip) VALUES(?,?,?,?,?,?)",
              (time.time(), s["email"], row["id"], row["event_id"], row["relpath"], ip()))


def real_path(folders, root, relpath):
    """Only files inside a folder that is still on the event's list."""
    if root not in db.split_folders(folders) or gdrive.is_drive(root):
        abort(404)
    base = Path(root).resolve()
    p = (base / relpath).resolve()
    if base not in p.parents or not p.is_file():
        abort(404)      # also the case when that drive is offline
    return p


def preview_file(row, small=False):
    """The watermarked preview of one photo (made once, then kept): large for
    the full-page view, small for the photo page. The small one is made from
    the copy kept by the scan, so the original (which may be on Google Drive)
    is not read again. When it cannot be made, g.why says why."""
    if row["root"] not in db.split_folders(row["folders"]):
        g.why = "its folder was taken off the album"
        abort(404)
    dest = indexer.preview_path(row, small)
    if dest.exists():
        return dest
    side = config.THUMB_SIDE if small else config.PREVIEW_SIDE
    kept = indexer.thumb_path(row["id"])
    if small and kept.exists():
        try:
            make_preview(kept, dest, "", side)
            return dest
        except (OSError, ValueError):
            pass
    try:
        if gdrive.is_drive(row["root"]) and not old_copy(row):
            if not row["ref"]:
                g.why = "scanned by an older version - press 'Scan again' and wait for green"
                abort(404)
            ext = ""
            try:
                data = gdrive.fetch_small({"id": row["ref"]}, 1200)    # a JPEG made by Google
                if not data:
                    data, ext = gdrive.fetch_full(row["ref"]), Path(row["relpath"]).suffix
            except gdrive.DriveError as e:
                g.why = f"Google Drive: {e}"
                abort(503)
            make_preview(io.BytesIO(data), dest, ext, side)
        else:
            g.why = "file not found - the drive is not connected, or the photo was moved"
            src = old_copy(row) or real_path(row["folders"], row["root"], row["relpath"])
            make_preview(src, dest, Path(row["relpath"]).suffix, side)
    except (OSError, ValueError, RuntimeError) as e:        # unreadable or damaged picture
        g.why = f"the picture could not be opened ({e})"
        abort(404)
    return dest


def drive_download(ref, name):
    """Passes the original from Google Drive straight on to the guest."""
    try:
        r = gdrive.open_original(ref)
    except gdrive.DriveError:
        abort(503)

    def chunks():
        with r:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    return
                yield chunk
    headers = {"Content-Disposition": "attachment; filename*=UTF-8''" + quote(name)}
    if r.headers.get("Content-Length"):
        headers["Content-Length"] = r.headers["Content-Length"]
    return app.response_class(
        chunks(), headers=headers,
        mimetype=r.headers.get("Content-Type") or "application/octet-stream")


def make_preview(src, dest, ext="", side=None):
    if ext.lower() in config.HEIC_EXT | config.RAW_EXT:      # iPhone / camera RAW files
        import numpy as np
        bgr = faces.load(src.read() if hasattr(src, "read") else Path(src).read_bytes(), ext)
        if bgr is None:
            raise ValueError("no picture could be read from this file")
        im = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))
    else:
        im = Image.open(src)
        im.draft("RGB", (side or config.PREVIEW_SIDE,) * 2)    # big JPEGs open several times faster at reduced size
        im = ImageOps.exif_transpose(im).convert("RGB")
    side = side or config.PREVIEW_SIDE
    im.thumbnail((side, side))
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ImageFont.load_default(size=max(14, im.width // 24))
    text = f"{config.BRAND} - PREVIEW"
    tw = int(d.textlength(text, font=font)) + 40
    step = int(font.size * 5)
    for row, y in enumerate(range(10, im.height, step)):
        for x in range(-tw, im.width, tw):
            d.text((x + (row % 2) * tw // 2, y), text, font=font, fill=(255, 255, 255, 80))
    out = Image.alpha_composite(im.convert("RGBA"), layer).convert("RGB")
    if brand.LOGO.exists():                 # the studio logo, top right, as on downloads
        try:
            brand.add_logo(out)
        except Exception:
            pass
    tmp = dest.with_suffix(f".{secrets.token_hex(4)}.tmp")
    out.save(tmp, "JPEG", quality=72, optimize=True, progressive=True)
    os.replace(tmp, dest)


def _static_v(name):
    """Changes whenever the file changes, so browsers keep it for a year but
    always get the new one after an update."""
    try:
        return int((Path(app.static_folder) / name).stat().st_mtime)
    except OSError:
        return 0


# ------------------------------------------------- studio look and promotions
@app.context_processor
def _studio():
    c = con()
    return dict(brand=config.BRAND, static_v=_static_v, studio=dict(
        tagline=brand.get(c, "tagline"), logo=brand.LOGO.exists(),
        background=brand.BACKGROUND.exists(), v=brand.version() + brand.get(c, "accent")[1:]))


def _ink_for(colour):
    """Black or white text, whichever reads better on this colour."""
    r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
    return "#16132d" if (0.299 * r + 0.587 * g + 0.114 * b) > 150 else "#ffffff"


@app.get("/brand/theme.css")
def brand_theme():
    c = con()
    accent = brand.get(c, "accent")
    css = f":root{{--accent:{accent};--accent-ink:{_ink_for(accent)};}}\n"
    if brand.BACKGROUND.exists():
        url = f"url(/brand/background?v={brand.version()})"
        veil = "linear-gradient(rgba(250,248,245,.90),rgba(250,248,245,.94))"
        css += (f".backdrop{{background-image:{veil},{url};}}\n"
                f".login-wrap{{background-image:linear-gradient(rgba(13,11,26,.72),rgba(13,11,26,.72)),{url};"
                "background-size:cover;background-position:center;}}\n")
    return app.response_class(css, mimetype="text/css")


@app.get("/brand/logo")
def brand_logo():
    if not brand.LOGO.exists():
        abort(404)
    return send_file(brand.LOGO, mimetype="image/png")


@app.get("/brand/background")
def brand_background():
    if not brand.BACKGROUND.exists():
        abort(404)
    return send_file(brand.BACKGROUND, mimetype="image/jpeg")


@app.get("/brand/promo/<int:pid>")
def brand_promo(pid):
    f = brand.promo_image(pid)
    if not f.exists():
        abort(404)
    return send_file(f, mimetype="image/jpeg")


def promos(where):
    """Upcoming events and offers to show ('before' or 'after' sign-in).
    An upcoming event disappears by itself once its date has passed."""
    rows = con().execute(
        f"SELECT * FROM promos WHERE active=1 AND show_{where}=1 "
        "AND (kind<>'event' OR event_date='' OR event_date>=?) "
        "ORDER BY kind DESC, CASE WHEN event_date='' THEN '9999' ELSE event_date END, id DESC "
        "LIMIT 12", (time.strftime("%Y-%m-%d"),)).fetchall()
    return [dict(r, image=brand.promo_image(r["id"]).exists()) for r in rows]


def guest_page(status=200, **kw):
    return render_template("guest.html", promos_before=promos("before"),
                           look_first=brand.get(con(), "look_first") == "1",
                           promos_after=promos("after"), **kw), status


# ------------------------------------------------------------ guest pages
@app.get("/")
def home():
    if not config.PORTAL:
        return guest_page(404, event=None, portal=False)
    today = time.strftime("%Y-%m-%d")
    month_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    return guest_page(event=None, portal=True, today=today, month_ago=month_ago)


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/e/<code>")
def event_page(code):
    ev = event_by_code(code)
    if not ev:
        return guest_page(404, event=None, portal=False)
    return guest_page(event=ev, portal=False)


@app.post("/api/otp/send")
def otp_send():
    d = request.get_json(silent=True) or {}
    ev = event_for(d.get("code"))
    email = str(d.get("email") or "").strip().lower()
    if not ev:
        return err("This link is not valid.", 404)
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return err("Please enter a valid email address.")
    name = " ".join(str(d.get("name") or "").split())[:80]
    phone = re.sub(r"[\s().-]", "", str(d.get("phone") or ""))
    if len(name) < 2:
        return err("Please enter your name.")
    if not PHONE_RE.fullmatch(phone):
        return err("Please enter your phone number (digits only, with country code if outside India).")
    if d.get("consent") is not True:
        return err("Please accept the face-matching consent to continue.")
    c, now = con(), time.time()
    since = now - 900
    n_email = c.execute("SELECT COUNT(*) FROM otps WHERE email=? AND created_at>?",
                        (email, since)).fetchone()[0]
    n_ip = c.execute("SELECT COUNT(*) FROM otps WHERE ip=? AND created_at>?",
                     (ip(), since)).fetchone()[0]
    if n_email >= config.OTP_PER_EMAIL or n_ip >= config.OTP_PER_IP:
        return err("Too many codes requested. Please wait 15 minutes.", 429)
    code = f"{secrets.randbelow(10**6):06d}"
    c.execute("UPDATE otps SET used=1 WHERE email=? AND event_id=? AND used=0",
              (email, ev["id"]))
    c.execute("INSERT INTO otps(email,event_id,code_hash,expires_at,created_at,ip,name,phone) "
              "VALUES(?,?,?,?,?,?,?,?)",
              (email, ev["id"], h(f"{email}|{ev['id']}|{code}"), now + config.OTP_TTL, now, ip(),
               name, phone))
    db.audit(c, "otp_sent", email, ev["id"], ip=ip())
    c.commit()
    try:
        mailer.send_otp(email, code, ev["name"] or config.BRAND)
    except Exception as e:
        app.logger.error("mail failed: %s", e)
        db.audit(c, "mail_failed", email, ev["id"], str(e)[:200], ip())
        c.commit()
        return err("We could not send the email. Please try again later.", 502)
    return jsonify(ok=True, ttl=config.OTP_TTL)


@app.post("/api/look")
def look():
    """Start looking without signing in: the guest can take a selfie and see
    watermarked previews. Nothing can be downloaded or saved until the email
    is verified with a code."""
    d = request.get_json(silent=True) or {}
    ev = event_for(d.get("code"))
    c, now = con(), time.time()
    if not ev or brand.get(c, "look_first") != "1":
        return err("Please sign in with your email.", 404)
    if d.get("consent") is not True:
        return err("Please accept the face-matching consent to continue.")
    recent = c.execute("SELECT COUNT(*) FROM sessions WHERE email='' AND ip=? AND created_at>?",
                       (ip(), now - 3600)).fetchone()[0]
    if recent >= 30:
        return err("Too many tries from this network. Please sign in with your email.", 429)
    token = secrets.token_urlsafe(32)
    c.execute("INSERT INTO sessions(token_hash,email,event_id,created_at,expires_at,ip) "
              "VALUES(?,?,?,?,?,?)", (h(token), "", ev["id"], now, now + config.SESSION_TTL, ip()))
    db.audit(c, "looking", None, ev["id"] or None, "without sign-in", ip())
    c.commit()
    r = jsonify(ok=True)
    r.set_cookie(COOKIE, token, max_age=config.SESSION_TTL, httponly=True,
                 secure=config.COOKIE_SECURE, samesite="Strict")
    return r


@app.post("/api/not-me/<int:pid>")
def not_me(pid):
    """The guest says a photo does not show them: it leaves their results,
    and its score is kept so the owner can see how strict matching must be."""
    s = guest()
    if not s:
        return err("Session expired.", 401)
    c = con()
    row = c.execute(
        "SELECT m.score, p.relpath, p.event_id FROM matches m JOIN photos p ON p.id=m.photo_id "
        "WHERE m.session_id=? AND m.photo_id=?", (s["id"], pid)).fetchone()
    if not row:
        abort(404)
    c.execute("DELETE FROM matches WHERE session_id=? AND photo_id=?", (s["id"], pid))
    c.execute("DELETE FROM bookmarks WHERE email=? AND photo_id=? AND email<>''", (s["email"], pid))
    db.audit(c, "not_me", s["email"] or None, row["event_id"], f"{row['score']:.2f} | {row['relpath']}", ip())
    c.commit()
    return jsonify(ok=True)


@app.post("/api/otp/verify")
def otp_verify():
    d = request.get_json(silent=True) or {}
    ev = event_for(d.get("code"))
    email = str(d.get("email") or "").strip().lower()
    otp = str(d.get("otp") or "").strip()
    if not ev:
        return err("This link is not valid.", 404)
    c, now = con(), time.time()
    row = c.execute("SELECT * FROM otps WHERE email=? AND event_id=? AND used=0 "
                    "ORDER BY id DESC LIMIT 1", (email, ev["id"])).fetchone()
    if not row or row["expires_at"] < now:
        return err("This code has expired. Please request a new one.")
    if row["attempts"] >= config.OTP_MAX_ATTEMPTS:
        c.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
        c.commit()
        return err("Too many wrong attempts. Please request a new code.", 429)
    if not hmac.compare_digest(row["code_hash"], h(f"{email}|{ev['id']}|{otp}")):
        c.execute("UPDATE otps SET attempts=attempts+1 WHERE id=?", (row["id"],))
        db.audit(c, "otp_wrong", email, ev["id"], ip=ip())
        c.commit()
        return err("That code is not correct.")
    c.execute("UPDATE otps SET used=1 WHERE id=?", (row["id"],))
    looking = guest()
    token = None
    if looking and looking["email"] == "" and looking["event_id"] == ev["id"]:
        # the guest was looking at previews: keep their photos, add the verified email
        c.execute("UPDATE sessions SET email=?, expires_at=? WHERE id=?",
                  (email, now + config.SESSION_TTL, looking["id"]))
    else:
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO sessions(token_hash,email,event_id,created_at,expires_at,ip) "
                  "VALUES(?,?,?,?,?,?)",
                  (h(token), email, ev["id"], now, now + config.SESSION_TTL, ip()))
    c.execute("INSERT INTO users(email,name,phone,created_at,last_login) VALUES(?,?,?,?,?) "
              "ON CONFLICT(email) DO UPDATE SET name=excluded.name, phone=excluded.phone, "
              "last_login=excluded.last_login", (email, row["name"], row["phone"], now, now))
    db.audit(c, "verified", email, ev["id"], f"{row['name']} | {row['phone']}", ip())
    c.commit()
    r = jsonify(ok=True)
    if token:
        r.set_cookie(COOKIE, token, max_age=config.SESSION_TTL, httponly=True,
                     secure=config.COOKIE_SECURE, samesite="Strict")
    return r


def _state(s):
    c = con()
    ev = c.execute("SELECT * FROM events WHERE id=?", (s["event_id"],)).fetchone()
    photos, groups = [], []
    if s["status"] in ("matched", "approved"):
        by_event = {}
        for r in c.execute(
                "SELECT m.photo_id, e.id AS eid, e.name, e.event_date FROM matches m "
                "JOIN photos p ON p.id=m.photo_id JOIN events e ON e.id=p.event_id "
                "WHERE m.session_id=? ORDER BY e.event_date DESC, e.id DESC, m.score DESC",
                (s["id"],)):
            grp = by_event.get(r["eid"])
            if grp is None:
                grp = by_event[r["eid"]] = dict(name=r["name"], date=r["event_date"], photos=[])
                groups.append(grp)
            grp["photos"].append(r["photo_id"])
            photos.append(r["photo_id"])
    saved = [r[0] for r in c.execute(
        "SELECT b.photo_id FROM bookmarks b JOIN photos p ON p.id=b.photo_id "
        "JOIN events e ON e.id=p.event_id WHERE b.email=? AND e.active=1", (s["email"],))]
    u = c.execute("SELECT name FROM users WHERE email=?", (s["email"],)).fetchone()
    return dict(ok=True, email=s["email"], status=s["status"], photos=photos, groups=groups,
                saved=saved, name=u["name"] if u else "", looking=s["email"] == "",
                event_code=ev["code"] if ev else ("" if s["event_id"] == 0 else None),
                date_from=s["date_from"], date_to=s["date_to"],
                tries_left=max(0, config.SELFIE_MAX_TRIES - s["selfie_tries"]))


@app.get("/api/me")
def me():
    s = guest()
    if not s:
        return err("Not signed in.", 401)
    return jsonify(_state(s))


@app.get("/api/events")
def my_events():
    """General link: the events inside the guest's dates, to choose from."""
    s = guest()
    if not s or s["event_id"] != 0 or not config.PORTAL:
        return err("Please verify your email first.", 401)
    frm, to = request.args.get("from", ""), request.args.get("to", "")
    if not (DATE_RE.fullmatch(frm) and DATE_RE.fullmatch(to)) or frm > to:
        return err("Please choose the dates (from - to).")
    rows = con().execute(
        "SELECT id, name, event_date FROM events WHERE active=1 AND event_date<>'' "
        "AND event_date BETWEEN ? AND ? ORDER BY event_date DESC, id DESC", (frm, to)).fetchall()
    return jsonify(ok=True, events=[dict(id=r["id"], name=r["name"], date=r["event_date"])
                                    for r in rows])


@app.post("/api/bookmark/<int:pid>")
def bookmark(pid):
    """Save / un-save one of the guest's own photos for later visits."""
    s = guest()
    if not s:
        return err("Session expired. Please sign in again.", 401)
    if not s["email"]:
        return signin_needed()
    c = con()
    on = (request.get_json(silent=True) or {}).get("on") is True
    if on:
        if not allowed(s, pid):
            abort(404)
        c.execute("INSERT OR IGNORE INTO bookmarks(email,photo_id,created_at) VALUES(?,?,?)",
                  (s["email"], pid, time.time()))
    else:
        c.execute("DELETE FROM bookmarks WHERE email=? AND photo_id=?", (s["email"], pid))
    c.commit()
    return jsonify(ok=True, on=on)


@app.get("/api/bookmarks")
def bookmarks():
    """The guest's saved photos, grouped by event - no new selfie needed."""
    s = guest()
    if not s:
        return err("Not signed in.", 401)
    groups, by_event = [], {}
    for r in con().execute(
            "SELECT b.photo_id, e.id AS eid, e.name, e.event_date FROM bookmarks b "
            "JOIN photos p ON p.id=b.photo_id JOIN events e ON e.id=p.event_id "
            "WHERE b.email=? AND e.active=1 ORDER BY e.event_date DESC, e.id DESC, b.created_at",
            (s["email"],)):
        grp = by_event.get(r["eid"])
        if grp is None:
            grp = by_event[r["eid"]] = dict(name=r["name"], date=r["event_date"], photos=[])
            groups.append(grp)
        grp["photos"].append(r["photo_id"])
    return jsonify(ok=True, groups=groups)


@app.post("/api/selfie")
def selfie():
    s = guest()
    if not s:
        return err("Please verify your email first.", 401)
    c = con()
    d = request.get_json(silent=True) or {}
    date_from = date_to = ""
    if s["event_id"] == 0:                  # general link: every open album in the dates
        date_from, date_to = str(d.get("date_from") or ""), str(d.get("date_to") or "")
        if not (DATE_RE.fullmatch(date_from) and DATE_RE.fullmatch(date_to)) or date_from > date_to:
            return err("Please choose the dates of your event (from - to).")
        albums = c.execute(
            "SELECT * FROM events WHERE active=1 AND event_date<>'' AND event_date BETWEEN ? AND ? "
            "ORDER BY event_date DESC", (date_from, date_to)).fetchall() if config.PORTAL else []
        chosen = d.get("events")
        if isinstance(chosen, list):        # the guest ticked some of the events
            albums = [a for a in albums if a["id"] in chosen]
            if not albums:
                return err("Please choose at least one event.")
        if not albums:
            return err("There is no album for these dates. Please check the dates, "
                       "or ask the photographer.", 404)
    else:
        albums = c.execute("SELECT * FROM events WHERE id=? AND active=1",
                           (s["event_id"],)).fetchall()
        if not albums:
            return err("This event is closed.", 404)
    log_event = s["event_id"] or None
    if s["selfie_tries"] >= config.SELFIE_MAX_TRIES:
        # start over cleanly instead of leaving the guest stuck on this step
        c.execute("DELETE FROM matches WHERE session_id=?", (s["id"],))
        c.execute("DELETE FROM sessions WHERE id=?", (s["id"],))
        c.commit()
        r = jsonify(ok=False, error="Too many selfies. Please enter your email again to continue.")
        r.delete_cookie(COOKIE)
        return r, 401
    try:
        raw = base64.b64decode(str(d.get("image", "")).split(",", 1)[-1], validate=True)
    except Exception:
        return err("Could not read the picture.")
    img = faces.decode(raw) if raw else None
    if img is None:
        return err("Could not read the picture.")
    c.execute("UPDATE sessions SET selfie_tries=selfie_tries+1 WHERE id=?", (s["id"],))
    c.commit()
    vecs = faces.get_engine().embed(img)
    if len(vecs) == 0:
        return err("We could not see a face. Use good light and face the camera.")
    if len(vecs) > 1:
        return err("More than one face found. Please take the selfie alone.")
    n_faces, best, found, ask_first = 0, 0.0, [], False
    for ev in albums:
        n, top = indexer.stats(c, ev["id"], vecs[0])
        n_faces, best = n_faces + n, max(best, top)
        hits = indexer.search(c, ev["id"], vecs[0])
        found += hits
        ask_first = ask_first or bool(hits and ev["require_approval"])
    if not n_faces:
        db.audit(c, "selfie_no_album_photos", s["email"], log_event,
                 "the album has no scanned faces yet", ip())
        c.commit()
        return err("This album has no scanned photos yet. Please ask the photographer "
                   "to finish the scan and try again.", 409)
    status = "pending" if ask_first else "matched"
    c.execute("DELETE FROM matches WHERE session_id=?", (s["id"],))
    c.executemany("INSERT INTO matches(session_id,photo_id,score) VALUES(?,?,?)",
                  [(s["id"], pid, sc) for pid, sc in found])
    c.execute("UPDATE sessions SET status=?, date_from=?, date_to=? WHERE id=?",
              (status, date_from, date_to, s["id"]))
    db.audit(c, "selfie_matched", s["email"] or None, log_event,
             f"{len(found)} photos | best score {best:.2f} (needs {config.MATCH_THRESHOLD:.2f}) "
             f"| album has {n_faces} faces"
             + (f" | {len(albums)} albums, {date_from} to {date_to}" if not log_event else ""), ip())
    c.commit()
    if found and status == "matched":
        ids = [pid for pid, _ in found[:60]]
        warm_previews(c.execute(
            "SELECT p.*, e.folders FROM photos p JOIN events e ON e.id=p.event_id "
            f"WHERE p.id IN ({','.join('?' * len(ids))})", ids).fetchall())
    pend = config.SELFIE_DIR / f"{s['id']}.jpg"
    if status == "pending":      # kept only until you approve / reject
        pend.write_bytes(raw)
    else:                        # otherwise the selfie is never stored
        pend.unlink(missing_ok=True)
    return jsonify(_state(guest()))


@app.get("/api/preview/<int:pid>")
def preview(pid):
    s = guest()
    row = allowed(s, pid) if s else None
    if not row:
        abort(404)
    r = send_file(preview_file(row, request.args.get("s") == "t"), mimetype="image/jpeg")
    r.headers["X-Keep"] = "1"               # the browser may keep it for an hour (see _headers)
    return r


def warm_previews(rows):
    """Right after a selfie search: get the first tiles ready in the
    background, so they are there when the browser asks for them."""
    import threading
    todo = [dict(r) for r in rows[:60] if not indexer.preview_path(r, True).exists()]
    if not todo:
        return

    for i in range(3):
        chunk = todo[i::3]
        if chunk:
            threading.Thread(target=lambda c=chunk: [_warm_one(x) for x in c], daemon=True).start()


def _warm_one(row):
    try:
        with app.test_request_context():
            preview_file(row, True)
    except Exception:
        pass


def photo_bytes(row):
    """The original file of one photo, read into memory."""
    if gdrive.is_drive(row["root"]) and not old_copy(row):
        if not row["ref"]:
            raise OSError("no Drive id")
        return gdrive.fetch_full(row["ref"])
    p = old_copy(row) or real_path(row["folders"], row["root"], row["relpath"])
    if p.stat().st_size > config.MAX_PHOTO_BYTES:
        raise OSError("too large")
    return p.read_bytes()


def with_logo(c, row):
    """(bytes, file name) of the photo with the studio logo in the top right
    corner - or None when that is switched off or not possible for this
    file type; the untouched original is then sent."""
    name = Path(row["relpath"]).name
    ext = Path(name).suffix
    if brand.get(c, "stamp") != "1" or ext.lower() not in brand.STAMPABLE \
            or not brand.LOGO.exists():
        return None
    try:
        out = brand.stamp(photo_bytes(row), ext)
    except (OSError, gdrive.DriveError, HTTPException):
        return None
    return (out, Path(name).stem + ".jpg") if out else None


def zip_download(c, s, rows):
    """Several photos as one zip file; every photo goes into the record."""
    tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for r in rows:
            if r["root"] not in db.split_folders(r["folders"]):
                continue
            name = Path(r["relpath"]).name
            marked = with_logo(c, r)
            if marked:
                n += 1
                z.writestr(f"{n:04d}_{marked[1]}", marked[0])
            elif old_copy(r):
                n += 1
                z.write(old_copy(r), f"{n:04d}_{name}")
            elif gdrive.is_drive(r["root"]):         # straight from Drive into the zip
                try:
                    src = gdrive.open_original(r["ref"])
                except gdrive.DriveError:
                    continue
                n += 1
                with src, z.open(f"{n:04d}_{name}", "w", force_zip64=True) as dst:
                    while True:
                        chunk = src.read(1024 * 1024)
                        if not chunk:
                            break
                        dst.write(chunk)
            else:
                base = Path(r["root"])
                p = base / r["relpath"]
                if not (p.is_file() and base.resolve() in p.resolve().parents):
                    continue                         # photos on an offline drive are left out
                n += 1
                z.write(p, f"{n:04d}_{name}")
            record_download(c, s, r)
    if not n:
        abort(404)
    tmp.seek(0)
    db.audit(c, "download_all", s["email"], s["event_id"] or None, f"{n} photos", ip())
    c.commit()
    return send_file(tmp, mimetype="application/zip", as_attachment=True,
                     download_name="my-photos.zip")


@app.post("/api/download-link/<what>")
def download_link(what):
    s = guest()
    if not s:
        return err("Session expired. Please verify again.", 401)
    if not s["email"]:
        return signin_needed()
    if what == "selected":                  # the photos the guest ticked
        ids = (request.get_json(silent=True) or {}).get("ids")
        if not isinstance(ids, list) or not ids or len(ids) > 2000 \
                or not all(isinstance(i, int) and allowed(s, i) for i in ids):
            abort(404)
        c = con()
        db.kv_set(c, f"sel:{s['id']}", json.dumps(ids))
        c.commit()
    elif what == "saved":
        pass
    elif what != "all":
        if not what.isdigit() or not allowed(s, int(what)):
            abort(404)
    elif s["status"] not in ("matched", "approved"):
        abort(404)
    token = signer.dumps({"s": s["id"], "p": what})
    return jsonify(ok=True, url=url_for("download", token=token))


@app.get("/api/download/<token>")
def download(token):
    s = guest()
    try:
        data = signer.loads(token, max_age=config.DOWNLOAD_LINK_TTL)
    except BadSignature:
        abort(403)
    # The link works only in the browser that passed the email OTP.
    if not s or s["id"] != data.get("s") or not s["email"]:
        abort(403)
    c = con()
    if data["p"] == "selected":
        ids = json.loads(db.kv_get(c, f"sel:{s['id']}") or "[]")
        rows = [r for r in (allowed(s, i) for i in ids) if r]
        if not rows:
            abort(404)
        return zip_download(c, s, rows)
    if data["p"] == "saved":
        rows = c.execute(
            "SELECT p.*, e.folders FROM bookmarks b JOIN photos p ON p.id=b.photo_id "
            "JOIN events e ON e.id=p.event_id WHERE b.email=? AND e.active=1",
            (s["email"],)).fetchall()
        if not rows:
            abort(404)
        return zip_download(c, s, rows)
    if data["p"] == "all":
        if s["status"] not in ("matched", "approved"):
            abort(403)
        rows = c.execute(
            "SELECT p.*, e.folders FROM matches m JOIN photos p ON p.id=m.photo_id "
            "JOIN events e ON e.id=p.event_id WHERE m.session_id=?", (s["id"],)).fetchall()
        if not rows:
            abort(404)
        return zip_download(c, s, rows)
    row = allowed(s, int(data["p"]))
    if not row:
        abort(403)
    if row["root"] not in db.split_folders(row["folders"]):
        abort(404)
    db.audit(c, "download", s["email"], s["event_id"] or None, row["relpath"], ip())
    record_download(c, s, row)
    c.commit()
    marked = with_logo(c, row)
    if marked:
        return send_file(io.BytesIO(marked[0]), mimetype="image/jpeg", as_attachment=True,
                         download_name=marked[1])
    if gdrive.is_drive(row["root"]) and row["ref"]:
        return drive_download(row["ref"], Path(row["relpath"]).name)
    if old_copy(row):
        return send_file(old_copy(row), as_attachment=True, download_name=Path(row["relpath"]).name)
    p = real_path(row["folders"], row["root"], row["relpath"])
    return send_file(p, as_attachment=True, download_name=p.name)


@app.post("/api/forget")
def forget():
    """Guest signs out; their matches and any stored selfie are erased."""
    s = guest()
    if s:
        c = con()
        c.execute("DELETE FROM matches WHERE session_id=?", (s["id"],))
        c.execute("DELETE FROM sessions WHERE id=?", (s["id"],))
        db.audit(c, "forgot", s["email"], s["event_id"], ip=ip())
        c.commit()
        (config.SELFIE_DIR / f"{s['id']}.jpg").unlink(missing_ok=True)
    r = jsonify(ok=True)
    r.delete_cookie(COOKIE)
    return r


# ------------------------------------------------------------------ admin
def is_admin():
    if session.get("admin") is not True:
        return False
    who = session.get("staff")
    if who:                                 # a staff account must still be active
        row = con().execute("SELECT active FROM staff WHERE username=?", (who,)).fetchone()
        return bool(row and row["active"])
    return True


def is_owner():
    """The owner signs in with the main password; staff can only manage albums."""
    return is_admin() and not session.get("staff")


def admin_post_ok():
    return is_admin() and hmac.compare_digest(
        request.form.get("csrf", ""), session.get("csrf", "x"))


def owner_post_ok():
    return admin_post_ok() and is_owner()


def who():
    return session.get("staff") or "owner"


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    msg = ""
    if not config.ADMIN_PASSWORD:
        msg = "Set ADMIN_PASSWORD in the .env file and restart."
    elif request.method == "POST":
        c = con()
        fails = c.execute(
            "SELECT COUNT(*) FROM audit WHERE action='admin_login_fail' AND ip=? AND ts>?",
            (ip(), time.time() - 900)).fetchone()[0]
        user = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        staff = None
        if fails >= 5:
            msg = "Too many attempts. Wait 15 minutes."
        elif user in ("", "admin", "owner"):
            ok = hmac.compare_digest(password.encode(), config.ADMIN_PASSWORD.encode())
        else:
            staff = c.execute("SELECT * FROM staff WHERE username=? AND active=1",
                              (user,)).fetchone()
            ok = bool(staff) and brand.check_password(password, staff["salt"], staff["pw_hash"])
        if not msg and ok:
            session.clear()
            session.permanent = True
            session["admin"] = True
            session["csrf"] = secrets.token_hex(16)
            if staff:
                session["staff"] = staff["username"]
            db.audit(c, "admin_login", detail=who(), ip=ip())
            c.commit()
            return redirect(url_for("admin"))
        if not msg:
            db.audit(c, "admin_login_fail", detail=user, ip=ip())
            c.commit()
            msg = "Wrong user name or password."
    return render_template("admin_login.html", msg=msg)


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


def fmt_time(t):
    return time.strftime("%d %b %Y %H:%M", time.localtime(t)) if t else "-"


_dir_cache = {}                       # folder -> (when, exists)
_dir_pool = ThreadPoolExecutor(8)


def fast_isdir(path, wait=1.5, keep=30):
    """Is this folder there? A disconnected or slow drive can make the operating
    system wait for many seconds, so the answer is asked for in the background,
    waited for only briefly, and remembered for half a minute."""
    now = time.time()
    hit = _dir_cache.get(path)
    if hit and now - hit[0] < keep:
        return hit[1]
    fut = _dir_pool.submit(lambda: Path(path).is_dir())
    try:
        ok = fut.result(timeout=wait)
    except Exception:
        return bool(hit and hit[1])       # no answer yet: keep what we knew
    _dir_cache[path] = (now, ok)
    return ok


_TITLES = dict(albums="Albums", guests="Guests", guest="Guest", brand="Look", team="Staff",
               settings="Settings", speed="Speed check")


def admin_page(page, **kw):
    layout = "admin/_bare.html" if request.headers.get("X-PJAX") else "admin/base.html"
    return render_template(
        f"admin/{page}.html", layout=layout, page=page, title=_TITLES.get(page, "Admin"),
        csrf=session["csrf"], owner=is_owner(), who=who(), pending_n=pending_count(),
        msg=request.args.get("msg", ""), fmt=fmt_time, today=time.strftime("%Y-%m-%d"), **kw)


_pend = [0, 0]


def pending_count():
    """People waiting for approval (shown on the Albums menu); asked at most every 5 s."""
    if time.time() - _pend[0] > 5:
        _pend[1] = con().execute("SELECT COUNT(*) FROM sessions WHERE status='pending' AND expires_at>?",
                                 (time.time(),)).fetchone()[0]
        _pend[0] = time.time()
    return _pend[1]


_tot = {"t": 0, "v": None}


def cached_totals(c):
    """Album, photo, guest and download totals; recounted at most every 10 s."""
    if time.time() - _tot["t"] > 10 or _tot["v"] is None:
        _tot["v"] = dict(albums=c.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                         photos=c.execute("SELECT COUNT(*) FROM photos").fetchone()[0],
                         guests=c.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                         downloads=c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0])
        _tot["t"] = time.time()
    return _tot["v"]


def paging(total, per):
    """Current page number, number of pages, row offset."""
    pages = max(1, -(-total // per))
    page = min(pages, max(1, request.args.get("page", 1, type=int) or 1))
    return page, pages, (page - 1) * per


ALBUMS_PER_PAGE, GUESTS_PER_PAGE = 20, 30


@app.get("/admin")
def admin():
    """Albums: search, 20 per page - works the same with ten albums or lakhs."""
    if not is_admin():
        return redirect(url_for("admin_login"))
    c = con()
    q = request.args.get("q", "").strip()[:80]
    show = request.args.get("show", "")
    where, args = [], []
    if q:
        where.append("(name LIKE ? OR event_date LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if show in ("open", "closed"):
        where.append("active=?")
        args.append(1 if show == "open" else 0)
    sql = (" WHERE " + " AND ".join(where)) if where else ""
    total = c.execute("SELECT COUNT(*) FROM events" + sql, args).fetchone()[0]
    page, pages, offset = paging(total, ALBUMS_PER_PAGE)
    rows = c.execute("SELECT * FROM events" + sql + " ORDER BY id DESC LIMIT ? OFFSET ?",
                     args + [ALBUMS_PER_PAGE, offset]).fetchall()
    ids = [r["id"] for r in rows]
    mark = ",".join("?" * len(ids))
    n_ph = dict(c.execute(f"SELECT event_id, COUNT(*) FROM photos WHERE event_id IN ({mark}) GROUP BY event_id", ids).fetchall()) if ids else {}
    n_fc = dict(c.execute(f"SELECT event_id, COUNT(*) FROM faces WHERE event_id IN ({mark}) GROUP BY event_id", ids).fetchall()) if ids else {}
    n_er = dict(c.execute(f"SELECT event_id, COUNT(*) FROM scan_errors WHERE event_id IN ({mark}) GROUP BY event_id", ids).fetchall()) if ids else {}
    names = dict((k[11:], v) for k, v in c.execute("SELECT k, v FROM kv WHERE k LIKE 'drive_name:%'").fetchall())
    events = []
    for e in rows:
        e = dict(e)
        e["photos"], e["faces"], e["n_errors"] = n_ph.get(e["id"], 0), n_fc.get(e["id"], 0), n_er.get(e["id"], 0)
        e["status"], e["state"] = status_text(e["id"], e["photos"], e["faces"])
        e["folder_list"] = [(f"Google Drive: {names.get(f[7:]) or f[7:]}" if gdrive.is_drive(f) else f,
                             gdrive.is_drive(f) or fast_isdir(f))
                            for f in db.split_folders(e["folders"])]
        e["link"] = guest_link(e["code"])
        e["errors"] = c.execute("SELECT relpath, reason FROM scan_errors WHERE event_id=? LIMIT 200",
                                (e["id"],)).fetchall() if e["n_errors"] else []
        e["live"] = live_numbers(e["id"])
        e["fresh"] = time.time() - e["created_at"] < 20
        events.append(e)
    pending = c.execute(
        "SELECT s.id, s.email, s.created_at, s.date_from, s.date_to, "
        "COALESCE(e.name, 'General link') AS name, "
        "(SELECT COUNT(*) FROM matches m WHERE m.session_id=s.id) AS n "
        "FROM sessions s LEFT JOIN events e ON e.id=s.event_id "
        "WHERE s.status='pending' AND s.expires_at>? ORDER BY s.id LIMIT 50", (time.time(),)).fetchall()
    totals = cached_totals(c)
    return admin_page("albums", events=events, pending=pending, totals=totals, q=q, show=show,
                      total=total, page_no=page, pages=pages,
                      portal_link=guest_link("") if config.PORTAL else "",
                      drive_email=gdrive.robot_email())


def _range():
    """The report's date filter -> (from text, to text, sql, args) on downloads.ts."""
    frm, to = request.args.get("from", ""), request.args.get("to", "")
    sql, args = "", []
    if DATE_RE.fullmatch(frm):
        sql += " AND d.ts>=?"
        args.append(time.mktime(time.strptime(frm, "%Y-%m-%d")))
    else:
        frm = ""
    if DATE_RE.fullmatch(to):
        sql += " AND d.ts<?"
        args.append(time.mktime(time.strptime(to, "%Y-%m-%d")) + 86400)
    else:
        to = ""
    return frm, to, sql, args


@app.get("/admin/guests")
def admin_guests():
    """Download report: totals, busiest albums, and the guests - searchable,
    filtered by date, in pages. Built to stay quick with lakhs of rows."""
    if not is_owner():
        return redirect(url_for("admin"))
    c = con()
    q = request.args.get("q", "").strip()[:80]
    frm, to, dsql, dargs = _range()
    only = request.args.get("only", "")
    where, args = [], []
    if q:
        where.append("(u.name LIKE ? OR u.phone LIKE ? OR u.email LIKE ?)")
        args += [f"%{q}%"] * 3
    if dsql or only == "downloaded":      # guests who downloaded (in these dates)
        where.append("EXISTS(SELECT 1 FROM downloads d WHERE d.email=u.email" + dsql + ")")
        args += dargs
    wsql = (" WHERE " + " AND ".join(where)) if where else ""
    total = c.execute("SELECT COUNT(*) FROM users u" + wsql, args).fetchone()[0]
    page, pages, offset = paging(total, GUESTS_PER_PAGE)
    people = c.execute(
        "SELECT u.email, u.name, u.phone, u.last_login, "
        "(SELECT COUNT(DISTINCT photo_id) FROM downloads d WHERE d.email=u.email" + dsql + ") AS n, "
        "(SELECT MAX(ts) FROM downloads d WHERE d.email=u.email" + dsql + ") AS last_dl, "
        "(SELECT COUNT(*) FROM bookmarks b WHERE b.email=u.email) AS saved "
        "FROM users u" + wsql + " ORDER BY u.last_login DESC LIMIT ? OFFSET ?",
        dargs + dargs + args + [GUESTS_PER_PAGE, offset]).fetchall()
    now = time.time()
    day = time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d"))
    count = lambda sql, a=(): c.execute(sql, a).fetchone()[0]      # noqa: E731
    totals = dict(
        today=count("SELECT COUNT(*) FROM downloads WHERE ts>=?", (day,)),
        week=count("SELECT COUNT(*) FROM downloads WHERE ts>=?", (now - 7 * 86400,)),
        all=count("SELECT COUNT(*) FROM downloads"),
        ranged=count("SELECT COUNT(*) FROM downloads d WHERE 1=1" + dsql, dargs),
        guests=count("SELECT COUNT(*) FROM users"),
        takers=count("SELECT COUNT(DISTINCT email) FROM downloads d WHERE 1=1" + dsql, dargs))
    top = c.execute(
        "SELECT COALESCE(e.name, 'Deleted album') AS name, COUNT(*) AS n, "
        "COUNT(DISTINCT d.email) AS people, MAX(d.ts) AS last FROM downloads d "
        "LEFT JOIN events e ON e.id=d.event_id WHERE 1=1" + dsql +
        " GROUP BY d.event_id ORDER BY n DESC LIMIT 10", dargs).fetchall()
    return admin_page("guests", people=people, q=q, total=total, page_no=page, pages=pages,
                      totals=totals, top=top, frm=frm, to=to, only=only)


@app.get("/admin/guest")
def admin_guest():
    """One guest: every photo they downloaded, 50 per page."""
    if not is_owner():
        return redirect(url_for("admin"))
    c = con()
    email = request.args.get("email", "")
    u = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    if not u:
        return redirect(url_for("admin_guests", msg="That guest was not found."))
    total = c.execute("SELECT COUNT(*) FROM downloads WHERE email=?", (email,)).fetchone()[0]
    page, pages, offset = paging(total, 50)
    files = c.execute(
        "SELECT d.ts, d.relpath, d.ip, COALESCE(e.name, 'Deleted album') AS album FROM downloads d "
        "LEFT JOIN events e ON e.id=d.event_id WHERE d.email=? ORDER BY d.id DESC LIMIT 50 OFFSET ?",
        (email, offset)).fetchall()
    albums = c.execute(
        "SELECT COALESCE(e.name, 'Deleted album') AS name, COUNT(*) AS n FROM downloads d "
        "LEFT JOIN events e ON e.id=d.event_id WHERE d.email=? GROUP BY d.event_id ORDER BY n DESC",
        (email,)).fetchall()
    saved = c.execute("SELECT COUNT(*) FROM bookmarks WHERE email=?", (email,)).fetchone()[0]
    return admin_page("guest", u=u, files=files, albums=albums, saved=saved, total=total,
                      page_no=page, pages=pages, q=email)


@app.get("/admin/brand")
def admin_brand():
    if not is_owner():
        return redirect(url_for("admin"))
    c = con()
    rows = [dict(r, image=brand.promo_image(r["id"]).exists())
            for r in c.execute("SELECT * FROM promos ORDER BY id DESC LIMIT 100")]
    return admin_page("brand", promo_rows=rows, accent=brand.get(c, "accent"),
                      tagline=brand.get(c, "tagline"), stamp=brand.get(c, "stamp") == "1")


@app.post("/admin/brand")
def admin_brand_save():
    if not owner_post_ok():
        abort(403)
    c, f, notes = con(), request.form, []
    name = " ".join(f.get("brand", "").split())[:60]
    if name:
        brand.put(c, "brand", name)
        config.BRAND = name
    brand.put(c, "tagline", " ".join(f.get("tagline", "").split())[:140] or None)
    if brand.COLOUR_RE.fullmatch(f.get("accent", "")):
        brand.put(c, "accent", f["accent"])
    brand.put(c, "stamp", "1" if f.get("stamp") else "0")
    for field, dest, side in (("logo", brand.LOGO, 600), ("background", brand.BACKGROUND, 2400)):
        if f.get("remove_" + field):
            dest.unlink(missing_ok=True)
        up = request.files.get(field)
        if up and up.filename:
            try:
                brand.save_image(up, dest, side)
            except ValueError as e:
                notes.append(f"{field.capitalize()}: {e}")
    if f.get("stamp") and not brand.LOGO.exists():
        notes.append("Upload a logo first - without one, downloads are sent unchanged.")
    db.audit(c, "branding_changed", detail=who(), ip=ip())
    c.commit()
    if brand.version() != config.PREVIEW_TAG:       # logo changed: previews are made again
        config.PREVIEW_TAG = brand.version()

        def clear():
            for f in config.CACHE_DIR.glob("*.jpg"):
                try:
                    f.unlink()
                except OSError:
                    pass
        import threading
        threading.Thread(target=clear, daemon=True).start()
    return redirect(url_for("admin_brand", msg=" ".join(notes) or "Saved. Guests see the new look now."))


@app.post("/admin/promos")
def admin_promo_create():
    if not owner_post_ok():
        abort(403)
    c, f = con(), request.form
    title = " ".join(f.get("title", "").split())[:90]
    link = f.get("link", "").strip()[:300]
    date = f.get("event_date", "").strip()
    if not title:
        return redirect(url_for("admin_brand", msg="Not added. Type a title."))
    if link and not re.match(r"https?://", link):
        return redirect(url_for("admin_brand", msg="Not added. The link must start with https://"))
    pid = c.execute(
        "INSERT INTO promos(kind,title,body,link,event_date,show_before,show_after,created_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        ("event" if f.get("kind") == "event" else "ad", title, f.get("body", "").strip()[:400],
         link, date if DATE_RE.fullmatch(date) else "", 1 if f.get("show_before") else 0,
         1 if f.get("show_after") else 0, time.time())).lastrowid
    c.commit()
    up, note = request.files.get("image"), ""
    if up and up.filename:
        try:
            brand.save_image(up, brand.promo_image(pid), 1400)
        except ValueError as e:
            note = f" Picture not used: {e}"
    return redirect(url_for("admin_brand", msg="Added." + note))


@app.post("/admin/promos/<int:pid>/<action>")
def admin_promo_action(pid, action):
    if not owner_post_ok():
        abort(403)
    c = con()
    if action == "toggle":
        c.execute("UPDATE promos SET active=1-active WHERE id=?", (pid,))
    elif action == "delete":
        c.execute("DELETE FROM promos WHERE id=?", (pid,))
        brand.promo_image(pid).unlink(missing_ok=True)
    else:
        abort(404)
    c.commit()
    return redirect(url_for("admin_brand"))


@app.get("/admin/team")
def admin_team():
    if not is_owner():
        return redirect(url_for("admin"))
    return admin_page("team", staff=con().execute("SELECT * FROM staff ORDER BY username").fetchall())


@app.post("/admin/team")
def admin_team_create():
    if not owner_post_ok():
        abort(403)
    user = request.form.get("username", "").strip().lower()
    password = request.form.get("password", "")
    if not re.fullmatch(r"[a-z0-9._-]{3,30}", user) or user in ("admin", "owner"):
        return redirect(url_for("admin_team", msg="Not added. User name: 3-30 letters, digits, dot, dash."))
    if len(password) < 8:
        return redirect(url_for("admin_team", msg="Not added. The password needs at least 8 characters."))
    c = con()
    if c.execute("SELECT 1 FROM staff WHERE username=?", (user,)).fetchone():
        return redirect(url_for("admin_team", msg="Not added. That user name is already taken."))
    salt, digest = brand.hash_password(password)
    c.execute("INSERT INTO staff(username,salt,pw_hash,created_at) VALUES(?,?,?,?)",
              (user, salt, digest, time.time()))
    db.audit(c, "staff_added", detail=user, ip=ip())
    c.commit()
    return redirect(url_for("admin_team", msg=f"{user} can now sign in and manage albums."))


@app.post("/admin/team/<int:sid>/<action>")
def admin_team_action(sid, action):
    if not owner_post_ok():
        abort(403)
    c, msg = con(), ""
    if action == "toggle":
        c.execute("UPDATE staff SET active=1-active WHERE id=?", (sid,))
    elif action == "delete":
        c.execute("DELETE FROM staff WHERE id=?", (sid,))
    elif action == "password":
        password = request.form.get("password", "")
        if len(password) < 8:
            msg = "Not changed. The password needs at least 8 characters."
        else:
            salt, digest = brand.hash_password(password)
            c.execute("UPDATE staff SET salt=?, pw_hash=? WHERE id=?", (salt, digest, sid))
            msg = "Password changed."
    else:
        abort(404)
    db.audit(c, "staff_" + action, detail=str(sid), ip=ip())
    c.commit()
    return redirect(url_for("admin_team", msg=msg))


@app.get("/admin/speed")
def admin_speed_page():
    if not is_owner():
        return redirect(url_for("admin"))
    return admin_page("speed")


@app.get("/admin/settings")
def admin_settings_page():
    if not is_owner():
        return redirect(url_for("admin"))
    c = con()
    wrong = [float(r[0].split(" | ")[0]) for r in c.execute(
        "SELECT detail FROM audit WHERE action='not_me' ORDER BY id DESC LIMIT 500")
        if r[0][:4].replace(".", "").isdigit()]
    reported = dict(n=len(wrong), low=min(wrong), high=max(wrong),
                    suggest=min(0.80, max(wrong) + 0.01)) if wrong else None
    return admin_page("settings", reported=reported, look_first=brand.get(c, "look_first") == "1",
                      threshold=f"{config.MATCH_THRESHOLD:.2f}",
                      scan_side=config.SCAN_SIDE, drive_email=gdrive.robot_email(),
                      auto=autoalbums.settings(c), auto_state=autoalbums.state,
                      log=c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 200").fetchall())


def status_text(eid, photos, faces):
    """Plain-words scan status for the admin page -> (sentence, state)."""
    p = indexer.progress.get(eid) or {}
    err_text = p.get("error", "")
    if "ONNX" in err_text or "Face models" in err_text:
        err_text = ("The face model files are damaged. Close the black window and "
                    "run start.bat again - it repairs them by itself.")
    if p.get("state") == "running":
        if p.get("phase"):
            return p["phase"], "running"
        done, total = p.get("done", 0), p.get("total", 0)
        if not total:
            return "Looking for photos ...", "running"
        if p.get("listing"):
            text = f"Scanning ... {done} photos done, {total} found so far (still looking for more)"
        else:
            text = f"Scanning ... {done} of {total} photos"
        took = time.time() - p.get("started", 0)
        if p.get("scanned", 0) >= 10 and took > 0:
            rate = p["scanned"] / took
            text += f" - {rate:.1f} photos per second"
            if not p.get("listing"):
                left = (total - done) / rate / 60
                text += (", about " + (f"{left / 60:.1f} hours" if left >= 90
                                       else f"{max(1, round(left))} minutes") + " left")
        if done:
            text += ". Photos already scanned can be found now."
        return (text + " " + indexer.timing(p)).strip(), "running"
    if p.get("state") == "failed":
        return "Scan failed. " + err_text, "failed"
    if not photos:
        return "No photos scanned yet. " + err_text, "empty"
    note = f" Note: {err_text}" if err_text else ""
    last = ""
    if p.get("state") == "finished" and p.get("scanned"):
        mins = p.get("took", 0) / 60
        last = (f" Last scan: {p['scanned']} photos in "
                + (f"{mins:.0f} minutes" if mins >= 2 else f"{p.get('took', 0):.0f} seconds")
                + f". {indexer.timing(p)}")
    return f"Ready - {photos} photos, {faces} faces found.{last}{note}", "ready"


def live_numbers(eid):
    """Numbers behind the progress bar of an album that is being scanned."""
    p = indexer.progress.get(eid) or {}
    if p.get("state") != "running":
        return dict(running=False)
    done, total = p.get("done", 0), p.get("total", 0)
    took = max(1e-6, time.time() - p.get("started", time.time()))
    rate = p.get("scanned", 0) / took if p.get("scanned", 0) >= 5 else 0
    return dict(running=True, done=done, total=total, listing=bool(p.get("listing")),
                pct=round(100 * done / total, 1) if total else 0,
                rate=round(rate, 1),
                eta=int((total - done) / rate) if rate and total and not p.get("listing") else None)


def _drive_kind(path):
    """Windows: what kind of disk holds this folder (a network or removable drive
    is a common reason for a slow or damaged database)."""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        root = os.path.splitdrive(os.path.abspath(path))[0] + "\\"
        kinds = {2: "removable (USB)", 3: "internal disk", 4: "NETWORK drive", 5: "CD/DVD", 6: "RAM disk"}
        return kinds.get(ctypes.windll.kernel32.GetDriveTypeW(root), "unknown")
    except Exception:
        return ""


def speed_report():
    """Measures the things that make an admin page slow on THIS computer, one
    by one, and says which one it is."""
    out, tips = {}, []
    # 1. the database: opening it and reading
    t = []
    for _ in range(20):
        t0 = time.time()
        c = db.connect(); c.execute("SELECT COUNT(*) FROM events").fetchone(); c.close()
        t.append((time.time() - t0) * 1000)
    out["db_open_read_ms"] = round(sum(t) / len(t), 1)
    # 2. saving: one small commit on a scratch file next to the database (what a scan does per batch)
    scratch = config.DATA_DIR / "speedtest.db"
    try:
        import sqlite3
        k = sqlite3.connect(scratch, timeout=10)
        k.execute("CREATE TABLE IF NOT EXISTS t(a)")
        t = []
        for i in range(10):
            t0 = time.time(); k.execute("INSERT INTO t VALUES(?)", (i,)); k.commit(); t.append((time.time() - t0) * 1000)
        k.close(); scratch.unlink(missing_ok=True)
        out["db_commit_ms"] = round(sum(t) / len(t), 1)
    except Exception as e:
        out["db_commit_ms"] = None
    # 3. the disk: write and read 16 MB
    blob = os.urandom(16 * 1024 * 1024); f = config.DATA_DIR / "speedtest.bin"
    try:
        t0 = time.time()
        with open(f, "wb") as fh:
            fh.write(blob); fh.flush(); os.fsync(fh.fileno())
        out["disk_write_mbs"] = round(16 / max(1e-3, time.time() - t0), 1)
        t0 = time.time(); f.read_bytes()
        out["disk_read_mbs"] = round(16 / max(1e-3, time.time() - t0), 1)
    finally:
        f.unlink(missing_ok=True)
    try:
        du = shutil.disk_usage(config.DATA_DIR)
        out["disk_free_gb"] = round(du.free / 1e9, 1)
    except OSError:
        out["disk_free_gb"] = None
    out["db_size_mb"] = round(config.DB_PATH.stat().st_size / 1e6, 1) if config.DB_PATH.exists() else 0
    out["db_disk"] = _drive_kind(config.DATA_DIR)
    c = con()
    out["journal"] = c.execute("PRAGMA journal_mode").fetchone()[0]
    # 4. this computer's processor: is something else using it?
    t0 = time.time(); sum(i * i for i in range(400000)); out["cpu_burn_ms"] = round((time.time() - t0) * 1000)
    out["cpus"] = os.cpu_count()
    out["scans_running"] = sum(1 for p in indexer.progress.values() if p.get("state") == "running")
    # 5. every album folder: how long it takes to answer
    folders = []
    for r in c.execute("SELECT name, folders FROM events ORDER BY id DESC LIMIT 60"):
        for fo in db.split_folders(r["folders"]):
            if not gdrive.is_drive(fo):
                folders.append((r["name"], fo))
    slow = []
    def probe(item):
        t0 = time.time()
        try:
            ok = Path(item[1]).is_dir()
            if ok:
                next(os.scandir(item[1]), None)
        except OSError:
            ok = False
        return item, ok, (time.time() - t0) * 1000
    with ThreadPoolExecutor(8) as pool:
        for item, ok, ms in pool.map(probe, folders[:60]):
            if ms > 150 or not ok:
                slow.append(dict(album=item[0], folder=item[1], ok=ok, ms=round(ms)))
    out["folders_checked"], out["folders_slow"] = len(folders[:60]), slow
    # 6. Google Drive
    if gdrive.key_info():
        try:
            t0 = time.time(); gdrive.token(); out["drive_token_ms"] = round((time.time() - t0) * 1000)
        except Exception as e:
            out["drive_token_ms"] = None
    # what the app itself measured for real requests
    by = collections.defaultdict(list)
    for _, name, ms in list(_timings):
        by[name].append(ms)
    rows = []
    for name, v in by.items():
        v.sort()
        rows.append(dict(route=name, n=len(v), avg=round(sum(v) / len(v)), p95=round(v[int(len(v) * .95) - 1] if len(v) > 1 else v[0]), worst=round(v[-1])))
    out["routes"] = sorted(rows, key=lambda r: -r["p95"])[:12]
    # verdict, in plain words
    if out["db_commit_ms"] and out["db_commit_ms"] > 40:
        tips.append(f"Saving to the database takes {out['db_commit_ms']:.0f} ms. That is a slow disk. Put DATA_DIR on an internal SSD (not USB, network or a synced folder).")
    if out["db_disk"] in ("NETWORK drive", "removable (USB)"):
        tips.append(f"The data folder is on a {out['db_disk']}. Move it to an internal disk: DATA_DIR=C:\\facefind-data in .env.")
    if out["disk_write_mbs"] and out["disk_write_mbs"] < 40:
        tips.append(f"The disk writes only {out['disk_write_mbs']} MB/s - slow for previews and thumbnails.")
    if out["cpu_burn_ms"] > 120:
        tips.append(f"The processor is busy (a simple test took {out['cpu_burn_ms']} ms, normally under 60). Other programs, antivirus or a running scan are using it.")
    for sl in slow:
        tips.append(("Folder not reachable: " if not sl["ok"] else f"Folder is slow to answer ({sl['ms']} ms): ") + sl["folder"] + f"  (album {sl['album']})")
    if (out.get("drive_token_ms") or 0) > 1500:
        tips.append(f"Google took {out['drive_token_ms']} ms to answer. The internet line is slow.")
    if not tips:
        tips.append("This computer looks fine. If pages still feel slow, the delay is on the way to the browser (internet line or tunnel). The page time shown above splits server time from network time.")
    out["tips"] = tips
    return out


@app.get("/admin/speed/run")
def admin_speed_run():
    if not is_owner():
        abort(403)
    return jsonify(speed_report())


@app.get("/admin/ping")
def admin_ping():
    """Answers at once; the browser times it to separate network from server."""
    if not is_admin():
        abort(403)
    return jsonify(ok=True)


@app.get("/admin/status")
def admin_status():
    """Live scan status of the albums on the page (ids=1,2,3)."""
    if not is_admin():
        abort(403)
    c, out = con(), {}
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.isdigit()][:50]
    if not ids:
        ids = [r[0] for r in c.execute("SELECT id FROM events ORDER BY id DESC LIMIT 50")]
    mark = ",".join("?" * len(ids))
    n_ph = dict(c.execute(f"SELECT event_id, COUNT(*) FROM photos WHERE event_id IN ({mark}) GROUP BY event_id", ids).fetchall())
    n_fc = dict(c.execute(f"SELECT event_id, COUNT(*) FROM faces WHERE event_id IN ({mark}) GROUP BY event_id", ids).fetchall())
    for eid in ids:
        n, f = n_ph.get(eid, 0), n_fc.get(eid, 0)
        text, state = status_text(eid, n, f)
        out[eid] = dict(text=text, state=state, photos=n, faces=f, **live_numbers(eid))
    return jsonify(out)


@app.get("/admin/downloads.csv")
def admin_downloads_csv():
    """The download record as a file that opens in Excel. Sent row by row, so
    it works with lakhs of downloads; ?from=&to= limits the dates."""
    if not is_owner():
        abort(403)
    import csv
    frm, to, dsql, dargs = _range()
    email = request.args.get("email", "")
    if email:
        dsql += " AND d.email=?"
        dargs = dargs + [email]

    def rows():
        c = db.connect()                    # own connection: this runs after the request ends
        out = io.StringIO()
        w = csv.writer(out)
        try:
            w.writerow(["Time", "Name", "Phone", "Email", "Album", "Photo", "IP"])
            for r in c.execute(
                    "SELECT d.ts, u.name, u.phone, d.email, COALESCE(e.name, '') AS album, "
                    "d.relpath, d.ip FROM downloads d LEFT JOIN users u ON u.email=d.email "
                    "LEFT JOIN events e ON e.id=d.event_id WHERE 1=1" + dsql + " ORDER BY d.id DESC",
                    dargs):
                row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["name"] or "",
                       r["phone"] or "", r["email"], r["album"], r["relpath"], r["ip"]]
                # a cell that starts with = + - @ would be run as a formula by Excel
                w.writerow([("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in row])
                if out.tell() > 64 * 1024:
                    yield out.getvalue()
                    out.seek(0)
                    out.truncate()
            yield out.getvalue()
        finally:
            c.close()
    return app.response_class(
        _prefixed(rows()),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=facefind-downloads.csv"})


def _prefixed(chunks):
    yield "\ufeff"                           # tells Excel the file is UTF-8
    yield from chunks


@app.post("/admin/settings")
def admin_settings():
    if not owner_post_ok():
        abort(403)
    back = lambda m: redirect(url_for("admin_settings_page", msg=m))     # noqa: E731
    c = con()
    if request.form.get("what") == "look_first":
        brand.put(c, "look_first", "1" if request.form.get("look_first") else "0")
        c.commit()
        return back("Saved.")
    if request.form.get("what") == "auto":
        roots, bad = [], []
        for line in db.split_folders(request.form.get("roots", "")):
            e = autoalbums.add_root(line)
            (roots if e else bad).append(e or line)
        try:
            mins = max(1, min(240, int(request.form.get("minutes", "3"))))
        except ValueError:
            mins = 3
        on = bool(request.form.get("on")) and bool(roots)
        db.kv_set(c, "auto:on", "1" if on else "0")
        db.kv_set(c, "auto:roots", "\n".join(roots))
        db.kv_set(c, "auto:minutes", str(mins))
        db.audit(c, "auto_albums_saved", detail=f"{'on' if on else 'off'} | {len(roots)} folders | by {who()}", ip=ip())
        c.commit()
        made = autoalbums.run_once() if on and request.form.get("now") else 0
        msg = ("Automatic albums are ON." if on else "Automatic albums are OFF.")
        if made:
            msg += f" {made} new album{'s' if made != 1 else ''} created and being scanned."
        if bad:
            msg += " Not found: " + " | ".join(bad)
        return back(msg)
    side = request.form.get("scan_side", "")
    if side:
        if side not in ("1280", "1600", "2000"):
            return back("Not saved.")
        brand.put(c, "scan_side", side)
        config.SCAN_SIDE = int(side)
        config.DRIVE_SCAN_SIDE = max(1600, config.SCAN_SIDE)
        c.commit()
        return back("Scan detail saved. It is used from the next scan on.")
    try:
        v = float(request.form.get("threshold", ""))
        assert 0.30 <= v <= 0.80
    except (ValueError, AssertionError):
        return back("Not saved. Strictness must be between 0.30 and 0.80.")
    db.kv_set(c, "match_threshold", f"{v:.3f}")
    db.audit(c, "strictness_changed", detail=f"{v:.2f}", ip=ip())
    c.commit()
    config.MATCH_THRESHOLD = v
    return back(f"Match strictness is now {v:.2f}. It applies to the next selfie - "
                "no new scan is needed.")


@app.get("/admin/browse")
def admin_browse():
    """Folder picker for the admin page: lists drives, then folders."""
    if not is_admin():
        abort(403)
    path = request.args.get("path", "")
    if not path:
        if os.name == "nt":
            roots = [f"{c}:\\" for c in string.ascii_uppercase if os.path.isdir(f"{c}:\\")]
        else:
            roots = ["/"]
        return jsonify(path="", parent=None, photos=0,
                       dirs=[dict(name=r, path=r) for r in roots])
    p = Path(path)
    if not p.is_dir():
        return err("Folder not found.", 404)
    dirs, photos = [], 0
    try:
        for i, d in enumerate(sorted(p.iterdir(), key=lambda x: x.name.lower())):
            if i > 5000:
                break
            if d.name.startswith((".", "$")):
                continue
            if d.is_dir():
                dirs.append(dict(name=d.name, path=str(d)))
            elif d.suffix.lower() in config.IMAGE_EXT:
                photos += 1
    except OSError:
        pass
    parent = "" if p.parent == p else str(p.parent)
    return jsonify(path=str(p), parent=parent, photos=photos, dirs=dirs[:1000])


@app.post("/admin/events")
def admin_event_create():
    if not admin_post_ok():
        abort(403)
    name = request.form.get("name", "").strip()[:120]
    folders, bad = _clean_folders(request.form.get("folders", ""))
    if not name or not folders or bad:
        return redirect(url_for("admin", msg="Not created. " + (" | ".join(bad) or "Type a name and add a folder or link.")))
    c = con()
    date = request.form.get("event_date", "").strip()
    eid = c.execute(
        "INSERT INTO events(name,code,folders,require_approval,created_at,event_date) "
        "VALUES(?,?,?,?,?,?)",
        (name, secrets.token_urlsafe(9), "\n".join(folders),
         1 if request.form.get("approval") else 0, time.time(),
         date if DATE_RE.fullmatch(date) else "")).lastrowid
    db.audit(c, "album_created", detail=f"{name} | by {who()}", ip=ip())
    c.commit()
    indexer.start_index(eid)
    return redirect(url_for("admin", msg="Album created. The photos are being scanned now."))


def _clean_folders(text):
    """Each line is a folder on this computer or a Google Drive folder link."""
    good, bad = [], []
    for f in db.split_folders(text):
        fid = gdrive.parse_link(f)
        if fid:
            entry = "gdrive:" + fid
            if not gdrive.key_info():
                bad.append("Google Drive is not connected yet - see 'Google Drive links' on this page")
                continue
            # Whether the robot can see the folder is checked by the scan itself, in the
            # background (it says so on the album card), so adding an album never waits.
            if entry not in good:
                good.append(entry)
        elif f.lower().startswith(("http://", "https://")):
            bad.append(f"{f} (this is not a Google Drive FOLDER link)")
        elif fast_isdir(f, wait=3, keep=0):
            r = str(Path(f).resolve())
            if r not in good:
                good.append(r)
        else:
            bad.append(f)
    return good, bad


@app.post("/admin/google-key")
def admin_google_key():
    """Upload of the Google key file that lets the app read Drive links."""
    if not owner_post_ok():
        abort(403)
    up = request.files.get("key")
    try:
        d = json.loads(up.read(200_000).decode("utf-8"))
        assert d["type"] == "service_account" and d["client_email"] and d["private_key"]
        gdrive.make_assertion(d)             # the key inside must be usable
    except Exception:
        return redirect(url_for("admin_settings_page", msg="That is not the right file. Upload the .json key "
                                             "file downloaded from Google Cloud."))
    config.GOOGLE_KEY.write_text(json.dumps(d), encoding="utf-8")
    try:
        config.GOOGLE_KEY.chmod(0o600)
    except OSError:
        pass
    gdrive._token.update(value=None, exp=0)
    db.audit(con(), "google_key_uploaded", ip=ip())
    con().commit()
    return redirect(url_for("admin_settings_page", msg="Google Drive connected. Share your Drive folders with "
                                         + d["client_email"] + " and paste their links."))


def _delete_album(c, eid):
    """Removes an old album from this app: its photo list, faces, saved photos,
    small copies. The original photos on the drives are never touched. The
    record of who downloaded what stays, so the report is still complete."""
    ev = c.execute("SELECT name, folders FROM events WHERE id=?", (eid,)).fetchone()
    if not ev:
        return "That album was already removed."
    folders = ev["folders"]
    if indexer.progress.get(eid, {}).get("state") == "running":
        return "A scan is running for this album. Delete it when the scan has finished."
    while True:
        ids = [r[0] for r in c.execute("SELECT id FROM photos WHERE event_id=? LIMIT 2000", (eid,))]
        if not ids:
            break
        for pid in ids:
            indexer._forget_photo(c, pid)
        c.commit()
    for sql in ("DELETE FROM faces WHERE event_id=?", "DELETE FROM scan_errors WHERE event_id=?",
                "DELETE FROM otps WHERE event_id=?", "DELETE FROM events WHERE id=?"):
        c.execute(sql, (eid,))
    db.kv_set(c, f"scanning:{eid}", None)
    if db.kv_get(c, f"autoev:{eid}"):          # a deleted automatic album must not come back
        for entry in db.split_folders(folders):
            db.kv_set(c, "auto:skip:" + entry, "1")
        db.kv_set(c, f"autoev:{eid}", None)
    indexer.progress.pop(eid, None)
    indexer._drop_cache(eid)
    db.audit(c, "album_deleted", event_id=eid, detail=ev["name"], ip=ip())
    return f"Album \"{ev['name']}\" deleted. Your original photos were not touched."


@app.post("/admin/events/<int:eid>/<action>")
def admin_event_action(eid, action):
    if not admin_post_ok():
        abort(403)
    c = con()
    msg = ""
    if action == "index":
        msg = "Scan started." if indexer.start_index(eid) else "Scan already running."
    elif action == "rescan":            # every photo again, with the current scan settings
        if indexer.progress.get(eid, {}).get("state") == "running":
            msg = "A scan is already running. Try again when it has finished."
        else:
            c.execute("UPDATE photos SET mtime=-1, stamp='again' WHERE event_id=?", (eid,))
            c.commit()
            indexer.start_index(eid)
            msg = "Scanning every photo of this album again. Guests can keep using it meanwhile."
    elif action == "folders":
        folders, bad = _clean_folders(request.form.get("folders", ""))
        ev = c.execute("SELECT folders FROM events WHERE id=?", (eid,)).fetchone()
        keep = [b for b in bad if ev and b in db.split_folders(ev["folders"])]
        folders += keep          # a drive already on the list may be offline
        bad = [b for b in bad if b not in keep]
        if bad or not folders:
            msg = "Not saved. " + " | ".join(bad)
        else:
            c.execute("UPDATE events SET folders=? WHERE id=?", ("\n".join(folders), eid))
            c.commit()
            indexer.start_index(eid)
            msg = "Folders saved. Scanning the photos now."
    elif action == "date":
        date = request.form.get("event_date", "").strip()
        if date and not DATE_RE.fullmatch(date):
            msg = "Not saved. Choose the date from the calendar."
        else:
            c.execute("UPDATE events SET event_date=? WHERE id=?", (date, eid))
            msg = ("Event date saved." if date else
                   "Date removed - this album is now only reachable by its own link.")
    elif action == "toggle":
        c.execute("UPDATE events SET active=1-active WHERE id=?", (eid,))
    elif action == "approval":
        c.execute("UPDATE events SET require_approval=1-require_approval WHERE id=?", (eid,))
    elif action == "delete":
        msg = _delete_album(c, eid)
    else:
        abort(404)
    c.commit()
    return redirect(url_for("admin", msg=msg, q=request.form.get("q", ""),
                            page=request.form.get("page", "1")))


def _test_message(eid, img):
    """How a face picture matches an album, in plain words."""
    try:
        vecs = faces.get_engine().embed(img)
    except Exception as e:
        return f"The face engine is not working: {e}"
    c = con()
    n_photos = c.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (eid,)).fetchone()[0]
    n_faces = c.execute("SELECT COUNT(*) FROM faces WHERE event_id=?", (eid,)).fetchone()[0]
    if not vecs:
        return "No face was found in the picture. Look straight at the camera in good light."
    lines = []
    for i, v in enumerate(vecs, 1):
        _, best = indexer.stats(c, eid, v)
        hits = len(indexer.search(c, eid, v))
        lines.append(f"face {i}: best score {best:.2f}, {hits} photos match")
    msg = (f"Album has {n_photos} photos and {n_faces} faces. "
           f"The picture has {len(vecs)} face(s): " + "; ".join(lines) +
           f". A score of {config.MATCH_THRESHOLD:.2f} or more counts as the same person.")
    if not n_faces:
        msg += " The album has NO scanned faces yet - look at its status line for the reason."
    return msg


def _test_photos(eid, img, limit=24):
    """The photos a guest with this face would get (best first)."""
    try:
        vecs = faces.get_engine().embed(img)
    except Exception:
        return []
    best = {}
    for v in vecs:
        for pid, score in indexer.search(con(), eid, v):
            best[pid] = max(score, best.get(pid, 0))
    top = sorted(best.items(), key=lambda x: -x[1])[:limit]
    return [dict(id=pid, score=round(score, 2)) for pid, score in top]


@app.get("/admin/photo/<int:pid>")
def admin_photo(pid):
    """Preview of one album photo, for the admin's own test."""
    if not is_admin():
        abort(403)
    row = con().execute(
        "SELECT p.*, e.folders FROM photos p JOIN events e ON e.id=p.event_id WHERE p.id=?",
        (pid,)).fetchone()
    if not row:
        abort(404)
    from werkzeug.exceptions import HTTPException
    try:
        return send_file(preview_file(row), mimetype="image/jpeg")
    except HTTPException as e:              # tell the admin why, not just a broken picture
        return f"{row['relpath']}: {g.get('why') or 'not available'}", e.code
    except Exception as e:
        return f"{row['relpath']}: {e}", 500


@app.post("/admin/events/<int:eid>/test-selfie")
def admin_event_test_selfie(eid):
    """Admin takes a live selfie on the admin page, exactly like a guest."""
    if not is_admin() or not hmac.compare_digest(
            request.headers.get("X-CSRF", ""), session.get("csrf", "x")):
        abort(403)
    d = request.get_json(silent=True) or {}
    try:
        img = faces.decode(base64.b64decode(str(d.get("image", "")).split(",", 1)[-1]))
    except Exception:
        img = None
    if img is None:
        return jsonify(text="Could not read the picture.", photos=[])
    return jsonify(text=_test_message(eid, img), photos=_test_photos(eid, img))


@app.post("/admin/events/<int:eid>/test")
def admin_event_test(eid):
    """Admin uploads any face photo and sees exactly how it matches the album."""
    if not admin_post_ok():
        abort(403)
    up = request.files.get("photo")
    img = faces.decode(up.read()) if up else None
    if img is None:
        return redirect(url_for("admin", msg="Test: that file is not a picture (use JPG or PNG)."))
    return redirect(url_for("admin", msg="Test result - " + _test_message(eid, img)))


@app.post("/admin/requests/<int:sid>/<action>")
def admin_request(sid, action):
    if not admin_post_ok() or action not in ("approve", "reject"):
        abort(403)
    c = con()
    s = c.execute("SELECT * FROM sessions WHERE id=? AND status='pending'", (sid,)).fetchone()
    if s:
        if action == "approve":
            c.execute("UPDATE sessions SET status='approved' WHERE id=?", (sid,))
        else:
            c.execute("UPDATE sessions SET status='rejected' WHERE id=?", (sid,))
            c.execute("DELETE FROM matches WHERE session_id=?", (sid,))
        db.audit(c, f"admin_{action}", s["email"], s["event_id"], ip=ip())
        c.commit()
        (config.SELFIE_DIR / f"{sid}.jpg").unlink(missing_ok=True)
    return redirect(url_for("admin"))


@app.get("/admin/selfie/<int:sid>")
def admin_selfie(sid):
    if not is_admin():
        abort(403)
    p = config.SELFIE_DIR / f"{sid}.jpg"
    if not p.exists():
        abort(404)
    return send_file(p, mimetype="image/jpeg")


def _open_browser(url):
    """Opens the admin page by itself a moment after the app starts."""
    if os.environ.get("NO_BROWSER") or not sys.stdin.isatty():
        return
    import threading
    import webbrowser
    print(f"Opening {url} in your browser ...")
    threading.Timer(2.0, lambda: webbrowser.open(url)).start()


def startup_checks():
    """Runs when the app starts: is the face engine alive, and does any album
    still need its first scan (for example after a failed one)?"""
    try:
        faces.get_engine()
        print("Face engine: OK")
    except Exception as e:
        print(f"\n!! Face engine is NOT working: {e}\n!! Close this window and run start.bat again.\n")
        return
    c = db.connect()
    try:
        cut_off = indexer.unfinished(c)
        for e in c.execute("SELECT id, name FROM events WHERE active=1").fetchall():
            n = c.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (e["id"],)).fetchone()[0]
            if e["id"] in cut_off:
                print(f"Album '{e['name']}': the last scan was cut off - continuing it now.")
                indexer.start_index(e["id"])
            elif not n:
                print(f"Album '{e['name']}' has no scanned photos - scanning it now.")
                indexer.start_index(e["id"])
    finally:
        c.close()


def main():
    startup_checks()
    autoalbums.start()                      # new sub-folders become albums by themselves
    indexer.warm_all()                      # every open album in memory before the first guest
    host = config.HOST
    try:                                    # one fixed port, remembered in data/port.txt
        port = netutil.sticky_port(host, config.PORT, config.DATA_DIR / "port.txt")
    except RuntimeError as e:
        raise SystemExit(f"\n  {e}\n")
    if port != config.PORT:
        print(f"Using port {port} (kept in {config.DATA_DIR / 'port.txt'}).")
    scheme = "https" if config.HTTPS else "http"
    if config.DEV_MODE:
        print("!! DEV_MODE is ON - for testing on your own computer only !!")
    print(f"\nAdmin page (on this computer): {scheme}://127.0.0.1:{port}/admin")
    if host in ("0.0.0.0", ""):
        for addr in netutil.local_ips():
            print(f"On your LAN / VLAN:            {scheme}://{addr}:{port}/admin")
        print(f"If other devices cannot connect, allow TCP port {port} in the firewall.\n")
    cert, key = netutil.ensure_cert(config.TLS_DIR, config.TLS_NAMES)
    if config.HTTPS:
        netutil.offer_trust(cert)
    _open_browser(f"{scheme}://127.0.0.1:{port}/admin")
    threading.Thread(target=netutil.self_check, args=(port,), daemon=True).start()
    # One port answers https:// AND http://, on 127.0.0.1, localhost and the LAN
    # address, so a tunnel or proxy works whichever way it is set up.
    netutil.serve_dual(app, host, port, cert, key)


if __name__ == "__main__":
    main()
