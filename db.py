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
    con.commit()
    con.close()


def audit(con, action, email=None, event_id=None, detail="", ip=""):
    con.execute(
        "INSERT INTO audit(ts,email,event_id,action,detail,ip) VALUES(?,?,?,?,?,?)",
        (time.time(), email, event_id, action, detail, ip))


def split_folders(text):
    """One event can read from many drives: one folder per line."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip().strip('"')
        if line and line not in out:
            out.append(line)
    return out
