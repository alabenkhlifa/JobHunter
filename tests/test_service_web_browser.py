"""Opt-in HTTPS navigation checks with real Chromium and WebKit.

Run with JOBHUNTER_BROWSER_TESTS=1 and Playwright available to Node (NODE_PATH
can point to an existing installation). All users, cookies and pages are synthetic.
"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import time
from unittest.mock import Mock

from aiohttp import web
from aiohttp.test_utils import TestServer
import pytest

from jobhunter_service.service import JobHunterService
from jobhunter_service.web import create_app


pytestmark = pytest.mark.skipif(
    os.environ.get('JOBHUNTER_BROWSER_TESTS') != '1',
    reason='requires opt-in JOBHUNTER_BROWSER_TESTS=1, Node Playwright and browsers',
)


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_linkedin_signin_detection_with_real_dom(tmp_path, engine):
    from jobhunter_service.connections import LINKEDIN_SIGNIN_EXPRESSION
    modern = ('<nav><a href="/mynetwork">Network</a><a href="/messaging/">Messages</a>'
              '<a href="https://www.linkedin.com/notifications/?filter=all">Notifications</a></nav>')
    cases = [
        {'name': 'current navigation', 'path': '/feed/', 'html': modern, 'expected': True},
        {'name': 'role navigation', 'path': '/feed/',
         'html': modern.replace('<nav>', '<div role="navigation">').replace('</nav>', '</div>'), 'expected': True},
        {'name': 'legacy navigation', 'path': '/feed/', 'html': '<nav id="global-nav"><div class="global-nav__me"></div></nav>', 'expected': True},
        {'name': 'login password', 'path': '/feed/', 'html': modern + '<input type="password">', 'expected': False},
        {'name': 'content links', 'path': '/feed/', 'html': modern.replace('<nav>', '<article>').replace('</nav>', '</article>'), 'expected': False},
        {'name': 'partial navigation', 'path': '/feed/', 'html': '<nav><a href="/messaging/">Messages</a></nav>', 'expected': False},
        {'name': 'split navigation', 'path': '/feed/', 'html': '<nav><a href="/mynetwork">Network</a></nav><nav><a href="/messaging/">Messages</a><a href="/notifications/">Notifications</a></nav>', 'expected': False},
        {'name': 'foreign destination', 'path': '/feed/', 'html': modern.replace('href="/mynetwork"', 'href="https://linkedin.com.example.test/mynetwork"'), 'expected': False},
        {'name': 'verification challenge', 'path': '/checkpoint/challenge/', 'html': modern, 'expected': False},
        {'name': 'login page', 'path': '/login', 'html': modern, 'expected': False},
        {'name': 'auth wall', 'path': '/authwall', 'html': modern, 'expected': False},
        {'name': 'unknown interface', 'path': '/feed/', 'html': '<main>Public page</main>', 'expected': False},
    ]
    result = subprocess.run(['node', str(Path(__file__).with_name('browser_signin_check.cjs')), engine],
        input=json.dumps({'expression': LINKEDIN_SIGNIN_EXPRESSION, 'cases': cases}),
        text=True, capture_output=True, timeout=30, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize('engine', ['chromium', 'webkit'])
def test_fresh_browser_link_from_another_site_reaches_viewer(tmp_path, engine):
    assert shutil.which('node') and shutil.which('openssl')
    key, certificate = tmp_path / 'key.pem', tmp_path / 'certificate.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(certificate), '-days', '1',
                    '-subj', '/CN=localhost'], check=True, capture_output=True)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)

    async def scenario():
        upstream_app = web.Application()
        async def page(request):
            return web.Response(text='Synthetic candidate viewer', content_type='text/html')
        upstream_app.router.add_get('/vnc.html', page)
        async with TestServer(upstream_app) as upstream:
            browser = Mock()
            expiry = time.time() + 1800
            browser.start.side_effect = browser.status.side_effect = lambda profile: {
                'profile_id': profile, 'status': 'running',
                'viewer_port': upstream.port, 'expires_at': expiry,
            }
            service = JobHunterService(tmp_path / 'data', 1,
                                       public_url='https://jobs.example.test', browser_manager=browser)
            service.admin(1, 'add', 11)
            service.authorize(11)
            viewer = TestServer(create_app(service))
            await viewer.start_server(ssl=tls)
            try:
                service.public_url = str(viewer.make_url('')).rstrip('/')
                link = service.connect(11, 'linkedin')
                sender_app = web.Application()
                async def invitation(request):
                    return web.Response(text=f'<a href="{link}">Open browser</a>', content_type='text/html')
                sender_app.router.add_get('/', invitation)
                sender = TestServer(sender_app)
                await sender.start_server(ssl=tls)
                try:
                    # localhost and 127.0.0.1 are different sites to the browser.
                    sender_url = str(sender.make_url('/')).replace('127.0.0.1', 'localhost')
                    driver = Path(__file__).with_name('browser_link_check.cjs')
                    process = await asyncio.create_subprocess_exec(
                        'node', str(driver), engine, sender_url, link,
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    try:
                        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    assert process.returncode == 0, (stdout + stderr).decode()
                finally:
                    await sender.close()
            finally:
                await viewer.close()
            browser.start.assert_called_once_with('u11')
    asyncio.run(scenario())
