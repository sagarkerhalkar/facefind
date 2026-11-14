"""Automatic albums.

The owner names one or more PARENT folders (on a drive or a Google Drive
folder). Every sub-folder inside is an event: when a new sub-folder appears it
becomes an album by itself (name = folder name, date = date in the name or the
folder's date) and is scanned. Albums made this way are re-checked every few
minutes, so photos added later become findable without anyone pressing a button.
The original photos are never touched.
"""
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path

import config
import db
import gdrive
import indexer

log = logging.getLogger("facefind.auto")
state = dict(last="", last_ts=0.0, running=False, made=0, error="")
_lock = threading.Lock()

_SKIP = {"system volume information", "$recycle.bin", "recycler", "lost+found", "@eadir"}
_D1 = re.compile(r"(?<!\d)(\d{4})[-_. ](\d{1,2})[-_. ](\d{1,2})(?!\d)")
_D2 = re.compile(r"(?<!\d)(\d{1,2})[-_. ](\d{1,2})[-_. ](\d{4}|\d{2})(?!\d)")


def date_from_name(name):
    """YYYY-MM-DD from '2026-11-08 Wedding' or '08-11-2026 Wedding', else ''."""
    import datetime
    for rx, order in ((_D1, "ymd"), (_D2, "dmy")):
        m = rx.search(name)
        if not m:
            continue
        a, b, c = (int(x) for x in m.groups())
        y, mo, d = (a, b, c) if order == "ymd" else (c + (2000 if c < 100 else 0), b, a)
        try:
            return datetime.date(y, mo, d).isoformat()
        except ValueError:
            continue
    return ""


def settings(c):
    return dict(on=db.kv_get(c, "auto:on", "0") == "1",
                roots=db.split_folders(db.kv_get(c, "auto:roots", "")),
                minutes=max(1, min(240, int(db.kv_get(c, "auto:minutes", "3") or 3))))


def _has_image(path, limit=4000):
    n = 0
    for _r, _d, files in os.walk(path):
        for f in files:
            n += 1
            if os.path.splitext(f)[1].lower() in config.IMAGE_EXT:
                return True
            if n > limit:
                return True                       # a big folder: assume it has photos
    return False


def _children(root):
    """(entry, name, date) of every sub-folder of a parent folder."""
    if root.startswith("gdrive:"):
        for f in gdrive.list_children(root[7:]):
            if f.get("mimeType") == gdrive.FOLDER and f.get("name", "").lower() not in _SKIP:
                yield "gdrive:" + f["id"], f["name"], (f.get("modifiedTime") or "")[:10]
        return
    try:
        with os.scandir(root) as it:
            for e in sorted(it, key=lambda e: e.name.lower()):
                if not e.is_dir() or e.name.startswith((".", "$")) or e.name.lower() in _SKIP:
                    continue
                try:
                    day = time.strftime("%Y-%m-%d", time.localtime(e.stat().st_mtime))
                except OSError:
                    day = ""
                yield str(Path(e.path).resolve()), e.name, day
    except OSError as err:
        raise RuntimeError(f"Cannot open {root}: {err.strerror or err}")


def add_root(text):
    """Parent folder as typed by the owner -> stored entry, or None."""
    fid = gdrive.parse_link(text)
    if fid:
        return "gdrive:" + fid if gdrive.key_info() else None
    return str(Path(text).resolve()) if os.path.isdir(text) else None


def run_once():
    """One pass: new sub-folders become albums, auto albums are re-scanned.
    Returns how many albums were created."""
    if not _lock.acquire(blocking=False):
        return 0
    made = 0
    state.update(running=True, error="")
    c = db.connect()
    try:
        s = settings(c)
        if not s["on"] or not s["roots"]:
            return 0
        have = set()
        for r in c.execute("SELECT folders FROM events"):
            have.update(db.split_folders(r[0]))
        for root in s["roots"]:
            try:
                kids = list(_children(root))
            except Exception as e:                       # one bad root never stops the rest
                state["error"] = str(e)[:300]
                log.warning("auto albums: %s", e)
                continue
            for entry, name, day in kids:
                if entry in have or db.kv_get(c, "auto:skip:" + entry):
                    continue
                if not entry.startswith("gdrive:") and not _has_image(entry):
                    continue                              # empty folder: wait until photos arrive
                eid = c.execute(
                    "INSERT INTO events(name,code,folders,require_approval,created_at,event_date) "
                    "VALUES(?,?,?,?,?,?)",
                    (name[:120], secrets.token_urlsafe(9), entry, 0, time.time(),
                     date_from_name(name) or day)).lastrowid
                db.kv_set(c, f"autoev:{eid}", "1")
                db.audit(c, "album_auto_created", detail=f"{name} | from {root}")
                c.commit()
                have.add(entry)
                indexer.start_index(eid)
                made += 1
        # photos added later to albums made this way
        due = time.time() - 60 * max(10, s["minutes"] * 3)
        for r in c.execute("SELECT id FROM events WHERE active=1").fetchall():
            eid = r[0]
            if not db.kv_get(c, f"autoev:{eid}"):
                continue
            last = float(db.kv_get(c, f"autorescan:{eid}", "0") or 0)
            if last < due and indexer.progress.get(eid, {}).get("state") != "running":
                db.kv_set(c, f"autorescan:{eid}", str(time.time()))
                c.commit()
                indexer.start_index(eid)
        state.update(made=state["made"] + made)
        return made
    except Exception as e:
        state["error"] = str(e)[:300]
        log.exception("auto albums failed")
        return made
    finally:
        state.update(running=False, last_ts=time.time())
        c.close()
        _lock.release()


def start():
    """Background loop; checks the settings each time, so changes apply at once."""
    def loop():
        time.sleep(20)                                    # let the app settle after start
        while True:
            wait = 60
            try:
                c = db.connect()
                try:
                    s = settings(c)
                finally:
                    c.close()
                wait = s["minutes"] * 60
                if s["on"]:
                    run_once()
            except Exception:
                log.exception("auto albums loop")
            time.sleep(max(30, wait))
    threading.Thread(target=loop, daemon=True, name="auto-albums").start()
