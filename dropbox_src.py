"""Dropbox folders - photos are read IN PLACE, never copied to this PC.

A folder is written as  dropbox:/Folder/Sub  (or paste the address from the
Dropbox website, https://www.dropbox.com/home/Folder). The app talks to
Dropbox with an app key + a long-lived refresh token, created on the admin
Settings page. Only read access is asked (files.metadata.read, files.content.read).
No extra Python package is used.
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
import gdrive
import sourcecfg

API = "https://api.dropboxapi.com/2/"
CONTENT = "https://content.dropboxapi.com/2/"
TOKEN_URL = "https://api.dropboxapi.com/oauth2/token"
THUMB_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".gif", ".webp", ".bmp"}
_token = {"value": None, "exp": 0}
_lock = threading.Lock()


class DropboxError(gdrive.DriveError):
    pass


def settings():
    return sourcecfg.load().get("dropbox") or {}


def connected():
    s = settings()
    return bool(s.get("app_key") and s.get("refresh_token"))


def authorize_url(app_key):
    return ("https://www.dropbox.com/oauth2/authorize?client_id=" + urllib.parse.quote(app_key)
            + "&response_type=code&token_access_type=offline")


def parse_link(text):
    """'dropbox:/Folder' or a dropbox.com/home/... address -> 'dropbox:/Folder', else None."""
    text = (text or "").strip()
    if text.lower().startswith("dropbox:"):
        path = text[8:].strip()
    else:
        m = re.match(r"https?://(?:www\.)?dropbox\.com/home(/.*)?$", text, re.I)
        if not m:
            return None
        path = urllib.parse.unquote(m.group(1) or "").split("?")[0]
    path = "/" + path.strip("/") if path.strip("/") else ""
    return "dropbox:" + path


def folder_path(entry):
    return entry[8:]


def label(entry):
    return "Dropbox: " + (folder_path(entry) or "/")


# -------------------------------------------------------------------- token
def _post_form(url, fields, auth=None):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode())
    if auth:
        req.add_header("Authorization", "Basic " + base64.b64encode(auth.encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def exchange_code(app_key, app_secret, code):
    """One-time code from the Dropbox page -> refresh token."""
    try:
        r = _post_form(TOKEN_URL, {"code": code.strip(), "grant_type": "authorization_code"},
                       f"{app_key}:{app_secret}")
    except urllib.error.HTTPError:
        raise DropboxError("Dropbox did not accept that code. Open the link again and copy a new code.")
    except Exception as e:
        raise DropboxError(f"Could not reach Dropbox ({e}).")
    if not r.get("refresh_token"):
        raise DropboxError("Dropbox gave no refresh token. Open the link from this page (it asks for offline access).")
    return r["refresh_token"]


def token():
    with _lock:
        if _token["value"] and _token["exp"] > time.time():
            return _token["value"]
        s = settings()
        if not (s.get("app_key") and s.get("refresh_token")):
            raise DropboxError("Dropbox is not connected yet. Connect it on the admin Settings page.")
        try:
            r = _post_form(TOKEN_URL, {"grant_type": "refresh_token",
                                       "refresh_token": s["refresh_token"]},
                           f"{s['app_key']}:{s.get('app_secret', '')}")
        except urllib.error.HTTPError:
            raise DropboxError("Dropbox did not accept the saved connection. Connect it again.")
        except Exception as e:
            raise DropboxError(f"Could not reach Dropbox ({e}).")
        _token.update(value=r["access_token"], exp=time.time() + int(r.get("expires_in", 14400)) - 300)
        return _token["value"]


def reset_token():
    _token.update(value=None, exp=0)


# ------------------------------------------------------------------ calls
def _call(url, arg=None, body=None, content=False, tries=5):
    for attempt in range(tries):
        headers = {"Authorization": "Bearer " + token()}
        data = None
        if content:
            headers["Dropbox-API-Arg"] = json.dumps(arg)
            data = b""
        else:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            return urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            text = ""
            try:
                text = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code == 401 and attempt < tries - 1:
                reset_token()
                continue
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                wait = e.headers.get("Retry-After") if e.headers else None
                time.sleep(min(30, int(wait)) if wait and wait.isdigit() else 2 ** attempt)
                continue
            raise DropboxError(_explain(e.code, text))
        except (urllib.error.URLError, OSError) as e:
            if attempt < tries - 1:
                time.sleep(2 ** attempt)
                continue
            raise DropboxError(f"Could not reach Dropbox ({e}).")


def _explain(code, text):
    if "not_found" in text:
        return "Dropbox cannot find this folder. Check the path (it is case-insensitive) and that the app can see it."
    if code == 401:
        return "Dropbox did not accept the connection. Connect it again on the Settings page."
    if code == 403 or "missing_scope" in text:
        return "No permission. The Dropbox app needs the files.metadata.read and files.content.read permissions."
    return f"Dropbox error {code}."


def _json(url, body):
    with _call(url, body=body) as r:
        return json.loads(r.read())


# ----------------------------------------------------------------- listing
def stamp(info):
    return f"{info.get('size')}|{info.get('modified')}|{info.get('hash', '')}"


def check(entry):
    p = folder_path(entry)
    if p:
        d = _json(API + "files/get_metadata", {"path": p})
        if d.get(".tag") != "folder":
            raise DropboxError("That path is a file. Please give a FOLDER.")


def walk(entry):
    """Every photo below the folder (including sub-folders) -> (relative path, info)."""
    base = folder_path(entry)
    body = {"path": base, "recursive": True, "limit": 2000,
            "include_non_downloadable_files": False}
    d = _json(API + "files/list_folder", body)
    while True:
        for f in d.get("entries", []):
            if f.get(".tag") != "file":
                continue
            disp = f.get("path_display", "")
            rel = disp[len(base):].lstrip("/") if disp.lower().startswith(base.lower()) else f["name"]
            if Path(rel).suffix.lower() not in config.IMAGE_EXT:
                continue
            yield rel, {"id": "dbx:" + f["id"], "name": f["name"], "size": f.get("size"),
                        "modified": f.get("server_modified"), "hash": f.get("content_hash", ""),
                        "src": "dropbox"}
        if not d.get("has_more"):
            return
        d = _json(API + "files/list_folder/continue", {"cursor": d["cursor"]})


def _id(ref):
    if not ref.startswith("dbx:"):
        raise DropboxError("Not a Dropbox photo.")
    return ref[4:]


def fetch_small(info, side=2048):
    """A JPEG made by Dropbox, about `side` pixels, or None for file types it cannot do."""
    if Path(info.get("name", "")).suffix.lower() not in THUMB_EXT and "name" in info:
        return None
    size = "w2048h1536" if side > 1024 else "w1024h768"
    try:
        with _call(CONTENT + "files/get_thumbnail_v2",
                   arg={"resource": {".tag": "path", "path": _id(info["id"])},
                        "format": "jpeg", "size": size, "mode": "bestfit"},
                   content=True, tries=2) as r:
            return r.read(30 * 1024 * 1024)
    except DropboxError:
        return None


def open_original(ref):
    return _call(CONTENT + "files/download", arg={"path": _id(ref)}, content=True)


def fetch_full(ref):
    with open_original(ref) as r:
        data = r.read(config.MAX_PHOTO_BYTES + 1)
    if len(data) > config.MAX_PHOTO_BYTES:
        raise DropboxError("This file is too large to read.")
    return data
