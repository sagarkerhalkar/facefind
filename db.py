import sqlite3
import time

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, code TEXT UNIQUE NOT NULL,
  folders TEXT NOT NULL, require_approval INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, root TEXT NOT NULL,
  relpath TEXT NOT NULL, mtime REAL, UNIQUE(event_id, root, relpath));
CREATE TABLE IF NOT EXISTS faces(
  id INTEGER PRIMARY KEY, photo_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
  embedding BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS faces_event ON faces(event_id);
CREATE INDEX IF NOT EXISTS faces_photo ON faces(photo_id);
CREATE TABLE IF NOT EXISTS otps(
  id INTEGER PRIMARY KEY, email TEXT, event_id INTEGER, code_hash TEXT,
  expires_at REAL, attempts INTEGER DEFAULT 0, used INTEGER DEFAULT 0,
  created_at REAL, ip TEXT);
CREATE INDEX IF NOT EXISTS otps_email ON otps(email, created_at);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, token_hash TEXT UNIQUE, email TEXT, event_id INTEGER,
  status TEXT DEFAULT 'verified', selfie_tries INTEGER DEFAULT 0,
  created_at REAL, expires_at REAL, ip TEXT);
CREATE TABLE IF NOT EXISTS matches(
  session_id INTEGER, photo_id INTEGER, score REAL,
  PRIMARY KEY(session_id, photo_id));
CREATE INDEX IF NOT EXISTS matches_photo ON matches(photo_id);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS users(
  email TEXT PRIMARY KEY, name TEXT, phone TEXT, created_at REAL, last_login REAL);
CREATE TABLE IF NOT EXISTS bookmarks(
  email TEXT, photo_id INTEGER, created_at REAL, PRIMARY KEY(email, photo_id));
CREATE TABLE IF NOT EXISTS downloads(
  id INTEGER PRIMARY KEY, ts REAL, email TEXT, photo_id INTEGER, event_id INTEGER,
  relpath TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS downloads_email ON downloads(email);
CREATE TABLE IF NOT EXISTS staff(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT, pw_hash TEXT,
  active INTEGER DEFAULT 1, created_at REAL);
CREATE TABLE IF NOT EXISTS promos(
  id INTEGER PRIMARY KEY, kind TEXT, title TEXT, body TEXT, link TEXT, event_date TEXT,
  show_before INTEGER DEFAULT 1, show_after INTEGER DEFAULT 1, active INTEGER DEFAULT 1,
  created_at REAL);
CREATE TABLE IF NOT EXISTS scan_errors(
  event_id INTEGER, relpath TEXT, reason TEXT);
CREATE INDEX IF NOT EXISTS scan_errors_event ON scan_errors(event_id);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY, ts REAL, email TEXT, event_id INTEGER,
  action TEXT, detail TEXT, ip TEXT);
"""


def connect():
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def _healthy():
    """True when the database opens and passes SQLite's own check (or is new)."""
    if not config.DB_PATH.exists():
        return True
    try:
        con = sqlite3.connect(config.DB_PATH, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            return con.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False


def is_damage(e):
    """True for the SQLite errors that mean the database file itself is broken."""
    m = str(e).lower()
    return isinstance(e, sqlite3.DatabaseError) and any(
        t in m for t in ("malformed", "not a database", "disk i/o error", "corrupt"))


def restart_to_repair(why=""):
    """A broken database is repaired at start-up, when no other part of the app
    has the file open (Windows will not let a file in use be replaced). So the
    app ends itself with exit code 75 and start.bat / start.sh start it again."""
    import os
    import threading
    print(f"\n!! The database is damaged ({why}). Restarting to repair it ...\n", flush=True)
    threading.Timer(1.5, lambda: os._exit(75)).start()


def repair():
    """A damaged data/facefind.db (power cut, drive full, copied while the app
    was running, cloud-sync folder) used to stop the whole app. Now: keep the
    broken files in data/broken-db/, build a fresh database and copy across every
    row that can still be read, then carry on. Returns a message or None."""
    if _healthy():
        return None
    import shutil
    keep = config.DATA_DIR / "broken-db" / time.strftime("%Y%m%d-%H%M%S")
    keep.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        f = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        if f.exists():
            shutil.copy2(f, keep / f.name)
    old = keep / config.DB_PATH.name
    for suffix in ("", "-wal", "-shm"):
        f = config.DB_PATH.with_name(config.DB_PATH.name + suffix)
        try:
            f.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise SystemExit(
                f"\n  The database is damaged and {f.name} is in use by another program.\n"
                "  Close every other facefind window (and any program using the data folder), "
                "then start again.\n")
    new = sqlite3.connect(config.DB_PATH, timeout=30)
    new.executescript(SCHEMA)
    saved = lost = 0
    try:
        src = sqlite3.connect(f"file:{old}?mode=ro", uri=True)
        tables = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    except sqlite3.DatabaseError:
        src, tables = None, []
    for t in tables:
        try:
            have = {r[1] for r in new.execute(f"PRAGMA table_info({t})")}
            if not have:
                continue
            cur = src.execute(f"SELECT * FROM {t}")
            cols = [d[0] for d in cur.description if d[0] in have]
            idx = [i for i, d in enumerate(cur.description) if d[0] in have]
            q = f"INSERT OR IGNORE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
            while True:
                try:
                    row = cur.fetchone()
                except sqlite3.DatabaseError:
                    lost += 1
                    break
                if row is None:
                    break
                new.execute(q, [row[i] for i in idx])
                saved += 1
        except sqlite3.DatabaseError:
            lost += 1
    new.commit()
    new.close()
    if src:
        src.close()
    msg = (f"The database was damaged. Kept a copy in {keep}; rebuilt it and "
           f"recovered {saved} records ({lost} table(s) cut short).")
    print("WARNING:", msg, flush=True)
    return msg


def init():
    repair()
    con = connect()
    con.executescript(SCHEMA)
    # Added later: where a Google Drive photo lives (its file id) and the
    # "version stamp" Drive gives it, so unchanged photos are not scanned twice.
    for table, col in (("photos", "ref"), ("photos", "stamp"),
                       ("events", "event_date"),            # day of the event, YYYY-MM-DD
                       ("sessions", "date_from"), ("sessions", "date_to"),
                       ("otps", "name"), ("otps", "phone"),
                       ("faces", "size")):                  # face width in pixels (as text)
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT DEFAULT ''")
    con.execute("CREATE INDEX IF NOT EXISTS events_date ON events(event_date)")
    con.execute("CREATE INDEX IF NOT EXISTS downloads_ts ON downloads(ts)")
    con.execute("CREATE INDEX IF NOT EXISTS downloads_event ON downloads(event_id)")
    # Photos are given without waiting for approval (asked for by the owner);
    # done once, the per-album switch still works afterwards.
    if not con.execute("SELECT 1 FROM kv WHERE k='approval_off_done'").fetchone():
        con.execute("UPDATE events SET require_approval=0")
        con.execute("INSERT INTO kv(k,v) VALUES('approval_off_done','1')")
    con.commit()
    con.close()


def audit(con, action, email=None, event_id=None, detail="", ip=""):
    con.execute(
        "INSERT INTO audit(ts,email,event_id,action,detail,ip) VALUES(?,?,?,?,?,?)",
        (time.time(), email, event_id, action, detail, ip))


def kv_get(con, key, default=""):
    row = con.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row[0] if row else default


def kv_set(con, key, value):
    if value is None:
        con.execute("DELETE FROM kv WHERE k=?", (key,))
    else:
        con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                    (key, value))


def split_folders(text):
    """One event can read from many drives: one folder per line."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().strip('"')
        if line and line not in out:
            out.append(line)
    return out
