"""Scans an event folder once, stores a face signature for every face,
and answers 'which photos contain this face?'."""
import os
import threading
import time
from pathlib import Path

import numpy as np

import config
import db
import faces
import gdrive

progress = {}          # event_id -> dict(state, done, total, faces, error)
_cache = {}            # event_id -> (matrix, photo_ids)
_cache_lock = threading.Lock()


def _scan(folder):
    root = Path(folder)
    for dirpath, _, files in os.walk(root):
        for fn in files:
            if Path(fn).suffix.lower() in config.IMAGE_EXT:
                p = Path(dirpath) / fn
                yield p.relative_to(root).as_posix(), p


def index_event(event_id):
    """Scans every folder (drive) of the event. A drive that is switched off
    or unplugged is skipped - its photos stay in the index until it is back."""
    con = db.connect()
    try:
        ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        st = progress[event_id] = dict(state="running", done=0, total=0, faces=0,
                                       error="", phase="")
        engine = faces.get_engine()
        roots = db.split_folders(ev["folders"])
        notes = []
        for r in roots:                       # Google Drive links: fetch new photos first
            if not r.startswith("gdrive:"):
                continue
            def show(done, total):
                st["phase"] = f"Getting photos from Google Drive ... {done} of {total}"
            try:
                st["phase"] = "Reading the Google Drive folder ..."
                _, failed, note = gdrive.sync(r[7:], show)
                if failed:
                    notes.append(f"{failed} Drive photos could not be fetched ({note})")
            except gdrive.DriveError as e:
                notes.append(str(e))
        st["phase"] = ""
        online = [r for r in roots if Path(gdrive.local_root(r)).is_dir()]
        offline = [gdrive.label(r) for r in roots if r not in online]
        if offline:
            notes.append("Not reachable, skipped: " + ", ".join(offline))
        files = [(root, rel, path) for root in online
                 for rel, path in _scan(gdrive.local_root(root))]
        known = {(r["root"], r["relpath"]): (r["id"], r["mtime"]) for r in con.execute(
            "SELECT id,root,relpath,mtime FROM photos WHERE event_id=?", (event_id,))}
        st["total"] = len(files)
        st["error"] = " ".join(notes)
        seen = set()
        for root, rel, path in files:
            key = (root, rel)
            seen.add(key)
            try:
                mtime = path.stat().st_mtime
                if key in known and known[key][1] == mtime:
                    continue
                img = faces.decode(path.read_bytes())
                if img is None:
                    continue
                vecs = engine.embed(img)
                if key in known:
                    pid = known[key][0]
                    con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
                    con.execute("UPDATE photos SET mtime=? WHERE id=?", (mtime, pid))
                    (config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)
                else:
                    pid = con.execute(
                        "INSERT INTO photos(event_id,root,relpath,mtime) VALUES(?,?,?,?)",
                        (event_id, root, rel, mtime)).lastrowid
                con.executemany(
                    "INSERT INTO faces(photo_id,event_id,embedding) VALUES(?,?,?)",
                    [(pid, event_id, v.astype(np.float32).tobytes()) for v in vecs])
                st["faces"] += len(vecs)
            except Exception as e:           # one bad file must not stop the run
                st["bad"] = f"{rel}: {e}"
            finally:
                st["done"] += 1
                if st["done"] % 50 == 0:
                    con.commit()
        # Remove from the index: photos deleted from a reachable drive, and
        # photos of folders that were taken off the event's list.
        for (root, rel), (pid, _) in known.items():
            if (root in online and (root, rel) not in seen) or root not in roots:
                con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
                con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
                con.execute("DELETE FROM photos WHERE id=?", (pid,))
                (config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)
        con.commit()
        st["state"] = "finished"
    except Exception as e:
        progress[event_id] = dict(state="failed", done=0, total=0, faces=0, error=str(e), phase="")
    finally:
        con.close()
        with _cache_lock:
            _cache.pop(event_id, None)


def start_index(event_id):
    if progress.get(event_id, {}).get("state") == "running":
        return False
    progress[event_id] = dict(state="running", done=0, total=0, faces=0, error="", phase="")
    threading.Thread(target=index_event, args=(event_id,), daemon=True).start()
    return True


def _matrix(con, event_id):
    with _cache_lock:
        if event_id in _cache:
            return _cache[event_id]
    rows = con.execute("SELECT photo_id, embedding FROM faces WHERE event_id=?",
                       (event_id,)).fetchall()
    if rows:
        m = np.vstack([np.frombuffer(r["embedding"], np.float32) for r in rows])
        ids = np.array([r["photo_id"] for r in rows])
    else:
        m, ids = np.zeros((0, 1), np.float32), np.array([], int)
    with _cache_lock:
        _cache[event_id] = (m, ids)
    return m, ids


def search(con, event_id, vec):
    """Returns [(photo_id, score)] best first."""
    m, ids = _matrix(con, event_id)
    if not len(ids):
        return []
    sims = m @ vec
    best = {}
    for i in np.nonzero(sims >= config.MATCH_THRESHOLD)[0]:
        pid, s = int(ids[i]), float(sims[i])
        if s > best.get(pid, -1):
            best[pid] = s
    return sorted(best.items(), key=lambda x: -x[1])[: config.MAX_RESULTS]
