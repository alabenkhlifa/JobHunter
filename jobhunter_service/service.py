"""Trusted account boundary shared by Telegram, web callbacks and the scheduler.

Actor IDs reach this class from authenticated transports, never model output.
The model may propose changes; only a stored explicit human confirmation applies them.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from .presentation import DEFAULT_PRESENTATION as PRESENTATION_DEFAULTS
from .scheduling import next_run, validate_schedule
from .state import Store, private_json


def merge(old, patch):
    result = copy.deepcopy(old)
    for key, value in patch.items():
        result[key] = merge(result[key], value) if isinstance(value, dict) and isinstance(result.get(key), dict) else copy.deepcopy(value)
    return result


def validate_presentation(value):
    """Normalize explicitly supplied presentation preferences; never templates."""
    if not isinstance(value, dict) or set(value) - set(PRESENTATION_DEFAULTS):
        raise ValueError('Telegram presentation only supports style, show_salary, show_match_reason and group_by_market.')
    result = {**PRESENTATION_DEFAULTS, **value}
    if not isinstance(result['style'], str) or result['style'] not in {'standard', 'compact'}:
        raise ValueError('Telegram presentation style must be standard or compact.')
    for field in ('show_salary', 'show_match_reason', 'group_by_market'):
        if type(result[field]) is not bool:
            raise ValueError(f'Telegram presentation {field} must be a boolean.')
    return result


def initial_settings(user_id):
    return {
        'search': {'matching': {'preset': 'generic'}, 'markets': [], 'keywords': []},
        'schedule': {'timezone': 'UTC', 'time': '09:00', 'weekdays': list(range(7)), 'enabled': False},
        'telegram': {'destinations': [{'chat_id': str(user_id), 'kind': 'private', 'label': 'Private chat'}]},
        'accounts': {'gmail': {'enabled': False, 'account': ''},
                     'tracker': {'enabled': False, 'account': '', 'viewer_email': ''}},
        'resume': {},
    }


class JobHunterService:
    def __init__(self, data_root, owner_id, *, public_url='', telegram_client=None, browser_manager=None, google_enabled=False):
        if type(owner_id) is not int or owner_id <= 0:
            raise ValueError('A positive owner Telegram user ID is required.')
        self.root = Path(data_root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.owner_id = owner_id
        self._mutation_locks = {}
        self._mutation_guard = threading.Lock()
        self.store = Store(self.root / 'service' / 'registry.sqlite3')
        self.public_url = public_url.rstrip('/')
        url = urlsplit(self.public_url)
        if self.public_url and (url.scheme != 'https' or not url.hostname or url.username or url.password or url.path or url.query or url.fragment):
            raise ValueError('Public service URL must use HTTPS.')
        self.telegram_client = telegram_client
        self.browser_manager = browser_manager
        self.google_enabled = bool(google_enabled)

    def connection_status(self, actor_id):
        from .connections import connection_status
        return connection_status(self, actor_id)

    def check_connection(self, actor_id, provider):
        from .connections import check_connection
        return check_connection(self, actor_id, provider)

    @contextmanager
    def mutation(self, actor_id):
        if type(actor_id) is not int or actor_id <= 0:
            raise PermissionError('Invalid candidate identity.')
        with self._mutation_guard:
            lock = self._mutation_locks.setdefault(actor_id, threading.RLock())
        with lock:
            yield

    def profile_dir(self, member):
        name = member['profile_id']
        if not re.fullmatch(r'u[1-9][0-9]{0,19}', name):
            raise PermissionError('Invalid registered profile.')
        path = (self.root / name).resolve()
        if path != self.root / name:
            raise PermissionError('Profile path escapes its data directory.')
        return path

    def _member(self, actor_id, *, active=True):
        if type(actor_id) is not int or actor_id <= 0:
            raise PermissionError('Invalid Telegram identity.')
        member = self.store.member(actor_id)
        if not member or (active and member['status'] != 'active'):
            raise PermissionError('JobHunter access requires an active owner invitation.')
        return member

    def authorize(self, actor_id):
        if actor_id == self.owner_id:
            return
        member = self._member(actor_id, active=False)
        if member['status'] == 'pending':
            with self.store.connect() as db:
                db.execute("UPDATE members SET status='active' WHERE user_id=? AND status='pending'", (actor_id,))
            member = self._member(actor_id)
            self.materialize(member)
        else:
            self._member(actor_id)

    def admin(self, actor_id, operation, target_id=None):
        if actor_id != self.owner_id:
            raise PermissionError('Only the owner can manage JobHunter registrations.')
        if operation == 'list':
            return self._admin(actor_id, operation, target_id)
        with self.mutation(target_id):
            return self._admin(actor_id, operation, target_id)

    def _admin(self, actor_id, operation, target_id=None):
        if actor_id != self.owner_id:
            raise PermissionError('Only the owner can manage JobHunter registrations.')
        if operation not in {'add', 'list', 'suspend', 'revoke'}:
            raise ValueError('Unknown registration operation.')
        if operation != 'list' and (type(target_id) is not int or not 0 < target_id < 2**63):
            raise ValueError('A positive numeric Telegram user ID is required.')
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if operation == 'list':
                return {'users': [dict(row) for row in db.execute('SELECT user_id,profile_id,status FROM members ORDER BY user_id')]}
            if operation == 'add':
                db.execute('INSERT INTO members VALUES(?,?,?,0,?,?) ON CONFLICT(user_id) DO UPDATE SET status=CASE '
                           "WHEN members.status IN ('suspended','revoked') THEN 'pending' ELSE members.status END",
                           (target_id, f'u{target_id}', 'pending', json.dumps(initial_settings(target_id)), time.time()))
            else:
                row = db.execute('SELECT settings FROM members WHERE user_id=?', (target_id,)).fetchone()
                if row is None:
                    raise ValueError('User is not registered.')
                settings = json.loads(row['settings'])
                settings['schedule']['enabled'] = False
                db.execute('UPDATE members SET status=?,revision=revision+1,settings=? WHERE user_id=?',
                           ('suspended' if operation == 'suspend' else 'revoked', json.dumps(settings), target_id))
                db.execute('UPDATE tokens SET consumed=1 WHERE user_id=?', (target_id,))
                db.execute('UPDATE actions SET consumed=1 WHERE user_id=? AND consumed=0', (target_id,))
                db.execute("UPDATE deliveries SET status='cancelled' WHERE user_id=? AND status='pending'", (target_id,))
            db.execute('INSERT INTO audit(actor,operation,target,created_at) VALUES(?,?,?,?)',
                       (actor_id, operation, target_id, time.time()))
        if operation in {'suspend', 'revoke'} and self.browser_manager:
            self.browser_manager.stop(f'u{target_id}')
        return {'user_id': target_id, 'status': self.store.member(target_id)['status']}

    def snapshot(self, actor_id):
        member = self._member(actor_id)
        settings = json.loads(member['settings'])
        result = {'profile_id': member['profile_id'], 'revision': member['revision'], 'settings': settings,
                  'next_run': None, 'onboarding': self.onboarding_status(actor_id),
                  'connections': self.connection_status(actor_id),
                  'recommended_accounts': 'Use a dedicated jobs Gmail and share its tracker and documents with your personal account.'}
        upcoming = next_run(settings['schedule'])
        if upcoming:
            result['next_run'] = upcoming.isoformat()
        source = self.profile_dir(member) / 'state' / 'resume_source.json'
        if source.exists():
            result['resume_source'] = json.loads(source.read_text())
        history = self.store.checkpoint(f'history:{actor_id}')
        result['history'] = json.loads(history) if history else []
        return result

    def record_turn(self, actor_id, user_text, assistant_text):
        self._member(actor_id)
        from .hermes import contains_credentials
        if contains_credentials(user_text) or contains_credentials(assistant_text):
            return
        key = f'history:{actor_id}'
        history = json.loads(self.store.checkpoint(key) or '[]')
        history.extend([{'role': 'user', 'content': str(user_text)[:6000]},
                        {'role': 'assistant', 'content': str(assistant_text)[:6000]}])
        self.store.checkpoint(key, json.dumps(history[-20:]))

    @staticmethod
    def readiness(settings):
        missing = []
        if not settings.get('resume', {}).get('name'):
            missing.append('confirmed resume')
        if not settings['search'].get('keywords'):
            missing.append('search keywords')
        if not settings['search'].get('markets'):
            missing.append('destinations and work authorization')
        return {'ready': not missing, 'missing': missing}

    def _onboarding_source_digest(self, actor_id):
        from .onboarding import digest
        path = self.profile_dir(self._member(actor_id)) / 'state' / 'resume_source.json'
        if path.is_symlink():
            raise PermissionError('The resume source cannot follow a symlink.')
        if not path.exists():
            return ''
        if path.stat().st_size > 500000:
            raise ValueError('The saved resume source is too large.')
        return digest(json.loads(path.read_text()))

    def onboarding_status(self, actor_id, *, start=False):
        from . import onboarding
        with self.mutation(actor_id):
            member = self._member(actor_id)
            with self.store.connect() as db:
                if start:
                    db.execute('BEGIN IMMEDIATE')
                state = onboarding.load(db, actor_id)
                if start and state is None:
                    state = onboarding.fresh()
                    onboarding.save(db, actor_id, state)
            settings = json.loads(member['settings'])
            result = onboarding.status(settings, state, member['revision'],
                self._onboarding_source_digest(actor_id) if state is not None else '', self.connection_status(actor_id))
            if state is None:
                # Legacy callers retain their existing readiness contract until
                # the candidate deliberately starts the guided workflow.
                result.update(self.readiness(settings))
            return result

    def onboarding_action(self, actor_id, action, step, revision):
        from . import onboarding
        if (not isinstance(action, str) or not isinstance(step, str)
                or action not in {'acknowledge', 'skip', 'reopen', 'check', 'activate'}
                or step not in onboarding.STEPS or type(revision) is not int):
            raise ValueError('Invalid onboarding action.')
        with self.mutation(actor_id):
            current = self.onboarding_status(actor_id)
            if not current['started'] or revision != current['revision']:
                raise ValueError('This onboarding button is stale. Open /onboarding for current progress.')
            entry = next(item for item in current['steps'] if item['id'] == step)
            if action not in entry['actions']:
                raise ValueError('Finish the required confirmed settings before continuing this step.')
            if action == 'check':
                self.check_connection(actor_id, step)
                return self.onboarding_status(actor_id)
            if action == 'activate':
                return self._propose(actor_id, {'schedule': {'enabled': True}}, onboarding_action='activate')
            member = self._member(actor_id)
            settings = json.loads(member['settings'])
            if action == 'skip' and step in {'gmail', 'tracker'} and settings['accounts'][step]['enabled']:
                return self._propose(actor_id, {'accounts': {step: {'enabled': False}}}, onboarding_action='skip:' + step)
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                actual = db.execute("SELECT revision FROM members WHERE user_id=? AND status='active'", (actor_id,)).fetchone()
                state = onboarding.load(db, actor_id)
                if not actual or onboarding.revision(actual['revision'], state) != revision:
                    raise ValueError('This onboarding button is stale. Open /onboarding again.')
                values = onboarding.subjects(settings, self._onboarding_source_digest(actor_id))
                if action == 'reopen':
                    state['acknowledgements'].pop(step, None)
                    state['acknowledgements'].pop('review', None)
                    state['activated'] = None
                else:
                    onboarding.acknowledge(state, values, step, action)
                onboarding.save(db, actor_id, state)
            return self.onboarding_status(actor_id)

    def _validate(self, actor_id, settings):
        from jobhunter_matching import validate_config
        from resume_refiner import validate_profile

        if set(settings) != {'search', 'schedule', 'telegram', 'accounts', 'resume'}:
            raise ValueError('Only JobHunter profile settings can be changed.')
        search = settings['search']
        allowed = {'matching', 'markets', 'keywords', 'delivery', 'score_threshold', 'max_job_age_days', 'max_pages', 'min_matching_jobs'}
        if not isinstance(search, dict) or set(search) - allowed:
            raise ValueError('Unsupported search setting; paths and executable commands are not configurable.')
        if search.get('matching', {}).get('preset', 'generic') != 'generic':
            raise ValueError('Service profiles use explicit generic matching settings.')
        validate_config(search)
        if 'max_pages' in search and (type(search['max_pages']) is not int or not 1 <= search['max_pages'] <= 3):
            raise ValueError('Service collection allows one to three pages per query.')
        if len(search.get('keywords', [])) > 15 or len(search.get('markets', [])) > 10:
            raise ValueError('Use at most 15 keywords and 10 search destinations.')
        settings['schedule'] = validate_schedule(settings['schedule'])
        if settings['resume']:
            validate_profile(settings['resume'])
        if settings['schedule']['enabled'] and not self.readiness(settings)['ready']:
            raise ValueError('Confirm your resume, keywords and destinations before enabling the schedule.')
        telegram = settings['telegram']
        if (not isinstance(telegram, dict) or 'destinations' not in telegram
                or set(telegram) - {'destinations', 'presentation'}):
            raise ValueError('Invalid Telegram settings.')
        if 'presentation' in telegram:
            telegram['presentation'] = validate_presentation(telegram['presentation'])
        destinations = telegram['destinations']
        if not isinstance(destinations, list) or not 1 <= len(destinations) <= 5:
            raise ValueError('Configure one to five Telegram destinations.')
        seen = set()
        for dest in destinations:
            if not isinstance(dest, dict) or set(dest) - {'chat_id', 'kind', 'label'}:
                raise ValueError('Invalid delivery destination.')
            chat = str(dest.get('chat_id', ''))
            if not re.fullmatch(r'-?[1-9][0-9]{0,19}', chat) or chat in seen:
                raise ValueError('Destinations require unique numeric Telegram chat IDs.')
            seen.add(chat)
            dest['chat_id'] = chat
            if dest.get('kind') == 'private':
                if chat != str(actor_id):
                    raise PermissionError('Personal delivery must use your own private chat.')
            elif dest.get('kind') == 'channel':
                if not self.telegram_client:
                    raise ValueError('Channel validation is unavailable.')
                self.telegram_client.validate_destination(actor_id, dest)
            else:
                raise ValueError('Delivery kind must be private or channel.')
        accounts = settings['accounts']
        if not isinstance(accounts, dict) or set(accounts) != {'gmail', 'tracker'}:
            raise ValueError('Invalid account settings.')
        for kind, account in accounts.items():
            allowed_keys = {'enabled', 'account'} | ({'viewer_email', 'spreadsheet_id'} if kind == 'tracker' else set())
            if not isinstance(account, dict) or set(account) - allowed_keys or type(account.get('enabled')) is not bool:
                raise ValueError('Invalid account settings.')
            for key in ('account', 'viewer_email'):
                value = account.get(key, '')
                if not isinstance(value, str) or (value and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value)):
                    raise ValueError('Enter a valid account email.')
                if key in account:
                    account[key] = value.strip().casefold()
            if account.get('enabled') and not account.get('account'):
                raise ValueError('Configure the account before enabling its integration.')
            sid = account.get('spreadsheet_id', '')
            if sid and (not isinstance(sid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,200}', sid)):
                raise ValueError('Invalid spreadsheet ID.')
        return settings

    def propose(self, actor_id, patch):
        with self.mutation(actor_id):
            return self._propose(actor_id, patch)

    def _propose(self, actor_id, patch, *, onboarding_action=None):
        from . import onboarding
        member = self._member(actor_id)
        if not isinstance(patch, dict) or not patch:
            raise ValueError('A settings change is required.')
        patch = copy.deepcopy(patch)
        current = json.loads(member['settings'])
        with self.store.connect() as db:
            state = onboarding.load(db, actor_id)
        source = self._onboarding_source_digest(actor_id) if state is not None else ''
        metadata = {}
        if state is not None:
            progress = onboarding.status(current, state, member['revision'], source, self.connection_status(actor_id))
            if onboarding_action == 'activate':
                if not progress['ready_for_activation']:
                    raise ValueError('Complete the guided review before requesting activation.')
                metadata = {'onboarding_action': 'activate', 'onboarding_generation': state['generation'],
                            'onboarding_binding': onboarding.binding(onboarding.subjects(current, source), state)}
            elif onboarding_action and onboarding_action.startswith('skip:'):
                metadata = {'onboarding_action': onboarding_action, 'onboarding_generation': state['generation']}
            elif not progress['complete'] and not current['schedule']['enabled']:
                # Canonical preview shows this pause; an LLM cannot activate a
                # fresh guided profile by including enabled=true in a patch.
                if merge(current, patch)['schedule'].get('enabled'):
                    patch.setdefault('schedule', {})['enabled'] = False
        updated = self._validate(actor_id, merge(current, patch))
        if state is not None and current['schedule']['enabled'] and updated['schedule']['enabled']:
            before, after = onboarding.subjects(current, source), onboarding.subjects(updated, source)
            if before != after:
                raise ValueError('Your search is running. First request and confirm a pause before changing guided setup settings.')
        def canonical_patch(supplied, applied):
            return {field: canonical_patch(value, applied[field]) if isinstance(value, dict) else copy.deepcopy(applied[field])
                    for field, value in supplied.items()}
        patch = canonical_patch(patch, updated)
        encoded = json.dumps(patch, ensure_ascii=False, indent=2)
        if len(encoded) > 20000:
            raise ValueError('Confirm smaller sections of this change separately.')
        action_id = secrets.token_urlsafe(18)
        with self.store.connect() as db:
            db.execute('INSERT INTO actions VALUES(?,?,?,?,?,0)',
                       (action_id, actor_id, member['revision'], json.dumps({'settings': updated, 'patch': patch, **metadata}), time.time() + 1800))
        preview = 'Review these exact changes before confirming:\n' + encoded
        if onboarding_action == 'activate':
            preview = progress['summary'] + '\n\n' + preview
        response = {'action_id': action_id, 'patch': patch}
        if 'schedule' in patch:
            upcoming = next_run(updated['schedule'])
            preview += '\nNext run: ' + (upcoming.isoformat() if upcoming else 'paused')
            response['next_run'] = upcoming.isoformat() if upcoming else None
        if onboarding_action == 'activate':
            response['summary'] = progress['summary']
        response['preview'] = preview
        return response

    def confirm(self, actor_id, action_id):
        with self.mutation(actor_id):
            return self._confirm(actor_id, action_id)

    def _confirm(self, actor_id, action_id):
        self._member(actor_id)
        newly_applied, resume_changed = False, False
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            member_row = db.execute("SELECT * FROM members WHERE user_id=? AND status='active'", (actor_id,)).fetchone()
            row = db.execute('SELECT * FROM actions WHERE id=? AND user_id=?', (action_id, actor_id)).fetchone()
            if not member_row or not row:
                raise ValueError('This confirmation is unavailable. Request a fresh preview.')
            # Applied actions remain receipts. A retry after a lost Telegram
            # acknowledgement repairs files from the latest registry revision.
            if row['consumed'] != 2:
                if row['consumed'] or row['expires_at'] <= time.time() or row['revision'] != member_row['revision']:
                    raise ValueError('This confirmation expired or the profile changed. Request a fresh preview.')
                action = json.loads(row['payload'])
                settings = action['settings']
                proposed_resume = action['patch'].get('resume', {})
                for field in ('evidence_bank', 'resume_variants'):
                    proposed_ids = {item.get('id') for item in proposed_resume.get(field, [])}
                    for item in settings.get('resume', {}).get(field, []):
                        if item.get('id') in proposed_ids:
                            item['confirmation'] = 'candidate-confirmed'
                settings = self._validate(actor_id, settings)
                from . import onboarding
                state = onboarding.load(db, actor_id)
                if state is not None:
                    current_settings = json.loads(member_row['settings'])
                    source = self._onboarding_source_digest(actor_id)
                    before = onboarding.subjects(current_settings, source)
                    after = onboarding.subjects(settings, source)
                    progress = onboarding.status(current_settings, state, member_row['revision'], source,
                                                 self.connection_status(actor_id))
                    guided_action = action.get('onboarding_action')
                    if guided_action:
                        if action.get('onboarding_generation') != state['generation']:
                            raise ValueError('Onboarding progress changed. Request a fresh preview.')
                        if guided_action == 'activate' and (not progress['ready_for_activation']
                                or action.get('onboarding_binding') != onboarding.binding(before, state)):
                            raise ValueError('Review the current guided setup before activating it.')
                    if (settings['schedule']['enabled'] and not current_settings['schedule']['enabled']
                            and not progress['complete'] and guided_action != 'activate'):
                        raise ValueError('Use the guided activation preview after completing your review.')
                    if current_settings['schedule']['enabled'] and settings['schedule']['enabled'] and before != after:
                        raise ValueError('Pause the running search before changing guided setup settings.')
                    updated_state = onboarding.reconcile(state, before, after)
                    if guided_action == 'activate':
                        updated_state['activated'] = onboarding.binding(after, updated_state)
                    elif guided_action and guided_action.startswith('skip:'):
                        onboarding.acknowledge(updated_state, after, guided_action.split(':', 1)[1], 'skip')
                    if updated_state != state:
                        onboarding.save(db, actor_id, updated_state)
                db.execute('UPDATE members SET settings=?,revision=revision+1 WHERE user_id=?', (json.dumps(settings), actor_id))
                db.execute('UPDATE actions SET consumed=2 WHERE id=?', (action_id,))
                db.execute('INSERT INTO audit(actor,operation,target,created_at) VALUES(?,?,?,?)',
                           (actor_id, 'confirm_settings', actor_id, time.time()))
                newly_applied = True
                resume_changed = settings['resume'] != json.loads(member_row['settings'])['resume']
        self.materialize(self._member(actor_id))
        return {**self.snapshot(actor_id), 'confirmation': {
            'newly_applied': newly_applied, 'resume_changed': resume_changed}}

    def materialize(self, member):
        with self.mutation(member['user_id']):
            # Callers may have captured a revision before another thread saved
            # a new profile. Only the active canonical record reaches disk.
            self._materialize(self._member(member['user_id']))

    def _materialize(self, member):
        root = self.profile_dir(member)
        settings = json.loads(member['settings'])
        for name in ('state', 'secrets', 'output'):
            (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        private_json(root / 'config.json', settings['search'])
        if settings['resume']:
            from resume_refiner import atomic_update_profile
            target = root / 'master-profile.json'
            if target.exists():
                if json.loads(target.read_text()) != settings['resume']:
                    atomic_update_profile(target, settings['resume'], candidate_confirmed=True)
            else:
                private_json(target, settings['resume'])
        from .account_state import reconcile_account_settings
        previous_file = root / 'settings.json'
        previous = json.loads(previous_file.read_text()) if previous_file.is_file() else None
        reconcile_account_settings(root, previous, settings)
        private_json(previous_file, settings)

    def stage_resume(self, actor_id, resume):
        with self.mutation(actor_id):
            return self._stage_resume(actor_id, resume)

    def _stage_resume(self, actor_id, resume):
        member = self._member(actor_id)
        if not isinstance(resume, dict) or not isinstance(resume.get('text'), str):
            raise ValueError('A safely extracted resume is required.')
        if not resume['text'].strip() or len(resume['text']) > 100000:
            raise ValueError('Resume text is empty or too large.')
        source = {key: resume[key] for key in ('text', 'filename', 'sha256') if key in resume}
        source['untrusted'] = True
        private_json(self.profile_dir(member) / 'state' / 'resume_source.json', source)
        from . import onboarding
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = onboarding.load(db, actor_id)
            if state is not None:
                state['acknowledgements'].pop('resume', None)
                state['acknowledgements'].pop('review', None)
                state['activated'] = None
                onboarding.save(db, actor_id, state)
        return {'status': 'resume_received', 'message': 'Resume imported as source material. Confirm the proposed facts before they are saved to your profile.'}

    def connect(self, actor_id, provider, purpose=''):
        self._member(actor_id)
        if not self.public_url:
            raise ValueError('Secure account connection links are not configured by the operator yet.')
        if provider == 'google' and purpose in {'gmail', 'tracker'}:
            if not self.google_enabled:
                raise ValueError('Google connection is not available yet. You can skip Gmail and tracker while the owner completes setup.')
            member = self._member(actor_id)
            account = json.loads(member['settings'])['accounts'][purpose]['account']
            if not account:
                raise ValueError('Configure and confirm the account email first.')
            token = self.store.token(actor_id, 'google_connect', {'kind': purpose, 'account': account, 'revision': member['revision']})
            return f'{self.public_url}/connect/google?t={quote(token)}'
        if provider == 'linkedin' and purpose in {'', 'login'}:
            if not self.browser_manager:
                raise ValueError('Isolated browser access is not configured by the operator yet.')
            token = self.store.token(actor_id, 'browser_connect')
            return f'{self.public_url}/connect/browser?t={quote(token)}'
        raise ValueError('Unsupported account connection.')

    def get_update_offset(self):
        return int(self.store.checkpoint('telegram_offset') or 0)

    def acknowledge_update(self, update_id):
        current = self.get_update_offset()
        self.store.checkpoint('telegram_offset', max(current, update_id + 1))
