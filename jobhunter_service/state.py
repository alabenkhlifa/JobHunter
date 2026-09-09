"""Private durable registration, confirmation and delivery state."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def private_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS members (
                    user_id INTEGER PRIMARY KEY, profile_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                    settings TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, actor INTEGER NOT NULL,
                    operation TEXT NOT NULL, target INTEGER, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tokens (
                    digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL, purpose TEXT NOT NULL,
                    payload TEXT NOT NULL, expires_at REAL NOT NULL, consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS checkpoints (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, local_slot TEXT NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL, error TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL,
                    UNIQUE(user_id, local_slot));
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, user_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL, part INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    message_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(run_id, chat_id, part));
                CREATE TABLE IF NOT EXISTS delivery_jobs (
                    run_id TEXT NOT NULL, user_id INTEGER NOT NULL, job_id TEXT NOT NULL,
                    PRIMARY KEY(run_id,job_id));
            ''')
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA foreign_keys=ON')
            with db:
                yield db
        finally:
            db.close()

    def member(self, user_id: int):
        with self.connect() as db:
            row = db.execute('SELECT * FROM members WHERE user_id=?', (user_id,)).fetchone()
        return dict(row) if row else None

    def token(self, user_id: int, purpose: str, payload=None, ttl=600) -> str:
        raw = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO tokens VALUES(?,?,?,?,?,0)',
                       (hashlib.sha256(raw.encode()).hexdigest(), user_id, purpose,
                        json.dumps(payload or {}), time.time() + ttl))
        return raw

    def read_token(self, raw: str, purpose: str, *, consume=True):
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT t.* FROM tokens t JOIN members m ON m.user_id=t.user_id '
                             'WHERE digest=? AND purpose=? AND consumed=0 AND expires_at>? '
                             "AND m.status='active'", (digest, purpose, time.time())).fetchone()
            if row is None:
                raise PermissionError('This link has expired or access was revoked.')
            if consume:
                db.execute('UPDATE tokens SET consumed=1 WHERE digest=?', (digest,))
            return row['user_id'], json.loads(row['payload'])

    def checkpoint(self, key, value=None):
        with self.connect() as db:
            if value is None:
                row = db.execute('SELECT value FROM checkpoints WHERE key=?', (key,)).fetchone()
                return row['value'] if row else None
            db.execute('INSERT INTO checkpoints VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, str(value)))
