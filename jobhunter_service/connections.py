"""Candidate-scoped connection evidence and explicit, bounded connection checks.

Ordinary onboarding reads never contact providers or start a browser. Checks
return fixed messages, never page contents, credentials, or provider errors.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit

from .account_state import connection_matches

PROVIDERS = ('linkedin', 'gmail', 'tracker')
CHECK_TTL = 1800
CHECK_TIMEOUT = 20


def google_configured(path, public_url):
    """Validate operator configuration without returning any client values."""
    if not path or not public_url:
        return False
    try:
        client = json.loads(Path(path).read_text())['web']
        return bool(isinstance(client, dict) and isinstance(client.get('client_id'), str) and client['client_id']
                    and isinstance(client.get('client_secret'), str) and client['client_secret']
                    and isinstance(client.get('redirect_uris'), list)
                    and public_url + '/oauth/google/callback' in client['redirect_uris'])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _private_file(root, relative):
    path = root / relative
    if path.is_symlink() or path.resolve() != path or not path.is_relative_to(root):
        raise ValueError('Connection state must remain in your own profile.')
    return path


def _json(root, relative):
    path = _private_file(root, relative)
    if not path.is_file():
        return {}
    if path.stat().st_size > 65536:
        raise ValueError('Connection metadata exceeds the supported limit.')
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}


def _fingerprint(settings, provider):
    value = settings['accounts'][provider] if provider != 'linkedin' else {'provider': 'linkedin'}
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _key(actor_id, provider):
    return f'connection-check:{actor_id}:{provider}'


def _generation(service, actor_id):
    with service.store.connect() as db:
        return db.execute("SELECT COALESCE(MAX(id),0) FROM audit WHERE target=? "
                          "AND operation IN ('suspend','revoke')", (actor_id,)).fetchone()[0]


def _result(status, message, **extra):
    return {'status': status, 'message': message, **extra}


def clear_connection(service, actor_id, provider):
    service._member(actor_id)
    if provider not in PROVIDERS:
        raise ValueError('Choose LinkedIn, Gmail, or tracker.')
    service.store.checkpoint(_key(actor_id, provider), '{}')


def connection_status(service, actor_id):
    member = service._member(actor_id)
    root = service.profile_dir(member)
    settings = json.loads(member['settings'])
    generation = _generation(service, actor_id)
    result = {}
    for provider in PROVIDERS:
        if provider == 'linkedin':
            available = bool(service.public_url and service.browser_manager)
            state = _result('configured', 'Use /connect linkedin to open your private browser and sign in. Then choose Check connection or send /check linkedin. You can skip this for now.')
        else:
            available = bool(service.public_url and getattr(service, 'google_enabled', False))
            state = _result('configured', 'Confirm the account email, then use /connect ' + provider + '. After consent, use /check ' + provider + '. You can skip it for now.')
        if not available:
            result[provider] = _result('unavailable',
                'LinkedIn browser access needs owner setup. You can skip it and continue.' if provider == 'linkedin' else
                'Google connection needs owner setup. You can skip Gmail and the tracker and continue onboarding.')
            continue
        try:
            cached = json.loads(service.store.checkpoint(_key(actor_id, provider)) or '{}')
            if (isinstance(cached, dict) and cached.get('fingerprint') == _fingerprint(settings, provider)
                    and cached.get('generation') == generation
                    and type(cached.get('checked_at')) in {int, float}
                    and 0 <= time.time() - cached['checked_at'] < CHECK_TTL
                    and cached.get('status') in {'connected', 'unverified', 'error'}):
                # Persisted cache contains fixed messages generated only below.
                state = _result(cached['status'], _check_message(provider, cached['status']),
                                checked_at=cached['checked_at'])
            if provider != 'linkedin':
                account = settings['accounts'][provider]
                identity = _json(root, f'secrets/{provider}_account.json')
                token = _private_file(root, f'secrets/{provider}_token.json')
                matched = bool(account.get('account') and identity.get('email') == account['account']
                               and token.is_file() and token.stat().st_size)
                if matched and provider == 'tracker':
                    matched = connection_matches(_json(root, 'state/tracker_connection.json'), account)
                if not matched:
                    state = _result('configured', 'This account is not connected. Confirm its email and use /connect ' + provider + ', or skip it.')
                elif 'checked_at' not in state:
                    state = _result('unverified', 'Account consent is saved. Choose Check connection to verify current access.')
            result[provider] = state
        except (OSError, ValueError, TypeError):
            result[provider] = _result('error', 'Saved connection state could not be checked. Use /support for a report for the owner.')
    return result


def _check_message(provider, status):
    if status == 'connected':
        return ('LinkedIn sign-in was detected in your isolated browser at the last check.' if provider == 'linkedin' else
                'Account access was verified at the last check.' if provider == 'gmail' else
                'Account access and the application tracker were verified at the last check.')
    if status == 'unverified':
        return ('LinkedIn sign-in could not be confirmed. Open /connect linkedin, finish signing in, and check again. '
                'Complete any verification yourself in the browser; never send codes here.' if provider == 'linkedin' else
                'Account access could not be confirmed. Reconnect this account or skip it for now.')
    return 'The connection check could not finish. Retry it, reconnect, or use /support for a report for the owner.'


def _run_check(payload):
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHON_DOTENV_DISABLED': '1',
           'PYTHONSAFEPATH': '1', 'PYTHONPATH': str(Path(__file__).resolve().parent.parent)}
    result = subprocess.run([sys.executable, '-m', 'jobhunter_service.connections'],
        input=json.dumps(payload), capture_output=True, text=True, env=env,
        timeout=CHECK_TIMEOUT, check=False)
    if result.returncode or len(result.stdout) > 1024:
        raise ValueError('Connection check could not finish.')
    response = json.loads(result.stdout)
    if response not in ({'status': 'connected'}, {'status': 'unverified'}):
        raise ValueError('Connection check returned an unsupported result.')
    return response['status']


def check_connection(service, actor_id, provider):
    if provider not in PROVIDERS:
        raise ValueError('Choose LinkedIn, Gmail, or tracker.')
    # Share the revocation/settings lock: a completed probe cannot certify an
    # account that changed while it ran. No provider writes reach another user.
    with service.mutation(actor_id):
        member = service._member(actor_id)
        settings = json.loads(member['settings'])
        before = connection_status(service, actor_id)
        if before[provider]['status'] == 'unavailable':
            return before
        root = service.profile_dir(member)
        status = 'unverified'
        try:
            if provider == 'linkedin':
                browser = service.browser_manager.status(member['profile_id'])
                if browser.get('status') == 'running' and browser.get('profile_id') == member['profile_id']:
                    port, expiry = browser.get('cdp_port'), browser.get('expires_at')
                    if (type(port) is not int or not 1024 <= port <= 65535
                            or type(expiry) not in {int, float} or not time.time() < expiry <= time.time() + 3605):
                        raise ValueError('Invalid browser lease.')
                    status = _run_check({'provider': provider, 'port': port})
            elif before[provider]['status'] != 'configured':
                status = _run_check({'provider': provider, 'root': str(root),
                                     'account': settings['accounts'][provider]['account']})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            status = 'error'
        service._member(actor_id)
        service.store.checkpoint(_key(actor_id, provider), json.dumps({
            'fingerprint': _fingerprint(settings, provider), 'generation': _generation(service, actor_id),
            'status': status, 'checked_at': time.time()}))
        return connection_status(service, actor_id)


def _linkedin_check(port):
    import requests
    from jobhunter_auto_apply.cdp import CDPClient

    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError('Invalid browser port.')
    with requests.Session() as session:
        session.trust_env = False
        with session.get(f'http://127.0.0.1:{port}/json/list', timeout=3, allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise ValueError('Browser discovery unavailable.')
            body = response.raw.read(65537)
            if len(body) > 65536:
                raise ValueError('Browser discovery exceeds limit.')
            targets = json.loads(body)
    if not isinstance(targets, list) or len(targets) > 30:
        raise ValueError('Invalid browser discovery.')
    for target in targets:
        if not isinstance(target, dict) or target.get('type') != 'page':
            continue
        page = urlsplit(target.get('url', ''))
        if page.scheme != 'https' or page.hostname not in {'linkedin.com', 'www.linkedin.com'}:
            continue
        identifier = target.get('id', '')
        endpoint = urlsplit(target.get('webSocketDebuggerUrl', ''))
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier)
                or endpoint.scheme != 'ws' or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}
                or endpoint.port not in {port, 9222, 9223} or endpoint.username or endpoint.password
                or endpoint.query or endpoint.fragment or endpoint.path != f'/devtools/page/{identifier}'):
            raise ValueError('Browser target is outside the assigned session.')
        client = CDPClient(f'ws://127.0.0.1:{port}/devtools/page/{identifier}', timeout=3)
        try:
            # Read only a boolean. Never inspect cookies, form values, messages,
            # identity text, or verification challenges; never navigate/click.
            signed_in = client.evaluate("""Boolean(
                location.protocol === 'https:' &&
                ['linkedin.com','www.linkedin.com'].includes(location.hostname) &&
                !/^\\/(login|uas|checkpoint|authwall|signup|challenge)(\\/|$)/.test(location.pathname) &&
                !document.querySelector('input[type=password]') &&
                document.querySelector('.global-nav__me, #global-nav a[href*=\"/mynetwork/\"]'))""")
            if signed_in is True:
                return 'connected'
        finally:
            client.close()
    return 'unverified'


def _google_check(provider, root, account):
    from jobhunter_integrations.web_oauth import _private_exchange
    root = Path(root).resolve()
    token = _private_file(root, f'secrets/{provider}_token.json')
    metadata = _private_file(root, f'secrets/{provider}_account.json')
    with _private_exchange():
        if provider == 'gmail':
            from jobhunter_integrations.gmail_auth import gmail_service
            gmail_service(token, account, account_config_path=metadata)
        else:
            from jobhunter_integrations.google_tracker_auth import checked_services, inspect_tracker
            sheets, drive = checked_services(token, account, account_config_path=metadata)
            connection = _json(root, 'state/tracker_connection.json')
            result = inspect_tracker(sheets, connection['spreadsheet_id'], connection['sheet_id'])
            if (result.get('read_access') is not True or not result.get('tabs')
                    or any(tab.get('expected_headers') is not True for tab in result['tabs'])):
                raise ValueError('Tracker layout needs review.')
            capabilities = drive.files().get(fileId=connection['spreadsheet_id'],
                                              fields='capabilities(canEdit)').execute()
            if capabilities.get('capabilities', {}).get('canEdit') is not True:
                raise ValueError('Tracker write access is unavailable.')
    return 'connected'


if __name__ == '__main__':
    try:
        request = json.loads(sys.stdin.read(4096))
        provider = request['provider']
        if provider == 'linkedin' and set(request) == {'provider', 'port'}:
            state = _linkedin_check(request['port'])
        elif provider in {'gmail', 'tracker'} and set(request) == {'provider', 'root', 'account'}:
            state = _google_check(provider, request['root'], request['account'])
        else:
            raise ValueError('Unsupported connection check.')
        print(json.dumps({'status': state}))
    except Exception:
        # Provider exceptions and URLs can contain credentials. Exit quietly.
        raise SystemExit(1) from None
