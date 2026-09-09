"""Service worker integration and tool-free Hermes subprocess contracts."""
import asyncio
from contextlib import closing
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.egress import public_address, resolve_public
from jobhunter_service.hermes_runner import HermesPlanner
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.service import JobHunterService
from jobhunter_service.state import private_json
from test_service_core import Telegram, activate, create_run, job_state, ready


def test_real_hermes_child_has_no_ambient_owner_tools_or_files(tmp_path, monkeypatch):
    source = tmp_path / 'sdk'
    source.mkdir()
    (source / 'toolsets.py').write_text("TOOLSETS = {'terminal': {}, 'files': {}, 'cron': {}}")
    (source / 'run_agent.py').write_text('''
import os
from pathlib import Path
class AIAgent:
    def __init__(self, **kw):
        assert kw['enabled_toolsets'] == []
        assert set(kw['disabled_toolsets']) == {'terminal', 'files', 'cron'}
        assert kw['skip_memory'] and kw['skip_context_files'] and kw['skip_background_review']
        assert kw['session_db'] is None and not kw['load_soul_identity']
        assert not kw['save_trajectories'] and not kw['checkpoints_enabled']
        assert kw['api_key'] == 'synthetic-dedicated-key'
        assert os.environ.get('OWNER_SECRET') is None
        assert os.environ.get('TELEGRAM_BOT_TOKEN') is None
        assert Path(os.environ['HERMES_HOME']).resolve() == Path.cwd()
        assert not list(Path.cwd().iterdir())
        self.tools = []
    def run_conversation(self, user_message, system_message):
        return {'final_response': '{"operation":"reply","reply":"Scoped response"}'}
    def close(self): pass
''')
    monkeypatch.setenv('OWNER_SECRET', 'synthetic-owner-secret')
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'synthetic-owner-token')
    planner = HermesPlanner(sys.executable, source, model='test', provider='test', api_key='synthetic-dedicated-key')
    assert planner([{'role': 'user', 'content': 'Configure my jobs'}], {})['reply'] == 'Scoped response'
    (source / 'run_agent.py').write_text("class AIAgent:\n def __init__(self, **kw): self.tools = ['terminal']\n")
    with pytest.raises(RuntimeError, match='scoped response'):
        planner([], {})


@pytest.mark.parametrize('address', ['127.0.0.1', '10.0.0.1', '172.30.77.1', '192.168.1.1',
    '100.104.67.94', '169.254.169.254', '::1', 'fe80::1', 'fc00::1', '::ffff:127.0.0.1', '224.0.0.1'])
def test_browser_egress_denies_private_host_and_vpn_addresses(address):
    assert not public_address(address)


def test_dns_mixed_public_private_answers_are_rejected(monkeypatch):
    async def scenario():
        async def answers(*args, **kwargs):
            return [(2, 1, 6, '', ('1.1.1.1', 443)), (2, 1, 6, '', ('127.0.0.1', 443))]
        monkeypatch.setattr(asyncio.get_running_loop(), 'getaddrinfo', answers)
        with pytest.raises(ValueError):
            await resolve_public('example.test', 443)
        with pytest.raises(ValueError):
            await resolve_public('example.test', 22)
    asyncio.run(scenario())


def test_digest_parts_wait_for_complete_destination_before_notified(tmp_path, monkeypatch):
    client = Telegram()
    service = JobHunterService(tmp_path, 900, telegram_client=client)
    ready(service)
    root = create_run(service)
    scheduler = Scheduler(service, None, client)
    monkeypatch.setattr(scheduler.delivery, '_preflight', lambda *args: 'open')
    scheduler.delivery.enqueue(123, 'run1', 'x' * 6500, ['same-id'])
    real_send = client.send_message
    def only_first(chat, message):
        if client.sends:
            raise RuntimeError('Temporary outage')
        return real_send(chat, message)
    client.send_message = only_first
    assert scheduler.delivery.drain()['sent'] == 1
    assert job_state(root) == (0, 'delivery_pending')
    client.send_message = real_send
    assert scheduler.delivery.drain()['sent'] == 2
    assert job_state(root) == (1, 'new')
    assert len(client.sends) == 3


def test_scheduler_collection_review_and_acknowledged_delivery(tmp_path):
    client = Telegram()
    service = JobHunterService(tmp_path, 900, telegram_client=client)
    ready(service)
    root = create_run(service)
    with closing(sqlite3.connect(root / 'jobs.db')) as db, db:
        for field in ('title', 'company', 'location', 'url'):
            db.execute(f'ALTER TABLE jobs ADD COLUMN {field} TEXT')
        db.execute("UPDATE jobs SET title='Designer',company='Example',location='France',url='https://example.test/job'")
    with service.store.connect() as db:
        db.execute("UPDATE runs SET status='queued' WHERE id='run1'")
    planner = Mock(return_value={'verdicts': []})
    scheduler = Scheduler(service, planner, client)
    def child(*args):
        output = Path(args[args.index('--output') + 1])
        if args[0] == 'collect':
            private_json(output, {'candidates': [], 'omitted_candidates': [{'job_id': 'same-id'}]})
        else:
            raise AssertionError('No review for omitted candidates')
    scheduler._child = child
    assert scheduler.run_next()
    assert 'remain queued' in client.sends[-1][1]
    assert not planner.called
    assert not scheduler.run_next()


def test_private_dispatch_routes_only_authenticated_candidate_and_callback(tmp_path):
    client = Telegram()
    service = JobHunterService(tmp_path, 900, telegram_client=client)
    activate(service, 123)
    activate(service, 456)
    client.answer_callback = Mock()
    handler = ApplicationTelegramHandler(service, client)
    handler.applications = Mock()
    def update(uid, text, number, kind='private'):
        return {'update_id': number, 'message': {'from': {'id': uid}, 'chat': {'id': uid, 'type': kind}, 'text': text}}
    handler.handle_update(update(123, '/apply own-job', 1))
    handler.applications.prepare.assert_called_once_with(123, 'own-job')
    handler.handle_update(update(456, '/apply own-job', 2, 'group'))
    assert handler.applications.prepare.call_count == 1
    handler.handle_update({'update_id': 3, 'callback_query': {'id': 'cb', 'from': {'id': 123},
        'message': {'chat': {'id': 123, 'type': 'private'}}, 'data': 'jh:application:' + 'x' * 43}})
    handler.applications.execute.assert_called_once_with(123, 'x' * 43)
