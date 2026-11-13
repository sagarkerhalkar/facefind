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


def init():
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
