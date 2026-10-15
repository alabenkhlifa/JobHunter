"""Keep listing closure separate from application history and delivery receipts."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS tracker_listing_closures (
        row_key TEXT PRIMARY KEY, job_id TEXT, company TEXT NOT NULL,
        title TEXT NOT NULL, url TEXT NOT NULL, closed_at TEXT NOT NULL,
        reason TEXT NOT NULL, previous_status TEXT NOT NULL,
        tracker_synced_at TEXT, telegram_notified_at TEXT,
        notification_cancelled_at TEXT
    )""")


def serialized_key(row, key):
    return json.dumps(key(row), ensure_ascii=False, separators=(",", ":"))


def record_closure(conn, row, *, key, job_id, checked_at, reason):
    """Queue a closure once, without changing any application stage or receipt."""
    datetime.fromisoformat(checked_at)
    ensure_schema(conn)
    inserted = conn.execute("""INSERT INTO tracker_listing_closures
        (row_key,job_id,company,title,url,closed_at,reason,previous_status)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(row_key) DO NOTHING""", (
        serialized_key(row, key), job_id, row[4], row[3], key(row)[0],
        checked_at, reason, str(row[2]),
    )).rowcount
    if job_id:
        conn.execute("UPDATE jobs SET status='unavailable' WHERE id=? AND status NOT IN ('rejected','closed')", (job_id,))
    return bool(inserted)


def events(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tracker_listing_closures'").fetchone():
        return []
    cursor = conn.execute("SELECT * FROM tracker_listing_closures ORDER BY closed_at,row_key")
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def project_rows(rows, db_path, *, key):
    """Apply closures after the normal merge, including manually added Sheet rows."""
    path = Path(db_path).resolve()
    if not path.exists():
        return rows
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as conn:
        closed = {event['row_key']: event for event in events(conn)}
    result = [list(row) for row in rows]
    for row in result[1:]:
        event = closed.get(serialized_key(row, key))
        if not event or str(row[2]).strip().casefold() == 'rejected':
            continue
        previous = str(row[2]) if str(row[2]).strip().casefold() != 'closed' else event['previous_status']
        label = 'Listing closed at ' + event['closed_at'] + '; application status: ' + previous
        notes = [part for part in str(row[12]).split(' | ') if part and not part.startswith('Listing closed at ')]
        row[12] = ' | '.join([*notes, label])
        row[2] = 'closed'
        closed_time = datetime.fromisoformat(event['closed_at']).astimezone(timezone.utc)
        try:
            existing = datetime.strptime(str(row[1]).lstrip("'"), '%d/%m/%Y %H:%M').replace(tzinfo=timezone.utc)
        except ValueError:
            existing = None
        if existing is None or existing < closed_time:
            row[1] = closed_time.strftime('%d/%m/%Y %H:%M')
        row[13] = 'Listing closed; retain application history and monitor recruiter replies if already applied'
    return result


def mark_synced(conn, rows, *, key, now):
    """Only allow alerts after the Sheet's closed status has been read back."""
    visible = {serialized_key(row, key) for row in rows[1:] if str(row[2]).strip().casefold() == 'closed'}
    for event in events(conn):
        if event['telegram_notified_at'] or event['notification_cancelled_at']:
            continue
        if event['row_key'] in visible:
            conn.execute('UPDATE tracker_listing_closures SET tracker_synced_at=? WHERE row_key=?', (now, event['row_key']))
        else:
            conn.execute('UPDATE tracker_listing_closures SET notification_cancelled_at=? WHERE row_key=?', (now, event['row_key']))
    conn.commit()
