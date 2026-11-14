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
        need = min(1200, config.SCAN_SIDE, int(original * 0.9)) if original else 1000
        if max(img.shape[:2]) >= need:
            return img, "small"
    ext = Path(info.get("name", "")).suffix
    return faces.load(gdrive.fetch_full(info["id"]), ext), "full"


def _small_copy(img):
    """JPEG bytes of the picture at tile size (made while it is in memory anyway)."""
    try:
        import cv2
        h, w = img.shape[:2]
        s = config.THUMB_SIDE / max(h, w)
        if s < 1:
            img = cv2.resize(img, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])
        return buf.tobytes() if ok else None
    except Exception:
        return None


def thumb_path(pid):
    return config.THUMB_DIR / f"{int(pid) // 1000:05d}" / f"{int(pid)}.jpg"


def _embed_file(job):
    """Runs in a scanning thread: read one photo, return its face signatures."""
    t0 = time.time()
    job["t_read"] = job["t_face"] = 0.0
    try:
        if job["info"] is not None:
            img, job["how"] = _drive_image(job["info"])
        else:
            img, job["how"] = faces.load(job["path"].read_bytes(), job["path"].suffix), "file"
        t1 = time.time()
        job["t_read"] = t1 - t0
        if img is None:
            return job, None, "not a readable picture"
        engine = faces.thread_engine()
        vecs = engine.embed(img)
        sizes = getattr(engine, "sizes", None) or []
        job["sizes"] = sizes if len(sizes) == len(vecs) else [0.0] * len(vecs)
        job["thumb"] = _small_copy(img) if vecs else None
        job["t_face"] = time.time() - t1
        return job, vecs, ""
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


def preview_path(row, small=False):
    """Where the watermarked preview of a photo is kept (large, or the small
    one for the photo page). The name changes when the photo changes, so an
    old preview can never be shown for it."""
    import hashlib
    side = config.THUMB_SIDE if small else config.PREVIEW_SIDE
    tag = hashlib.sha256(f"{row['root']}|{row['relpath']}|{row['mtime']}|{row['stamp']}"
                         f"|{side}|{config.PREVIEW_TAG}".encode()).hexdigest()[:16]
    return config.CACHE_DIR / f"{row['id']}-{tag}{'-s' if small else ''}.jpg"


def drop_preview(pid):
    """Windows refuses to delete a file that is being sent to a browser at
    that moment; that must not stop a scan, so such a file is left behind
    (it is never used again, see preview_path)."""
    for f in config.CACHE_DIR.glob(f"{pid}-*.jpg"):
        try:
            f.unlink()
        except OSError:
            pass


def _forget_photo(con, pid):
    con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
    con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
    con.execute("DELETE FROM bookmarks WHERE photo_id=?", (pid,))
    try:
        thumb_path(pid).unlink(missing_ok=True)
    except OSError:
        pass
    con.execute("DELETE FROM photos WHERE id=?", (pid,))
    drop_preview(pid)


def index_event(event_id):
    """Scans every source of the album, several photos at a time, while a
    second thread keeps finding photos. Photos become findable while the scan
    is still running. A drive that is switched off is skipped - its photos
    stay in the index until it is back. If the app is closed in the middle,
    the scan continues from where it stopped at the next start."""
    con = db.connect()
    st = progress[event_id] = dict(
        state="running", done=0, total=0, faces=0, error="", phase="Starting ...",
        scanned=0, started=time.time(), bad_count=0, bad="", listing=True,
        t_read=0.0, t_face=0.0, full=0, took=0.0)
    try:
        ev = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        db.kv_set(con, f"scanning:{event_id}", "1")
        con.execute("DELETE FROM scan_errors WHERE event_id=?", (event_id,))
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
                    st["t_read"] += job["t_read"]
                    st["t_face"] += job["t_face"]
                    st["full"] += job.get("how") == "full"
                    if vecs is None:
                        st["bad_count"] += 1
                        st["bad"] = f"{job['rel']}: {problem}"
                        con.execute("INSERT INTO scan_errors(event_id,relpath,reason) VALUES(?,?,?)",
                                    (event_id, f"{gdrive.label(job['root'])} / {job['rel']}",
                                     problem))
                        continue
                    key = (job["root"], job["rel"])
                    if key in known:
                        pid = known[key][0]
                        con.execute("DELETE FROM faces WHERE photo_id=?", (pid,))
                        con.execute("DELETE FROM matches WHERE photo_id=?", (pid,))
                        con.execute("UPDATE photos SET mtime=?, stamp=?, ref=? WHERE id=?",
                                    (job["mtime"], job["stamp"], job["ref"], pid))
                        drop_preview(pid)
                    else:
                        pid = con.execute(
                            "INSERT INTO photos(event_id,root,relpath,mtime,stamp,ref) "
                            "VALUES(?,?,?,?,?,?)",
                            (event_id, job["root"], job["rel"], job["mtime"], job["stamp"],
                             job["ref"])).lastrowid
                    con.executemany(
                        "INSERT INTO faces(photo_id,event_id,embedding,size) VALUES(?,?,?,?)",
                        [(pid, event_id, v.astype(np.float32).tobytes(), f"{z:.0f}")
                         for v, z in zip(vecs, job["sizes"])])
                    st["faces"] += len(vecs)
                    tp = thumb_path(pid)
                    if job.get("thumb"):
                        try:
                            tp.parent.mkdir(parents=True, exist_ok=True)
                            tp.write_bytes(job["thumb"])
                        except OSError:
                            pass
                    else:
                        tp.unlink(missing_ok=True)
                    job["thumb"] = None
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
            notes.append("No photos were found in the folder"
                         + (f" (it has {seen}, which are not photos)." if seen
                            else ". Check that you chose the folder that holds the photos."))
        if st["bad_count"]:
            notes.append(f"{st['bad_count']} files could not be read (last: {st['bad']}). "
                         "The full list is under 'Files that were not scanned'.")
        db.kv_set(con, f"scanning:{event_id}", None)
        con.commit()
        for r in roots:
            if r in complete and gdrive.is_drive(r):
                gdrive.drop_old_copy(r[7:])
        st["error"] = " ".join(notes)
        st["took"] = time.time() - st["started"]
        st["state"] = "finished"
    except Exception as e:
        if db.is_damage(e):
            db.restart_to_repair(str(e))
        st.update(state="failed", error=str(e), phase="")
    finally:
        con.close()
        _drop_cache(event_id)


def timing(st):
    """Where the scan's time goes, in plain words - shown on the admin page."""
    n = st.get("scanned", 0)
    if not n:
        return ""
    text = (f"Per photo: {st['t_read'] / n:.1f} s to read it, "
            f"{st['t_face'] / n:.1f} s to find the faces.")
    if st.get("full"):
        text += (f" {st['full']} photos had to be read from Drive in full size "
                 "(Google had no small version) - that is the slow part.")
    return text


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
    """(signatures, photo ids, face widths) of an album, kept in memory."""
    with _cache_lock:
        if event_id in _cache:
            return _cache[event_id]
    rows = con.execute("SELECT photo_id, embedding, size FROM faces WHERE event_id=?",
                       (event_id,)).fetchall()
    if rows:
        m = np.vstack([np.frombuffer(r["embedding"], np.float32) for r in rows])
        ids = np.array([r["photo_id"] for r in rows])
        sizes = np.array([float(r["size"] or 0) for r in rows], np.float32)
    else:
        m, ids, sizes = np.zeros((0, 1), np.float32), np.array([], int), np.zeros(0, np.float32)
    with _cache_lock:
        _cache[event_id] = (m, ids, sizes)
    return m, ids, sizes


def match(m, sizes, vec, threshold=None):
    """Which faces of an album are the person of this selfie -> (indexes, scores).

    1. A face must be at least `threshold` alike to the selfie; a tiny face
       (blurred, few pixels) must be clearly more alike.
    2. When four or more faces pass, the best of them describe how this
       person looks in THIS album. A face that resembles the selfie only by
       chance (a look-alike, a child, a blurred face) does not resemble that
       group and is dropped. With fewer faces this second check cannot be made.
    """
    thr = config.MATCH_THRESHOLD if threshold is None else threshold
    sims = m @ vec
    need = np.where((sizes > 0) & (sizes < config.SMALL_FACE), thr + config.SMALL_FACE_EXTRA, thr)
    cand = np.nonzero(sims >= need)[0]
    if len(cand) >= 4:
        order = cand[np.argsort(-sims[cand])]
        core = order[:max(3, len(order) // 4)]
        centre = m[core].mean(axis=0) + vec / len(core)
        centre /= np.linalg.norm(centre) or 1.0
        cand = cand[(m[cand] @ centre) >= thr + config.GROUP_EXTRA]
    return cand, sims[cand]


def search(con, event_id, vec):
    """Returns [(photo_id, score)] best first."""
    m, ids, sizes = _matrix(con, event_id)
    if not len(ids):
        return []
    found, scores = match(m, sizes, vec)
    best = {}
    for i, sc in zip(found, scores):
        pid, sc = int(ids[i]), float(sc)
        if sc > best.get(pid, -1):
            best[pid] = sc
    return sorted(best.items(), key=lambda x: -x[1])[: config.MAX_RESULTS]


def stats(con, event_id, vec):
    """(faces in the album, best similarity for this selfie) - for the log,
    so a 'no photos' result can be explained."""
    m, ids, _ = _matrix(con, event_id)
    if not len(ids):
        return 0, 0.0
    return int(len(ids)), float((m @ vec).max())
