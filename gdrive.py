"""Google Drive links.

The app reads Drive through a "robot" Google account (a service account).
Any folder that is shared with the robot's email - or set to "Anyone with
the link" - can be pasted as a link. Photos are copied once into the app's
own folder (data/drive/...) and then scanned like any other folder; later
scans only fetch new or changed photos.
"""
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import config

API = "https://www.googleapis.com/drive/v3/"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
ID_RE = re.compile(r"[A-Za-z0-9_-]{10,}")
_token = {"value": None, "exp": 0}
_lock = threading.Lock()


class DriveError(Exception):
    pass


# ------------------------------------------------------------- key / links
def key_info():
    try:
        d = json.loads(config.GOOGLE_KEY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("type") == "service_account" and d.get("client_email") and d.get("private_key"):
        return d
    return None


def robot_email():
    k = key_info()
    return k["client_email"] if k else ""


def parse_link(text):
    """Drive folder link (or 'gdrive:ID') -> folder id, else None."""
    text = (text or "").strip()
    if text.startswith("gdrive:"):
        fid = text[7:]
        return fid if ID_RE.fullmatch(fid) else None
    if "drive.google.com" not in text:
        return None
    m = (re.search(r"/folders/([A-Za-z0-9_-]{10,})", text)
         or re.search(r"[?&]id=([A-Za-z0-9_-]{10,})", text))
    return m.group(1) if m else None


def local_root(entry):
    """Where an album source lives on disk: the folder itself, or the local
    copy of a Drive folder."""
    fid = parse_link(entry) if entry.startswith("gdrive:") else None
    return str(config.DRIVE_DIR / fid) if fid else entry


def label(entry):
    if not entry.startswith("gdrive:"):
        return entry
    try:
        name = (Path(local_root(entry)) / ".name").read_text(encoding="utf-8")
    except OSError:
        name = entry[7:]
    return f"Google Drive: {name}"


# ------------------------------------------------------------------ access
def _b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def make_assertion(info, now=None):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    now = int(now or time.time())
    aud = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps({"iss": info["client_email"], "scope": SCOPE, "aud": aud,
                            "iat": now, "exp": now + 3600}).encode())
    key = serialization.load_pem_private_key(info["private_key"].encode(), password=None)
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{_b64(sig)}", aud


def _post_form(url, fields):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def token():
    with _lock:
        if _token["value"] and _token["exp"] > time.time():
            return _token["value"]
        info = key_info()
        if not info:
            raise DriveError("Google Drive is not connected yet. Upload the key file on the admin page.")
        try:
            assertion, aud = make_assertion(info)
            r = _post_form(aud, {
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion})
        except urllib.error.HTTPError:
            raise DriveError("Google did not accept the key file. Create a new key and upload it again.")
        except Exception as e:
            raise DriveError(f"Could not reach Google ({e}).")
        _token.update(value=r["access_token"], exp=time.time() + int(r.get("expires_in", 3600)) - 120)
        return _token["value"]


def _explain(e):
    try:
        text = e.read().decode("utf-8", "replace")
    except Exception:
        text = ""
    if e.code == 404:
        return (f"Google cannot see this folder. Share it with {robot_email()} "
                "(Viewer is enough) and try again.")
    if e.code == 403 and ("accessNotConfigured" in text or "has not been used" in text
                          or "is disabled" in text):
        return "The Google Drive API is not switched on in your Google Cloud project."
    if e.code == 403:
        return f"No permission. Share the folder with {robot_email()} and try again."
    if e.code == 401:
        return "Google did not accept the key file. Upload it again."
    return f"Google Drive error {e.code}."


def _open(url):
    """GET with the robot's access; retries when Google is busy."""
    for attempt in range(5):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise DriveError(_explain(e))
        except (urllib.error.URLError, OSError) as e:
            if attempt < 4:
                time.sleep(2 ** attempt)
                continue
            raise DriveError(f"Could not reach Google Drive ({e}).")


def _get_json(path, params):
    with _open(API + path + "?" + urllib.parse.urlencode(params)) as r:
        return json.loads(r.read())


def folder_name(fid):
    d = _get_json("files/" + fid, {"fields": "id,name,mimeType", "supportsAllDrives": "true"})
    if d.get("mimeType") != FOLDER:
        raise DriveError("That link is a single file. Please paste the link of a FOLDER.")
    return d.get("name", fid)


def list_children(fid):
    out, page = [], None
    while True:
        params = {
            "q": f"'{fid}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,shortcutDetails)",
            "pageSize": "1000", "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true"}
        if page:
            params["pageToken"] = page
        d = _get_json("files", params)
        out += d.get("files", [])
        page = d.get("nextPageToken")
        if not page:
            return out


def download_file(file_id, dest):
    part = dest.with_name(dest.name + ".part")
    try:
        with _open(API + "files/" + file_id + "?alt=media&supportsAllDrives=true") as r, \
                open(part, "wb") as f:
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                f.write(chunk)
        part.replace(dest)
    except OSError as e:
        part.unlink(missing_ok=True)
        raise DriveError(f"Download stopped ({e}).")


# -------------------------------------------------------------------- sync
def _safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "unnamed"


def walk(fid, prefix="", seen=None):
    """Every photo below a Drive folder -> (relative path, file info)."""
    seen = seen if seen is not None else set()
    if fid in seen:
        return
    seen.add(fid)
    used = set()
    for f in list_children(fid):
        name, file_id, mime = _safe(f.get("name", "")), f["id"], f.get("mimeType", "")
        sc = f.get("shortcutDetails") or {}
        if sc.get("targetId"):                       # shortcut -> the real thing
            file_id, mime = sc["targetId"], sc.get("targetMimeType", "")
        if name.lower() in used:                     # Drive allows same names
            p = Path(name)
            name = f"{p.stem}_{file_id[:6]}{p.suffix}"
        used.add(name.lower())
        if mime == FOLDER:
            yield from walk(file_id, prefix + name + "/", seen)
        elif Path(name).suffix.lower() in config.IMAGE_EXT:
            yield prefix + name, dict(f, id=file_id)


def sync(fid, on_progress=None):
    """Copies new / changed photos of a Drive folder to the local copy and
    removes photos that were deleted on Drive. Returns (photos, failed, note)."""
    dest = config.DRIVE_DIR / fid
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".name").write_text(folder_name(fid), encoding="utf-8")
    manifest_f = dest / ".manifest.json"
    try:
        old = json.loads(manifest_f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        old = {}
    items = list(walk(fid))
    new, failed, note = {}, 0, ""
    for i, (rel, f) in enumerate(items, 1):
        stamp = f"{f.get('size')}|{f.get('modifiedTime')}"
        target, prev = dest / rel, old.get(f["id"])
        if not (prev and prev["rel"] == rel and prev["stamp"] == stamp and target.exists()):
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                download_file(f["id"], target)
            except DriveError as e:
                failed, note = failed + 1, str(e)
                if prev and (dest / prev["rel"]).exists():
                    new[f["id"]] = prev
                continue
            if prev and prev["rel"] != rel:
                (dest / prev["rel"]).unlink(missing_ok=True)
        new[f["id"]] = {"rel": rel, "stamp": stamp}
        if on_progress:
            on_progress(i, len(items))
    keep = {v["rel"] for v in new.values()}
    for prev in old.values():
        if prev["rel"] not in keep:
            (dest / prev["rel"]).unlink(missing_ok=True)
    manifest_f.write_text(json.dumps(new), encoding="utf-8")
    return len(items), failed, note
