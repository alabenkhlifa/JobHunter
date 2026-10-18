"""Offline Chromium regressions for inspected application frames and uploads."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from jobhunter_auto_apply.cdp import CDPClient, CDPError, list_targets
from jobhunter_auto_apply.engine import inspect_page


pytestmark = pytest.mark.skipif(os.environ.get('JOBHUNTER_BROWSER_TESTS') != '1',
                                reason='requires local Chromium')

FORM = '''<!doctype html><title>AXA Group Operations | Candidate Profile</title>
<h1>AXA Group Operations</h1><h2>Software Engineer Tech Lead</h2>
<form id="candidate"><label>Email<input type="email" name="email" required></label>
<label for="cv">CV</label><input id="cv" type="file" required>
<button type="submit">Continue</button></form>'''


@pytest.fixture
def browser(tmp_path):
    routes = {'/': '<!doctype html><title>AXA Group Operations</title>', '/candidate': FORM}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = routes.get(self.path, '<!doctype html>').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    executable = os.environ.get('JOBHUNTER_CHROMIUM_EXECUTABLE') or shutil.which('chromium')
    if not executable:
        executable = subprocess.check_output(
            ['node', '-p', "require('playwright').chromium.executablePath()"], text=True).strip()
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen([executable, '--headless=new', '--disable-gpu', '--no-first-run',
                                '--no-default-browser-check', '--disable-background-networking',
                                '--use-mock-keychain', '--password-store=basic',
                                '--remote-debugging-address=127.0.0.1', f'--remote-debugging-port={port}',
                                f'--user-data-dir={tmp_path / "browser"}', 'about:blank'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    clients = []
    base = f'http://127.0.0.1:{server.server_port}'

    def open_page(html):
        routes['/'] = html
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                target = next(t for t in list_targets(port=port) if t.type == 'page')
                break
            except (OSError, StopIteration):
                time.sleep(0.05)
        else:
            pytest.fail('Chromium CDP did not start')
        client = CDPClient(target.websocket_url)
        clients.append(client)
        client.call('Page.enable')
        client.call('Page.navigate', {'url': base + '/'})
        while time.monotonic() < deadline:
            if client.evaluate("document.readyState === 'complete' && location.href !== 'about:blank'"):
                return client
            time.sleep(0.05)
        pytest.fail('Synthetic application did not load')

    try:
        yield open_page, routes, base
    finally:
        for client in clients:
            client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_inspection_and_upload_use_the_same_embedded_form(browser, tmp_path):
    open_page, _, base = browser
    client = open_page('<!doctype html><iframe id="icims" src="/candidate" width="800" height="600"></iframe>')
    inspection = inspect_page(client)
    assert inspection.verifies_application_for('Software Engineer Tech Lead', 'AXA Group Operations')
    assert inspection.frame_path == ['#icims']
    assert inspection.url == base + '/'
    assert inspection.missing_required == ['Email', 'CV']
    assert not inspection.verifies_application_for('Other Role', 'AXA Group Operations')
    assert not inspection.verifies_application_for('Software Engineer Tech Lead', 'Other Employer')
    document = tmp_path / 'synthetic-cv.txt'
    document.write_text('Synthetic upload fixture')
    client.upload_file('#cv', str(document))
    assert client.evaluate("document.querySelector('#cv').files[0].name") == document.name
    assert client.evaluate('location.href') == base + '/'
    client.call('Runtime.evaluate', {'expression': "document.querySelector('#icims').hidden = true"})
    with pytest.raises(CDPError, match='no longer accessible'):
        client.evaluate("document.querySelector('#cv')")


@pytest.mark.parametrize('mode', ['hidden', 'cross_origin', 'ambiguous', 'nested', 'challenge'])
def test_frame_boundaries_and_challenges_remain_enforced(browser, mode):
    open_page, routes, base = browser
    attributes = 'hidden' if mode == 'hidden' else ''
    url = base.replace('127.0.0.1', 'localhost') + '/candidate' if mode == 'cross_origin' else '/candidate'
    html = f'<!doctype html><iframe id="first" src="{url}" width="800" height="600" {attributes}></iframe>'
    if mode == 'ambiguous':
        html += '<iframe id="second" src="/candidate" width="800" height="600"></iframe>'
    if mode == 'nested':
        routes['/wrapper'] = html
        html = '<!doctype html><iframe id="outer" src="/wrapper" width="900" height="700"></iframe>'
    if mode == 'challenge':
        routes['/candidate'] += '<iframe src="/captcha-delivery.com/challenge" width="300" height="200"></iframe>'
    client = open_page(html)
    inspection = inspect_page(client)
    if mode in {'hidden', 'cross_origin', 'ambiguous'}:
        assert not inspection.actionable_application_form
        assert not client.frame_path
    else:
        assert inspection.verifies_application_for('Software Engineer Tech Lead', 'AXA Group Operations')
        assert inspection.frame_path == (['#outer', '#first'] if mode == 'nested' else ['#first'])
    if mode == 'challenge':
        assert 'human verification' in inspection.blockers
        assert not inspection.safe_to_continue
    if mode == 'ambiguous':
        assert 'multiple embedded application forms' in inspection.blockers
