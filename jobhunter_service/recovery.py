"""Private, bounded retries for candidate intent after a model outage.

Records contain intent only, never a model proposal, confirmation, credentials,
or uploaded resume text. The handler must replan against a fresh snapshot and
keep normal confirmation gates. Failed Telegram delivery remains an ingress
failure; persisting this record is not acknowledgement of a delivered notice.
"""
from __future__ import annotations

import json
import hashlib
import re
import secrets
import time

from .hermes import MAX_MESSAGE_CHARS, contains_credentials


MAX_RECOVERY_ATTEMPTS = 3
RECOVERY_TTL_SECONDS = 24 * 60 * 60
RESUME_RETRY_INTENT = (
    'I uploaded my resume. Begin refinement with one focused question about my '
    'first experience. Do not confirm any facts.'
)
RESUME_FOLLOWUP_INTENT = (
    'My resume changes have been confirmed and saved. Continue the experience '
    'interview with one new focused question, using my confirmed facts, uploaded '
    'source and conversation so I do not repeat an answer. Return only a reply; '
    'do not propose or save changes. I will choose Done reviewing when finished.'
)


def _initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS onboarding_recovery (
        user_id INTEGER PRIMARY KEY, reference TEXT NOT NULL UNIQUE,
        text TEXT NOT NULL, kind TEXT NOT NULL, attempts INTEGER NOT NULL,
        request_ids TEXT NOT NULL, expires_at REAL NOT NULL,
        revocation_id INTEGER NOT NULL)''')


def _active_generation(db, actor_id):
    if type(actor_id) is not int or not 0 < actor_id < 2**63:
        raise PermissionError('A private candidate identity is required.')
    member = db.execute('SELECT status FROM members WHERE user_id=?', (actor_id,)).fetchone()
    if member is None or member['status'] != 'active':
        raise PermissionError('Active JobHunter access is required.')
    return db.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE target=? "
                      "AND operation IN ('suspend','revoke')", (actor_id,)).fetchone()[0]


def _current(db, actor_id, generation):
    row = db.execute('SELECT * FROM onboarding_recovery WHERE user_id=?', (actor_id,)).fetchone()
    if row and (row['expires_at'] <= time.time() or row['revocation_id'] != generation):
        db.execute('DELETE FROM onboarding_recovery WHERE user_id=?', (actor_id,))
        return None
    return row


def _details(row):
    return {key: row[key] for key in ('text', 'kind', 'reference', 'attempts')} | {'category': 'model_unavailable'}


def save_recovery(service, actor_id, text, kind='message'):
    """Persist safe intent; return only a reference and nonsensitive status.

    Repeated notice delivery or another failed attempt for the same intent
    keeps its retry count and reference. A different intent replaces the old
    one. Resume recovery always refers to the separately staged source.
    """
    if kind not in {'message', 'resume', 'resume_followup'}:
        raise ValueError('Unsupported onboarding recovery request.')
    if kind == 'resume':
        text = RESUME_RETRY_INTENT
    elif kind == 'resume_followup':
        text = RESUME_FOLLOWUP_INTENT
    if (not isinstance(text, str) or not text.strip() or len(text) > MAX_MESSAGE_CHARS
            or contains_credentials(text)):
        raise ValueError('Send one short JobHunter request without credentials.')
    with service.mutation(actor_id), service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        generation = _active_generation(db, actor_id)
        row = _current(db, actor_id, generation)
        if row is None or row['text'] != text or row['kind'] != kind:
            reference = 'JH-' + secrets.token_hex(6).upper()
            db.execute('INSERT INTO onboarding_recovery VALUES(?,?,?,?,?,?,?,?) '
                       'ON CONFLICT(user_id) DO UPDATE SET reference=excluded.reference,text=excluded.text,'
                       'kind=excluded.kind,attempts=0,request_ids=excluded.request_ids,'
                       'expires_at=excluded.expires_at,revocation_id=excluded.revocation_id',
                       (actor_id, reference, text, kind, 0, '[]', time.time() + RECOVERY_TTL_SECONDS, generation))
            row = _current(db, actor_id, generation)
        return {key: value for key, value in _details(row).items() if key not in {'text', 'kind'}}


def get_recovery(service, actor_id):
    """Read only this active candidate's unexpired failed intent."""
    with service.mutation(actor_id), service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        row = _current(db, actor_id, _active_generation(db, actor_id))
        return _details(row) if row else None


def callback_retry_id(callback_id):
    """Stable receipt key: one callback redelivery, distinct deliberate clicks."""
    if not isinstance(callback_id, str) or not 1 <= len(callback_id) <= 256:
        raise ValueError('A valid private retry callback is required.')
    return 'callback:' + hashlib.sha256(callback_id.encode()).hexdigest()


def begin_retry(service, actor_id, request_id=None, *, reference=None):
    """Reserve a bounded attempt; redelivery of the same message is idempotent."""
    message_id = type(request_id) is int and 0 < request_id < 2**63
    callback_id = isinstance(request_id, str) and re.fullmatch(r'callback:[0-9a-f]{64}', request_id)
    if request_id is not None and not (message_id or callback_id):
        raise ValueError('A valid private retry message is required.')
    with service.mutation(actor_id), service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        row = _current(db, actor_id, _active_generation(db, actor_id))
        if row is None:
            raise ValueError('There is no pending request to retry. Use /continue or send your request again.')
        if reference is not None and row['reference'] != reference:
            raise ValueError('This retry belongs to an older request. Use /status to continue.')
        request_ids = json.loads(row['request_ids'])
        if request_id is not None and request_id in request_ids:
            return _details(row)
        if row['attempts'] >= MAX_RECOVERY_ATTEMPTS:
            raise ValueError('This request has reached its retry limit. Use /support for a reference to share with the owner.')
        if request_id is not None:
            request_ids.append(request_id)
        db.execute('UPDATE onboarding_recovery SET attempts=attempts+1,request_ids=? WHERE user_id=?',
                   (json.dumps(request_ids), actor_id))
        return {**_details(row), 'attempts': row['attempts'] + 1}


def clear_recovery(service, actor_id, reference):
    """Clear only the successfully handled intent, never a newer failure."""
    with service.mutation(actor_id), service.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _initialize(db)
        _active_generation(db, actor_id)
        return bool(db.execute('DELETE FROM onboarding_recovery WHERE user_id=? AND reference=?',
                               (actor_id, reference)).rowcount)
