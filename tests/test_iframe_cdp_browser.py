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


@pytest.mark.parametrize('mode', ['hidden', 'cross_origin', 'ambiguous', 'nested', 'challenge', 'passive_badge'])
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
    if mode in {'challenge', 'passive_badge'}:
        routes['/candidate'] += '<p>Protected by hCaptcha Privacy | Terms</p>'
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
    if mode == 'passive_badge':
        assert not inspection.blockers


@pytest.mark.parametrize('hidden', [False, True])
def test_visible_custom_dropdown_exposes_its_required_backing_select(browser, hidden):
    open_page, routes, _ = browser
    widget = '''<label for="country">Country</label><select id="country" style="display:none" i_required="true">
      <option value="-999">Please select a country</option><option value="TN">Tunisia</option></select>
      <a id="country_icimsDropdown" role="combobox" aria-required="true">Please select a country</a>'''
    if hidden:
        widget = '<div hidden>' + widget + '</div>'
    routes['/candidate'] = FORM.replace('</form>', widget + '</form>')
    client = open_page('<!doctype html><iframe id="form" src="/candidate" width="800" height="600"></iframe>')
    inspection = inspect_page(client)
    country = next((item for item in inspection.inputs if item['id'] == 'country'), None)
    if hidden:
        assert country is None
        assert 'Country' not in inspection.missing_required
    else:
        assert country['widget_id'] == 'country_icimsDropdown'
        assert country['required'] and not country['value_present']
        assert 'Country' in inspection.missing_required
        client.evaluate("document.getElementById('country').value = 'TN'")
        assert 'Country' not in inspect_page(client).missing_required


@pytest.mark.parametrize('change,uploaded', [('none', True), ('hidden', False), ('missing_id', False),
                                            ('wrong_label', False), ('other_form', False)])
def test_stored_upload_requires_a_visible_filename_and_matching_form_metadata(browser, change, uploaded):
    open_page, routes, _ = browser
    stored = '<input type="hidden" id="Resume" value="synthetic-file-id">'
    metadata = stored + '''<input type="hidden" id="Resume_FileName" value="Synthetic CV.txt">
      <div id="Resume_CurrentFile"><span id="Resume_FileNameLabel">Synthetic CV.txt</span></div>'''
    html = FORM.replace('for="cv"', 'for="Resume_File"').replace('id="cv"', 'id="Resume_File"')
    html = html.replace('</form>', metadata + '</form>')
    if change == 'hidden':
        html = html.replace('id="Resume_CurrentFile"', 'id="Resume_CurrentFile" hidden')
    if change == 'missing_id':
        html = html.replace('value="synthetic-file-id"', 'value=""')
    if change == 'wrong_label':
        html = html.replace('id="Resume_FileNameLabel">Synthetic CV.txt', 'id="Resume_FileNameLabel">Other.txt')
    if change == 'other_form':
        html = html.replace(stored, '') + stored
    routes['/candidate'] = html
    client = open_page('<!doctype html><iframe id="form" src="/candidate" width="800" height="600"></iframe>')
    inspection = inspect_page(client)
    control = next(item for item in inspection.inputs if item['id'] == 'Resume_File')
    assert control['value_present'] is uploaded
    assert control['uploaded_file_name'] == ('Synthetic CV.txt' if uploaded else '')
    assert ('CV' in inspection.missing_required) is not uploaded


@pytest.mark.parametrize('change,required', [('none', False), ('missing_country', True),
                                           ('other_placeholder', True), ('available_states', True)])
def test_unavailable_dependent_dropdown_requires_matching_parent_evidence(browser, change, required):
    open_page, routes, _ = browser
    widget = '''<select id="-1_PersonProfileFields.Country" name="-1_PersonProfileFields.Country"
      data-ddd-child-link="PersonProfileFields.State" data-ddd-no-children="No states available">
      <option value="TN">Tunisia</option></select>
      <label for="-1_PersonProfileFields.State">County/State</label>
      <select id="-1_PersonProfileFields.State" name="-1_PersonProfileFields.State" style="display:none"
      data-ddd-parent-link="PersonProfileFields.Country" i_required="true">
      <option value="-999">No states available</option></select>
      <a id="-1_PersonProfileFields.State_icimsDropdown" role="combobox" aria-required="true">No states available</a>'''
    if change == 'missing_country': widget = widget.replace('value="TN"', 'value=""')
    if change == 'other_placeholder': widget = widget.replace('value="-999">No states available', 'value="-999">Please select')
    if change == 'available_states': widget = widget.replace('value="-999">No states available</option>', 'value="-999">No states available</option><option value="X">Example</option>')
    routes['/candidate'] = FORM.replace('</form>', widget + '</form>')
    client = open_page('<!doctype html><iframe id="form" src="/candidate" width="800" height="600"></iframe>')
    inspection = inspect_page(client)
    control = next(item for item in inspection.inputs if item['id'] == '-1_PersonProfileFields.State')
    assert bool(control['required']) is required
    assert ('County/State' in inspection.missing_required) is required
