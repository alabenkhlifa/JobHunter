"""Exercise production submit guards on offline DOMs, without ever clicking."""
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import AutoApplyEngine, ApplyConfig
from jobhunter_auto_apply.cdp import CDPError
from test_teamtailor_form_browser import HTML, URL, ROLE, EXTERNAL_REQUIRED_CONTROLS, pytestmark


class GuardResult(Exception):
    """Stop at the DOM guard, before clicking or post-submit side effects."""


def submit_guard(monkeypatch, html, selector='#apply', approved=True, guard_html=None):
    def evaluate(expression, rendered_html=None):
        if expression == 'location.href':
            return URL
        result = subprocess.run(['node', str(Path(__file__).with_name('browser_application_inspection.cjs'))],
            input=json.dumps({'html': html if rendered_html is None else rendered_html, 'url': URL, 'expression': expression}),
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    client = Mock()
    client.evaluate.side_effect = evaluate
    engine = AutoApplyEngine(ApplyConfig(expected_page_url=URL, tracker_sync=False, evidence_enabled=False), client)
    monkeypatch.setattr(engine, '_refuse_resubmit', lambda *_: None)
    monkeypatch.setattr(engine, '_prefill_audit', lambda *_: {'issues': []})
    monkeypatch.setattr('jobhunter_auto_apply.engine._expected_job', lambda *_: (ROLE, 'knowmad mood', None))
    monkeypatch.setattr('jobhunter_auto_apply.engine._expected_location', lambda *_: None)
    monkeypatch.setattr('jobhunter_auto_apply.engine._record', lambda *_, **__: None)

    def guard(_job_id, _client, expression):
        # Execute the production guard; replace only its final click, then abort
        # Python before recording an attempt, email verification, or sleeping.
        assert 'el.click();' in expression
        expression = expression.replace('el.click();', 'return {ok:true, guard_passed:true};')
        raise GuardResult(evaluate(expression, guard_html))

    monkeypatch.setattr(engine, '_submit_evaluate', guard)
    with pytest.raises(GuardResult) as result:
        engine.click_submit('sj-537a7e8ce4', selector, approved=approved)
    return result.value.args[0]


@pytest.mark.parametrize('override', [
    'formaction="https://other.example.test/collect"',
    'formaction="/login"',
    'formmethod="get"',
    'formmethod="dialog"',
    'formtarget="_blank"',
    'formtarget="other-window"',
])
def test_teamtailor_submit_guard_rejects_conflicting_submitter_destination(monkeypatch, override):
    html = HTML.replace('id="apply"', f'id="apply" {override}')
    assert submit_guard(monkeypatch, html)['ok'] is False


@pytest.mark.parametrize('override', [
    'formaction="/applications"', 'formmethod="post"', 'formtarget="_self"', 'formtarget=""',
])
def test_teamtailor_submit_guard_accepts_equivalent_submitter_destination(monkeypatch, override):
    html = HTML.replace('id="apply"', f'id="apply" {override}')
    assert submit_guard(monkeypatch, html) == {'ok': True, 'guard_passed': True}


@pytest.mark.parametrize('disabled', ['control', 'fieldset'])
def test_teamtailor_submit_guard_rechecks_enabled_requisition(monkeypatch, disabled):
    requisition = '''<input type="hidden" id="candidate_job_applications_attributes_0_job_id"
 name="candidate[job_applications_attributes][0][job_id]" value="8622923">'''
    replacement = (requisition.replace('<input ', '<input disabled ') if disabled == 'control'
                   else f'<fieldset disabled>{requisition}</fieldset>')
    assert submit_guard(monkeypatch, HTML, guard_html=HTML.replace(requisition, replacement))['ok'] is False


@pytest.mark.parametrize('login', ['value=""', 'value="employee" aria-invalid="true"'])
def test_teamtailor_submit_guard_ignores_unrelated_login(monkeypatch, login):
    assert submit_guard(monkeypatch, HTML.replace('value="employee"', login)) == {'ok': True, 'guard_passed': True}


@pytest.mark.parametrize('markup,name,label', EXTERNAL_REQUIRED_CONTROLS)
@pytest.mark.parametrize('value', ['', 'Confirmed answer'])
def test_teamtailor_submit_guard_rechecks_external_required_controls(monkeypatch, markup, name, label, value):
    # Insert only at guard time so inspection cannot mask a guard bypass.
    result = submit_guard(monkeypatch, HTML, guard_html=HTML + markup.format(value=value))
    assert result['ok'] is bool(value)
    if value:
        assert result == {'ok': True, 'guard_passed': True}


@pytest.mark.parametrize('change', ['missing_name', 'invalid_email', 'missing_resume', 'missing_consent', 'invalid_aria'])
def test_teamtailor_submit_guard_blocks_real_application_errors(monkeypatch, change):
    html = HTML
    if change == 'missing_name': html = html.replace('value="Example"', 'value=""')
    if change == 'invalid_email': html = html.replace('value="candidate@example.test"', 'value="invalid"')
    if change == 'missing_resume': html = html.replace('class="dz-max-files-reached"', 'class=""')
    if change == 'missing_consent': html = html.replace('required checked', 'required')
    if change == 'invalid_aria': html = html.replace('value="Example"', 'value="Example" aria-invalid="true"')
    assert submit_guard(monkeypatch, html)['ok'] is False


@pytest.mark.parametrize('selector', ['#employee-login button', '#apply, #employee-login button'])
def test_teamtailor_submit_guard_refuses_unowned_or_ambiguous_selector(monkeypatch, selector):
    assert submit_guard(monkeypatch, HTML, selector)['ok'] is False


def test_teamtailor_submit_still_requires_separate_approval(monkeypatch):
    with pytest.raises(PermissionError, match='explicit approval'):
        submit_guard(monkeypatch, HTML, approved=False)


@pytest.mark.parametrize('challenge', ['<p>Verify your phone</p>', '<iframe src="https://captcha-delivery.com/challenge" width="300" height="200"></iframe>'])
def test_teamtailor_challenge_outside_form_still_blocks_submit(monkeypatch, challenge):
    with pytest.raises(CDPError, match='verification blocker'):
        submit_guard(monkeypatch, HTML + challenge)
