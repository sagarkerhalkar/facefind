"""Connection settings for S3 and Dropbox, kept in data/sources.json
(only the owner can change them on the admin Settings page)."""
import json
import os
import threading

import config

FILE = config.DATA_DIR / "sources.json"
_lock = threading.Lock()
_cache = {"mtime": None, "data": {}}


def load():
    try:
        m = FILE.stat().st_mtime
    except OSError:
        d = {}
        # environment variables work too (useful for servers)
        if os.environ.get("S3_ACCESS_KEY"):
            d["s3"] = {"access_key": os.environ["S3_ACCESS_KEY"],
                       "secret_key": os.environ.get("S3_SECRET_KEY", ""),
                       "region": os.environ.get("S3_REGION", ""),
                       "endpoint": os.environ.get("S3_ENDPOINT", "")}
        if os.environ.get("DROPBOX_REFRESH_TOKEN"):
            d["dropbox"] = {"app_key": os.environ.get("DROPBOX_APP_KEY", ""),
                            "app_secret": os.environ.get("DROPBOX_APP_SECRET", ""),
                            "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"]}
        return d
    with _lock:
        if _cache["mtime"] != m:
            try:
                _cache.update(mtime=m, data=json.loads(FILE.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                _cache.update(mtime=m, data={})
        return _cache["data"]


def save(section, values):
    with _lock:
        try:
            d = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        d[section] = values
        tmp = FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(d), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        os.replace(tmp, FILE)
        _cache["mtime"] = None
