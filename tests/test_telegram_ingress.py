"""Authenticated shared-poller forwarding; no real Telegram or model calls."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import subprocess
import sys
import threading

from aiohttp.test_utils import TestClient, TestServer
import pytest

from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.service import JobHunterService
from jobhunter_service.telegram import TelegramAPIError
from jobhunter_service.telegram_ingress import IngressConflict, MAX_UPDATE_BYTES, TelegramIngress
from jobhunter_service.web import create_app
from test_service_applications import app


TOKEN = 'synthetic-local-admin-token-for-ingress-tests'


def message(update_id=1, actor=11, text='My job preferences', **fields):
    return {'update_id': update_id, 'message': {
        'message_id': update_id + 1, 'from': {'id': actor, 'is_bot': False},
        'chat': {'id': actor, 'type': 'private'}, 'text': text, **fields}}


def callback(update_id=2, actor=11, data='jh:confirm:synthetic_confirmation_id'):
    return {'update_id': update_id, 'callback_query': {
        'id': 'synthetic-callback-' + str(update_id), 'from': {'id': actor, 'is_bot': False}, 'data': data,
        'message': {'message_id': 99, 'from': {'id': 999, 'is_bot': True}, 'chat': {'id': actor, 'type': 'private'}}}}


class Telegram:
    def __init__(self):
        self.sends, self.downloads, self.answers = [], [], []
        self.outage = False

    def send_message(self, actor, text, reply_markup=None):
        if self.outage:
            raise TelegramAPIError('Synthetic temporary Telegram outage')
        self.sends.append((actor, text, reply_markup))
        return {'message_id': len(self.sends)}

    def answer_callback(self, callback_id, text):
        self.answers.append((callback_id, text))

    def download_document(self, document):
        self.downloads.append(deepcopy(document))
        return b'Synthetic Candidate\n2020-2024: Frontend developer building React applications.'


class Planner:
    def __init__(self):
        self.calls = []

    def plan(self, text, snapshot):
        self.calls.append((text, snapshot['profile_id']))
        return {'operation': 'reply', 'reply': 'Scoped JobHunter response'}


@pytest.fixture
def system(tmp_path):
    client, planner = Telegram(), Planner()
    service = JobHunterService(tmp_path, 1, telegram_client=client)
    for actor in (11, 22):
        service.admin(1, 'add', actor)
        service.authorize(actor)
    clock = [100.0]
    handler = ApplicationTelegramHandler(service, client, planner)
    ingress = TelegramIngress(service, handler, clock=lambda: clock[0], lease_seconds=5)
    return service, handler, ingress, client, planner, clock


def receipt(service, update_id=1):
    with service.store.connect() as db:
        row = db.execute('SELECT * FROM telegram_ingress WHERE update_id=?', (update_id,)).fetchone()
        return dict(row) if row else None


def test_http_auth_precedes_parsing_and_enqueue_never_calls_handler_inline(system):
    service, _, ingress, client, planner, _ = system
    async def scenario():
        async with TestClient(TestServer(create_app(service, admin_token=TOKEN, telegram_ingress=ingress))) as http:
            for headers in ({}, {'Authorization': 'Bearer wrong'}):
                response = await http.post('/internal/telegram', data='{not json', headers=headers)
                assert response.status == 403
            assert receipt(service) is None
            response = await http.post('/internal/telegram', json=message(), headers={'Authorization': 'Bearer ' + TOKEN})
            assert response.status == 202
            assert await response.json() == {'accepted': True, 'update_id': 1, 'duplicate': False, 'status': 'pending'}
            assert response.headers['Cache-Control'] == 'no-store'
            assert client.sends == planner.calls == []
            duplicate = await http.post('/internal/telegram', json=message(), headers={'Authorization': 'Bearer ' + TOKEN})
            assert duplicate.status == 202 and (await duplicate.json())['duplicate'] is True
            conflict = await http.post('/internal/telegram', json=message(text='Different payload'), headers={'Authorization': 'Bearer ' + TOKEN})
            assert conflict.status == 409
            malformed = await http.post('/internal/telegram', json={'actor_id': 1}, headers={'Authorization': 'Bearer ' + TOKEN})
            assert malformed.status == 400
            forged = await http.post('/internal/telegram', json=message(chat={'id': 22, 'type': 'private'}), headers={'Authorization': 'Bearer ' + TOKEN})
            assert forged.status == 403
            large = await http.post('/internal/telegram', json=message(extra='x' * MAX_UPDATE_BYTES), headers={'Authorization': 'Bearer ' + TOKEN})
            assert large.status == 413
    asyncio.run(scenario())
    assert ingress.drain_one()
    assert len(client.sends) == len(planner.calls) == 1


def test_unconfigured_internal_route_is_authenticated_and_unavailable(system):
    service, *_ = system
    async def scenario():
        async with TestClient(TestServer(create_app(service, admin_token=TOKEN))) as http:
            denied = await http.post('/internal/telegram', json=message())
            assert denied.status == 403
            disabled = await http.post('/internal/telegram', json=message(), headers={'Authorization': 'Bearer ' + TOKEN})
            assert disabled.status == 503
    asyncio.run(scenario())


def test_reordered_updates_use_individual_receipts_without_touching_poll_offset(system, monkeypatch):
    service, _, ingress, client, planner, _ = system
    service.acknowledge_update(999)
    monkeypatch.setattr(service, 'get_update_offset', lambda: pytest.fail('Shared ingress must not read the polling offset'))
    monkeypatch.setattr(service, 'acknowledge_update', lambda *args: pytest.fail('Shared ingress must not update the polling offset'))
    ingress.enqueue(message(20, text='First arrival'))
    assert ingress.drain_one()
    ingress.enqueue(message(10, text='Later arrival with lower update ID'))
    assert ingress.drain_one()
    assert [text for text, _ in planner.calls] == ['First arrival', 'Later arrival with lower update ID']
    assert len(client.sends) == 2
    assert ingress.enqueue(message(20, text='First arrival'))['status'] == 'done'
    assert not ingress.drain_one() and len(client.sends) == 2
    assert service.store.checkpoint('telegram_offset') == '1000'


def test_dedup_canonicalizes_key_order_but_rejects_payload_conflicts(system):
    service, _, ingress, *_ = system
    payload = message()
    ingress.enqueue(payload)
    reordered = dict(reversed(list(payload.items())))
    assert ingress.enqueue(reordered)['duplicate'] is True
    with pytest.raises(IngressConflict):
        ingress.enqueue(message(text='Changed text'))
    assert json.loads(receipt(service)['payload']) == payload


@pytest.mark.parametrize('mutate', [
    lambda u: u['message']['chat'].update(id=22),
    lambda u: u['message']['chat'].update(type='supergroup'),
    lambda u: u['message']['from'].update(id='11'),
    lambda u: u['message']['from'].update(id=True),
    lambda u: u['message']['from'].update(is_bot=True),
    lambda u: u['message']['from'].pop('is_bot'),
    lambda u: u.update(update_id=True),
    lambda u: u.update(update_id=2**63),
    lambda u: u.update(callback_query={}),
    lambda u: u['message'].pop('message_id'),
])
def test_malformed_or_forged_private_messages_never_persist(system, mutate):
    service, _, ingress, client, planner, _ = system
    payload = message()
    mutate(payload)
    with pytest.raises((ValueError, PermissionError)):
        ingress.enqueue(payload)
    assert receipt(service) is None and client.sends == planner.calls == []


@pytest.mark.parametrize('mutate', [
    lambda u: u['callback_query']['message']['chat'].update(id=22),
    lambda u: u['callback_query']['from'].update(id=22),
    lambda u: u['callback_query']['from'].update(is_bot=True),
    lambda u: u['callback_query']['message']['from'].update(is_bot=False),
    lambda u: u['callback_query'].update(data='owner:terminal:run'),
    lambda u: u['callback_query'].pop('message'),
])
def test_callback_identity_and_namespace_cannot_be_forged(system, mutate):
    service, _, ingress, client, planner, _ = system
    payload = callback()
    mutate(payload)
    with pytest.raises((ValueError, PermissionError)):
        ingress.enqueue(payload)
    assert receipt(service, 2) is None and client.sends == planner.calls == []


def test_resume_document_is_downloaded_only_by_worker_and_remains_unconfirmed(system):
    service, _, ingress, client, planner, _ = system
    document = {'file_id': 'synthetic_telegram_file', 'file_name': 'resume.txt', 'mime_type': 'text/plain'}
    ingress.enqueue(message(text='', document=document))
    assert client.downloads == []
    assert ingress.drain_one()
    assert client.downloads == [document]
    assert planner.calls[0][1] == 'u11'
    snapshot = service.snapshot(11)
    assert snapshot['settings']['resume'] == {}
    assert (service.root / 'u11' / 'state' / 'resume_source.json').exists()
    assert not (service.root / 'u22' / 'state' / 'resume_source.json').exists()
    assert receipt(service)['status'] == 'done'


def test_owner_admin_is_admitted_but_owner_general_messages_never_reach_model(system):
    service, _, ingress, client, planner, _ = system
    for payload in (message(actor=1, text='Change my Pi services'), message(actor=1, text='/jobs'),
                    callback(actor=1), message(actor=1, text='', document={'file_id': 'synthetic'})):
        with pytest.raises(PermissionError):
            ingress.enqueue(payload)
    assert planner.calls == client.sends == []
    ingress.enqueue(message(actor=1, text='/jobhunter add 33'))
    assert ingress.drain_one()
    assert service.store.member(33)['status'] == 'pending'
    assert planner.calls == [] and client.sends[0][0] == 1


@pytest.mark.parametrize('status', ['unknown', 'suspended', 'revoked'])
def test_nonmembers_and_revoked_users_stay_in_denial_path_without_model_or_admin(system, status):
    service, _, ingress, client, planner, _ = system
    actor = 33 if status == 'unknown' else 11
    if status != 'unknown':
        service.admin(1, 'suspend' if status == 'suspended' else 'revoke', actor)
    ingress.enqueue(message(actor=actor, text='/jobhunter add 44'))
    ingress.enqueue(message(2, actor=actor, text='Run a command on the Pi'))
    assert ingress.drain_one() and ingress.drain_one()
    assert service.store.member(44) is None
    assert planner.calls == [] and all(send[0] == actor for send in client.sends)
    assert all('do not have access' in send[1] for send in client.sends)


def test_telegram_outage_retries_durably_with_bounded_backoff_and_no_terminal_drop(system):
    service, handler, ingress, client, _, clock = system
    ingress.enqueue(message())
    client.outage = True
    assert ingress.drain_one()
    first = receipt(service)
    assert first['status'] == 'pending' and first['attempts'] == 1 and first['next_attempt'] == 102
    assert not ingress.drain_one()
    restarted = TelegramIngress(service, handler, clock=lambda: clock[0])
    for _ in range(12):
        clock[0] = receipt(service)['next_attempt']
        assert restarted.drain_one()
        row = receipt(service)
        assert row['status'] == 'pending' and 1 <= row['next_attempt'] - clock[0] <= 300
    client.outage = False
    clock[0] = receipt(service)['next_attempt']
    assert restarted.drain_one() and receipt(service)['status'] == 'done'
    assert len(client.sends) == 1


def test_failed_actor_update_blocks_only_their_later_arrivals(system):
    service, handler, ingress, _, planner, _ = system
    original = handler.handle_update
    def outage(update, **kwargs):
        if update['update_id'] == 1:
            raise RuntimeError('Synthetic model outage')
        return original(update, **kwargs)
    handler.handle_update = outage
    ingress.enqueue(message(1, 11))
    ingress.enqueue(message(2, 11))
    ingress.enqueue(message(3, 22))
    assert ingress.drain_one() and ingress.drain_one()
    assert not ingress.drain_one()
    assert receipt(service, 1)['status'] == receipt(service, 2)['status'] == 'pending'
    assert receipt(service, 3)['status'] == 'done'
    assert [actor for _, actor in planner.calls] == ['u22']


def test_real_assistant_model_outage_stays_pending_and_recovers_without_new_user_message(system):
    service, handler, ingress, client, _, clock = system
    calls = []
    def planner(*args):
        calls.append(args)
        if len(calls) == 1:
            raise RuntimeError('Synthetic provider outage')
        return {'operation': 'reply', 'reply': 'Recovered scoped response'}
    handler.assistant = RestrictedHermesAssistant(planner)
    ingress.enqueue(message())
    assert ingress.drain_one()
    assert receipt(service)['status'] == 'pending'
    assert client.sends == []
    clock[0] = receipt(service)['next_attempt']
    assert ingress.drain_one()
    assert receipt(service)['status'] == 'done'
    assert [text for _, text, _ in client.sends] == ['Recovered scoped response']


def test_invalid_model_response_is_a_visible_validation_error_and_not_retried(system):
    service, handler, ingress, client, _, _ = system
    handler.assistant = RestrictedHermesAssistant(lambda *args: {'operation': 'run_shell', 'command': 'forbidden'})
    ingress.enqueue(message())
    assert ingress.drain_one()
    assert receipt(service)['status'] == 'done'
    assert len(client.sends) == 1
    assert 'model' in client.sends[0][1].lower()
    assert not ingress.drain_one()


def test_confirmation_retry_after_saved_mutation_does_not_increment_revision_twice(system):
    service, _, ingress, client, _, clock = system
    proposal = service.propose(11, {'schedule': {'time': '10:30'}})
    ingress.enqueue(callback(data='jh:confirm:' + proposal['action_id']))
    client.outage = True
    assert ingress.drain_one()
    assert service.store.member(11)['revision'] == 1
    assert receipt(service, 2)['status'] == 'pending'
    client.outage = False
    clock[0] = receipt(service, 2)['next_attempt']
    assert ingress.drain_one()
    assert receipt(service, 2)['status'] == 'done'
    assert service.store.member(11)['revision'] == 1


def test_application_callback_retry_never_repeats_consumed_submit_approval(app):
    facade, service, _, _, _, client = app
    facade.prepare(11, 'same-job')
    facade.inspect(11, 'same-job')
    token = facade.propose_submit(11, 'same-job')['token']
    handler = ApplicationTelegramHandler(service, client)
    handler.applications = facade
    clock = [100.0]
    ingress = TelegramIngress(service, handler, clock=lambda: clock[0])
    ingress.enqueue(callback(data='jh:application:' + token))
    client.send_message.side_effect = TelegramAPIError('Synthetic lost delivery acknowledgement')
    assert ingress.drain_one()
    assert receipt(service, 2)['status'] == 'pending'
    client.send_message.side_effect = None
    clock[0] = receipt(service, 2)['next_attempt']
    assert ingress.drain_one()
    submits = [call for call in facade._run.call_args_list if call.args[1]['operation'] == 'submit']
    assert len(submits) == 1 and receipt(service, 2)['status'] == 'done'


def test_worker_restart_recovers_crashed_processing_lease(system):
    service, handler, ingress, client, _, clock = system
    ingress.enqueue(message())
    script = '''
import os, sys
from jobhunter_service.service import JobHunterService
from jobhunter_service.telegram_ingress import TelegramIngress
class Crash:
    def handle_update(self, update, **kwargs): os._exit(7)
service = JobHunterService(sys.argv[1], 1)
TelegramIngress(service, Crash(), clock=lambda: 100, lease_seconds=5).drain_one()
'''
    process = subprocess.run([sys.executable, '-c', script, str(service.root)], capture_output=True, timeout=10)
    assert process.returncode == 7
    assert receipt(service)['status'] == 'processing' and receipt(service)['attempts'] == 1
    restarted = TelegramIngress(service, handler, clock=lambda: clock[0])
    assert not restarted.drain_one()
    clock[0] = 106
    assert restarted.drain_one()
    assert receipt(service)['status'] == 'done' and receipt(service)['attempts'] == 2
    assert len(client.sends) == 1


def test_delivery_lock_serializes_other_threads_and_processes_even_after_lease_time(system):
    service, handler, ingress, client, _, clock = system
    entered, release = threading.Event(), threading.Event()
    original = handler.handle_update
    def block(update, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(update, **kwargs)
    handler.handle_update = block
    ingress.enqueue(message())
    second = TelegramIngress(service, handler, clock=lambda: clock[0])
    script = '''
import sys
from jobhunter_service.service import JobHunterService
from jobhunter_service.telegram_ingress import TelegramIngress
class Unexpected:
    def handle_update(self, update, **kwargs): raise AssertionError('Lock bypassed')
print(TelegramIngress(JobHunterService(sys.argv[1], 1), Unexpected(), clock=lambda: 999).drain_one())
'''
    with ThreadPoolExecutor(max_workers=1) as executor:
        work = executor.submit(ingress.drain_one)
        try:
            assert entered.wait(5)
            clock[0] = 999
            assert not second.drain_one()
            process = subprocess.run([sys.executable, '-c', script, str(service.root)], capture_output=True, text=True, timeout=10)
            assert process.returncode == 0 and process.stdout.strip() == 'False'
        finally:
            release.set()
        assert work.result(5)
    assert receipt(service)['attempts'] == 1 and len(client.sends) == 1


def test_direct_payload_bound_and_receipts_live_only_in_private_registry(system):
    service, _, ingress, *_ = system
    with pytest.raises(ValueError, match='payload size'):
        ingress.enqueue(message(extra='x' * MAX_UPDATE_BYTES))
    ingress.enqueue(message())
    assert service.store.path.stat().st_mode & 0o777 == 0o600
    assert not list((service.root / 'u11').glob('*ingress*'))
    assert not list((service.root / 'u22').glob('*ingress*'))
