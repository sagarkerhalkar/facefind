"""Scans an album once, stores a face signature for every face, and answers
'which photos contain this face?'.

Photos are scanned where they are: a folder on any drive is read from that
drive, a Google Drive link is read straight from Google into memory.
Nothing is copied."""
import os
import queue
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

progress = {}          # event_id -> dict(state, done, total, faces, error, ...)
_cache = {}            # event_id -> (matrix, photo_ids)
_cache_lock = threading.Lock()
BATCH = 48


def _scan(folder, others):
    root = Path(folder)
    for dirpath, _, files in os.walk(root):
        for fn in files:
            ext = Path(fn).suffix.lower()
            if ext in config.IMAGE_EXT:
                p = Path(dirpath) / fn
                yield p.relative_to(root).as_posix(), p
            elif ext in config.OTHER_EXT:
                others[ext] += 1


def _jobs(roots, others, notes, complete):
    """Every photo of every source of the album, as it is found."""
    for root in roots:
        if gdrive.is_drive(root):
            fid = root[7:]
            try:
                gdrive.remember_name(fid, gdrive.folder_name(fid))
                for rel, info in gdrive.walk(fid):
                    yield dict(root=root, rel=rel, path=None, info=info, mtime=0.0,
                               stamp=gdrive.stamp(info), ref=info["id"])
                complete.add(root)
            except gdrive.DriveError as e:
                notes.append(f"{gdrive.label(root)}: {e}")
        elif not Path(root).is_dir():
            notes.append(f"Not reachable, skipped: {root}")
        else:
            for rel, path in _scan(root, others):
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = 0.0
                yield dict(root=root, rel=rel, path=path, info=None, mtime=mtime,
                           stamp="", ref="")
            complete.add(root)


def _drive_image(info):
    """A Drive photo as pixels, read into memory only. Google's ready-made
    1600-pixel version is used when it is good enough; otherwise the
    original is read (also into memory)."""
    data = gdrive.fetch_small(info, config.DRIVE_SCAN_SIDE)
    img = faces.decode(data) if data else None
    if img is not None:
        meta = info.get("imageMediaMetadata") or {}
        original = max(int(meta.get("width") or 0), int(meta.get("height") or 0))
        need = min(1200, int(original * 0.9)) if original else 1000
        if max(img.shape[:2]) >= need:
            return img
    return faces.decode_fast(gdrive.fetch_full(info["id"]))


def _embed_file(job):
    """Runs in a scanning thread: read one photo, return its face signatures."""
    try:
        if job["info"] is not None:
            img = _drive_image(job["info"])
        else:
            img = faces.decode_fast(job["path"].read_bytes())
        if img is None:
            return job, None, "not a readable picture"
        return job, faces.thread_engine().embed(img), ""
    except Exception as e:
        return job, None, str(e)[:200]


def _batches(q):
    """Groups of photos from the finder thread, until it says it is done."""
    while True:
        job = q.get()
        if job is None:
            return
        batch = [job]
        while len(batch) < BATCH:
            try:
                job = q.get_nowait()
            except queue.Empty:
                break
            if job is None:
                yield batch
                return
            batch.append(job)
        yield batch


def _drop_cache(event_id):
    with _cache_lock:
        _cache.pop(event_id, None)


def _forget_photo(con, pid):
    con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
    con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
    con.execute("DELETE FROM photos WHERE id=?", (pid,))
    (config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)


def index_event(event_id):
    """Scans every source of the album, several photos at a time, while a
    second thread keeps finding photos. Photos become findable while the scan
    is still running. A drive that is switched off is skipped - its photos
    stay in the index until it is back. If the app is closed in the middle,
    the scan continues from where it stopped at the next start."""
    con = db.connect()
    st = progress[event_id] = dict(
        state="running", done=0, total=0, faces=0, error="", phase="Starting ...",
        scanned=0, started=time.time(), bad_count=0, bad="", listing=True)
    try:
        ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        db.kv_set(con, f"scanning:{event_id}", "1")
        con.commit()
        faces.get_engine()                      # fail early if the models are broken
        roots = db.split_folders(ev["folders"])
        known = {(r["root"], r["relpath"]): (r["id"], r["mtime"], r["stamp"] or "")
                 for r in con.execute(
                     "SELECT id,root,relpath,mtime,stamp FROM photos WHERE event_id=?",
                     (event_id,))}
        others, notes, complete, seen_keys = Counter(), [], set(), set()
        q = queue.Queue()

        def finder():
            try:
                for job in _jobs(roots, others, notes, complete):
                    seen_keys.add((job["root"], job["rel"]))
                    st["total"] += 1
                    q.put(job)
            except Exception as e:              # never leave the scan waiting
                notes.append(f"Looking for photos stopped: {e}")
                complete.clear()
            finally:
                st["listing"] = False
                q.put(None)

        threading.Thread(target=finder, daemon=True).start()
        st["phase"] = ""
        workers = config.SCAN_WORKERS
        if any(gdrive.is_drive(r) for r in roots):     # waiting for Google is not PC work
            workers = max(workers, config.DRIVE_WORKERS)
        with ThreadPoolExecutor(workers) as pool:
            for batch in _batches(q):
                todo = []
                for job in batch:               # unchanged photos are not scanned again
                    old = known.get((job["root"], job["rel"]))
                    if old and old[1] == job["mtime"] and old[2] == job["stamp"]:
                        st["done"] += 1
                    else:
                        todo.append(job)
                for job, vecs, problem in pool.map(_embed_file, todo):
                    st["done"] += 1
                    st["scanned"] += 1
                    if vecs is None:
                        st["bad_count"] += 1
                        st["bad"] = f"{job['rel']}: {problem}"
                        continue
                    key = (job["root"], job["rel"])
                    if key in known:
                        pid = known[key][0]
                        con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
                        con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
                        con.execute("UPDATE photos SET mtime=?, stamp=?, ref=? WHERE id=?",
                                    (job["mtime"], job["stamp"], job["ref"], pid))
                        (config.CACHE_DIR / f"{pid}.jpg").unlink(missing_ok=True)
                    else:
                        pid = con.execute(
                            "INSERT INTO photos(event_id,root,relpath,mtime,stamp,ref) "
                            "VALUES(?,?,?,?,?,?)",
                            (event_id, job["root"], job["rel"], job["mtime"], job["stamp"],
                             job["ref"])).lastrowid
                    con.executemany(
                        "INSERT INTO faces(photo_id,event_id,embedding) VALUES(?,?,?)",
                        [(pid, event_id, v.astype(np.float32).tobytes()) for v in vecs])
                    st["faces"] += len(vecs)
                if todo:
                    con.commit()                # guests can already find these photos
                    _drop_cache(event_id)
        # Remove from the index: photos deleted from a source that was read
        # completely, and photos of sources taken off the album's list.
        for (root, rel), (pid, _, _) in known.items():
            if (root in complete and (root, rel) not in seen_keys) or root not in roots:
                _forget_photo(con, pid)
        if not st["total"] and not notes:
            seen = ", ".join(f"{n} {ext} files" for ext, n in others.most_common(4))
            notes.append("No JPG / PNG / WEBP photos were found in the folder"
                         + (f" (it has {seen}, which this app cannot read)." if seen
                            else ". Check that you chose the folder that holds the photos."))
        if st["bad_count"]:
            notes.append(f"{st['bad_count']} files could not be read (last: {st['bad']}).")
        db.kv_set(con, f"scanning:{event_id}", None)
        con.commit()
        for r in roots:
            if r in complete and gdrive.is_drive(r):
                gdrive.drop_old_copy(r[7:])
        st["error"] = " ".join(notes)
        st["state"] = "finished"
    except Exception as e:
        st.update(state="failed", error=str(e), phase="")
    finally:
        con.close()
        _drop_cache(event_id)


def unfinished(con):
    """Albums whose scan was cut off (app closed, power cut)."""
    return [int(r[0].split(":")[1]) for r in
            con.execute("SELECT k FROM kv WHERE k LIKE 'scanning:%'")]


def start_index(event_id):
    if progress.get(event_id, {}).get("state") == "running":
        return False
    progress[event_id] = dict(state="running", done=0, total=0, faces=0, error="",
                              phase="Starting ...")
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
