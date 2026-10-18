"""Private job commands layered onto the restricted onboarding transport."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing

from .applications import ApplicationService
from .job_intents import JOB_ID, MAX_DIGEST_JOBS, JobIntent, bare_show_id, matching_jobs, parse_job_callback, parse_job_intent
from .presentation import digest_markup
from .telegram import TelegramAPIError, TelegramHandler


class ApplicationTelegramHandler(TelegramHandler):
    def __init__(self, service, client, assistant=None, scheduler=None):
        super().__init__(service, client, assistant)
        self.scheduler = scheduler
        self.applications = ApplicationService(service, service.browser_manager, client,
                                               planner=getattr(assistant, 'planner', None))
        if scheduler:
            service.sync_tracker = scheduler.sync_candidate

    def _own_jobs(self, actor_id, *, job_id=None):
        member = self.service._member(actor_id)
        root = self.service.profile_dir(member)
        database = root / 'jobs.db'
        if database.resolve() != database:
            raise PermissionError('The jobs database must belong to your own profile.')
        if not database.is_file():
            return []
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            if job_id is not None:
                rows = db.execute('SELECT id,title,company,status FROM jobs WHERE id=?', (job_id,)).fetchall()
            else:
                rows = db.execute('SELECT id,title,company,status FROM jobs ORDER BY rowid DESC LIMIT 200').fetchall()
        return [dict(row) for row in rows]

    def _latest_digest_ids(self, actor_id):
        """Anchor numeric choices to a complete, acknowledged candidate digest."""
        member = self.service._member(actor_id)
        with self.service.store.connect() as db:
            columns = {row['name'] for row in db.execute('PRAGMA table_info(deliveries)')}
            digest_filter = " AND d.kind='digest'" if 'kind' in columns else ''
            other_filter = " AND other.kind='digest'" if 'kind' in columns else ''
            row = db.execute("SELECT d.run_id FROM deliveries d JOIN runs r ON r.id=d.run_id AND r.user_id=d.user_id "
                "WHERE d.user_id=? AND d.status='sent'" + digest_filter +
                " AND EXISTS (SELECT 1 FROM delivery_jobs j WHERE j.run_id=d.run_id AND j.user_id=d.user_id) "
                "AND NOT EXISTS (SELECT 1 FROM deliveries other WHERE other.run_id=d.run_id "
                "AND other.user_id=d.user_id AND other.chat_id=d.chat_id AND other.status!='sent'" + other_filter +
                ') ORDER BY d.id DESC LIMIT 1', (actor_id,)).fetchone()
            if row is None:
                return []
            run_id = row['run_id']
            columns = {entry['name'] for entry in db.execute('PRAGMA table_info(delivery_jobs)')}
            position = 'position' if 'position' in columns else '0 AS position'
            selected = db.execute(f'SELECT job_id,{position} FROM delivery_jobs WHERE user_id=? AND run_id=?',
                                  (actor_id, run_id)).fetchall()
        if not selected or len(selected) > MAX_DIGEST_JOBS:
            return []
        ordered = sorted(selected, key=lambda item: item['position'])
        if [item['position'] for item in ordered] == list(range(1, len(ordered) + 1)):
            return [item['job_id'] for item in ordered]
        # Old digests had no persisted positions. Never invent order by ID.
        if not isinstance(run_id, str) or not JOB_ID.fullmatch(run_id):
            return []
        root = self.service.profile_dir(member)
        path = root / 'state' / 'runs' / run_id / 'delivery-output.json'
        if not path.resolve().is_relative_to(root) or not path.is_file() or path.stat().st_size > 32_000:
            return []
        try:
            ids = json.loads(path.read_text()).get('selected_ids')
            if (isinstance(ids, list) and len(ids) == len(selected)
                    and all(isinstance(value, str) and JOB_ID.fullmatch(value) for value in ids)
                    and len(set(ids)) == len(ids) and set(ids) == {item['job_id'] for item in selected}):
                return ids
        except (ValueError, OSError, AttributeError):
            pass
        return []

    def _clarify_job(self, actor_id, matches=None):
        self._send(actor_id, 'Choose the exact role using its button or job ID. '
                   'A job number refers to your latest delivered digest; say "show my jobs" to see your choices.')
        if matches:
            self._send(actor_id, '\n\n'.join(f"{job['title']} — {job['company']}\nJob ID: {job['id']}" for job in matches[:MAX_DIGEST_JOBS]))
            self._navigation(actor_id, [job['id'] for job in matches], numbered=False)

    def _resolve_job(self, actor_id, reference, *, exact_id=False):
        if exact_id:
            jobs = self._own_jobs(actor_id, job_id=reference)
            if not jobs:
                raise ValueError('This job is not in your own collected jobs. Ask to show your jobs again.')
            return jobs[0]['id']
        number = re.fullmatch(r'#?([1-9][0-9]?)', reference)
        if number:
            ids = self._latest_digest_ids(actor_id)
            index = int(number[1]) - 1
            if index < len(ids) and self._own_jobs(actor_id, job_id=ids[index]):
                return ids[index]
            self._clarify_job(actor_id)
            return None
        if JOB_ID.fullmatch(reference):
            exact = self._own_jobs(actor_id, job_id=reference)
            if exact:
                return exact[0]['id']
        matches = matching_jobs(reference, self._own_jobs(actor_id))
        if len(matches) == 1:
            return matches[0]['id']
        self._clarify_job(actor_id, matches)
        return None

    def _navigation(self, actor_id, ids, *, numbered=False):
        markup = digest_markup(ids, numbered=numbered)
        if markup:
            self.client.send_message(actor_id, 'Choose Details to read a role, Interested to track it, '
                                     'or Prepare to create your resume. Each upload and final submission needs its own confirmation.',
                                     reply_markup=markup)

    def _show_jobs(self, actor_id, *, collected=False):
        ids = [] if collected else self._latest_digest_ids(actor_id)
        jobs = [job for job_id in ids for job in self._own_jobs(actor_id, job_id=job_id)] if ids else self._own_jobs(actor_id)[:MAX_DIGEST_JOBS]
        if not jobs:
            self._send(actor_id, 'No jobs collected yet. Use /status to check your next scheduled run.')
            return
        blocks = ['Your latest delivered matches:' if ids else 'Your collected jobs (including roles still held for review):']
        for job in jobs:
            # Keep displayed numbers tied to the acknowledged digest even if a
            # listing was removed from the candidate database afterwards.
            label = f"{ids.index(job['id']) + 1}. " if ids else ''
            blocks.append(f"{label}{job['title']} — {job['company']}\n{job['status']}\nJob ID: {job['id']}")
        self._send(actor_id, '\n\n'.join(blocks))
        if ids and len(jobs) != len(ids):
            for job in jobs:
                self._navigation(actor_id, [job['id']], numbered=False)
        else:
            self._navigation(actor_id, [job['id'] for job in jobs], numbered=bool(ids))

    def _job_action(self, actor_id, operation, job_id):
        action = {'details': self.applications.details, 'interested': self.applications.interested,
                  'prepare': self.applications.prepare}[operation]
        action(actor_id, job_id)
        self._navigation(actor_id, [job_id])

    def _handle_private(self, actor_id, message, callback):
        if callback and str(callback.get('data', '')).startswith('jh:job:'):
            self.service.authorize(actor_id)
            intent = parse_job_callback(callback['data'])
            job_id = self._resolve_job(actor_id, intent.reference, exact_id=True)
            try:
                self.client.answer_callback(callback['id'], 'Choice received')
            except TelegramAPIError:
                pass
            self._job_action(actor_id, intent.operation, job_id)
            return
        if callback and str(callback.get('data', '')).startswith('jh:application:'):
            self.service.authorize(actor_id)
            token = callback['data'][15:]
            if not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
                raise ValueError('This application confirmation is invalid.')
            self.applications.execute(actor_id, token)
            try:
                self.client.answer_callback(callback['id'], 'Application action recorded')
            except TelegramAPIError:
                pass
            return
        text = message.get('text', '')
        parts = text.split() if isinstance(text, str) else []
        command = parts[0].split('@', 1)[0].lower() if parts else ''
        actions = {'/details': self.applications.details, '/interested': self.applications.interested,
                   '/apply': self.applications.prepare, '/cover': self.applications.prepare_with_cover,
                   '/inspect': self.applications.inspect,
                   '/upload': self.applications.propose_upload, '/submit': self.applications.propose_submit}
        if callback is None and command in {*actions, '/jobs', '/tracker', '/logout'}:
            self.service.authorize(actor_id)
            member = self.service._member(actor_id)
            if command in actions:
                if len(parts) != 2:
                    raise ValueError(f'Use {command} <job_id> with a job ID from your digest or /jobs.')
                actions[command](actor_id, parts[1])
                if command in {'/details', '/interested', '/apply'}:
                    self._navigation(actor_id, [parts[1]])
            elif command == '/jobs':
                self._show_jobs(actor_id)
            elif command == '/tracker':
                connected = self.scheduler and self.scheduler.sync_candidate(actor_id)
                if connected:
                    info = json.loads((self.service.profile_dir(member) / 'state' / 'tracker_connection.json').read_text())
                    self._send(actor_id, 'Your tracker is synchronized.\n' + info['spreadsheet_url'])
                else:
                    self._send(actor_id, 'Configure your tracker account, then use /connect tracker.')
            else:
                if self.service.browser_manager:
                    self.service.browser_manager.stop(member['profile_id'])
                with self.service.store.connect() as db:
                    db.execute("UPDATE tokens SET consumed=1 WHERE user_id=? AND purpose IN ('browser_session','browser_connect','application_approval')", (actor_id,))
                from .connections import clear_connection
                clear_connection(self.service, actor_id, 'linkedin')
                self._send(actor_id, 'Your browser session is closed. Its private login profile is retained for your next connection.')
            return
        if callback is None:
            intent = parse_job_intent(text)
            bare_id = bare_show_id(text) if intent is None else None
            if bare_id and self._own_jobs(actor_id, job_id=bare_id):
                intent = JobIntent('details', bare_id)
            if intent:
                self.service.authorize(actor_id)
                if intent.operation == 'jobs':
                    self._show_jobs(actor_id, collected=intent.reference == 'collected')
                    return
                # A role mentioned during setup is not application approval.
                state = self.service.onboarding_status(actor_id)
                explicit = bool(re.fullmatch(r'#?[1-9][0-9]?', intent.reference)
                                or self._own_jobs(actor_id, job_id=intent.reference))
                if state.get('started') and not state.get('complete') and not explicit:
                    return super()._handle_private(actor_id, message, callback)
                job_id = self._resolve_job(actor_id, intent.reference)
                if job_id:
                    self._job_action(actor_id, intent.operation, job_id)
                return
        return super()._handle_private(actor_id, message, callback)
