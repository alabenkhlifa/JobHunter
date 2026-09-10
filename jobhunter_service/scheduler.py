"""Queue independent schedules and execute one bounded collection at a time."""
from __future__ import annotations

import json
import os
import signal
import sqlite3
from contextlib import closing
import secrets
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .delivery import DeliveryQueue
from .scheduling import due_slot
from .state import private_json


class Scheduler:
    def __init__(self, service, planner, client):
        self.service, self.planner, self.client = service, planner, client
        self.delivery = DeliveryQueue(service, client)

    def queue_due(self, now=None):
        now = now or datetime.now(timezone.utc)
        queued = 0
        with self.service.store.connect() as db:
            for member in db.execute("SELECT * FROM members WHERE status='active'").fetchall():
                schedule = json.loads(member['settings'])['schedule']
                slot = due_slot(schedule, now)
                if slot:
                    result = db.execute('INSERT OR IGNORE INTO runs VALUES(?,?,?,?,?,?,?,?)',
                        ('r' + secrets.token_hex(16), member['user_id'], slot, member['revision'],
                         'queued', None, time.time(), time.time()))
                    queued += result.rowcount
        return queued

    def recover(self):
        # Run IDs/manifests/outbox rows are stable across restart. Replaying
        # collection or a committed review cannot invent a second digest run.
        with self.service.store.connect() as db:
            db.execute("UPDATE runs SET status='queued' WHERE status='running'")

    def _child(self, *args):
        from .runner import child_environment
        process = subprocess.Popen([sys.executable, '-m', 'jobhunter_service.runner', *args],
                                   cwd=Path(__file__).resolve().parent.parent,
                                   env=child_environment(self.service.root),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        try:
            process.communicate(timeout=7500)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()
            raise RuntimeError('Collection exceeded its time budget.') from None
        if process.returncode:
            raise RuntimeError('Collection or review failed. The profile remains available for its next run.')

    def _digest(self, root, selected_ids, queued, *, settings=None):
        from .presentation import render_digest
        jobs = []
        with closing(sqlite3.connect(root / 'jobs.db')) as db, db:
            db.row_factory = sqlite3.Row
            for job_id in selected_ids:
                job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                if job is None:
                    raise ValueError('A selected job is missing from this profile.')
                jobs.append(dict(job))
        settings = settings or {}
        return render_digest(jobs, queued,
            presentation=settings.get('telegram', {}).get('presentation'),
            markets=settings.get('search', {}).get('markets'))

    def run_next(self):
        with self.service.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if run is None:
                return False
            db.execute("UPDATE runs SET status='running',updated_at=? WHERE id=?", (time.time(), run['id']))
        user_id, run_id = run['user_id'], run['id']
        try:
            member = self.service._member(user_id)
            settings = json.loads(member['settings'])
            if member['revision'] != run['revision'] or not settings['schedule']['enabled']:
                raise ValueError('The schedule or profile changed after this run was queued.')
            self.service.materialize(member)
            self.delivery.reconcile(user_id)
            root = self.service.profile_dir(member)
            directory = root / 'state' / 'runs' / run_id
            output = directory / 'collection-output.json'
            reviewed = directory / 'review-output.json'
            self._child('collect', '--profile', member['profile_id'], '--data-root', str(self.service.root),
                        '--run-id', run_id, '--revision', str(run['revision']),
                        '--max-pages', str(settings['search'].get('max_pages', 2)), '--output', str(output))
            manifest = json.loads(output.read_text())
            if not manifest['candidates']:
                self.client.send_message(user_id, 'Some eligible descriptions exceeded the review limit and remain queued.' if manifest.get('omitted_candidates') else 'No eligible job matches were found for this run.')
            else:
                from .runner import review_prompt
                envelope_path = directory / 'review-input.json'
                if envelope_path.exists():
                    envelope = json.loads(envelope_path.read_text())
                else:
                    envelope = self.planner(review_prompt(manifest), {'type': 'object', 'required': [
                        'profile_id', 'run_id', 'revision', 'config_digest', 'manifest_digest', 'verdicts']})
                    private_json(envelope_path, envelope)
                self._child('review', '--profile', member['profile_id'], '--data-root', str(self.service.root),
                            '--input', str(envelope_path), '--output', str(reviewed))
                prepared = directory / 'delivery-output.json'
                self._child('prepare-delivery', '--profile', member['profile_id'], '--data-root', str(self.service.root),
                            '--run-id', run_id, '--revision', str(run['revision']), '--output', str(prepared))
                result = json.loads(prepared.read_text())
                if result['selected_ids']:
                    # The final recipient list is still validated at enqueue.
                    current = self.service._member(user_id)
                    if current['revision'] != run['revision']:
                        raise ValueError('Profile changed before digest delivery.')
                    self.delivery.enqueue(user_id, run_id,
                        self._digest(root, result['selected_ids'], result['queued_count'], settings=settings),
                        result['selected_ids'])
                else:
                    evidence = result.get('availability', {})
                    self.client.send_message(user_id, 'No listings could be confirmed open. Uncertain checks remain queued for retry.' if evidence.get('unknown') or evidence.get('unchecked') else 'The review completed; no open jobs cleared your current requirements.')
            status, error = 'complete', None
        except Exception:
            status, error = 'failed', 'Run failed or became stale; inspect private run state.'
            try:
                self.service._member(user_id)
                self.client.send_message(user_id, 'Your JobHunter run could not complete. Previously queued deliveries are retained. Your next scheduled run is still enabled.')
            except Exception:
                pass
        with self.service.store.connect() as db:
            db.execute('UPDATE runs SET status=?,error=?,updated_at=? WHERE id=?', (status, error, time.time(), run_id))
        return True

    def sync_candidate(self, user_id):
        with self.service.mutation(user_id):
            return self._sync_candidate(user_id)

    def _sync_candidate(self, user_id):
        from jobhunter_integrations.google_tracker import parse_args, sync_tracker
        member = self.service._member(user_id)
        root = self.service.profile_dir(member)
        account = json.loads(member['settings'])['accounts']['tracker']
        connection = root / 'state' / 'tracker_connection.json'
        if not account['enabled'] or not connection.is_file():
            return False
        info = json.loads(connection.read_text())
        from .account_state import connection_matches
        if not connection_matches(info, account):
            raise ValueError('Tracker settings changed. Reconnect your tracker before syncing.')
        args = parse_args(['--spreadsheet-id', info['spreadsheet_id'], '--sheet-id', str(info['sheet_id']),
            '--google-token', str(root / 'secrets' / 'tracker_token.json'), '--account', account['account'],
            '--db-path', str(root / 'jobs.db'), '--repo-root', str(root), '--candidate-root', str(root),
            '--drive-state', str(root / 'state' / 'tracker_drive_files.json')])
        sync_tracker(args)
        return True

    def sync_due(self, now=None):
        now = now or datetime.now(timezone.utc)
        slot = str(int(now.timestamp()) // 900)
        with self.service.store.connect() as db:
            members = db.execute("SELECT * FROM members WHERE status='active'").fetchall()
        for member in members:
            if not json.loads(member['settings'])['accounts']['tracker']['enabled']:
                continue
            key = f'tracker:{member["user_id"]}'
            if self.service.store.checkpoint(key) == slot:
                continue
            try:
                if self.sync_candidate(member['user_id']):
                    self.service.store.checkpoint(key, slot)
            except Exception:
                continue

    def _send_mail(self, user_id, message):
        from .presentation import plain_html
        self.service._member(user_id)
        receipt = self.client.send_message(user_id, plain_html(message))
        message_id = receipt.get('message_id') if isinstance(receipt, dict) else None
        return type(message_id) is int and message_id > 0

    def monitor_due(self, now=None):
        from jobhunter_integrations.gmail_monitor import run_candidate_monitor
        from zoneinfo import ZoneInfo
        now = now or datetime.now(timezone.utc)
        with self.service.store.connect() as db:
            members = db.execute("SELECT * FROM members WHERE status='active'").fetchall()
        for member in members:
            try:
                with self.service.mutation(member['user_id']):
                    # Re-read after enumeration; a candidate may have paused
                    # monitoring or changed accounts while another was polled.
                    current = self.service._member(member['user_id'])
                    settings = json.loads(current['settings'])
                    local = now.astimezone(ZoneInfo(settings['schedule']['timezone']))
                    gmail = settings['accounts']['gmail']
                    root = self.service.profile_dir(current)
                    if not gmail['enabled'] or not (root / 'secrets' / 'gmail_token.json').is_file():
                        continue
                    slot = local.strftime('%Y-%m-%dT%H')
                    key = f'mail:{member["user_id"]}'
                    if local.hour not in {10, 15} or self.service.store.checkpoint(key) == slot:
                        continue
                    run_candidate_monitor(token_path=root / 'secrets' / 'gmail_token.json', account=gmail['account'],
                        db_path=root / 'jobs.db', state_path=root / 'state' / 'gmail_seen.json', candidate_root=root,
                        send=lambda text, uid=member['user_id']: self._send_mail(uid, text),
                        tracker_sync=(lambda uid=member['user_id']: self.sync_candidate(uid)) if settings['accounts']['tracker']['enabled'] else None)
                    self.service.store.checkpoint(key, slot)
            except Exception:
                continue
