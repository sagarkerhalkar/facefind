"""Google Drive links - photos are read IN PLACE, never copied to this PC.

The app reads Drive through a "robot" Google account (a service account).
Any folder that is shared with the robot's email - or set to "Anyone with
the link" - can be pasted as a link.

Scanning: each photo is streamed from Drive into memory, its face
signatures are stored, and the picture is thrown away. Drive is asked for
a ready-made 1600-pixel version (the size the scan uses anyway), so about
0.3 MB travels per photo instead of the 5-25 MB original.
Previews and downloads are streamed from Drive at the moment a guest asks.
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
import db

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


def is_drive(entry):
    return entry.startswith("gdrive:")


def remember_name(fid, name):
    con = db.connect()
    try:
        db.kv_set(con, "drive_name:" + fid, name)
        con.commit()
    finally:
        con.close()


def known_name(fid):
    con = db.connect()
    try:
        return db.kv_get(con, "drive_name:" + fid)
    finally:
        con.close()


def label(entry):
    if not is_drive(entry):
        return entry
    return f"Google Drive: {known_name(entry[7:]) or entry[7:]}"


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


def _explain(e, text=""):
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


def _open(url, tries=5):
    """GET with the robot's access; waits and retries when Google is busy."""
    for attempt in range(tries):
        req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token()})
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                text = ""
            busy = e.code in (429, 500, 502, 503, 504) or (
                e.code == 403 and "ateLimitExceeded" in text)
            if busy and attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise DriveError(_explain(e, text))
        except (urllib.error.URLError, OSError) as e:
            if attempt < tries - 1:
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


FILE_FIELDS = ("id,name,mimeType,size,modifiedTime,md5Checksum,thumbnailLink,"
               "imageMediaMetadata(width,height),shortcutDetails")


def list_children(fid):
    """One folder's content, 1000 entries per request, as a stream."""
    page = None
    while True:
        params = {
            "q": f"'{fid}' in parents and trashed=false",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
            "pageSize": "1000", "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true"}
        if page:
            params["pageToken"] = page
        d = _get_json("files", params)
        yield from d.get("files", [])
        page = d.get("nextPageToken")
        if not page:
            return


def file_meta(file_id):
    return _get_json("files/" + file_id, {"fields": FILE_FIELDS, "supportsAllDrives": "true"})


def _safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "unnamed"


def stamp(f):
    """Changes whenever the photo on Drive changes."""
    return f"{f.get('size')}|{f.get('modifiedTime')}|{f.get('md5Checksum', '')}"


def walk(fid, prefix="", seen=None):
    """Every photo below a Drive folder -> (relative path, file info).
    Only the list is read here - no photo is fetched."""
    seen = seen if seen is not None else set()
    if fid in seen:
        return
    seen.add(fid)
    used, folders = set(), []
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
            folders.append((file_id, prefix + name + "/"))
        elif Path(name).suffix.lower() in config.IMAGE_EXT:
            yield prefix + name, dict(f, id=file_id)
    for file_id, sub in folders:
        yield from walk(file_id, sub, seen)


# ------------------------------------------------- reading photos in place
_SIZE_RE = re.compile(r"=s\d+[^/=]*$")


def _sized(link, side):
    """Drive's small-picture link, changed to ask for `side` pixels."""
    if link and _SIZE_RE.search(link):
        return _SIZE_RE.sub(f"=s{side}", link)
    return None


def _read(url, limit, tries=5):
    with _open(url, tries) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise DriveError("This file is too large to read.")
    return data


def fetch_small(info, side):
    """The photo at about `side` pixels, made by Google - into memory only.
    Returns None when Drive has no such version (the caller then reads the
    original). `info` is the file's entry from the folder list."""
    url = _sized(info.get("thumbnailLink"), side)
    if url:
        try:
            return _read(url, 30 * 1024 * 1024, tries=2)
        except DriveError:
            pass                                    # these links expire after some hours
    try:
        url = _sized(file_meta(info["id"]).get("thumbnailLink"), side)
        return _read(url, 30 * 1024 * 1024, tries=2) if url else None
    except DriveError:
        return None


def fetch_full(file_id):
    """The original photo into memory (used when no smaller version exists)."""
    return _read(API + "files/" + file_id + "?alt=media&supportsAllDrives=true",
                 config.MAX_PHOTO_BYTES)


def open_original(file_id):
    """A stream of the original photo, for sending it on to a guest."""
    return _open(API + "files/" + file_id + "?alt=media&supportsAllDrives=true")


def drop_old_copy(fid):
    """Earlier versions of this app copied Drive photos to data/drive. That
    copy is not used any more, so the disk space is given back."""
    import shutil
    old = config.DRIVE_DIR / fid
    if old.is_dir() and config.DRIVE_DIR.resolve() in old.resolve().parents:
        shutil.rmtree(old, ignore_errors=True)
