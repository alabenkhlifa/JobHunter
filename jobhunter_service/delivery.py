"""Durable digest fan-out: retry only unacknowledged destinations."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
import time

import job_scoring
import jobhunter_availability as availability
import jobhunter_matching


MAX_RUN_JOBS = 12
MAX_DRAIN_CHECKS = 40
MAX_RUN_SECONDS = 120


class DeliveryQueue:
    def __init__(self, service, client, *, checker=None):
        self.service, self.client = service, client
        self.checker = checker

    def enqueue(self, user_id, run_id, message, selected_ids):
        with self.service.mutation(user_id):
            return self._enqueue(user_id, run_id, message, selected_ids)

    def _enqueue(self, user_id, run_id, message, selected_ids):
        member = self.service._member(user_id)
        from .telegram import message_chunks
        parts = message_chunks(message)
        if not parts:
            raise ValueError('A digest cannot be empty.')
        destinations = json.loads(member['settings'])['telegram']['destinations']
        with self.service.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT user_id,revision FROM runs WHERE id=?', (run_id,)).fetchone()
            current = db.execute("SELECT status,revision FROM members WHERE user_id=?", (user_id,)).fetchone()
            if not current or current['status'] != 'active' or not row or row['revision'] != current['revision']:
                raise ValueError('The candidate profile changed before delivery was queued.')
            if not row or row['user_id'] != user_id:
                raise PermissionError('The digest run belongs to another account.')
            for destination in destinations:
                if destination['kind'] == 'channel':
                    self.client.validate_destination(user_id, destination)
                for part, chunk in enumerate(parts):
                    db.execute('INSERT INTO deliveries(run_id,user_id,chat_id,part,message) VALUES(?,?,?,?,?) '
                               'ON CONFLICT(run_id,chat_id,part) DO NOTHING',
                               (run_id, user_id, destination['chat_id'], part, chunk))
            db.executemany('INSERT OR IGNORE INTO delivery_jobs VALUES(?,?,?)',
                           [(run_id, user_id, job_id) for job_id in selected_ids])
        self.reconcile(user_id)

    def reconcile(self, user_id):
        with self.service.mutation(user_id):
            return self._reconcile(user_id)

    def _reconcile(self, user_id):
        member = self.service._member(user_id)
        with self.service.store.connect() as db:
            rows = db.execute('SELECT j.job_id, '
                "MAX(CASE WHEN d.status='sent' AND NOT EXISTS (SELECT 1 FROM deliveries other WHERE other.run_id=d.run_id AND other.chat_id=d.chat_id AND other.status!='sent') THEN 1 ELSE 0 END) AS acknowledged, "
                "MAX(CASE WHEN d.status='pending' THEN 1 ELSE 0 END) AS pending "
                'FROM delivery_jobs j JOIN deliveries d ON d.run_id=j.run_id '
                'WHERE j.user_id=? GROUP BY j.job_id', (user_id,)).fetchall()
        root = self.service.profile_dir(member)
        path = root / 'jobs.db'
        if path.resolve() != path:
            raise PermissionError('The jobs database cannot follow a symlink.')
        if not path.is_file():
            return
        with closing(sqlite3.connect(path)) as jobs, jobs:
            for row in rows:
                if row['acknowledged']:
                    jobs.execute("UPDATE jobs SET notified=1,status=CASE WHEN status='delivery_pending' THEN 'new' ELSE status END WHERE id=?", (row['job_id'],))
                elif row['pending']:
                    jobs.execute("UPDATE jobs SET status='delivery_pending' WHERE id=? AND status='new' AND notified=0", (row['job_id'],))
                else:
                    jobs.execute("UPDATE jobs SET status='new' WHERE id=? AND status='delivery_pending'", (row['job_id'],))

    def _cancel_run(self, user_id, run_id):
        with self.service.store.connect() as db:
            db.execute("UPDATE deliveries SET status='cancelled' WHERE user_id=? AND run_id=? AND status='pending'",
                       (user_id, run_id))

    def _preflight(self, member, run_id, budget):
        """Freshly validate the immutable digest once for this drain's fan-out.

        No cached open result is sufficient for a retry. Unknown evidence keeps
        the complete unsent remainder pending; closure invalidates the message
        and cancels that remainder, including after another channel acknowledged.
        """
        user_id = member['user_id']
        with self.service.store.connect() as db:
            ids = [row['job_id'] for row in db.execute(
                'SELECT job_id FROM delivery_jobs WHERE user_id=? AND run_id=? ORDER BY job_id',
                (user_id, run_id))]
        if len(ids) > MAX_RUN_JOBS:
            return 'cancelled'
        # A legitimate no-matches digest has no listing to validate.
        if not ids:
            return 'open'
        path = self.service.profile_dir(member) / 'jobs.db'
        if not path.is_file() or path.resolve() != path:
            return 'cancelled'
        search = jobhunter_matching.validate_config(json.loads(member['settings'])['search'])
        now = datetime.now(timezone.utc)
        deadline = time.monotonic() + MAX_RUN_SECONDS
        with closing(sqlite3.connect(path)) as db:
            db.row_factory = sqlite3.Row
            jobs = []
            for job_id in ids:
                row = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if row is None:
                    return 'cancelled'
                job = dict(row)
                # Acknowledgement at one destination must not skip the
                # availability check for the other destinations.
                if job.get('status') not in ('new', 'delivery_pending') or job.get('ai_verdict') != 'send':
                    return 'cancelled'
                posted = None
                for field in ('date_posted', 'date_scraped'):
                    try:
                        posted = datetime.fromisoformat(str(job.get(field) or '').replace('Z', '+00:00'))
                        break
                    except ValueError:
                        continue
                if posted is None:
                    return 'cancelled'
                if posted.tzinfo is None:
                    posted = posted.replace(tzinfo=timezone.utc)
                if (now - posted).days > search['max_job_age_days']:
                    return 'cancelled'
                evaluation = job_scoring.evaluate(job, allowed_locations=search['allowed_locations'],
                    max_experience=search['max_experience'], matching=search['matching'],
                    markets=search['markets'], now=now)
                if (evaluation['reason'] is not None or evaluation['total'] < search['score_threshold']
                        or not jobhunter_matching.review_sendable(job, search['markets'])):
                    return 'cancelled'
                jobs.append(job)
            unknown = False
            for job in jobs:
                if (budget['checked'] >= MAX_DRAIN_CHECKS
                        or time.monotonic() + availability.TOTAL_SECONDS > deadline):
                    return 'unknown'
                budget['checked'] += 1
                try:
                    result = (self.checker or availability.check)(job)
                except Exception:
                    listing = availability._listing(job)
                    result = {'job_id': str(job['id']), 'state': 'unknown', 'reason': 'request_failed',
                              'url': listing.url if listing else '',
                              'source_job_id': listing.source_id if listing else '', 'matched': False,
                              'checked_at': datetime.now(timezone.utc).isoformat()}
                try:
                    availability.recordcheck(db, job, result)
                    db.commit()
                except Exception:
                    db.rollback()
                    unknown = True
                    continue
                if result['state'] == 'closed':
                    return 'cancelled'
                if result['state'] != 'open' or result.get('matched') is not True:
                    unknown = True
            return 'unknown' if unknown else 'open'

    def drain(self):
        with self.service.store.connect() as db:
            rows = db.execute("SELECT d.* FROM deliveries d JOIN members m ON m.user_id=d.user_id "
                              "WHERE d.status='pending' AND m.status='active' ORDER BY d.attempts,d.id LIMIT 100").fetchall()
        sent, failed = 0, 0
        preflights, budget = {}, {'checked': 0}
        for row in rows:
            with self.service.mutation(row['user_id']):
                try:
                    member = self.service._member(row['user_id'])
                    with self.service.store.connect() as db:
                        current = db.execute('SELECT status FROM deliveries WHERE id=?', (row['id'],)).fetchone()
                        if not current or current['status'] != 'pending':
                            continue
                        run = db.execute('SELECT user_id,revision FROM runs WHERE id=?', (row['run_id'],)).fetchone()
                        if not run or run['user_id'] != row['user_id'] or run['revision'] != member['revision']:
                            db.execute("UPDATE deliveries SET status='cancelled' WHERE user_id=? AND run_id=? AND status='pending'",
                                       (row['user_id'], row['run_id']))
                            continue
                    key = (row['user_id'], row['run_id'], member['revision'])
                    if key not in preflights:
                        # Untouched runs must keep their priority for the next
                        # drain. Counting a budget skip as a retry would let old
                        # inconclusive sources starve every later profile.
                        if budget['checked'] >= MAX_DRAIN_CHECKS:
                            continue
                        try:
                            preflights[key] = self._preflight(member, row['run_id'], budget)
                        except Exception:
                            preflights[key] = 'unknown'
                    if preflights[key] == 'cancelled':
                        self._cancel_run(row['user_id'], row['run_id'])
                        continue
                    if preflights[key] != 'open':
                        raise RuntimeError('Listing availability is inconclusive; retry the whole digest.')
                    current_destinations = json.loads(member['settings'])['telegram']['destinations']
                    destination = next((x for x in current_destinations if x['chat_id'] == row['chat_id']), None)
                    if destination is None:
                        with self.service.store.connect() as db:
                            db.execute("UPDATE deliveries SET status='cancelled' WHERE id=?", (row['id'],))
                        continue
                    if destination['kind'] == 'channel':
                        self.client.validate_destination(row['user_id'], destination)
                    receipt = self.client.send_message(row['chat_id'], row['message'])
                    message_id = receipt.get('message_id') if isinstance(receipt, dict) else None
                    if type(message_id) is not int or message_id <= 0:
                        raise RuntimeError('Telegram acknowledgement missing.')
                except Exception:
                    failed += 1
                    with self.service.store.connect() as db:
                        db.execute('UPDATE deliveries SET attempts=attempts+1 WHERE id=?', (row['id'],))
                    continue
                with self.service.store.connect() as db:
                    db.execute("UPDATE deliveries SET status='sent',message_id=?,attempts=attempts+1 WHERE id=?", (message_id, row['id']))
                sent += 1
        for user_id in {row['user_id'] for row in rows}:
            try:
                self.reconcile(user_id)
            except PermissionError:
                pass
        return {'sent': sent, 'pending_or_failed': failed}
