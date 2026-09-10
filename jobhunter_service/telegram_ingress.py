"""Durable private updates forwarded by the authenticated owner Hermes poller.

This queue provides at-least-once handling across crashes. Each update has its
own receipt; the owner's Bot API polling offset is never consulted or changed.
Existing confirmation and application tokens keep retried actions idempotent.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time

from .telegram import _private_actor


MAX_UPDATE_BYTES = 65536
MAX_BACKOFF_SECONDS = 300
_ADMIN_COMMAND = re.compile(r'^/jobhunter(?:@\w+)?(?:\s|$)', re.I)
_ONBOARD_CALLBACK = re.compile(
    r'jh:onboard:(?:acknowledge|skip|reopen|activate|check):'
    r'(?:resume|roles|markets|schedule|delivery|linkedin|gmail|tracker|review):(?:0|[1-9][0-9]{0,19})'
)
_RETRY_CALLBACK = re.compile(r'jh:retry:JH-[A-F0-9]{12}')


class IngressConflict(ValueError):
    """The same Telegram update ID was submitted with different content."""


def _validate_update(update, owner_id):
    if (not isinstance(update, dict) or type(update.get('update_id')) is not int
            or not 0 <= update['update_id'] < 2**63
            or set(update) not in ({'update_id', 'message'}, {'update_id', 'callback_query'})):
        raise ValueError('A supported raw Telegram update is required.')
    private = _private_actor(update)
    if private is None:
        raise PermissionError('Only the sender\'s own private Telegram chat is accepted.')
    actor, message, callback = private
    sender = callback['from'] if callback else message['from']
    if actor >= 2**63 or sender.get('is_bot') is not False:
        raise PermissionError('A private human Telegram identity is required.')
    if type(message.get('message_id')) is not int or message['message_id'] <= 0:
        raise ValueError('The Telegram message identity is missing.')
    if callback is not None:
        bot = message.get('from')
        if (not isinstance(bot, dict) or bot.get('is_bot') is not True
                or type(bot.get('id')) is not int or not 0 < bot['id'] < 2**63):
            raise PermissionError('The callback must belong to a bot message in the sender\'s private chat.')
        if (not isinstance(callback.get('id'), str) or not 1 <= len(callback['id']) <= 256
                or not isinstance(callback.get('data'), str)
                or not 1 <= len(callback['data'].encode('utf-8')) <= 64
                or not (callback['data'].startswith(('jh:confirm:', 'jh:application:'))
                        or _ONBOARD_CALLBACK.fullmatch(callback['data'])
                        or _RETRY_CALLBACK.fullmatch(callback['data']))):
            raise ValueError('A supported JobHunter callback is required.')
    else:
        text = message.get('text')
        document = message.get('document')
        if text is not None and (not isinstance(text, str) or len(text) > 4096):
            raise ValueError('The Telegram text exceeds the supported limit.')
        if document is not None:
            if (not isinstance(document, dict) or not isinstance(document.get('file_id'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,512}', document['file_id'])):
                raise ValueError('A Telegram document identifier is required.')
        elif not isinstance(text, str) or not text:
            raise ValueError('Send a text message or resume document.')
    if actor == owner_id and (callback is not None or not _ADMIN_COMMAND.match(message.get('text') or '')):
        raise PermissionError('Only owner JobHunter administration commands use this ingress.')
    return actor


class TelegramIngress:
    def __init__(self, service, handler, *, clock=None, lease_seconds=300):
        if (type(lease_seconds) not in (int, float) or not math.isfinite(lease_seconds)
                or not 1 <= lease_seconds <= 3600):
            raise ValueError('The ingress lease must be between one second and one hour.')
        self.service, self.handler = service, handler
        self.clock, self.lease_seconds = clock or time.time, lease_seconds
        self.lock_path = service.store.path.with_name('telegram-ingress.lock')
        with service.store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS telegram_ingress (
                id INTEGER PRIMARY KEY, update_id INTEGER NOT NULL UNIQUE,
                actor_id INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL, lease_token TEXT, lease_until REAL,
                created_at REAL NOT NULL, completed_at REAL, last_error TEXT)''')
            db.execute('CREATE INDEX IF NOT EXISTS telegram_ingress_ready ON telegram_ingress(status,next_attempt,id)')

    def enqueue(self, update):
        """Validate and persist only; no Telegram, model, or application calls."""
        actor = _validate_update(update, self.service.owner_id)
        try:
            payload = json.dumps(update, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
            encoded = payload.encode('utf-8')
        except (ValueError, TypeError, RecursionError, UnicodeError):
            raise ValueError('The Telegram update is not valid bounded JSON.') from None
        if len(encoded) > MAX_UPDATE_BYTES:
            raise ValueError('The Telegram update exceeds the supported payload size.')
        digest = hashlib.sha256(encoded).hexdigest()
        now = self.clock()
        with self.service.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT digest,status FROM telegram_ingress WHERE update_id=?', (update['update_id'],)).fetchone()
            duplicate = row is not None
            if row:
                if row['digest'] != digest:
                    raise IngressConflict('This Telegram update ID already has a different payload.')
                status = row['status']
            else:
                db.execute('INSERT INTO telegram_ingress(update_id,actor_id,payload,digest,next_attempt,created_at) VALUES(?,?,?,?,?,?)',
                           (update['update_id'], actor, payload, digest, now, now))
                status = 'pending'
        return {'accepted': True, 'update_id': update['update_id'], 'duplicate': duplicate, 'status': status}

    @contextmanager
    def _delivery_lock(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise PermissionError('The ingress delivery lock must be private to this service.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                yield True
        finally:
            os.close(fd)

    def drain_one(self):
        """Attempt one due update; False means idle or another process owns it."""
        with self._delivery_lock() as acquired:
            if not acquired:
                return False
            now, token = self.clock(), secrets.token_hex(16)
            with self.service.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE telegram_ingress SET status='pending',lease_token=NULL,lease_until=NULL "
                           "WHERE status='processing' AND lease_until<=?", (now,))
                # Unexpected or delivery failures keep mutation order. The
                # handler acknowledges model outages only after persisting a
                # recovery intent and delivering its explicit retry notice.
                # That completed receipt releases this actor's setup commands.
                row = db.execute("SELECT q.* FROM telegram_ingress q WHERE q.status='pending' AND q.next_attempt<=? "
                    "AND NOT EXISTS (SELECT 1 FROM telegram_ingress earlier WHERE earlier.actor_id=q.actor_id "
                    "AND earlier.id<q.id AND earlier.status!='done') ORDER BY q.id LIMIT 1", (now,)).fetchone()
                if row is None:
                    return False
                db.execute("UPDATE telegram_ingress SET status='processing',attempts=attempts+1,lease_token=?,lease_until=? WHERE id=?",
                           (token, now + self.lease_seconds, row['id']))
            try:
                if hashlib.sha256(row['payload'].encode('utf-8')).hexdigest() != row['digest']:
                    raise ValueError('The stored ingress payload failed validation.')
                update = json.loads(row['payload'])
                if _validate_update(update, self.service.owner_id) != row['actor_id']:
                    raise ValueError('The stored ingress identity failed validation.')
                self.handler.handle_update(update, use_offset=False)
            except Exception as error:
                attempts = row['attempts'] + 1
                delay = min(MAX_BACKOFF_SECONDS, 2 ** min(attempts, 9))
                with self.service.store.connect() as db:
                    db.execute("UPDATE telegram_ingress SET status='pending',next_attempt=?,lease_token=NULL,lease_until=NULL,last_error=? "
                               "WHERE id=? AND status='processing' AND lease_token=?",
                               (self.clock() + delay, type(error).__name__, row['id'], token))
            else:
                with self.service.store.connect() as db:
                    db.execute("UPDATE telegram_ingress SET status='done',completed_at=?,lease_token=NULL,lease_until=NULL,last_error=NULL "
                               "WHERE id=? AND status='processing' AND lease_token=?",
                               (self.clock(), row['id'], token))
            return True
