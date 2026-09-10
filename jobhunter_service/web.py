"""Authenticated Google consent and isolated browser viewer; no member filesystem API."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import re
import time
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout, TraceConfig, WSMsgType, web

from .state import private_json
from .browser import BrowserError


_VIEWER_PATH = re.compile(r'(?:vnc\.html|websockify|(?:app|core|vendor)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*)')
_CAPABILITY = re.compile(r'[A-Za-z0-9_-]{43}')
_BROWSER_ACCESS_POLL_SECONDS = 3


def _live_browser_expiry(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= time.time():
        raise PermissionError('This browser lease expired. Request a fresh connection link.')
    return value


def _browser_capability(raw):
    if not isinstance(raw, str) or not _CAPABILITY.fullmatch(raw):
        raise PermissionError('A fresh private browser connection is required.')
    return raw


@web.middleware
async def safe_responses(request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    except PermissionError:
        response = web.Response(status=403, text='Access denied. Request a fresh connection link in your private JobHunter chat.')
    except BrowserError as exc:
        response = web.Response(status=503, text=str(exc) + ' Return to your private chat and request /connect linkedin again, or skip LinkedIn for now.')
    except (ValueError, KeyError):
        response = web.Response(status=400, text='Invalid request. Review your settings in your private JobHunter chat.')
    except Exception:
        response = web.Response(status=503, text='This operation could not be completed. Your existing data is retained. Retry from JobHunter.')
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'"
    if request.path.startswith('/browser/'):
        # noVNC decodes received framebuffer images through data URLs.
        response.headers['Content-Security-Policy'] += "; img-src 'self' data:"
    if isinstance(response, web.HTTPException):
        raise response
    return response


def create_app(service, *, google_client=None, admin_token=None, telegram_ingress=None):
    # This trusted adapter is the authority for actual consent availability.
    # A public hostname alone does not make Google authorization available.
    service.google_enabled = google_client is not None
    app = web.Application(middlewares=[safe_responses], client_max_size=65536)

    async def health(request):
        return web.json_response({'status': 'ok'})

    async def admin(request):
        supplied = request.headers.get('Authorization', '')
        if not admin_token or not hmac.compare_digest(supplied, 'Bearer ' + admin_token):
            raise PermissionError
        data = await request.json()
        if set(data) - {'operation', 'user_id'}:
            raise ValueError
        # The trusted admin transport supplies the owner identity, never request JSON.
        result = await asyncio.to_thread(service.admin, service.owner_id, data['operation'], data.get('user_id'))
        return web.json_response(result)

    async def internal_telegram(request):
        supplied = request.headers.get('Authorization', '')
        if not admin_token or not hmac.compare_digest(supplied.encode(), ('Bearer ' + admin_token).encode()):
            raise PermissionError
        if telegram_ingress is None:
            raise web.HTTPServiceUnavailable(text='Shared Telegram ingress is not configured.')
        from .telegram_ingress import IngressConflict
        try:
            update = await request.json()
            receipt = await asyncio.to_thread(telegram_ingress.enqueue, update)
        except IngressConflict:
            raise web.HTTPConflict(text='This update ID already has different content.') from None
        except (RecursionError, UnicodeError):
            raise ValueError('Invalid Telegram update.') from None
        return web.json_response(receipt, status=202)

    async def google_start(request):
        if google_client is None:
            raise web.HTTPServiceUnavailable(text='Google connection is not configured yet.')
        user_id, payload = service.store.read_token(request.query.get('t', ''), 'google_connect')
        state = service.store.token(user_id, 'google_oauth', payload)
        authorization = await asyncio.to_thread(google_client.authorization_url, payload['kind'], state, payload['account'])
        payload['code_verifier'] = authorization['code_verifier']
        with service.store.connect() as db:
            db.execute('UPDATE tokens SET payload=? WHERE digest=?',
                       (json.dumps(payload), hashlib.sha256(state.encode()).hexdigest()))
        raise web.HTTPFound(authorization['url'])

    async def google_callback(request):
        user_id, payload = service.store.read_token(request.query.get('state', ''), 'google_oauth')
        if request.query.get('error'):
            return web.Response(text='Google access was not connected. Return to your private JobHunter chat.')
        member = service._member(user_id)
        settings = json.loads(member['settings'])
        account = settings['accounts'][payload['kind']]
        if account['account'] != payload['account'] or member['revision'] != payload['revision']:
            raise PermissionError
        grant = await asyncio.to_thread(google_client.exchange, payload['kind'], request.query['code'],
                                        payload['account'], payload['code_verifier'])
        def persist_connection():
            with service.mutation(user_id):
                current = service._member(user_id)
                if current['revision'] != payload['revision']:
                    raise PermissionError
                service.materialize(current)
                root = service.profile_dir(current)
                token_path = root / 'secrets' / f"{payload['kind']}_token.json"
                grant.persist(token_path)
                private_json(root / 'secrets' / f"{payload['kind']}_account.json", {'email': grant.account})
                if payload['kind'] != 'tracker':
                    return None
                from jobhunter_integrations.google_tracker_setup import provision_tracker
                result = provision_tracker(token_path, grant.account,
                    root / 'state' / 'tracker_setup.json', spreadsheet_id=account.get('spreadsheet_id') or None,
                    viewer_email=account.get('viewer_email') or None,
                    drive_state_path=root / 'state' / 'tracker_drive_files.json')
                if service._member(user_id)['revision'] != payload['revision']:
                    raise PermissionError
                result.update(account=account['account'], configured_spreadsheet_id=account.get('spreadsheet_id') or '',
                              viewer_email=account.get('viewer_email') or '')
                private_json(root / 'state' / 'tracker_connection.json', result)
                return result
        result = await asyncio.to_thread(persist_connection)
        if result and service.telegram_client:
            service._member(user_id)
            await asyncio.to_thread(service.telegram_client.send_message, user_id,
                                    'Your personal application tracker is connected.\n' + result['spreadsheet_url'])
        return web.Response(text='Your account is connected. Return to your private JobHunter chat.')

    async def browser_start(request):
        user_id, _ = service.store.read_token(_browser_capability(request.query.get('t', '')), 'browser_connect')
        def start_session():
            # Revocation shares this lock, so it cannot finish between the
            # final active-member check and issuance of a new session token.
            with service.mutation(user_id):
                member = service._member(user_id)
                status = service.browser_manager.start(member['profile_id'])
                service._member(user_id)
                if status.get('status') != 'running' or status.get('profile_id') != member['profile_id']:
                    raise PermissionError
                expires = _live_browser_expiry(status.get('expires_at'))
                remaining = min(1800, math.floor(expires - time.time()))
                if remaining < 1:
                    raise PermissionError
                cookie = service.store.token(user_id, 'browser_session',
                    {'profile_id': member['profile_id'], 'browser_expires_at': expires}, ttl=remaining)
                return cookie, remaining
        cookie, remaining = await asyncio.to_thread(start_session)
        response = web.HTTPFound('/browser/vnc.html?' + urlencode({'autoconnect': '1', 'resize': 'scale', 'path': 'browser/websockify'}))
        response.set_cookie('jobhunter_browser', cookie, secure=True, httponly=True, samesite='Strict', max_age=remaining, path='/browser')
        raise response

    def browser_session(raw):
        user_id, payload = service.store.read_token(_browser_capability(raw), 'browser_session', consume=False)
        member = service._member(user_id)
        if payload.get('profile_id') != member['profile_id']:
            raise PermissionError
        _live_browser_expiry(payload.get('browser_expires_at'))
        return member, payload

    async def browser_proxy(request):
        raw = request.cookies.get('jobhunter_browser', '')
        member, payload = browser_session(raw)
        path = request.match_info['tail']
        # Reject residual encoding as well as dot segments: a second URL parser
        # must not turn an allowed asset path into a different upstream path.
        if not _VIEWER_PATH.fullmatch(path) or any(part in {'.', '..'} for part in path.split('/')):
            raise web.HTTPNotFound()
        websocket = request.headers.get('Upgrade', '').lower() == 'websocket'
        if websocket:
            if path != 'websockify':
                raise web.HTTPNotFound()
            if not service.public_url or request.headers.get('Origin') != service.public_url:
                raise PermissionError
        status = await asyncio.to_thread(service.browser_manager.status, member['profile_id'])
        if (status.get('status') != 'running' or status.get('profile_id') != member['profile_id']
                or _live_browser_expiry(status.get('expires_at')) != payload['browser_expires_at']):
            raise PermissionError
        port = status['viewer_port']
        if type(port) is not int or not 1024 <= port <= 65535:
            raise PermissionError
        url = f'http://127.0.0.1:{port}/{path}'
        if websocket:
            # ws_connect follows redirects internally. Abort its redirect
            # trace before any second request can leave the fixed loopback URL.
            redirects = TraceConfig()
            async def reject_redirect(session, context, params):
                params.response.close()
                raise PermissionError('Viewer upstream redirects are not permitted.')
            redirects.on_request_redirect.append(reject_redirect)
            async with ClientSession(timeout=ClientTimeout(total=20), trace_configs=[redirects]) as client:
                async with client.ws_connect(url, max_msg_size=8 * 1024 * 1024) as upstream:
                    downstream = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024)
                    await downstream.prepare(request)
                    async def forward(source, target):
                        async for message in source:
                            if message.type == WSMsgType.BINARY:
                                await target.send_bytes(message.data)
                            elif message.type == WSMsgType.TEXT:
                                await target.send_str(message.data)
                            else:
                                break
                    async def watch_access():
                        while True:
                            await asyncio.sleep(min(_BROWSER_ACCESS_POLL_SECONDS,
                                max(0, payload['browser_expires_at'] - time.time())))
                            browser_session(raw)
                    tasks = [asyncio.create_task(forward(upstream, downstream)),
                             asyncio.create_task(forward(downstream, upstream)), asyncio.create_task(watch_access())]
                    try:
                        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await downstream.close()
                    return downstream
        async with ClientSession(timeout=ClientTimeout(total=20)) as client:
            async with client.get(url, allow_redirects=False) as upstream:
                if 300 <= upstream.status < 400:
                    raise PermissionError('Viewer upstream redirects are not permitted.')
                body = await upstream.content.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError
                return web.Response(body=body, status=upstream.status,
                                    headers={'Content-Type': upstream.headers.get('Content-Type', 'application/octet-stream')})

    app.router.add_get('/health', health)
    app.router.add_post('/admin', admin)
    app.router.add_post('/internal/telegram', internal_telegram)
    app.router.add_get('/connect/google', google_start)
    app.router.add_get('/oauth/google/callback', google_callback)
    app.router.add_get('/connect/browser', browser_start)
    app.router.add_get('/browser/{tail:.*}', browser_proxy)
    return app
