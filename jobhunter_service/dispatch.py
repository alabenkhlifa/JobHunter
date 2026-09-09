"""Private job commands layered onto the restricted onboarding transport."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing

from .applications import ApplicationService
from .telegram import TelegramAPIError, TelegramHandler


class ApplicationTelegramHandler(TelegramHandler):
    def __init__(self, service, client, assistant=None, scheduler=None):
        super().__init__(service, client, assistant)
        self.scheduler = scheduler
        self.applications = ApplicationService(service, service.browser_manager, client)
        if scheduler:
            service.sync_tracker = scheduler.sync_candidate

    def _handle_private(self, actor_id, message, callback):
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
                   '/apply': self.applications.prepare, '/inspect': self.applications.inspect,
                   '/upload': self.applications.propose_upload, '/submit': self.applications.propose_submit}
        if callback is None and command in {*actions, '/jobs', '/tracker', '/logout'}:
            self.service.authorize(actor_id)
            member = self.service._member(actor_id)
            if command in actions:
                if len(parts) != 2:
                    raise ValueError(f'Use {command} <job_id> with a job ID from your digest or /jobs.')
                actions[command](actor_id, parts[1])
            elif command == '/jobs':
                root = self.service.profile_dir(member)
                database = root / 'jobs.db'
                if not database.is_file():
                    self._send(actor_id, 'No jobs collected yet. Use /status to check your next scheduled run.')
                    return
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db, db:
                    rows = db.execute('SELECT id,title,company,status FROM jobs ORDER BY rowid DESC LIMIT 20').fetchall()
                self._send(actor_id, '\n\n'.join(f'{title} — {company}\n{status}\n/details {job_id}'
                           for job_id, title, company, status in rows) or 'No jobs collected yet.')
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
                self._send(actor_id, 'Your browser session is closed. Its private login profile is retained for your next connection.')
            return
        return super()._handle_private(actor_id, message, callback)
