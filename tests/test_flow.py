"""End-to-end check of the security rules with a stand-in face engine
(colour = person). Run:  python tests/test_flow.py"""
import base64, io, os, sys, tempfile, time
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ.update(DATA_DIR=str(tmp / "data"), DEV_MODE="1", ADMIN_PASSWORD="admin-pass-for-test",
                  SMTP_HOST="", BRAND="TestStudio")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import numpy as np
from PIL import Image
import app as A, faces, indexer, mailer, config

class Fake:
    def embed(self, bgr):
        b, g, r = [int(x) // 128 for x in bgr[5, 5]]
        if (b, g, r) == (0, 0, 0): return []
        rng = np.random.default_rng(b * 4 + g * 2 + r)
        v = rng.normal(size=128).astype(np.float32)
        return [v / np.linalg.norm(v)]
faces.get_engine = lambda: Fake()
sent = {}
mailer.send_otp = lambda to, code, name: sent.__setitem__(to, code)

def img(color, path=None):
    im = Image.new("RGB", (640, 480), color); buf = io.BytesIO(); im.save(buf, "PNG")
    if path: path.write_bytes(buf.getvalue())
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
RED, BLUE, BLACK = (255, 0, 0), (0, 0, 255), (0, 0, 0)
d1, d2 = tmp / "driveD" / "wed", tmp / "driveE" / "wed"
d1.mkdir(parents=True); d2.mkdir(parents=True)
img(RED, d1 / "red1.png"); img(BLUE, d1 / "blue1.png")
img(RED, d2 / "red2.png"); img(BLUE, d2 / "blue2.png"); img(BLUE, d2 / "blue3.png")

H = {"X-Requested-With": "facefind"}
adm = A.app.test_client()
assert adm.get("/admin").status_code == 302
assert b"Wrong" in adm.post("/admin/login", data={"password": "nope"}).data
assert adm.post("/admin/login", data={"password": "admin-pass-for-test"}).status_code == 302
with adm.session_transaction() as s: csrf = s["csrf"]
assert adm.post("/admin/events", data={"name": "Wed", "folders": f"{d1}\n{d2}"}).status_code == 403  # no csrf
adm.post("/admin/events", data={"name": "Wed", "folders": f"{d1}\n{d2}", "csrf": csrf})
con = A.db.connect(); ev = con.execute("SELECT * FROM events").fetchone(); code = ev["code"]
indexer.index_event(ev["id"])
assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5, "photos from both drives"
print("ok  admin login, CSRF, event with 2 drives indexed (5 photos)")

def login(client, email):
    r = client.post("/api/otp/send", json={"code": code, "email": email, "consent": True}, headers=H)
    assert r.status_code == 200, r.json
    r = client.post("/api/otp/verify", json={"code": code, "email": email, "otp": sent[email]}, headers=H)
    assert r.status_code == 200, r.json

g = A.app.test_client()
assert g.get("/e/" + code).status_code == 200 and g.get("/e/wrong").status_code == 404
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True}).status_code == 403
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com"}, headers=H).status_code == 400
assert g.get("/api/me").status_code == 401
assert g.post("/api/selfie", json={"image": img(RED)}, headers=H).status_code == 401
assert g.get("/api/preview/1").status_code == 404
# wrong OTP 5 times locks the code, even for the right one afterwards
g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True}, headers=H)
real = sent["a@x.com"]; wrong = "000000" if real != "000000" else "111111"
for _ in range(5):
    assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": wrong}, headers=H).status_code == 400
assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": real}, headers=H).status_code == 429
assert g.get("/api/me").status_code == 401
print("ok  nothing works before OTP; wrong codes lock out")
# expired code
g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True}, headers=H)
con.execute("UPDATE otps SET expires_at=? WHERE used=0", (time.time() - 1,)); con.commit()
assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": sent["a@x.com"]}, headers=H).status_code == 400
login(g, "a@x.com")
# code cannot be reused
g2 = A.app.test_client()
assert g2.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": sent["a@x.com"]}, headers=H).status_code == 400
# 4th send in 15 min is rate limited
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True}, headers=H).status_code == 429
print("ok  expiry, single use, rate limit")

assert g.post("/api/selfie", json={"image": img(BLACK)}, headers=H).status_code == 400   # no face
r = g.post("/api/selfie", json={"image": img(RED)}, headers=H).json
assert r["status"] == "matched" and len(r["photos"]) == 2, r
mine = set(r["photos"])
others = {x["id"] for x in con.execute("SELECT id FROM photos")} - mine
for pid in mine:
    p = g.get(f"/api/preview/{pid}"); assert p.status_code == 200 and p.mimetype == "image/jpeg"
    assert Image.open(io.BytesIO(p.data)).size[0] <= config.PREVIEW_SIDE
for pid in others:
    assert g.get(f"/api/preview/{pid}").status_code == 404
    assert g.post(f"/api/download-link/{pid}", json={}, headers=H).status_code == 404
print("ok  guest sees only own photos (from both drives), others are 404")

pid = next(iter(mine))
url = g.post(f"/api/download-link/{pid}", json={}, headers=H).json["url"]
r = g.get(url); assert r.status_code == 200 and "attachment" in r.headers["Content-Disposition"]
assert A.app.test_client().get(url).status_code == 403            # link alone is useless
b = A.app.test_client(); con.execute("DELETE FROM otps"); con.commit(); login(b, "b@x.com")
assert b.get(url).status_code == 403                               # other verified guest
assert b.post("/api/selfie", json={"image": img(BLUE)}, headers=H).json["photos"].__len__() == 3
assert b.get(url).status_code == 403 and b.get(f"/api/preview/{pid}").status_code == 404
real_ttl = config.DOWNLOAD_LINK_TTL; config.DOWNLOAD_LINK_TTL = -1
assert g.get(url).status_code == 403; config.DOWNLOAD_LINK_TTL = real_ttl   # expired link
z = g.get(g.post("/api/download-link/all", json={}, headers=H).json["url"])
import zipfile; assert len(zipfile.ZipFile(io.BytesIO(z.data)).namelist()) == 2
print("ok  download link bound to session, expires; zip has only own photos")

# approval mode
adm.post(f"/admin/events/{ev['id']}/approval", data={"csrf": csrf})
con.execute("DELETE FROM otps"); con.commit()
c3 = A.app.test_client(); login(c3, "c@x.com")
r = c3.post("/api/selfie", json={"image": img(RED)}, headers=H).json
assert r["status"] == "pending" and r["photos"] == []
assert c3.get(f"/api/preview/{pid}").status_code == 404
assert c3.post("/api/download-link/all", json={}, headers=H).status_code == 404
sid = con.execute("SELECT id FROM sessions WHERE email='c@x.com'").fetchone()[0]
assert (config.SELFIE_DIR / f"{sid}.jpg").exists() and adm.get(f"/admin/selfie/{sid}").status_code == 200
assert c3.get(f"/admin/selfie/{sid}").status_code == 403
adm.post(f"/admin/requests/{sid}/approve", data={"csrf": csrf})
assert not (config.SELFIE_DIR / f"{sid}.jpg").exists()
assert len(c3.get("/api/me").json["photos"]) == 2 and c3.get(f"/api/preview/{pid}").status_code == 200
print("ok  approval mode: nothing visible until approved; selfie deleted after")

# a drive goes offline: its photos stay indexed but cannot be served; removing a folder revokes
os.rename(tmp / "driveE", tmp / "driveE_off"); indexer.index_event(ev["id"])
assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5
os.rename(tmp / "driveE_off", tmp / "driveE")
adm.post(f"/admin/events/{ev['id']}/folders", data={"csrf": csrf, "folders": str(d1)})
e_pid = con.execute("SELECT id FROM photos WHERE root=? AND relpath='red2.png'", (str(d2.resolve()),)).fetchone()[0]
assert g.get(f"/api/preview/{e_pid}").status_code == 404
indexer.index_event(ev["id"]); assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 2
print("ok  offline drive keeps index; removed folder is revoked")

# path escape, closed event, sign out
con.execute("UPDATE photos SET relpath='../../data/secret.key' WHERE id=?", (pid,)); con.commit()
(config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)
assert g.get(f"/api/preview/{pid}").status_code == 404
assert g.post("/api/forget", json={}, headers=H).status_code == 200 and g.get("/api/me").status_code == 401
adm.post(f"/admin/events/{ev['id']}/toggle", data={"csrf": csrf})
assert A.app.test_client().get("/e/" + code).status_code == 404
hd = adm.get("/admin").headers
assert "frame-ancestors 'none'" in hd["Content-Security-Policy"] and hd["Cache-Control"] == "no-store"
# LAN / VLAN rules
import netutil
config.ALLOWED_NETWORKS = netutil.parse_networks("192.168.10.0/24, 192.168.20.0/24")
config.ADMIN_NETWORKS = netutil.parse_networks("192.168.10.0/24")
def from_ip(addr, path): return A.app.test_client().get(path, environ_base={"REMOTE_ADDR": addr}).status_code
assert from_ip("192.168.30.7", "/e/x") == 403            # VLAN not on the list
assert from_ip("192.168.20.7", "/e/x") == 404            # allowed guest VLAN (404 = closed event page)
assert from_ip("192.168.20.7", "/admin/login") == 404    # guest VLAN cannot see admin
assert from_ip("192.168.10.7", "/admin/login") == 200    # office VLAN can
config.ALLOWED_NETWORKS = config.ADMIN_NETWORKS = []
cert, key = netutil.ensure_cert(tmp / "tls", ["photos.lan"])
assert cert.exists() and netutil.ensure_cert(tmp / "tls", ["photos.lan"])[0].stat().st_mtime == cert.stat().st_mtime
import socket; busy = socket.socket(); busy.bind(("127.0.0.1", 0)); bp = busy.getsockname()[1]
assert netutil.free_port("127.0.0.1", bp) != bp; busy.close()
print("ok  VLAN allow-list, admin VLAN, certificate, busy-port fallback")
print("ok  path escape blocked, sign-out erases, closed link is dead\nALL TESTS PASSED")
