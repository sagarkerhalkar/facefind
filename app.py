"""FaceFind - guests verify their email with an OTP, take a selfie, and get
only the photos they appear in. Run:  python app.py"""
import base64
import hashlib
import hmac
import os
import re
import secrets
import tempfile
import time
import zipfile
from pathlib import Path

from flask import (Flask, abort, g, jsonify, redirect, render_template,
                   request, send_file, session, url_for)
from itsdangerous import BadSignature, URLSafeTimedSerializer
from PIL import Image, ImageDraw, ImageFont, ImageOps

import config
import db
import faces
import indexer
import mailer

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
signer = URLSafeTimedSerializer(config.SECRET_KEY, salt="download")
EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]{2,}")
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


def guest():
    """The verified guest session for this browser, or None."""
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    return con().execute(
        "SELECT * FROM sessions WHERE token_hash=? AND expires_at>?",
        (h(tok), time.time())).fetchone()


def allowed(s, photo_id):
    """A guest may touch a photo only if THEIR selfie matched it."""
    if s["status"] not in ("matched", "approved"):
        return None
    return con().execute(
        "SELECT p.*, e.folders FROM matches m JOIN photos p ON p.id=m.photo_id "
        "JOIN events e ON e.id=p.event_id WHERE m.session_id=? AND m.photo_id=?",
        (s["id"], photo_id)).fetchone()


def real_path(folders, root, relpath):
    """Only files inside a folder that is still on the event's list."""
    if root not in db.split_folders(folders):
        abort(404)
    base = Path(root).resolve()
    p = (base / relpath).resolve()
    if base not in p.parents or not p.is_file():
        abort(404)      # also the case when that drive is offline
    return p


def make_preview(src, dest):
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
    return render_template("guest.html", brand=config.BRAND, event=None), 404


@app.get("/e/<code>")
def event_page(code):
    ev = event_by_code(code)
    if not ev:
        return render_template("guest.html", brand=config.BRAND, event=None), 404
    return render_template("guest.html", brand=config.BRAND, event=ev)


@app.post("/api/otp/send")
def otp_send():
    d = request.get_json(silent=True) or {}
    ev = event_by_code(d.get("code"))
    email = str(d.get("email") or "").strip().lower()
    if not ev:
        return err("This link is not valid.", 404)
    if len(email) > 254 or not EMAIL_RE.fullmatch(email):
        return err("Please enter a valid email address.")
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
    c.execute("INSERT INTO otps(email,event_id,code_hash,expires_at,created_at,ip) "
              "VALUES(?,?,?,?,?,?)",
              (email, ev["id"], h(f"{email}|{ev['id']}|{code}"), now + config.OTP_TTL, now, ip()))
    db.audit(c, "otp_sent", email, ev["id"], ip=ip())
    c.commit()
    try:
        mailer.send_otp(email, code, ev["name"])
    except Exception as e:
        app.logger.error("mail failed: %s", e)
        return err("We could not send the email. Please try again later.", 502)
    return jsonify(ok=True, ttl=config.OTP_TTL)


@app.post("/api/otp/verify")
def otp_verify():
    d = request.get_json(silent=True) or {}
    ev = event_by_code(d.get("code"))
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
    db.audit(c, "verified", email, ev["id"], ip=ip())
    c.commit()
    r = jsonify(ok=True)
    r.set_cookie(COOKIE, token, max_age=config.SESSION_TTL, httponly=True,
                 secure=config.COOKIE_SECURE, samesite="Strict")
    return r


def _state(s):
    c = con()
    ev = c.execute("SELECT * FROM events WHERE id=?", (s["event_id"],)).fetchone()
    photos = []
    if s["status"] in ("matched", "approved"):
        photos = [r["photo_id"] for r in c.execute(
            "SELECT photo_id FROM matches WHERE session_id=? ORDER BY score DESC", (s["id"],))]
    return dict(ok=True, email=s["email"], status=s["status"], photos=photos,
                event_code=ev["code"] if ev else None,
                tries_left=max(0, config.SELFIE_MAX_TRIES - s["selfie_tries"]))


@app.get("/api/me")
def me():
    s = guest()
    if not s:
        return err("Not signed in.", 401)
    return jsonify(_state(s))


@app.post("/api/selfie")
def selfie():
    s = guest()
    if not s:
        return err("Please verify your email first.", 401)
    c = con()
    ev = c.execute("SELECT * FROM events WHERE id=? AND active=1", (s["event_id"],)).fetchone()
    if not ev:
        return err("This event is closed.", 404)
    if s["selfie_tries"] >= config.SELFIE_MAX_TRIES:
        return err("Selfie limit reached. Please verify your email again later.", 429)
    d = request.get_json(silent=True) or {}
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
    found = indexer.search(c, ev["id"], vecs[0])
    status = "pending" if (ev["require_approval"] and found) else "matched"
    c.execute("DELETE FROM matches WHERE session_id=?", (s["id"],))
    c.executemany("INSERT INTO matches(session_id,photo_id,score) VALUES(?,?,?)",
                  [(s["id"], pid, sc) for pid, sc in found])
    c.execute("UPDATE sessions SET status=? WHERE id=?", (status, s["id"]))
    db.audit(c, "selfie_matched", s["email"], ev["id"], f"{len(found)} photos", ip())
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
    if row["root"] not in db.split_folders(row["folders"]):
        abort(404)
    dest = config.CACHE_DIR / f"{pid}.jpg"
    if not dest.exists():
        make_preview(real_path(row["folders"], row["root"], row["relpath"]), dest)
    return send_file(dest, mimetype="image/jpeg")


@app.post("/api/download-link/<what>")
def download_link(what):
    s = guest()
    if not s:
        return err("Session expired. Please verify again.", 401)
    if what != "all":
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
    if data["p"] == "all":
        if s["status"] not in ("matched", "approved"):
            abort(403)
        rows = c.execute(
            "SELECT p.relpath, p.root, e.folders FROM matches m JOIN photos p ON p.id=m.photo_id "
            "JOIN events e ON e.id=p.event_id WHERE m.session_id=?", (s["id"],)).fetchall()
        if not rows:
            abort(404)
        tmp = tempfile.SpooledTemporaryFile(max_size=64 * 1024 * 1024)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
            n = 0
            for r in rows:
                base = Path(r["root"])
                p = base / r["relpath"]
                if r["root"] in db.split_folders(r["folders"]) and p.is_file() \
                        and base.resolve() in p.resolve().parents:
                    n += 1          # photos on an offline drive are left out
                    z.write(p, f"{n:04d}_{p.name}")
            if not n:
                abort(404)
        tmp.seek(0)
        db.audit(c, "download_all", s["email"], s["event_id"], f"{len(rows)} photos", ip())
        c.commit()
        return send_file(tmp, mimetype="application/zip", as_attachment=True,
                         download_name="my-photos.zip")
    row = allowed(s, int(data["p"]))
    if not row:
        abort(403)
    p = real_path(row["folders"], row["root"], row["relpath"])
    db.audit(c, "download", s["email"], s["event_id"], row["relpath"], ip())
    c.commit()
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
        e["progress"] = indexer.progress.get(e["id"])
        e["folder_list"] = [(f, Path(f).is_dir()) for f in db.split_folders(e["folders"])]
        e["link"] = url_for("event_page", code=e["code"], _external=True)
        events.append(e)
    pending = c.execute(
        "SELECT s.id, s.email, s.created_at, e.name, "
        "(SELECT COUNT(*) FROM matches m WHERE m.session_id=s.id) AS n "
        "FROM sessions s JOIN events e ON e.id=s.event_id "
        "WHERE s.status='pending' AND s.expires_at>? ORDER BY s.id", (time.time(),)).fetchall()
    log = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 150").fetchall()
    return render_template("admin.html", brand=config.BRAND, events=events, pending=pending,
                           log=log, csrf=session["csrf"], msg=request.args.get("msg", ""),
                           fmt=lambda t: time.strftime("%d %b %H:%M", time.localtime(t)))


@app.post("/admin/events")
def admin_event_create():
    if not admin_post_ok():
        abort(403)
    name = request.form.get("name", "").strip()[:120]
    folders, bad = _clean_folders(request.form.get("folders", ""))
    if not name or not folders or bad:
        return redirect(url_for("admin", msg="Name missing or folder not found: " + ", ".join(bad)))
    c = con()
    c.execute("INSERT INTO events(name,code,folders,require_approval,created_at) VALUES(?,?,?,?,?)",
              (name, secrets.token_urlsafe(9), "\n".join(folders),
               1 if request.form.get("approval") else 0, time.time()))
    c.commit()
    return redirect(url_for("admin", msg="Event created. Now press 'Scan photos'."))


def _clean_folders(text):
    good, bad = [], []
    for f in db.split_folders(text):
        if Path(f).is_dir():
            r = str(Path(f).resolve())
            if r not in good:
                good.append(r)
        else:
            bad.append(f)
    return good, bad


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
            msg = "Not saved - folder not found (is the drive connected?): " + ", ".join(bad)
        else:
            c.execute("UPDATE events SET folders=? WHERE id=?", ("\n".join(folders), eid))
            msg = "Folders saved. Press 'Scan photos' to read the new drives."
    elif action == "toggle":
        c.execute("UPDATE events SET active=1-active WHERE id=?", (eid,))
    elif action == "approval":
        c.execute("UPDATE events SET require_approval=1-require_approval WHERE id=?", (eid,))
    else:
        abort(404)
    c.commit()
    return redirect(url_for("admin", msg=msg))


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


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    if config.DEV_MODE:
        print("!! DEV_MODE is ON - for testing on your own computer only !!")
    print(f"Admin page: http://{host}:{port}/admin")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=8)
    except ImportError:
        app.run(host=host, port=port)
