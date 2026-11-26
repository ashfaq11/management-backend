import os
import sqlite3
import threading

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'admin', is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customers(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL, email TEXT,
  gender TEXT, dob TEXT, address TEXT, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS therapists(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT, fixed_salary REAL NOT NULL DEFAULT 0,
  incentive_percent REAL NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
  joined_on TEXT);
CREATE TABLE IF NOT EXISTS services(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, price REAL NOT NULL,
  duration_min INTEGER NOT NULL DEFAULT 60, is_active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS appointments(
  id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
  therapist_id INTEGER NOT NULL REFERENCES therapists(id),
  service_id INTEGER NOT NULL REFERENCES services(id),
  start_at TEXT NOT NULL, duration_min INTEGER NOT NULL, price REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'booked', notes TEXT, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS appt_start ON appointments(start_at);
CREATE INDEX IF NOT EXISTS appt_ther ON appointments(therapist_id, start_at);
"""


class Database:
    """One shared SQLite connection guarded by a lock (small single-site app)."""

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.lastrowid


def default_path():
    return os.environ.get("SPA_DB", "spa.db")
