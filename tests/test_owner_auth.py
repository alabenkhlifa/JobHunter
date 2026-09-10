"""Synthetic owner grants, real local sockets, and disposable planner children."""
import json
import logging
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobhunter_service.hermes_runner import SharedOwnerPlanner
from jobhunter_service.owner_auth import (
    FAILURE, MAX_REQUEST, HermesOwnerRuntime, OwnerAuthBroker, OwnerAuthClient,
    OwnerAuthError, peer_uid, validate_runtime,
)


def grant(**changes):
    return {'model': 'synthetic-model', 'provider': 'openai-codex', 'api_mode': 'codex_responses',
            'base_url': 'https://chatgpt.com/backend-api/codex', 'api_key': 'synthetic-access', **changes}


def resolve(broker, uid=321, **extra):
    return broker.dispatch(uid, {'operation': 'resolve', **extra})


def test_owner_resolution_is_current_and_never_passes_auth_overrides():
    runtime = HermesOwnerRuntime.__new__(HermesOwnerRuntime)
    runtime.load_config = Mock(side_effect=[
        {'model': {'provider': 'openai-codex', 'default': 'first'}},
        {'model': {'provider': 'nous', 'default': 'second'}},
    ])
    runtime.resolve_runtime_provider = Mock(return_value=grant())
    assert runtime()['model'] == 'first'
    assert runtime()['model'] == 'second'
    assert runtime.resolve_runtime_provider.call_args_list[0].kwargs == {
        'requested': 'openai-codex', 'target_model': 'first'}
    assert runtime.resolve_runtime_provider.call_args_list[1].kwargs == {
        'requested': 'nous', 'target_model': 'second'}


@pytest.mark.parametrize('model', [None, 'invalid', {'default': 'x', 'provider': 'copilot-acp'},
    {'default': 'x', 'provider': 'openai-codex', 'openai_runtime': 'codex_app_server'}])
def test_owner_runtime_rejects_unsupported_configuration(model):
    runtime = HermesOwnerRuntime.__new__(HermesOwnerRuntime)
    runtime.load_config = Mock(return_value={'model': model})
    runtime.resolve_runtime_provider = Mock()
    with pytest.raises(OwnerAuthError):
        runtime()
    runtime.resolve_runtime_provider.assert_not_called()


def test_grant_exports_only_effective_inference_fields_and_no_pool():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'synthetic-credential-id'
    raw = grant(credential_pool=pool, refresh_token='synthetic-refresh',
                history='synthetic-owner-history', auth_path='/synthetic/owner/auth.json')
    broker = OwnerAuthBroker(lambda: raw, [321])
    response = resolve(broker)
    assert response['runtime'] == grant()
    assert len(response['refresh_ticket']) == 43
    assert 'synthetic-refresh' not in json.dumps(response)
    assert 'synthetic-access' not in repr(broker.tickets)
    assert pool not in [field for record in broker.tickets.values() for field in record.values()]


def test_reactive_refresh_uses_fresh_pool_and_binds_exact_issuing_credential():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'credential-one'
    resolver = Mock(side_effect=[grant(credential_pool=pool), grant(credential_pool=pool),
                                 grant(credential_pool=pool, api_key='synthetic-rotated')])
    broker = OwnerAuthBroker(resolver, [321])
    ticket = resolve(broker)['refresh_ticket']
    result = resolve(broker, refresh_ticket=ticket)
    assert result['runtime']['api_key'] == 'synthetic-rotated'
    pool.try_refresh_matching.assert_called_once_with(
        credential_id='credential-one', api_key_hint='synthetic-access')
    with pytest.raises(OwnerAuthError):
        resolve(broker, refresh_ticket=ticket)


def test_proactive_rotation_does_not_refresh_single_use_token_again():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'credential-one'
    resolver = Mock(side_effect=[grant(credential_pool=pool),
                                 grant(credential_pool=pool, api_key='synthetic-rotated-elsewhere')])
    broker = OwnerAuthBroker(resolver, [321])
    ticket = resolve(broker)['refresh_ticket']
    assert resolve(broker, refresh_ticket=ticket)['runtime']['api_key'] == 'synthetic-rotated-elsewhere'
    pool.try_refresh_matching.assert_not_called()


def test_changed_provider_does_not_refresh_previous_account():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'credential-one'
    resolver = Mock(side_effect=[grant(credential_pool=pool), grant(provider='nous',
        api_mode='chat_completions', base_url='https://models.example.test/v1', credential_pool=pool)])
    broker = OwnerAuthBroker(resolver, [321])
    ticket = resolve(broker)['refresh_ticket']
    assert resolve(broker, refresh_ticket=ticket)['runtime']['provider'] == 'nous'
    pool.try_refresh_matching.assert_not_called()


def test_refresh_failure_is_generic_and_ticket_is_consumed():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'credential-one'
    pool.try_refresh_matching.side_effect = RuntimeError('synthetic-refresh-secret')
    broker = OwnerAuthBroker(lambda: grant(credential_pool=pool), [321])
    ticket = resolve(broker)['refresh_ticket']
    with pytest.raises(OwnerAuthError, match=FAILURE) as error:
        resolve(broker, refresh_ticket=ticket)
    assert 'synthetic' not in str(error.value)
    assert ticket not in broker.tickets


def test_tickets_cannot_be_used_by_other_peer_and_expire():
    clock = Mock(return_value=0)
    broker = OwnerAuthBroker(lambda: grant(), [321, 654], clock=clock)
    ticket = resolve(broker)['refresh_ticket']
    with pytest.raises(OwnerAuthError):
        resolve(broker, 654, refresh_ticket=ticket)
    assert ticket in broker.tickets
    clock.return_value = 301
    with pytest.raises(OwnerAuthError):
        resolve(broker, refresh_ticket=ticket)


@pytest.mark.parametrize('payload', [
    {'operation': 'resolve', 'provider': 'openai-codex'},
    {'operation': 'resolve', 'owner_home': '/owner'},
    {'operation': 'resolve', 'model': 'caller-choice'},
    {'operation': 'execute', 'command': 'cat /owner/auth.json'},
    {'operation': 'resolve', 'refresh_ticket': []}, [], None,
])
def test_broker_protocol_rejects_caller_configuration_before_owner_access(payload):
    resolver = Mock()
    with pytest.raises(OwnerAuthError):
        OwnerAuthBroker(resolver, [321]).dispatch(321, payload)
    resolver.assert_not_called()


def test_unauthorized_uid_never_resolves_and_provider_output_is_discarded(capsys, caplog):
    def noisy():
        print('synthetic-secret')
        print('synthetic-secret', file=sys.stderr)
        logging.error('synthetic-secret')
        raise RuntimeError('synthetic-secret')
    resolver = Mock(side_effect=noisy)
    broker = OwnerAuthBroker(resolver, [321])
    with pytest.raises(OwnerAuthError):
        resolve(broker, 654)
    resolver.assert_not_called()
    with pytest.raises(OwnerAuthError) as error:
        resolve(broker)
    assert str(error.value) == FAILURE
    captured = capsys.readouterr()
    assert 'synthetic-secret' not in captured.out + captured.err + caplog.text


def test_api_key_provider_401_does_not_exhaust_pool_with_oauth_refresh():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'api-key-entry'
    broker = OwnerAuthBroker(lambda: grant(provider='openai', api_mode='chat_completions',
        base_url='https://api.openai.com/v1', credential_pool=pool), [321])
    ticket = resolve(broker)['refresh_ticket']
    assert resolve(broker, refresh_ticket=ticket)['runtime']['provider'] == 'openai'
    pool.try_refresh_matching.assert_not_called()


def test_refresh_must_actually_replace_rejected_token():
    pool = Mock()
    pool.entry_id_for_api_key.return_value = 'credential-one'
    broker = OwnerAuthBroker(lambda: grant(credential_pool=pool), [321])
    ticket = resolve(broker)['refresh_ticket']
    with pytest.raises(OwnerAuthError):
        resolve(broker, refresh_ticket=ticket)
    assert ticket not in broker.tickets


@pytest.mark.parametrize('changes', [
    {'api_mode': 'codex_app_server'}, {'provider': 'copilot-acp'},
    {'base_url': 'https://user:secret@chatgpt.com/backend-api/codex'},
    {'base_url': 'http://chatgpt.com/backend-api/codex'},
    {'base_url': 'https://other.example.test/backend-api/codex'},
    {'base_url': 'https://chatgpt.com/backend-api/codex?secret=synthetic'},
    {'api_key': 'secret\nvalue'}, {'api_key': 'x' * 32769},
])
def test_invalid_runtime_never_exports_or_reaches_child(changes):
    with pytest.raises(OwnerAuthError) as error:
        validate_runtime(grant(**changes))
    assert str(error.value) == FAILURE


@pytest.fixture
def socket_server(tmp_path):
    listeners = []
    threads = []
    temporary = tempfile.TemporaryDirectory(prefix='jh-auth-', dir='/tmp')
    def start(broker):
        directory = Path(temporary.name) / str(len(listeners))
        directory.mkdir(mode=0o750)
        path = directory / 'auth.sock'
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(path))
        path.chmod(0o660)
        listener.listen(1)
        def run():
            connection, _ = listener.accept()
            with connection:
                broker.handle(connection)
        thread = threading.Thread(target=run)
        thread.start()
        listeners.append(listener)
        threads.append(thread)
        return path
    yield start
    for listener in listeners:
        listener.close()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    temporary.cleanup()


def test_real_unix_peer_credentials_and_client_protocol(socket_server):
    broker = OwnerAuthBroker(lambda: grant(), [os.getuid()])
    path = socket_server(broker)
    assert OwnerAuthClient(path).resolve()['api_key'] == 'synthetic-access'


def test_real_socket_rejects_unauthorized_uid(socket_server):
    resolver = Mock()
    path = socket_server(OwnerAuthBroker(resolver, [os.getuid() + 1000]))
    with pytest.raises(OwnerAuthError):
        OwnerAuthClient(path).resolve()
    resolver.assert_not_called()


def test_real_socket_bounds_oversize_requests_before_auth_lookup(socket_server):
    resolver = Mock()
    path = socket_server(OwnerAuthBroker(resolver, [os.getuid()]))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(path))
        client.sendall(b'x' * (MAX_REQUEST + 1) + b'\n')
        response = client.recv(4096)
    assert json.loads(response)['error'] == FAILURE
    resolver.assert_not_called()


def test_client_rejects_regular_file_and_redacts_response(tmp_path):
    path = tmp_path / 'auth.sock'
    path.write_text('synthetic-owner-token')
    with pytest.raises(OwnerAuthError) as error:
        OwnerAuthClient(path).resolve()
    assert str(error.value) == FAILURE


def fake_source(tmp_path, behavior='success'):
    source = tmp_path / 'sdk'
    source.mkdir()
    (source / 'toolsets.py').write_text("TOOLSETS = {'terminal': {}, 'files': {}}")
    (source / 'run_agent.py').write_text('''
import os
from pathlib import Path
class AIAgent:
    def __init__(self, **kw):
        assert kw['provider'] == 'openai-codex'
        assert kw['api_mode'] == 'codex_responses'
        assert kw['base_url'] == 'https://chatgpt.com/backend-api/codex'
        assert kw['enabled_toolsets'] == []
        assert set(kw['disabled_toolsets']) == {'terminal', 'files'}
        assert kw['skip_memory'] and kw['skip_background_review'] and kw['skip_context_files']
        assert kw['session_db'] is None and not kw['load_soul_identity']
        assert kw['quiet_mode'] and not kw['checkpoints_enabled'] and not kw['save_trajectories']
        assert Path(os.environ['HOME']).resolve() == Path(os.environ['HERMES_HOME']).resolve() == Path.cwd()
        assert not list(Path.cwd().iterdir())
        for field in ['OWNER_SECRET', 'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'CODEX_HOME', 'PYTHONPATH']:
            assert field not in os.environ
        self.key = kw['api_key']
        self.tools = []
    def run_conversation(self, user_message, system_message):
        BEHAVIOR
        return {'final_response': '{"reply":"Scoped"}'}
    def close(self): pass
'''.replace('        BEHAVIOR', {
        'success': '        pass',
        'auth': "        if self.key == 'synthetic-access':\n            self._try_refresh_codex_client_credentials(force=True)\n            return {'error':'synthetic-secret-401', 'final_response':''}",
        'always_auth': "        self._try_refresh_codex_client_credentials(force=True)\n        return {'error':'synthetic-secret-401', 'final_response':''}",
        'rate_limit': "        return {'error':'HTTP 429 synthetic-access', 'final_response':''}",
    }[behavior]))
    return source


def test_shared_planner_gets_fresh_runtime_for_every_call_and_isolates_child(tmp_path, monkeypatch):
    source = fake_source(tmp_path)
    for field in ['OWNER_SECRET', 'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN', 'CODEX_HOME', 'PYTHONPATH']:
        monkeypatch.setenv(field, 'synthetic-owner-secret')
    planner = SharedOwnerPlanner(sys.executable, source, '/unused/socket')
    planner.auth.resolve = Mock(side_effect=[{**grant(), 'refresh_ticket': 'a' * 43},
                                             {**grant(api_key='synthetic-rotated'), 'refresh_ticket': 'b' * 43}])
    assert planner([], {}) == {'reply': 'Scoped'}
    assert planner([], {}) == {'reply': 'Scoped'}
    assert planner.auth.resolve.call_count == 2
    assert not hasattr(planner, 'api_key')
    assert 'synthetic-access' not in repr(vars(planner))


@pytest.mark.parametrize('behavior', ['auth', 'always_auth', 'rate_limit'])
def test_child_concrete_401_refreshes_once_but_429_does_not(tmp_path, behavior):
    source = fake_source(tmp_path, behavior)
    planner = SharedOwnerPlanner(sys.executable, source, '/unused/socket')
    planner.auth.resolve = Mock(side_effect=[{**grant(), 'refresh_ticket': 'a' * 43},
        {**grant(api_key='synthetic-rotated'), 'refresh_ticket': 'b' * 43}])
    if behavior == 'auth':
        assert planner([], {}) == {'reply': 'Scoped'}
    else:
        with pytest.raises(RuntimeError) as error:
            planner([], {})
        assert 'synthetic' not in str(error.value)
    assert planner.auth.resolve.call_count == (1 if behavior == 'rate_limit' else 2)
    if behavior != 'rate_limit':
        assert planner.auth.resolve.call_args.kwargs == {'refresh_ticket': 'a' * 43}
