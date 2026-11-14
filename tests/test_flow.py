"""End-to-end check of the security rules with a stand-in face engine
(colour = person). Run:  python tests/test_flow.py"""
import sqlite3
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
faces.make_engine = lambda: Fake()
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
for _ in range(100):                      # creating an album starts the scan by itself
    if indexer.progress.get(ev["id"], {}).get("state") == "finished": break
    time.sleep(0.1)
assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5, "photos from both drives"
st = adm.get("/admin/status").json[str(ev["id"])]
assert st["state"] == "ready" and "5 photos" in st["text"], st
br = adm.get("/admin/browse", query_string={"path": str(d1)}).json
assert br["photos"] == 2 and adm.get("/admin/browse").json["dirs"]
assert A.app.test_client().get("/admin/browse").status_code == 403
page = adm.get("/admin").data.decode()
assert "Copy link" in page and "Ready - 5 photos" in page
print("ok  admin login, CSRF, event with 2 drives indexed (5 photos)")

def login(client, email):
    r = client.post("/api/otp/send", json={"code": code, "email": email, "consent": True, "name": "Test Guest", "phone": "98765 43210"}, headers=H)
    assert r.status_code == 200, r.json
    r = client.post("/api/otp/verify", json={"code": code, "email": email, "otp": sent[email]}, headers=H)
    assert r.status_code == 200, r.json

g = A.app.test_client()
assert g.get("/e/" + code).status_code == 200 and g.get("/e/wrong").status_code == 404
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "Test Guest", "phone": "98765 43210"}).status_code == 403
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com"}, headers=H).status_code == 400
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "A", "phone": "9876543210"}, headers=H).status_code == 400   # no name
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "Asha", "phone": "12"}, headers=H).status_code == 400       # no phone
assert g.get("/api/me").status_code == 401
assert g.post("/api/selfie", json={"image": img(RED)}, headers=H).status_code == 401
assert g.get("/api/preview/1").status_code == 404
# wrong OTP 5 times locks the code, even for the right one afterwards
g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "Test Guest", "phone": "98765 43210"}, headers=H)
real = sent["a@x.com"]; wrong = "000000" if real != "000000" else "111111"
for _ in range(5):
    assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": wrong}, headers=H).status_code == 400
assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": real}, headers=H).status_code == 429
assert g.get("/api/me").status_code == 401
print("ok  nothing works before OTP; wrong codes lock out")
# expired code
g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "Test Guest", "phone": "98765 43210"}, headers=H)
con.execute("UPDATE otps SET expires_at=? WHERE used=0", (time.time() - 1,)); con.commit()
assert g.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": sent["a@x.com"]}, headers=H).status_code == 400
login(g, "a@x.com")
# code cannot be reused
g2 = A.app.test_client()
assert g2.post("/api/otp/verify", json={"code": code, "email": "a@x.com", "otp": sent["a@x.com"]}, headers=H).status_code == 400
# 4th send in 15 min is rate limited
assert g.post("/api/otp/send", json={"code": code, "email": "a@x.com", "consent": True, "name": "Test Guest", "phone": "98765 43210"}, headers=H).status_code == 429
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
lg = con.execute("SELECT detail FROM audit WHERE action='selfie_matched' ORDER BY id DESC").fetchone()[0]
assert "best score 1.00" in lg and "album has 5 faces" in lg, lg
tr = adm.post(f"/admin/events/{ev['id']}/test", data={"csrf": csrf, "photo": (io.BytesIO(base64.b64decode(img(RED).split(",")[1])), "t.png")}, follow_redirects=True).data.decode()
assert "Album has 5 photos and 5 faces" in tr and "best score 1.00, 2 photos match" in tr, tr[:600]
tb = adm.post(f"/admin/events/{ev['id']}/test", data={"csrf": csrf, "photo": (io.BytesIO(base64.b64decode(img(BLACK).split(",")[1])), "t.png")}, follow_redirects=True).data.decode()
assert "No face was found" in tb
tj = adm.post(f"/admin/events/{ev['id']}/test-selfie", json={"image": img(BLUE)}, headers={"X-CSRF": csrf}).json
ts = tj["text"]
assert "best score 1.00, 3 photos match" in ts, ts
# the admin test shows the same photos a guest with that face would get
assert len(tj["photos"]) == 3 and {x["id"] for x in tj["photos"]} == others, tj
ap = adm.get(f"/admin/photo/{tj['photos'][0]['id']}"); assert ap.status_code == 200 and ap.mimetype == "image/jpeg"
assert A.app.test_client().get(f"/admin/photo/{tj['photos'][0]['id']}").status_code == 403
assert g.get(f"/admin/photo/{tj['photos'][0]['id']}").status_code == 403      # a guest is not an admin
assert adm.post(f"/admin/events/{ev['id']}/test-selfie", json={"image": img(BLUE)}).status_code == 403
# a folder without photos explains itself
rawd = tmp / "raw"; rawd.mkdir(); (rawd / "a.PSD").write_bytes(b"x"); (rawd / "b.mp4").write_bytes(b"x")
adm.post("/admin/events", data={"name": "Raw", "csrf": csrf, "folders": str(rawd)})
rid = con.execute("SELECT id FROM events WHERE name='Raw'").fetchone()[0]
for _ in range(50):
    if indexer.progress.get(rid, {}).get("state") == "finished": break
    time.sleep(0.1)
rst = adm.get("/admin/status").json[str(rid)]["text"]
assert "No photos were found" in rst and ".psd" in rst, rst
(rawd / "broken.jpg").write_bytes(b"not a jpeg"); indexer.index_event(rid)
assert "1 files could not be read" in adm.get("/admin/status").json[str(rid)]["text"]
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
indexer.index_event(ev["id"]); assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 2, indexer.progress[ev["id"]]
print("ok  offline drive keeps index; removed folder is revoked")

# path escape, closed event, sign out
con.execute("UPDATE photos SET relpath='../../data/secret.key' WHERE id=?", (pid,)); con.commit()
assert g.get(f"/api/preview/{pid}").status_code == 404
assert g.post("/api/forget", json={}, headers=H).status_code == 200 and g.get("/api/me").status_code == 401
adm.post(f"/admin/events/{ev['id']}/toggle", data={"csrf": csrf})
assert A.app.test_client().get("/e/" + code).status_code == 404
hd = adm.get("/admin").headers
assert "frame-ancestors 'none'" in hd["Content-Security-Policy"] and hd["Cache-Control"] == "no-store"
# an album without scanned photos says so instead of "0 photos"
empty = tmp / "empty"; empty.mkdir()
adm.post("/admin/events", data={"name": "Empty", "csrf": csrf, "folders": str(empty)})
eev = con.execute("SELECT * FROM events WHERE name='Empty'").fetchone(); time.sleep(0.5)
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
keep = code; code = eev["code"]; eg = A.app.test_client(); login(eg, "e@x.com")
er = eg.post("/api/selfie", json={"image": img(RED)}, headers=H)
assert er.status_code == 409 and "no scanned photos" in er.json["error"]; code = keep

# Google Drive links (Google is replaced by a stand-in; photos: 1 red, 1 blue in a sub-folder)
import gdrive, json as _json
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization as _ser
FID = "1AbCdEfGhIjKlMnOpQrStUv"
assert gdrive.parse_link(f"https://drive.google.com/drive/folders/{FID}?usp=sharing") == FID
assert gdrive.parse_link(f"https://drive.google.com/drive/u/0/folders/{FID}") == FID
assert gdrive.parse_link("https://example.com/folders/abcdefghijkl") is None
assert gdrive.parse_link("gdrive:../../etc") is None
r0 = adm.post("/admin/events", data={"name": "Drive", "csrf": csrf, "folders": f"https://drive.google.com/drive/folders/{FID}"}, follow_redirects=True)
assert b"not connected yet" in r0.data                       # no key file yet
pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
    _ser.Encoding.PEM, _ser.PrivateFormat.PKCS8, _ser.NoEncryption()).decode()
keyfile = _json.dumps({"type": "service_account", "client_email": "robot@proj.iam.gserviceaccount.com", "private_key": pem}).encode()
assert b"not the right file" in adm.post("/admin/google-key", data={"csrf": csrf, "key": (io.BytesIO(b"{}"), "k.json")}, follow_redirects=True).data
assert b"Google Drive connected" in adm.post("/admin/google-key", data={"csrf": csrf, "key": (io.BytesIO(keyfile), "k.json")}, follow_redirects=True).data
a_jwt, _ = gdrive.make_assertion(_json.loads(keyfile)); assert a_jwt.count(".") == 2
def png(color):
    return base64.b64decode(img(color).split(",")[1])
dims = {"width": 640, "height": 480}
tree = {FID: [{"id": "fileRED0001", "name": "a:red?.png", "mimeType": "image/png", "size": "1", "modifiedTime": "t1", "imageMediaMetadata": dims},
              {"id": "subFOLDER01", "name": "day2", "mimeType": gdrive.FOLDER},
              {"id": "docXXXXXXX1", "name": "notes.txt", "mimeType": "text/plain"}],
        "subFOLDER01": [{"id": "fileBLUE001", "name": "blue.png", "mimeType": "image/png", "size": "1", "modifiedTime": "t1", "imageMediaMetadata": dims}]}
colors = {"fileRED0001": RED, "fileBLUE001": BLUE}
gdrive.folder_name = lambda fid: "Sharma wedding" if fid == FID else (_ for _ in ()).throw(gdrive.DriveError("Google cannot see this folder. Share it with robot@proj"))
gdrive.list_children = lambda fid: iter(tree[fid])
calls, fulls = [], []
def fake_small(info, side): calls.append(info["id"]); return png(colors[info["id"]])
def fake_full(file_id): fulls.append(file_id); return png(colors[file_id])
class Stream(io.BytesIO):
    headers = {"Content-Type": "image/png"}
gdrive.fetch_small, gdrive.fetch_full = fake_small, fake_full
gdrive.open_original = lambda file_id: Stream(png(colors[file_id]))
bad = adm.post("/admin/events", data={"name": "X", "csrf": csrf, "folders": "https://drive.google.com/drive/folders/ZZZZZZZZZZZZZZ"}, follow_redirects=True)
assert b"Share it with robot@proj" in bad.data
old_copy = config.DRIVE_DIR / FID; old_copy.mkdir(parents=True); (old_copy / "left-over.jpg").write_bytes(b"x")   # from an older version
adm.post("/admin/events", data={"name": "Drive", "csrf": csrf, "folders": f"https://drive.google.com/drive/folders/{FID}?usp=sharing"})
dev = con.execute("SELECT * FROM events WHERE name='Drive'").fetchone()
assert dev["folders"] == "gdrive:" + FID
def wait(eid):
    for _ in range(100):
        if indexer.progress.get(eid, {}).get("state") in ("finished", "failed"): return
        time.sleep(0.1)
wait(dev["id"]); assert indexer.progress[dev["id"]]["state"] == "finished", indexer.progress[dev["id"]]
assert con.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (dev["id"],)).fetchone()[0] == 2
assert sorted(calls) == ["fileBLUE001", "fileRED0001"] and not fulls
# scanned in place: no Drive photo was written to this computer, and the old copy is gone
assert not old_copy.exists() and not [f for f in config.DRIVE_DIR.rglob("*") if f.is_file()]
indexer.index_event(dev["id"]); assert len(calls) == 2            # second scan reads nothing again
assert "Google Drive: Sharma wedding" in adm.get("/admin").data.decode()
con.execute("DELETE FROM otps"); con.commit()
sent.clear(); dg = A.app.test_client(); code_keep, code = code, dev["code"]; login(dg, "d@x.com")
rr = dg.post("/api/selfie", json={"image": img(RED)}, headers=H).json
assert rr["status"] == "matched" and len(rr["photos"]) == 1, rr
pv = dg.get(f"/api/preview/{rr['photos'][0]}"); assert pv.status_code == 200 and pv.mimetype == "image/jpeg"
got = dg.get(dg.post(f"/api/download-link/{rr['photos'][0]}", json={}, headers=H).json["url"])
assert got.status_code == 200 and "a_red_.png" in got.headers["Content-Disposition"] and got.data == png(RED)
zd = dg.get(dg.post("/api/download-link/all", json={}, headers=H).json["url"])
zf = zipfile.ZipFile(io.BytesIO(zd.data)); assert len(zf.namelist()) == 1 and zf.read(zf.namelist()[0]) == png(RED)
blue_pid = con.execute("SELECT id FROM photos WHERE ref='fileBLUE001'").fetchone()[0]
assert dg.get(f"/api/preview/{blue_pid}").status_code == 404       # someone else's Drive photo
# Google's small version is too small for a big original -> the original is read instead (in memory)
big = indexer._drive_image({"id": "fileRED0001", "imageMediaMetadata": {"width": 6000, "height": 4000}})
assert big is not None and fulls == ["fileRED0001"]
tree[FID].pop(0); indexer.index_event(dev["id"])                 # photo deleted on Drive
assert con.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (dev["id"],)).fetchone()[0] == 1
# a scan that was cut off is remembered and finished later
A.db.kv_set(con, f"scanning:{dev['id']}", "1"); con.commit()
assert indexer.unfinished(con) == [dev["id"]]
indexer.index_event(dev["id"]); assert indexer.unfinished(con) == []
code = code_keep
print("ok  Google Drive link: scanned in place (nothing saved), preview, download, zip, deletion, resume")

# selfie limit: the guest is sent back to the email step, not left stuck
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
keep2 = code; code = dev["code"]; lim = A.app.test_client(); login(lim, "lim@x.com")
con.execute("UPDATE sessions SET selfie_tries=? WHERE email='lim@x.com'", (config.SELFIE_MAX_TRIES,)); con.commit()
assert lim.post("/api/selfie", json={"image": img(RED)}, headers=H).status_code == 401
assert lim.get("/api/me").status_code == 401
con.execute("DELETE FROM otps"); con.commit(); login(lim, "lim@x.com")
assert lim.post("/api/selfie", json={"image": img(RED)}, headers=H).status_code == 200
code = keep2
print("ok  selfie limit sends the guest back to the email step")

# one general link: sign in once, choose the dates, get photos from every album in those dates
pa, pb, pc = tmp / "pa", tmp / "pb", tmp / "pc"
for d_ in (pa, pb, pc): d_.mkdir()
img(RED, pa / "r.png"); img(BLUE, pa / "b.png"); img(RED, pb / "r.png"); img(RED, pc / "r.png")
pid_of = {}
for nm, fd, dt in (("Haldi", pa, "2026-11-01"), ("Wedding", pb, "2026-11-10"), ("Old", pc, "2026-10-01")):
    adm.post("/admin/events", data={"name": nm, "csrf": csrf, "folders": str(fd), "event_date": dt})
    pid_of[nm] = con.execute("SELECT id FROM events WHERE name=?", (nm,)).fetchone()[0]; wait(pid_of[nm])
home = A.app.test_client().get("/"); assert home.status_code == 200 and b'data-portal="1"' in home.data
assert "One link for all events" in adm.get("/admin").data.decode()
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
keep3 = code; code = ""; pg = A.app.test_client(); login(pg, "p@x.com")
def find(frm, to, color=RED):
    return pg.post("/api/selfie", json={"image": img(color), "date_from": frm, "date_to": to}, headers=H)
assert find("", "").status_code == 400 and find("2026-11-30", "2026-11-01").status_code == 400
assert find("2025-01-01", "2025-12-31").status_code == 404          # no album in these dates
r = find("2026-11-01", "2026-11-30").json
assert r["status"] == "matched" and len(r["photos"]) == 2 and [x["name"] for x in r["groups"]] == ["Wedding", "Haldi"], r
assert r["event_code"] == "" and r["date_from"] == "2026-11-01"
assert len(find("2026-10-01", "2026-11-30").json["photos"]) == 3    # wider dates -> one more album
blue_h = con.execute("SELECT id FROM photos WHERE event_id=? AND relpath='b.png'", (pid_of["Haldi"],)).fetchone()[0]
assert pg.get(f"/api/preview/{blue_h}").status_code == 404          # still only the guest's own face
z3 = pg.get(pg.post("/api/download-link/all", json={}, headers=H).json["url"])
assert len(zipfile.ZipFile(io.BytesIO(z3.data)).namelist()) == 3
adm.post(f"/admin/events/{pid_of['Old']}/date", data={"csrf": csrf, "event_date": ""})   # no date -> not in the general link
assert len(find("2026-10-01", "2026-11-30").json["photos"]) == 2
adm.post(f"/admin/events/{pid_of['Wedding']}/approval", data={"csrf": csrf})            # one album asks first
r = find("2026-11-01", "2026-11-30").json; assert r["status"] == "pending" and r["photos"] == [], r
psid = con.execute("SELECT id FROM sessions WHERE email='p@x.com'").fetchone()[0]
assert "General link (2026-11-01 to 2026-11-30)" in adm.get("/admin").data.decode()
adm.post(f"/admin/requests/{psid}/approve", data={"csrf": csrf})
assert len(pg.get("/api/me").json["photos"]) == 2
config.PORTAL = False
assert A.app.test_client().get("/").status_code == 404
assert pg.post("/api/otp/send", json={"code": "", "email": "q@x.com", "consent": True, "name": "Test Guest", "phone": "98765 43210"}, headers=H).status_code == 404
assert find("2026-11-01", "2026-11-30").status_code == 404
config.PORTAL = True; code = keep3
# the scan says where its time goes, and a missing photo says why
stt = adm.get("/admin/status").json[str(pid_of["Haldi"])]["text"]
assert "Last scan: 2 photos" in stt and "to read it" in stt and "to find the faces" in stt, stt
(pa / "r.png").unlink(); gone = con.execute("SELECT id FROM photos WHERE event_id=? AND relpath='r.png'", (pid_of["Haldi"],)).fetchone()[0]
for f in config.CACHE_DIR.glob(f"{gone}-*.jpg"): f.unlink()
why = adm.get(f"/admin/photo/{gone}"); assert why.status_code == 404 and "file not found" in why.get_data(as_text=True), why.data
print("ok  general link: one sign-in, date range over several albums, approval, own photos only; scan timing; missing-photo reason")

# personal sign-in: choose events, save photos for later, admin sees who downloaded what
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
img(RED, pa / "r.png")                       # the photo removed in the check above is back
keep4 = code; code = ""; u1 = A.app.test_client(); login(u1, "u1@x.com")
assert A.app.test_client().get("/api/events?from=2026-11-01&to=2026-11-30").status_code == 401
evs = u1.get("/api/events?from=2026-11-01&to=2026-11-30").json["events"]
assert [e["name"] for e in evs] == ["Wedding", "Haldi"], evs
adm.post(f"/admin/events/{pid_of['Wedding']}/approval", data={"csrf": csrf})      # approval off again
sel = {"image": img(RED), "date_from": "2026-11-01", "date_to": "2026-11-30"}
assert u1.post("/api/selfie", json=dict(sel, events=[]), headers=H).status_code == 400
assert u1.post("/api/selfie", json=dict(sel, events=[999999]), headers=H).status_code == 400
only = u1.post("/api/selfie", json=dict(sel, events=[pid_of["Haldi"]]), headers=H).json
assert only["status"] == "matched" and [x["name"] for x in only["groups"]] == ["Haldi"] and only["name"] == "Test Guest", only
mine1 = only["photos"][0]
assert u1.post(f"/api/bookmark/{blue_h}", json={"on": True}, headers=H).status_code == 404     # cannot save someone else's
assert u1.post(f"/api/bookmark/{mine1}", json={"on": True}).status_code == 403                  # needs our page
assert u1.post(f"/api/bookmark/{mine1}", json={"on": True}, headers=H).json["on"] is True
assert u1.get("/api/me").json["saved"] == [mine1]
u1.get(u1.post(f"/api/download-link/{mine1}", json={}, headers=H).json["url"])
assert u1.post("/api/forget", json={}, headers=H).status_code == 200
# next visit: sign in again, saved photos are there without a new selfie; others still are not
con.execute("DELETE FROM otps"); con.commit(); sent.clear(); u2 = A.app.test_client(); login(u2, "u1@x.com")
bk = u2.get("/api/bookmarks").json["groups"]; assert bk[0]["name"] == "Haldi" and bk[0]["photos"] == [mine1], bk
assert u2.get(f"/api/preview/{mine1}").status_code == 200 and u2.get(f"/api/preview/{blue_h}").status_code == 404
zs = u2.get(u2.post("/api/download-link/saved", json={}, headers=H).json["url"])
assert len(zipfile.ZipFile(io.BytesIO(zs.data)).namelist()) == 1
con.execute("DELETE FROM otps"); con.commit(); sent.clear(); u3 = A.app.test_client(); login(u3, "other@x.com")
assert u3.get(f"/api/preview/{mine1}").status_code == 404 and u3.get("/api/bookmarks").json["groups"] == []
assert u2.post(f"/api/bookmark/{mine1}", json={"on": False}, headers=H).json["on"] is False
assert u2.get(f"/api/preview/{mine1}").status_code == 404           # un-saved and no selfie this visit
apage = adm.get("/admin/guests").data.decode()
assert "Guests and downloads" in apage and "Test Guest" in apage and "9876543210" in apage and "u1@x.com" in apage
assert "u1@x.com" in adm.get("/admin/guests?q=u1@").data.decode() and "u1@x.com" not in adm.get("/admin/guests?q=nobody").data.decode()
csvt = adm.get("/admin/downloads.csv").get_data(as_text=True)
assert "Test Guest,9876543210,u1@x.com,Haldi," in csvt and A.app.test_client().get("/admin/downloads.csv").status_code == 403
assert con.execute("SELECT COUNT(*) FROM downloads WHERE email='u1@x.com'").fetchone()[0] == 2    # one single + one in the zip
# match strictness is set by the admin and applies at once
assert adm.post("/admin/settings", data={"csrf": csrf, "threshold": "0.75"}).status_code == 302 and config.MATCH_THRESHOLD == 0.75
assert adm.post("/admin/settings", data={"csrf": csrf, "threshold": "5"}, follow_redirects=True).data.count(b"Not saved") == 1
assert adm.post("/admin/settings", data={"threshold": "0.5"}).status_code == 403
adm.post("/admin/settings", data={"csrf": csrf, "threshold": "0.45"}); assert config.MATCH_THRESHOLD == 0.45
code = keep4
print("ok  sign-in with name and phone, event choice, saved photos across visits, download record, strictness")

# more photo types: a camera RAW file (its embedded JPEG is used) and, when installed, HEIC
import cv2 as _cv
rawdir = tmp / "rawok"; rawdir.mkdir()
_ok, _jpg = _cv.imencode(".jpg", np.full((1200, 1600, 3), (0, 0, 255), np.uint8))       # red in BGR
(rawdir / "DSC001.ARW").write_bytes(b"II*\x00" + bytes(4096) + _jpg.tobytes() + bytes(2048))
kinds = 1
try:
    import pillow_heif
    pillow_heif.register_heif_opener(); Image.new("RGB", (640, 480), RED).save(rawdir / "IMG_1.heic"); kinds = 2
except ImportError:
    print("    (pillow-heif not installed here - HEIC part skipped)")
adm.post("/admin/events", data={"name": "Formats", "csrf": csrf, "folders": str(rawdir)})
fev = con.execute("SELECT * FROM events WHERE name='Formats'").fetchone(); wait(fev["id"])
assert con.execute("SELECT COUNT(*) FROM faces WHERE event_id=?", (fev["id"],)).fetchone()[0] == kinds, indexer.progress[fev["id"]]
for (fp,) in con.execute("SELECT id FROM photos WHERE event_id=?", (fev["id"],)).fetchall():
    pr = adm.get(f"/admin/photo/{fp}"); assert pr.status_code == 200 and pr.mimetype == "image/jpeg", pr.data
print(f"ok  RAW{' and HEIC' if kinds == 2 else ''} photos are scanned and previewed")

# several ticked photos in one download
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
keep5 = code; code = ""; m1 = A.app.test_client(); login(m1, "multi@x.com")
both = m1.post("/api/selfie", json={"image": img(RED), "date_from": "2026-11-01", "date_to": "2026-11-30"}, headers=H).json["photos"]
assert len(both) == 2
assert m1.post("/api/download-link/selected", json={"ids": both + [blue_h]}, headers=H).status_code == 404     # not all are the guest's
assert m1.post("/api/download-link/selected", json={"ids": []}, headers=H).status_code == 404
zsel = m1.get(m1.post("/api/download-link/selected", json={"ids": both[:1]}, headers=H).json["url"])
assert len(zipfile.ZipFile(io.BytesIO(zsel.data)).namelist()) == 1
code = keep5
print("ok  several ticked photos download as one zip, only the guest's own")

# studio look: name, colour, logo, background; logo on downloaded photos
import brand as B
def png_file(color, size=(300, 120)):
    b = io.BytesIO(); Image.new("RGBA", size, color + (255,)).save(b, "PNG"); b.seek(0); return b
assert A.app.test_client().get("/brand/logo").status_code == 404
sv = adm.post("/admin/brand", data={"csrf": csrf, "brand": "Kerhalkar Studio", "tagline": "Hello guests", "accent": "#0A7E8C", "stamp": "on",
                                    "logo": (png_file((0, 255, 0)), "logo.png"), "background": (png_file((9, 9, 9), (800, 600)), "bg.png")},
              content_type="multipart/form-data")
assert sv.status_code == 302 and config.BRAND == "Kerhalkar Studio" and B.LOGO.exists() and B.BACKGROUND.exists()
pub = A.app.test_client()
assert pub.get("/brand/logo").mimetype == "image/png" and pub.get("/brand/background").mimetype == "image/jpeg"
css = pub.get("/brand/theme.css").get_data(as_text=True); assert "--accent:#0A7E8C" in css and "/brand/background" in css
hp = pub.get("/").get_data(as_text=True); assert "Hello guests" in hp and "/brand/logo" in hp and "Kerhalkar Studio" in hp
assert adm.post("/admin/brand", data={"csrf": csrf, "brand": "Kerhalkar Studio", "stamp": "on", "accent": "#0A7E8C", "logo": (io.BytesIO(b"not a picture"), "a.png")},
                content_type="multipart/form-data", follow_redirects=True).data.count(b"not a picture") == 1
assert A.app.test_client().post("/admin/brand", data={"brand": "Hack"}).status_code == 403
got = m1.get(m1.post(f"/api/download-link/{both[0]}", json={}, headers=H).json["url"])
marked = Image.open(io.BytesIO(got.data)).convert("RGB")
assert got.mimetype == "image/jpeg" and ".jpg" in got.headers["Content-Disposition"], got.headers
w_, h_ = marked.size; px = marked.getpixel((w_ - int(max(marked.size) * 0.018) - 12, int(max(marked.size) * 0.018) + 6))
assert px[1] > 200 and px[0] < 80, px                                 # the green logo sits top right
assert marked.getpixel((10, h_ - 10))[0] > 200                        # the rest is still the red photo
adm.post("/admin/brand", data={"csrf": csrf, "brand": "Test Studio", "accent": "#E8A317"})        # logo on downloads off again
plain = m1.get(m1.post(f"/api/download-link/{both[0]}", json={}, headers=H).json["url"]); assert plain.mimetype == "image/png"
print("ok  studio name, colour, logo and background change; logo is put on downloaded photos when switched on")

# next events and offers, before and after sign-in
adm.post("/admin/promos", data={"csrf": csrf, "kind": "event", "title": "Diwali Mela shoot", "body": "Stall 4", "event_date": "2099-10-20",
                                "show_before": "on", "image": (png_file((1, 2, 3), (640, 360)), "p.png")}, content_type="multipart/form-data")
adm.post("/admin/promos", data={"csrf": csrf, "kind": "ad", "title": "Album printing offer", "link": "https://example.com/offer", "show_after": "on"})
adm.post("/admin/promos", data={"csrf": csrf, "kind": "event", "title": "Past event", "event_date": "2020-01-01", "show_before": "on", "show_after": "on"})
assert b"Not added" in adm.post("/admin/promos", data={"csrf": csrf, "title": "Bad", "link": "javascript:alert(1)"}, follow_redirects=True).data
hp = pub.get("/").get_data(as_text=True)
before, after = hp.split('id="after"')
assert "Diwali Mela shoot" in before and "Album printing offer" not in before and "Album printing offer" in after and "Past event" not in hp
assert 'href="https://example.com/offer"' in after and "javascript:" not in hp
pr1 = con.execute("SELECT id FROM promos WHERE title='Diwali Mela shoot'").fetchone()[0]
assert pub.get(f"/brand/promo/{pr1}").mimetype == "image/jpeg"
adm.post(f"/admin/promos/{pr1}/toggle", data={"csrf": csrf}); assert "Diwali Mela shoot" not in pub.get("/").get_data(as_text=True)
adm.post(f"/admin/promos/{pr1}/delete", data={"csrf": csrf}); assert pub.get(f"/brand/promo/{pr1}").status_code == 404
print("ok  next events and offers show before / after sign-in; past events hide; unsafe links refused")

# staff accounts: can manage albums, nothing else
import re
assert b"at least 8" in adm.post("/admin/team", data={"csrf": csrf, "username": "ravi", "password": "short"}, follow_redirects=True).data
adm.post("/admin/team", data={"csrf": csrf, "username": "Ravi", "password": "ravi-pass-1"})
st = A.app.test_client()
assert b"Wrong user name or password" in st.post("/admin/login", data={"username": "ravi", "password": "nope-nope"}).data
assert st.post("/admin/login", data={"username": "ravi", "password": "ravi-pass-1"}).status_code == 302
spage = st.get("/admin").get_data(as_text=True); scsrf = re.search(r'name="csrf" value="(\w+)"', spage).group(1)
assert "Signed in as ravi" in spage and "Guests and downloads" not in spage and "Add a new album" in spage
for path in ("/admin/guests", "/admin/brand", "/admin/team", "/admin/settings"):
    assert st.get(path).status_code == 302, path
assert st.get("/admin/downloads.csv").status_code == 403
assert st.post("/admin/settings", data={"csrf": scsrf, "threshold": "0.31"}).status_code == 403 and config.MATCH_THRESHOLD == 0.45
assert st.post("/admin/brand", data={"csrf": scsrf, "brand": "Hack"}).status_code == 403
assert st.post("/admin/team", data={"csrf": scsrf, "username": "evil", "password": "evil-pass-1"}).status_code == 403
sd = tmp / "staffalbum"; sd.mkdir(); img(RED, sd / "s.png")
assert st.post("/admin/events", data={"csrf": scsrf, "name": "Staff album", "folders": str(sd), "event_date": "2026-12-01"}).status_code == 302
sid_ = con.execute("SELECT id FROM events WHERE name='Staff album'").fetchone()[0]; wait(sid_)
assert con.execute("SELECT detail FROM audit WHERE action='album_created' ORDER BY id DESC").fetchone()[0].endswith("by ravi")
rid_ = con.execute("SELECT id FROM staff WHERE username='ravi'").fetchone()[0]
adm.post(f"/admin/team/{rid_}/toggle", data={"csrf": csrf})                # blocked: the open session stops working at once
assert st.get("/admin").status_code == 302 and st.post("/admin/login", data={"username": "ravi", "password": "ravi-pass-1"}).status_code == 200
adm.post(f"/admin/team/{rid_}/toggle", data={"csrf": csrf}); adm.post(f"/admin/team/{rid_}/password", data={"csrf": csrf, "password": "new-pass-22"})
assert st.post("/admin/login", data={"username": "ravi", "password": "ravi-pass-1"}).status_code == 200
assert st.post("/admin/login", data={"username": "ravi", "password": "new-pass-22"}).status_code == 302
print("ok  staff can add and scan albums only; blocking a staff member works at once")

# many albums: search and pages; files that were not scanned are listed
for i in range(23):
    con.execute("INSERT INTO events(name,code,folders,created_at,event_date) VALUES(?,?,?,?,?)", (f"Bulk {i:02d}", f"bulk{i}", "", time.time(), "2031-05-01"))
con.commit()
p1 = adm.get("/admin").get_data(as_text=True)
assert p1.count('<details class="album"') == 20 and "Page 1 of" in p1 and "Bulk 22" in p1 and "Bulk 01" not in p1
assert "Bulk 01" in adm.get("/admin?page=2").get_data(as_text=True)
one = adm.get("/admin?q=Bulk 07").get_data(as_text=True); assert one.count('<details class="album"') == 1 and "1 album matching" in one
assert adm.get("/admin?q=2031-05").get_data(as_text=True).count('<details class="album"') == 20
assert adm.get("/admin?q=zzzz").get_data(as_text=True).count("No album matches this search") == 1
assert list(adm.get(f"/admin/status?ids={sid_}").json) == [str(sid_)]
con.execute("DELETE FROM events WHERE name LIKE 'Bulk %'"); con.commit()
(sd / "damaged.jpg").write_bytes(b"not really a jpg"); indexer.index_event(sid_)
errs = con.execute("SELECT relpath, reason FROM scan_errors WHERE event_id=?", (sid_,)).fetchall()
assert len(errs) == 1 and errs[0]["relpath"].endswith("damaged.jpg") and "readable" in errs[0]["reason"], [tuple(e) for e in errs]
pg = adm.get("/admin?q=Staff album").get_data(as_text=True); assert "Files that were not scanned (1)" in pg and "damaged.jpg" in pg
assert adm.post("/admin/settings", data={"csrf": csrf, "scan_side": "1600"}).status_code == 302 and config.SCAN_SIDE == 1600
assert adm.post("/admin/settings", data={"csrf": csrf, "scan_side": "999"}).status_code == 302 and config.SCAN_SIDE == 1600
adm.post("/admin/settings", data={"csrf": csrf, "scan_side": "1280"})
print("ok  albums are searched and paged; unscanned files are listed with the reason; scan detail setting")

# matching rules, with made-up signatures: person P has 8 album faces; a look-alike resembles the selfie only
rng = np.random.default_rng(7)
def unit(v): return (v / np.linalg.norm(v)).astype(np.float32)
P = unit(rng.normal(size=128))
album_faces = [unit(P + 0.055 * rng.normal(size=128)) for _ in range(8)]              # the person, eight times
selfie_v = unit(P + 0.07 * rng.normal(size=128))                                       # the selfie differs a bit more
other = unit(rng.normal(size=128)); other = unit(other - (other @ selfie_v) * selfie_v)
lookalike = unit(0.52 * selfie_v + 0.854 * other)                                      # 0.52 alike to the selfie only
stranger = unit(rng.normal(size=128))
M = np.vstack(album_faces + [lookalike, stranger]); big = np.full(len(M), 150, np.float32)
assert float(lookalike @ selfie_v) > 0.5 and all(float(f @ selfie_v) > 0.5 for f in album_faces)
found_, _ = indexer.match(M, big, selfie_v, 0.45)
assert sorted(found_.tolist()) == list(range(8)), found_          # all eight kept, the look-alike dropped by the group check
few, _ = indexer.match(M[[0, 1, 8]], big[:3], selfie_v, 0.45)
assert sorted(few.tolist()) == [0, 1, 2]                          # with fewer than four matches the group check cannot run
weak = unit(0.5 * selfie_v + 0.866 * other)
tiny, _ = indexer.match(np.vstack([weak]), np.array([30], np.float32), selfie_v, 0.45)
normal_, _ = indexer.match(np.vstack([weak]), np.array([150], np.float32), selfie_v, 0.45)
assert len(tiny) == 0 and len(normal_) == 1                      # a tiny face needs a clearly higher score
print("ok  matching: group check drops a look-alike, tiny faces need a higher score")

# "Scan every photo again" re-reads all photos of an album
before_ids = [r[0] for r in con.execute("SELECT id FROM photos WHERE event_id=?", (sid_,))]
adm.post(f"/admin/events/{sid_}/rescan", data={"csrf": csrf}); wait(sid_)
assert indexer.progress[sid_]["scanned"] >= 1 and [r[0] for r in con.execute("SELECT id FROM photos WHERE event_id=?", (sid_,))] == before_ids
assert con.execute("SELECT size FROM faces WHERE event_id=?", (sid_,)).fetchone() is not None

# download report: totals, date filter, one guest's page, Excel file by date and by guest
con.execute("INSERT INTO downloads(ts,email,photo_id,event_id,relpath,ip) VALUES(?,?,?,?,?,?)",
            (time.mktime(time.strptime("2020-03-05", "%Y-%m-%d")) + 3600, "u1@x.com", 1, pid_of["Haldi"], "old/long-ago.jpg", "1.1.1.1"))
con.commit()
rep_ = adm.get("/admin/guests").get_data(as_text=True)
assert "photos downloaded today" in rep_ and "Albums with the most downloads" in rep_ and "Haldi" in rep_
old_ = adm.get("/admin/guests?from=2020-03-01&to=2020-03-31").get_data(as_text=True)
assert "1 photos downloaded by 1 guests" in old_ and "u1@x.com" in old_ and "multi@x.com" not in old_
assert "multi@x.com" in adm.get("/admin/guests?only=downloaded").get_data(as_text=True)
one_ = adm.get("/admin/guest?email=u1@x.com").get_data(as_text=True)
assert "old/long-ago.jpg" in one_ and "Test Guest" in one_ and "9876543210" in one_
assert adm.get("/admin/guest?email=nobody@x.com").status_code == 302 and st.get("/admin/guest?email=u1@x.com").status_code == 302
csv_old = adm.get("/admin/downloads.csv?from=2020-03-01&to=2020-03-31").get_data(as_text=True)
assert csv_old.count("\n") == 2 and "old/long-ago.jpg" in csv_old
csv_one = adm.get("/admin/downloads.csv?email=multi@x.com").get_data(as_text=True)
assert "multi@x.com" in csv_one and "u1@x.com" not in csv_one
con.executemany("INSERT INTO downloads(ts,email,photo_id,event_id,relpath,ip) VALUES(?,?,?,?,?,?)",
                [(time.time(), "u1@x.com", i, pid_of["Haldi"], f"bulk/{i}.jpg", "") for i in range(5000)]); con.commit()
t0 = time.time(); big_csv = adm.get("/admin/downloads.csv").get_data(as_text=True); page_ = adm.get("/admin/guests"); took = time.time() - t0
assert big_csv.count("\n") > 5000 and page_.status_code == 200 and took < 5, took
assert "Page 1 of" in adm.get("/admin/guest?email=u1@x.com").get_data(as_text=True)
con.execute("DELETE FROM downloads WHERE relpath LIKE 'bulk/%' OR relpath='old/long-ago.jpg'"); con.commit()
print("ok  full rescan; download report with totals, dates, per-guest page and Excel export")

# looking before signing in: previews yes, downloads and saving only after the email code
con.execute("DELETE FROM otps"); con.commit(); sent.clear()
keep7 = code; code = ""; lk = A.app.test_client()
assert lk.post("/api/look", json={"code": ""}, headers=H).status_code == 400            # consent is still needed
assert lk.post("/api/look", json={"code": "", "consent": True}).status_code == 403      # only from our page
assert lk.post("/api/look", json={"code": "", "consent": True}, headers=H).status_code == 200
stl = lk.get("/api/me").json; assert stl["looking"] is True and stl["email"] == "" and stl["status"] == "verified"
seen = lk.post("/api/selfie", json={"image": img(RED), "date_from": "2026-11-01", "date_to": "2026-11-30"}, headers=H).json
assert seen["status"] == "matched" and len(seen["photos"]) == 2 and seen["looking"] is True
assert lk.get(f"/api/preview/{seen['photos'][0]}").status_code == 200 and lk.get(f"/api/preview/{blue_h}").status_code == 404
for path, body in ((f"/api/download-link/{seen['photos'][0]}", {}), ("/api/download-link/all", {}),
                   ("/api/download-link/selected", {"ids": seen["photos"]}), (f"/api/bookmark/{seen['photos'][0]}", {"on": True})):
    r_ = lk.post(path, json=body, headers=H); assert r_.status_code == 403 and r_.json["signin"] is True, path
n_dl = con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
login(lk, "look@x.com")                                                               # same browser signs in: the photos stay
after_ = lk.get("/api/me").json; assert after_["looking"] is False and after_["email"] == "look@x.com" and after_["photos"] == seen["photos"]
assert lk.get(lk.post(f"/api/download-link/{seen['photos'][0]}", json={}, headers=H).json["url"]).status_code == 200
assert con.execute("SELECT COUNT(*) FROM downloads WHERE email='look@x.com'").fetchone()[0] == 1 and con.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == n_dl + 1
# "This is not me" takes a photo out of the results and tells the owner its score
assert lk.post(f"/api/not-me/{blue_h}", json={}, headers=H).status_code == 404
assert lk.post(f"/api/not-me/{seen['photos'][1]}", json={}, headers=H).status_code == 200
assert lk.get("/api/me").json["photos"] == seen["photos"][:1] and lk.get(f"/api/preview/{seen['photos'][1]}").status_code == 404
assert 'pressed "This is not me" on 1 photo' in adm.get("/admin/settings").get_data(as_text=True).replace("&#34;", '"')
# the owner can switch looking-first off
adm.post("/admin/settings", data={"csrf": csrf, "what": "look_first"})
assert A.app.test_client().post("/api/look", json={"code": "", "consent": True}, headers=H).status_code == 404
assert 'data-look=""' in A.app.test_client().get("/").get_data(as_text=True)
adm.post("/admin/settings", data={"csrf": csrf, "what": "look_first", "look_first": "on"})
assert 'data-look="1"' in A.app.test_client().get("/").get_data(as_text=True)
code = keep7
print("ok  looking without a code shows previews only; signing in keeps the photos; 'not me' is recorded")

# the studio logo is on previews too (top right)
import brand as B2
assert B2.LOGO.exists()
pv_ = lk.get(f"/api/preview/{seen['photos'][0]}"); im_ = Image.open(io.BytesIO(pv_.data)).convert("RGB")
m_ = int(max(im_.size) * 0.018); px_ = im_.getpixel((im_.width - m_ - 14, m_ + 5))
assert px_[1] > 150 and px_[0] < 120, px_
print("ok  previews carry the studio logo")

# quick photo page: a small copy is kept from the scan, tiles are small, the browser may keep previews
pid0 = seen["photos"][0]
assert indexer.thumb_path(pid0).exists()
small_ = lk.get(f"/api/preview/{pid0}?s=t"); large_ = lk.get(f"/api/preview/{pid0}")
assert small_.status_code == 200 and max(Image.open(io.BytesIO(small_.data)).size) <= config.THUMB_SIDE < max(Image.open(io.BytesIO(large_.data)).size)
assert small_.headers["Cache-Control"] == "private, max-age=3600" and "X-Keep" not in small_.headers
assert lk.get("/api/me").headers["Cache-Control"] == "no-store" and A.app.test_client().get(f"/api/preview/{pid0}?s=t").status_code == 404
# original not reachable (drive off): the tile still loads from the kept copy, the large view cannot
real_ = A.real_path; A.real_path = lambda *a_, **k_: A.abort(404)
tag_ = config.PREVIEW_TAG; config.PREVIEW_TAG = "fresh"                # forces both to be made again
assert lk.get(f"/api/preview/{pid0}?s=t").status_code == 200 and lk.get(f"/api/preview/{pid0}").status_code == 404
config.PREVIEW_TAG = tag_; A.real_path = real_
print("ok  small tiles come from the copy kept by the scan; previews may be kept by the browser")

# the port stays the same from start to start
import socket as _so
import netutil
memo = tmp / "port.txt"; first = netutil.sticky_port("127.0.0.1", 39950, memo)
assert memo.read_text() == str(first) and netutil.sticky_port("127.0.0.1", 40500, memo) == first
hold = _so.socket(); hold.bind(("127.0.0.1", first)); hold.listen()
try:
    netutil.sticky_port("127.0.0.1", 40500, memo); raise SystemExit("a busy fixed port must not be replaced silently")
except RuntimeError as e_:
    assert "already running" in str(e_)
hold.close()
print("ok  one fixed port, remembered; a busy port is reported, not replaced")

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
print("ok  path escape blocked, sign-out erases, closed link is dead")


# ---- a crash is logged with a reference, and the visitor sees a friendly page
_A = A
_orig_home = _A.guest_page
def _boom(*a, **k):
    raise RuntimeError("test crash")
_A.guest_page = _boom
_c = _A.app.test_client()
_r = _c.get("/")
assert _r.status_code == 500 and b"Reference" in _r.data and b"Traceback" not in _r.data
_j = _c.post("/api/otp/send", json={}, headers={"X-Requested-With": "facefind"})
assert _j.status_code != 200
_A.guest_page = _orig_home
_log = (config.DATA_DIR / "error.log").read_text(encoding="utf-8")
assert "test crash" in _log and "ref " in _log
print("ok crash page")

# an old album can be deleted; the report of downloads stays
import db as _db
_c = _db.connect()
_n0 = _c.execute("SELECT COUNT(*) FROM photos WHERE event_id=?", (ev["id"],)).fetchone()[0]
assert _n0 > 0
_dl0 = _c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
assert adm.post(f"/admin/events/{ev['id']}/delete").status_code == 403          # needs the admin form token
_r = adm.post(f"/admin/events/{ev['id']}/delete", data={"csrf": csrf}, follow_redirects=True)
assert b"deleted" in _r.data
for _t in ("events", "photos", "faces"):
    assert _c.execute(f"SELECT COUNT(*) FROM {_t} WHERE " + ("id" if _t == "events" else "event_id") + "=?", (ev["id"],)).fetchone()[0] == 0, _t
assert _c.execute("SELECT COUNT(*) FROM downloads").fetchone()[0] == _dl0
assert adm.get("/admin").status_code == 200
_c.close()
print("ok  an old album can be deleted, downloads report kept")

# a damaged database repairs itself instead of stopping the app (own file, so
# Windows' open handles on the live database cannot get in the way)
import db
_live = config.DB_PATH
config.DB_PATH = config.DATA_DIR / "repair-test.db"
try:
    db.init()
    _con = db.connect(); _con.execute("INSERT OR REPLACE INTO kv(k,v) VALUES('repair-test','kept')"); _con.commit(); _con.close()
    assert db.backup_now()                          # a good copy exists before the damage
    with open(config.DB_PATH, "r+b") as _f:         # wreck the start of the file
        _f.seek(0); _f.write(b"\x00garbage" * 800)
    for _x in ("-wal", "-shm"):
        try: config.DB_PATH.with_name(config.DB_PATH.name + _x).unlink()
        except OSError: pass
    db.init()                                       # must not raise
    _con = db.connect(); assert _con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    _con.execute("SELECT count(*) FROM events"); _con.close()
    assert list((config.DATA_DIR / "broken-db").glob("*/repair-test.db"))
    _con = db.connect(); assert _con.execute("SELECT v FROM kv WHERE k='repair-test'").fetchone()[0] == "kept"; _con.close()
finally:
    config.DB_PATH = _live
print("ok  damaged database is kept aside, rebuilt, and the app starts")
# the face matrix stays in memory while a scan adds photos, and refreshes later
import numpy as np, brand as _brand
_c = _db.connect()
def _faces(n):
    for _ in range(n):
        _c.execute("INSERT INTO faces(photo_id,event_id,embedding,size) VALUES(1,9999,?,?)",
                   (np.random.rand(128).astype(np.float32).tobytes(), "90"))
    _c.commit()
_faces(5)
assert len(indexer._matrix(_c, 9999)[1]) == 5
_faces(3); indexer._drop_cache(9999, soft=True)
assert len(indexer._matrix(_c, 9999)[1]) == 5                # still the one in memory (fresh enough)
_old = config.CACHE_REFRESH; config.CACHE_REFRESH = 0
assert len(indexer._matrix(_c, 9999)[1]) == 8                # refreshed
config.CACHE_REFRESH = _old
indexer._drop_cache(9999); _c.execute("DELETE FROM faces WHERE event_id=9999"); _c.commit(); _c.close()
assert _brand.DEFAULTS["stamp"] == "1"                       # logo on downloads is on by default
_r = pub.get("/static/style.css?v=1"); assert "immutable" in _r.headers["Cache-Control"]
assert "static/style.css?v=" in pub.get("/").get_data(as_text=True)
print("ok  matrix kept in memory during a scan; logo on by default; static files cached")

# one port answers both http:// and https://, by 127.0.0.1 and by localhost
import socket, ssl, threading, urllib.request, netutil
from flask import Flask as _F
_app = _F("dual"); _app.add_url_rule("/x", "x", lambda: "pong")
_so = socket.socket(); _so.bind(("127.0.0.1", 0)); _pt = _so.getsockname()[1]; _so.close()
_cert, _key = netutil.ensure_cert(config.DATA_DIR / "tls-dual", ["localhost", "127.0.0.1"])
threading.Thread(target=netutil.serve_dual, args=(_app, "127.0.0.1", _pt, _cert, _key), daemon=True).start()
_ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
for _i in range(50):
    try: urllib.request.urlopen(f"http://127.0.0.1:{_pt}/x", timeout=3); break
    except Exception: time.sleep(0.1)
for _u in (f"http://127.0.0.1:{_pt}/x", f"https://127.0.0.1:{_pt}/x", f"http://localhost:{_pt}/x", f"https://localhost:{_pt}/x"):
    assert urllib.request.urlopen(_u, timeout=5, context=_ctx).read() == b"pong", _u
print("ok  one port answers http and https on 127.0.0.1 and localhost")
print("ALL TESTS PASSED")
