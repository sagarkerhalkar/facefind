"""Scans an event folder once, stores a face signature for every face,
and answers 'which photos contain this face?'."""
import os
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

import config
import db
import faces
import gdrive

progress = {}          # event_id -> dict(state, done, total, faces, error)
_cache = {}            # event_id -> (matrix, photo_ids)
_cache_lock = threading.Lock()


def _scan(folder, st, others):
    root = Path(folder)
    for dirpath, _, files in os.walk(root):
        for fn in files:
            ext = Path(fn).suffix.lower()
            if ext in config.IMAGE_EXT:
                p = Path(dirpath) / fn
                st["counted"] += 1
                if st["counted"] % 200 == 0:
                    st["phase"] = f"Counting the photos ... {st['counted']} found so far"
                yield p.relative_to(root).as_posix(), p
            elif ext in config.OTHER_EXT:
                others[ext] += 1


def _embed_file(job):
    """Runs in a scanning thread: read one photo, return its face signatures."""
    root, rel, path = job
    try:
        mtime = path.stat().st_mtime
        img = faces.decode_fast(path.read_bytes())
        if img is None:
            return job, mtime, None, "not a readable picture"
        return job, mtime, faces.thread_engine().embed(img), ""
    except Exception as e:
        return job, 0, None, str(e)


def _drop_cache(event_id):
    with _cache_lock:
        _cache.pop(event_id, None)


def index_event(event_id):
    """Scans every folder (drive) of the album, several photos at a time.
    Photos become findable while the scan is still running. A drive that is
    switched off is skipped - its photos stay in the index until it is back."""
    con = db.connect()
    try:
        ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        st = progress[event_id] = dict(
            state="running", done=0, total=0, faces=0, error="", phase="Starting ...",
            counted=0, scanned=0, started=0.0, bad_count=0, bad="")
        faces.get_engine()                      # fail early if the models are broken
        roots = db.split_folders(ev["folders"])
        notes = []
        for r in roots:                         # Google Drive links: fetch new photos first
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
        st["phase"] = "Counting the photos ..."
        online = [r for r in roots if Path(gdrive.local_root(r)).is_dir()]
        offline = [gdrive.label(r) for r in roots if r not in online]
        if offline:
            notes.append("Not reachable, skipped: " + ", ".join(offline))
        others = Counter()
        files = [(root, rel, path) for root in online
                 for rel, path in _scan(gdrive.local_root(root), st, others)]
        if not files:
            seen = ", ".join(f"{n} {ext} files" for ext, n in others.most_common(4))
            notes.append("No JPG / PNG / WEBP photos were found in the folder"
                         + (f" (it has {seen}, which this app cannot read)." if seen
                            else ". Check that you chose the folder that holds the photos."))
        known = {(r["root"], r["relpath"]): (r["id"], r["mtime"]) for r in con.execute(
            "SELECT id,root,relpath,mtime FROM photos WHERE event_id=?", (event_id,))}
        seen_keys = {(root, rel) for root, rel, _ in files}
        todo = []
        for job in files:                       # unchanged photos are not scanned again
            root, rel, path = job
            try:
                same = (root, rel) in known and known[(root, rel)][1] == path.stat().st_mtime
            except OSError:
                same = False
            if not same:
                todo.append(job)
        st.update(total=len(files), done=len(files) - len(todo), phase="",
                  error=" ".join(notes), started=time.time())
        with ThreadPoolExecutor(config.SCAN_WORKERS) as pool:
            for start in range(0, len(todo), 40):
                for job, mtime, vecs, problem in pool.map(_embed_file, todo[start:start + 40]):
                    root, rel, path = job
                    st["done"] += 1
                    st["scanned"] += 1
                    if vecs is None:
                        st["bad_count"] += 1
                        st["bad"] = f"{rel}: {problem}"
                        continue
                    key = (root, rel)
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
                con.commit()                    # guests can already find these photos
                _drop_cache(event_id)
        # Remove from the index: photos deleted from a reachable drive, and
        # photos of folders that were taken off the album's list.
        for (root, rel), (pid, _) in known.items():
            if (root in online and (root, rel) not in seen_keys) or root not in roots:
                con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
                con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
                con.execute("DELETE FROM photos WHERE id=?", (pid,))
                (config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)
        con.commit()
        if st["bad_count"]:
            st["error"] = (st["error"] + f" {st['bad_count']} files could not be read "
                           f"(last: {st['bad']}).").strip()
        st["state"] = "finished"
    except Exception as e:
        progress[event_id] = dict(state="failed", done=0, total=0, faces=0, error=str(e), phase="")
    finally:
        con.close()
        _drop_cache(event_id)


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


def stats(con, event_id, vec):
    """(faces in the album, best similarity for this selfie) - for the log,
    so a 'no photos' result can be explained."""
    m, ids = _matrix(con, event_id)
    if not len(ids):
        return 0, 0.0
    return int(len(ids)), float((m @ vec).max())
