"""FaceFind - guests verify their email with an OTP, take a selfie, and get
only the photos they appear in. Run:  python app.py"""
import base64
import hashlib
import io
import hmac
import json
import os
import re
import secrets
import string
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.parse import quote

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from PIL import Image, ImageDraw, ImageFont, ImageOps

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


def _load_settings():
    """Settings changed on the admin page are kept in the database."""
    c = db.connect()
    try:
        v = db.kv_get(c, "match_threshold")
        if v:
            config.MATCH_THRESHOLD = float(v)
    finally:
        c.close()


_load_settings()
signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="download")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
PHONE_RE = re.compile(r"\+?\d{7,15}")
# The general link: not one album, but every open album in the guest's dates.
PORTAL = {"id": 0, "name": config.BRAND, "code": ""}
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
    return request.remote_addr or ""


def err(msg, status=400):
    return jsonify(ok=False, error=msg), status


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


@app.after_request
def _headers(r):
    r.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; "
        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
    r.headers["X-Content-Type-Options"] = "nosniff"
    r.headers["X-Frame-Options"] = "DENY"
    r.headers["Referrer-Policy"] = "no-referrer"
    r.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
    if not config.DEV_MODE:
        r.headers["Strict-Transport-Security"] = "max-age=31536000"
    if not request.path.startswith("/static/"):
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
    return c.execute(
        "SELECT p.*, e.folders FROM bookmarks b JOIN photos p ON p.id=b.photo_id "
        "JOIN events e ON e.id=p.event_id WHERE b.email=? AND b.photo_id=? AND e.active=1",
        (s["email"], photo_id)).fetchone()


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


def preview_file(row):
    """The small watermarked preview of one photo (made once, then kept).
    A Google Drive photo is read from Drive for this - it is never copied.
    When it cannot be made, g.why says why (shown to the admin)."""
    if row["root"] not in db.split_folders(row["folders"]):
        g.why = "its folder was taken off the album"
        abort(404)
    dest = indexer.preview_path(row)
    if dest.exists():
        return dest
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
            make_preview(io.BytesIO(data), dest, ext)
        else:
            g.why = "file not found - the drive is not connected, or the photo was moved"
            src = old_copy(row) or real_path(row["folders"], row["root"], row["relpath"])
            make_preview(src, dest, Path(row["relpath"]).suffix)
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


def make_preview(src, dest, ext=""):
    if ext.lower() in config.HEIC_EXT | config.RAW_EXT:      # iPhone / camera RAW files
        import numpy as np
        bgr = faces.load(src.read() if hasattr(src, "read") else Path(src).read_bytes(), ext)
        if bgr is None:
            raise ValueError("no picture could be read from this file")
        im = Image.fromarray(np.ascontiguousarray(bgr[:, :, ::-1]))
    else:
        im = ImageOps.exif_transpose(Image.open(src)).convert("RGB")
    im.thumbnail((config.PREVIEW_SIDE, config.PREVIEW_SIDE))
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
    tmp = dest.with_suffix(f".{secrets.token_hex(4)}.tmp")
    out.save(tmp, "JPEG", quality=72)
    os.replace(tmp, dest)


# ------------------------------------------------------------ guest pages
@app.get("/")
def home():
    if not config.PORTAL:
        return render_template("guest.html", brand=config.BRAND, event=None, portal=False), 404
    today = time.strftime("%Y-%m-%d")
    month_ago = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    return render_template("guest.html", brand=config.BRAND, event=None, portal=True,
                           today=today, month_ago=month_ago)


@app.get("/favicon.ico")
def favicon():
    return "", 204


@app.get("/e/<code>")
def event_page(code):
    ev = event_by_code(code)
    if not ev:
        return render_template("guest.html", brand=config.BRAND, event=None, portal=False), 404
    return render_template("guest.html", brand=config.BRAND, event=ev, portal=False)


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
        mailer.send_otp(email, code, ev["name"])
    except Exception as e:
        app.logger.error("mail failed: %s", e)
        db.audit(c, "mail_failed", email, ev["id"], str(e)[:200], ip())
        c.commit()
        return err("We could not send the email. Please try again later.", 502)
    return jsonify(ok=True, ttl=config.OTP_TTL)


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
                saved=saved, name=u["name"] if u else "",
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
    db.audit(c, "selfie_matched", s["email"], log_event,
             f"{len(found)} photos | best score {best:.2f} (needs {config.MATCH_THRESHOLD:.2f}) "
             f"| album has {n_faces} faces"
             + (f" | {len(albums)} albums, {date_from} to {date_to}" if not log_event else ""), ip())
    c.commit()
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
    return send_file(preview_file(row), mimetype="image/jpeg")


@app.post("/api/download-link/<what>")
def download_link(what):
    s = guest()
    if not s:
        return err("Session expired. Please verify again.", 401)
    if what == "saved":
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
    if not s or s["id"] != data.get("s"):
        abort(403)
    c = con()
    if data["p"] in ("all", "saved"):
        if data["p"] == "saved":
            rows = c.execute(
                "SELECT p.*, e.folders FROM bookmarks b JOIN photos p ON p.id=b.photo_id "
                "JOIN events e ON e.id=p.event_id WHERE b.email=? AND e.active=1",
                (s["email"],)).fetchall()
        else:
            if s["status"] not in ("matched", "approved"):
                abort(403)
            rows = c.execute(
                "SELECT p.*, e.folders FROM matches m JOIN photos p ON p.id=m.photo_id "
                "JOIN events e ON e.id=p.event_id WHERE m.session_id=?", (s["id"],)).fetchall()
        if not rows:
            abort(404)
        tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            n = 0
            for r in rows:
                if r["root"] not in db.split_folders(r["folders"]):
                    continue
                name = Path(r["relpath"]).name
                if gdrive.is_drive(r["root"]) and old_copy(r):
                    n += 1
                    z.write(old_copy(r), f"{n:04d}_{name}")
                    record_download(c, s, r)
                    continue
                if gdrive.is_drive(r["root"]):       # straight from Drive into the zip
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
                    record_download(c, s, r)
                    continue
                base = Path(r["root"])
                p = base / r["relpath"]
                if p.is_file() and base.resolve() in p.resolve().parents:
                    n += 1          # photos on an offline drive are left out
                    z.write(p, f"{n:04d}_{name}")
                    record_download(c, s, r)
            if not n:
                abort(404)
        tmp.seek(0)
        db.audit(c, "download_all", s["email"], s["event_id"], f"{n} photos", ip())
        c.commit()
        return send_file(tmp, mimetype="application/zip", as_attachment=True,
                         download_name="my-photos.zip")
    row = allowed(s, int(data["p"]))
    if not row:
        abort(403)
    if row["root"] not in db.split_folders(row["folders"]):
        abort(404)
    db.audit(c, "download", s["email"], s["event_id"], row["relpath"], ip())
    record_download(c, s, row)
    c.commit()
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
    return session.get("admin") is True


def admin_post_ok():
    return is_admin() and hmac.compare_digest(
        request.form.get("csrf", ""), session.get("csrf", "x"))


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
        if fails >= 5:
            msg = "Too many attempts. Wait 15 minutes."
        elif hmac.compare_digest(request.form.get("password", "").encode(),
                                 config.ADMIN_PASSWORD.encode()):
            session.clear()
            session.permanent = True
            session["admin"] = True
            session["csrf"] = secrets.token_hex(16)
            db.audit(c, "admin_login", ip=ip())
            c.commit()
            return redirect(url_for("admin"))
        else:
            db.audit(c, "admin_login_fail", ip=ip())
            c.commit()
            msg = "Wrong password."
    return render_template("admin_login.html", brand=config.BRAND, msg=msg)


@app.post("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.get("/admin")
def admin():
    if not is_admin():
        return redirect(url_for("admin_login"))
    c = con()
    events = []
    for e in c.execute("SELECT * FROM events ORDER BY id DESC"):
        e = dict(e)
        e["photos"] = c.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (e["id"],)).fetchone()[0]
        e["faces"] = c.execute("SELECT COUNT(*) FROM faces WHERE event_id=?", (e["id"],)).fetchone()[0]
        e["status"], e["state"] = status_text(e["id"], e["photos"], e["faces"])
        e["folder_list"] = [(gdrive.label(f), gdrive.is_drive(f) or Path(f).is_dir())
                            for f in db.split_folders(e["folders"])]
        e["link"] = guest_link(e["code"])
        events.append(e)
    pending = c.execute(
        "SELECT s.id, s.email, s.created_at, s.date_from, s.date_to, "
        "COALESCE(e.name, 'General link') AS name, "
        "(SELECT COUNT(*) FROM matches m WHERE m.session_id=s.id) AS n "
        "FROM sessions s LEFT JOIN events e ON e.id=s.event_id "
        "WHERE s.status='pending' AND s.expires_at>? ORDER BY s.id", (time.time(),)).fetchall()
    log = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 150").fetchall()
    people = []
    for u in c.execute(
            "SELECT u.email, u.name, u.phone, u.last_login, "
            "(SELECT COUNT(DISTINCT photo_id) FROM downloads d WHERE d.email=u.email) AS n, "
            "(SELECT MAX(ts) FROM downloads d WHERE d.email=u.email) AS last_dl, "
            "(SELECT COUNT(*) FROM bookmarks b WHERE b.email=u.email) AS saved "
            "FROM users u ORDER BY COALESCE(last_dl, 0) DESC, u.last_login DESC LIMIT 300").fetchall():
        u = dict(u)
        u["files"] = c.execute(
            "SELECT d.relpath, MAX(d.ts) AS ts, COALESCE(e.name, '') AS album FROM downloads d "
            "LEFT JOIN events e ON e.id=d.event_id WHERE d.email=? "
            "GROUP BY d.photo_id, d.relpath ORDER BY ts DESC LIMIT 200", (u["email"],)).fetchall()
        people.append(u)
    return render_template("admin.html", brand=config.BRAND, events=events, pending=pending,
                           log=log, csrf=session["csrf"], people=people,
                           threshold=f"{config.MATCH_THRESHOLD:.2f}",
                           portal_link=guest_link("") if config.PORTAL else "",
                           today=time.strftime("%Y-%m-%d"), drive_email=gdrive.robot_email(), msg=request.args.get("msg", ""),
                           fmt=lambda t: time.strftime("%d %b %H:%M", time.localtime(t)))


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


@app.get("/admin/status")
def admin_status():
    if not is_admin():
        abort(403)
    c, out = con(), {}
    for e in c.execute("SELECT id FROM events"):
        n = c.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (e["id"],)).fetchone()[0]
        f = c.execute("SELECT COUNT(*) FROM faces WHERE event_id=?", (e["id"],)).fetchone()[0]
        text, state = status_text(e["id"], n, f)
        out[e["id"]] = dict(text=text, state=state)
    return jsonify(out)


@app.get("/admin/downloads.csv")
def admin_downloads_csv():
    """The whole download record as a file that opens in Excel."""
    if not is_admin():
        abort(403)
    import csv
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Time", "Name", "Phone", "Email", "Album", "Photo", "IP"])
    for r in con().execute(
            "SELECT d.ts, u.name, u.phone, d.email, COALESCE(e.name, '') AS album, d.relpath, d.ip "
            "FROM downloads d LEFT JOIN users u ON u.email=d.email "
            "LEFT JOIN events e ON e.id=d.event_id ORDER BY d.id DESC"):
        row = [time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"])), r["name"] or "",
               r["phone"] or "", r["email"], r["album"], r["relpath"], r["ip"]]
        # a cell that starts with = + - @ would be run as a formula by Excel
        w.writerow([("'" + v) if isinstance(v, str) and v[:1] in "=+-@" else v for v in row])
    return app.response_class(
        "\ufeff" + out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=facefind-downloads.csv"})


@app.post("/admin/settings")
def admin_settings():
    if not admin_post_ok():
        abort(403)
    try:
        v = float(request.form.get("threshold", ""))
        assert 0.30 <= v <= 0.80
    except (ValueError, AssertionError):
        return redirect(url_for("admin", msg="Not saved. Strictness must be between 0.30 and 0.80."))
    c = con()
    db.kv_set(c, "match_threshold", f"{v:.3f}")
    db.audit(c, "strictness_changed", detail=f"{v:.2f}", ip=ip())
    c.commit()
    config.MATCH_THRESHOLD = v
    return redirect(url_for("admin", msg=f"Match strictness is now {v:.2f}. It applies to the "
                                         "next selfie - no new scan is needed."))


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
            if not gdrive.known_name(fid):
                try:                      # check right now that the robot can see it
                    gdrive.remember_name(fid, gdrive.folder_name(fid))
                except gdrive.DriveError as e:
                    bad.append(str(e))
                    continue
            if entry not in good:
                good.append(entry)
        elif f.lower().startswith(("http://", "https://")):
            bad.append(f"{f} (this is not a Google Drive FOLDER link)")
        elif Path(f).is_dir():
            r = str(Path(f).resolve())
            if r not in good:
                good.append(r)
        else:
            bad.append(f)
    return good, bad


@app.post("/admin/google-key")
def admin_google_key():
    """Upload of the Google key file that lets the app read Drive links."""
    if not admin_post_ok():
        abort(403)
    up = request.files.get("key")
    try:
        d = json.loads(up.read(200_000).decode("utf-8"))
        assert d["type"] == "service_account" and d["client_email"] and d["private_key"]
        gdrive.make_assertion(d)             # the key inside must be usable
    except Exception:
        return redirect(url_for("admin", msg="That is not the right file. Upload the .json key "
                                             "file downloaded from Google Cloud."))
    config.GOOGLE_KEY.write_text(json.dumps(d), encoding="utf-8")
    try:
        config.GOOGLE_KEY.chmod(0o600)
    except OSError:
        pass
    gdrive._token.update(value=None, exp=0)
    db.audit(con(), "google_key_uploaded", ip=ip())
    con().commit()
    return redirect(url_for("admin", msg="Google Drive connected. Share your Drive folders with "
                                         + d["client_email"] + " and paste their links."))


@app.post("/admin/events/<int:eid>/<action>")
def admin_event_action(eid, action):
    if not admin_post_ok():
        abort(403)
    c = con()
    msg = ""
    if action == "index":
        msg = "Scan started." if indexer.start_index(eid) else "Scan already running."
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
    else:
        abort(404)
    c.commit()
    return redirect(url_for("admin", msg=msg))


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
    host = config.HOST
    port = netutil.free_port(host, config.PORT)
    if port != config.PORT:
        print(f"Port {config.PORT} is busy - using port {port} instead.")
    scheme = "https" if config.HTTPS else "http"
    if config.DEV_MODE:
        print("!! DEV_MODE is ON - for testing on your own computer only !!")
    print(f"\nAdmin page (on this computer): {scheme}://127.0.0.1:{port}/admin")
    if host in ("0.0.0.0", ""):
        for addr in netutil.local_ips():
            print(f"On your LAN / VLAN:            {scheme}://{addr}:{port}/admin")
        print(f"If other devices cannot connect, allow TCP port {port} in the firewall.\n")
    if config.HTTPS:
        cert, key = netutil.ensure_cert(config.TLS_DIR, config.TLS_NAMES)
        netutil.offer_trust(cert)
        _open_browser(f"https://localhost:{port}/admin")
        from werkzeug.serving import run_simple
        run_simple(host, port, app, threaded=True, ssl_context=(str(cert), str(key)))
    else:
        _open_browser(f"http://localhost:{port}/admin")
        try:
            from waitress import serve
            serve(app, host=host, port=port, threads=8)
        except ImportError:
            app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
