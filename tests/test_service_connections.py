"""Connection checks use only synthetic profiles, provider fixtures and local state."""
import json
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jobhunter_service import connections
from jobhunter_service.service import JobHunterService
from jobhunter_service.state import private_json


@pytest.fixture
def service(tmp_path):
    browser = Mock()
    browser.status.side_effect = lambda profile: {'profile_id': profile, 'status': 'stopped'}
    service = JobHunterService(tmp_path / 'state', 1, public_url='https://jobs.example.test',
                               browser_manager=browser)
    service.google_enabled = True
    for actor in (11, 22):
        service.admin(1, 'add', actor)
        service.authorize(actor)
        proposal = service.propose(actor, {'accounts': {
            'gmail': {'account': f'jobs{actor}@example.test'},
            'tracker': {'account': f'jobs{actor}@example.test', 'viewer_email': f'viewer{actor}@example.test'}}})
        service.confirm(actor, proposal['action_id'])
    return service


def google_state(service, actor=11, provider='gmail'):
    root = service.profile_dir(service._member(actor))
    private_json(root / f'secrets/{provider}_account.json', {'email': f'jobs{actor}@example.test'})
    private_json(root / f'secrets/{provider}_token.json', {'token': 'synthetic-only-private-value'})
    if provider == 'tracker':
        private_json(root / 'state/tracker_connection.json', {
            'account': f'jobs{actor}@example.test', 'configured_spreadsheet_id': '',
            'viewer_email': f'viewer{actor}@example.test', 'spreadsheet_id': 'synthetic-tracker', 'sheet_id': 0})
    return root


def running(service, actor=11, **overrides):
    status = {'profile_id': f'u{actor}', 'status': 'running', 'viewer_port': 33000,
              'cdp_port': 33001, 'expires_at': time.time() + 1800, **overrides}
    service.browser_manager.status.side_effect = None
    service.browser_manager.status.return_value = status


def test_google_operator_config_requires_web_client_and_exact_callback(tmp_path):
    path = tmp_path / 'client.json'
    assert not connections.google_configured(path, 'https://jobs.example.test')
    path.write_text(json.dumps({'installed': {'client_id': 'example'}}))
    assert not connections.google_configured(path, 'https://jobs.example.test')
    path.write_text(json.dumps({'web': {'client_id': 'example', 'client_secret': 'synthetic',
        'redirect_uris': ['https://jobs.example.test/oauth/google/callback']}}))
    assert connections.google_configured(path, 'https://jobs.example.test')
    assert not connections.google_configured(path, 'https://another.example.test')


def test_google_unavailable_is_explicit_and_does_not_issue_a_link(service):
    service.google_enabled = False
    status = connections.connection_status(service, 11)
    assert status['gmail']['status'] == status['tracker']['status'] == 'unavailable'
    assert 'skip' in status['gmail']['message'].lower()
    with pytest.raises(ValueError, match='Google'):
        service.connect(11, 'google', 'gmail')
    with service.store.connect() as db:
        assert db.execute('SELECT COUNT(*) FROM tokens').fetchone()[0] == 0


def test_status_reads_do_not_start_browsers_or_contact_providers(service, monkeypatch):
    probe = Mock(side_effect=AssertionError('No implicit connection checks'))
    monkeypatch.setattr(connections, '_run_check', probe)
    assert connections.connection_status(service, 11)['linkedin']['status'] == 'configured'
    service.browser_manager.status.assert_not_called()
    service.browser_manager.start.assert_not_called()
    probe.assert_not_called()


@pytest.mark.parametrize('provider', ['gmail', 'tracker'])
def test_saved_google_consent_is_unverified_until_explicit_check(service, monkeypatch, provider):
    google_state(service, provider=provider)
    assert connections.connection_status(service, 11)[provider]['status'] == 'unverified'
    probe = Mock(return_value='connected')
    monkeypatch.setattr(connections, '_run_check', probe)
    result = connections.check_connection(service, 11, provider)
    assert result[provider]['status'] == 'connected'
    assert result[provider]['checked_at'] <= time.time()
    assert 'synthetic-only-private-value' not in json.dumps(result)
    assert connections.connection_status(service, 22)[provider]['status'] == 'configured'
    assert probe.call_args.args[0]['root'] == str(service.root / 'u11')


def test_google_account_change_invalidates_success_and_preserves_other_profile(service, monkeypatch):
    google_state(service)
    monkeypatch.setattr(connections, '_run_check', lambda payload: 'connected')
    connections.check_connection(service, 11, 'gmail')
    proposal = service.propose(11, {'accounts': {'gmail': {'account': 'new@example.test'}}})
    service.confirm(11, proposal['action_id'])
    assert connections.connection_status(service, 11)['gmail']['status'] == 'configured'
    assert connections.connection_status(service, 22)['gmail']['status'] == 'configured'


def test_tracker_requires_matching_viewer_and_spreadsheet_metadata(service):
    root = google_state(service, provider='tracker')
    state = json.loads((root / 'state/tracker_connection.json').read_text())
    state['viewer_email'] = 'someone-else@example.test'
    private_json(root / 'state/tracker_connection.json', state)
    assert connections.connection_status(service, 11)['tracker']['status'] == 'configured'


def test_connection_metadata_cannot_follow_another_profile_symlink(service):
    root = google_state(service, 22)
    (service.root / 'u11/secrets/gmail_account.json').symlink_to(root / 'secrets/gmail_account.json')
    assert connections.connection_status(service, 11)['gmail']['status'] == 'error'


def test_linkedin_check_requires_live_broker_session_and_is_candidate_scoped(service, monkeypatch):
    probe = Mock(return_value='connected')
    monkeypatch.setattr(connections, '_run_check', probe)
    assert connections.check_connection(service, 11, 'linkedin')['linkedin']['status'] == 'unverified'
    probe.assert_not_called()
    running(service)
    assert connections.check_connection(service, 11, 'linkedin')['linkedin']['status'] == 'connected'
    probe.assert_called_once_with({'provider': 'linkedin', 'port': 33001})
    assert connections.connection_status(service, 22)['linkedin']['status'] == 'configured'
    connections.clear_connection(service, 11, 'linkedin')
    assert connections.connection_status(service, 11)['linkedin']['status'] == 'configured'


@pytest.mark.parametrize('override', [{'cdp_port': True}, {'cdp_port': 22}, {'expires_at': float('nan')},
                                    {'expires_at': 0}, {'profile_id': 'u22'}])
def test_invalid_or_other_candidates_browser_cannot_be_probed(service, monkeypatch, override):
    running(service, **override)
    probe = Mock(side_effect=AssertionError('Probe must not run'))
    monkeypatch.setattr(connections, '_run_check', probe)
    assert connections.check_connection(service, 11, 'linkedin')['linkedin']['status'] != 'connected'
    probe.assert_not_called()


def test_timed_out_check_keeps_grant_and_returns_only_safe_recovery(service, monkeypatch):
    root = google_state(service)
    before = (root / 'secrets/gmail_token.json').read_bytes()
    monkeypatch.setattr(connections, '_run_check', Mock(side_effect=subprocess.TimeoutExpired('private-example', 20)))
    status = connections.check_connection(service, 11, 'gmail')['gmail']
    assert status['status'] == 'error' and '/support' in status['message']
    assert 'private-example' not in json.dumps(status)
    assert (root / 'secrets/gmail_token.json').read_bytes() == before


def test_success_expires_and_revoked_member_cannot_probe(service, monkeypatch):
    running(service)
    monkeypatch.setattr(connections, '_run_check', lambda payload: 'connected')
    connections.check_connection(service, 11, 'linkedin')
    later = time.time() + connections.CHECK_TTL + 1
    monkeypatch.setattr(connections.time, 'time', lambda: later)
    assert connections.connection_status(service, 11)['linkedin']['status'] != 'connected'
    service.admin(1, 'revoke', 11)
    with pytest.raises(PermissionError):
        connections.check_connection(service, 11, 'linkedin')


def test_reinvitation_never_reuses_an_old_successful_check(service, monkeypatch):
    running(service)
    monkeypatch.setattr(connections, '_run_check', lambda payload: 'connected')
    assert connections.check_connection(service, 11, 'linkedin')['linkedin']['status'] == 'connected'
    service.admin(1, 'revoke', 11)
    service.admin(1, 'add', 11)
    service.authorize(11)
    assert connections.connection_status(service, 11)['linkedin']['status'] != 'connected'


@pytest.mark.parametrize('headers,editable,passes', [(False, True, False), (True, False, False), (True, True, True)])
def test_tracker_probe_requires_expected_headers_and_edit_access(service, monkeypatch, headers, editable, passes):
    from jobhunter_integrations import google_tracker_auth
    root = google_state(service, provider='tracker')
    sheets, drive = Mock(), Mock()
    drive.files.return_value.get.return_value.execute.return_value = {'capabilities': {'canEdit': editable}}
    monkeypatch.setattr(google_tracker_auth, 'checked_services', Mock(return_value=(sheets, drive)))
    monkeypatch.setattr(google_tracker_auth, 'inspect_tracker', Mock(return_value={
        'read_access': True, 'tabs': [{'expected_headers': headers}]}))
    if passes:
        assert connections._google_check('tracker', root, 'jobs11@example.test') == 'connected'
    else:
        with pytest.raises(ValueError):
            connections._google_check('tracker', root, 'jobs11@example.test')


def test_probe_process_has_bounded_timeout_and_no_ambient_credentials(monkeypatch):
    monkeypatch.setenv('JOBHUNTER_SERVICE_BOT_TOKEN', 'synthetic-bot-secret')
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"status":"connected"}'))
    monkeypatch.setattr(connections.subprocess, 'run', runner)
    assert connections._run_check({'provider': 'linkedin', 'port': 33001}) == 'connected'
    args = runner.call_args.kwargs
    assert args['timeout'] == connections.CHECK_TIMEOUT
    assert 'JOBHUNTER_SERVICE_BOT_TOKEN' not in args['env']
    assert args['env']['PYTHONSAFEPATH'] == '1'


def browser_discovery(monkeypatch, targets, signed_in=True):
    import requests
    from jobhunter_auto_apply import cdp
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    response = Mock(status_code=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.raw.read.return_value = json.dumps(targets).encode()
    session.get.return_value = response
    monkeypatch.setattr(requests, 'Session', lambda: session)
    client = Mock()
    client.evaluate.return_value = signed_in
    factory = Mock(return_value=client)
    monkeypatch.setattr(cdp, 'CDPClient', factory)
    return session, factory, client


def target(**overrides):
    return {'type': 'page', 'id': 'EXAMPLE_PAGE', 'url': 'https://www.linkedin.com/feed/',
            'webSocketDebuggerUrl': 'ws://127.0.0.1:9222/devtools/page/EXAMPLE_PAGE', **overrides}


def test_read_only_signin_probe_uses_broker_port_and_only_boolean_dom_evidence(monkeypatch):
    session, factory, client = browser_discovery(monkeypatch, [target()])
    assert connections._linkedin_check(33001) == 'connected'
    assert session.trust_env is False
    assert session.get.call_args.kwargs['allow_redirects'] is False
    factory.assert_called_once_with('ws://127.0.0.1:33001/devtools/page/EXAMPLE_PAGE', timeout=3)
    expression = client.evaluate.call_args.args[0]
    assert 'document.cookie' not in expression and '.click(' not in expression
    assert 'checkpoint' in expression and 'input[type=password]' in expression
    client.close.assert_called_once()


@pytest.mark.parametrize('url', ['ws://100.64.0.1:33001/devtools/page/EXAMPLE_PAGE',
                               'ws://127.0.0.1:8765/admin',
                               'ws://127.0.0.1:33001/devtools/page/EXAMPLE_PAGE?token=synthetic'])
def test_browser_discovery_cannot_redirect_probe_to_another_authority(monkeypatch, url):
    _, factory, _ = browser_discovery(monkeypatch, [target(webSocketDebuggerUrl=url)])
    with pytest.raises(ValueError):
        connections._linkedin_check(33001)
    factory.assert_not_called()


def test_unrelated_tab_and_unknown_dom_never_count_as_linkedin_signin(monkeypatch):
    _, factory, _ = browser_discovery(monkeypatch, [target(url='https://linkedin.com.example.test/feed/')])
    assert connections._linkedin_check(33001) == 'unverified'
    factory.assert_not_called()
    browser_discovery(monkeypatch, [target()], signed_in=False)
    assert connections._linkedin_check(33001) == 'unverified'
