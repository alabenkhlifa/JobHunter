"""Sanitized offline Greenhouse React-select regressions; all traffic is routed locally."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import ApplyConfig, AutoApplyEngine, inspect_page

pytestmark = pytest.mark.skipif(os.environ.get('JOBHUNTER_BROWSER_TESTS') != '1',
                                reason='requires Node Playwright and Chromium')
URL = 'https://job-boards.greenhouse.io/example/jobs/123456'
# Minimal structure from a sanitized selected widget. React removes the unnamed
# native required surrogate after selection; the combobox search buffer is empty.
SELECT = '''<div class="select"><div class="select__container">
<label id="answer-label" for="answer">Work authorization</label>
<div class="select-shell"><div class="select__control">
<div class="select__value-container select__value-container--has-value">
<div class="select__single-value">No</div>
<div class="select__input-container" data-value=""><input class="select__input"
 id="answer" role="combobox" aria-labelledby="answer-label" aria-required="true"
 aria-invalid="false" aria-autocomplete="list" value="" style="opacity:0"></div>
</div><button type="button" aria-label="Clear selections">Clear</button>
</div></div></div></div>'''
HTML = '''<!doctype html><title>Example Company - Lead Engineer</title>
<h1>Lead Engineer</h1><h2>Example Company</h2><form id="application">
<label>Name<input name="name" value="Example Applicant"></label>
<label>Email<input type="email" value="applicant@example.test"></label>''' + SELECT + '''
<button id="submit" type="button" onclick="document.body.dataset.clicked='yes'">Submit application</button></form>'''


def evaluate_html(html, expression, url=URL):
    result = subprocess.run(['node', str(Path(__file__).with_name('browser_application_inspection.cjs'))],
                            input=json.dumps({'html': html, 'url': url, 'expression': expression}),
                            text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def inspect_html(html=HTML, url=URL):
    client = Mock()
    client.evaluate.side_effect = lambda expression: evaluate_html(html, expression, url)
    return inspect_page(client)


def test_selected_single_value_is_present_despite_empty_search_buffer():
    page = inspect_html()
    answer = next(field for field in page.inputs if field['id'] == 'answer')
    assert answer['required'] and answer['value_present']
    assert not page.missing_required
    assert not page.invalid_fields
    assert page.verifies_application_for('Lead Engineer', 'Example Company')
    assert not page.verifies_application_for('Other Engineer', 'Example Company')
    assert not page.verifies_application_for('Lead Engineer', 'Other Company')


def submit_html(tmp_path, monkeypatch, html=HTML, approved=True):
    """Exercise click_submit's real JS and exact-role preflight, offline only."""
    clicks = []
    client = Mock()

    def evaluate(expression):
        if 'el.click();' in expression:
            observed = evaluate_html(html, '(() => { const result = (' + expression +
                                     "); return {result, clicked: document.body.dataset.clicked || ''}; })()")
            assert observed['clicked'] == ('yes' if observed['result'].get('ok') else '')
            clicks.append(observed)
            return observed['result']
        return evaluate_html(html, expression)

    client.evaluate.side_effect = evaluate
    engine = AutoApplyEngine(ApplyConfig(db_path=str(tmp_path / 'jobs.db'), tracker_sync=False,
                                        evidence_enabled=False, expected_page_url=URL), client)
    monkeypatch.setattr('jobhunter_auto_apply.engine._expected_job',
                        lambda *args: ('Lead Engineer', 'Example Company', None))
    monkeypatch.setattr('jobhunter_auto_apply.engine._expected_location', lambda *args: None)
    monkeypatch.setattr(engine, '_prefill_audit', lambda *args: {'issues': []})
    monkeypatch.setattr(engine, '_refuse_resubmit', lambda *args: None)
    monkeypatch.setattr('jobhunter_auto_apply.engine._record', Mock())
    monkeypatch.setattr('jobhunter_auto_apply.engine.complete_email_verification', Mock())
    monkeypatch.setattr('jobhunter_auto_apply.engine.time.sleep', lambda *args: None)
    monkeypatch.setattr(engine, 'inspect', Mock())
    engine.click_submit('fixture', '#submit', approved=approved)
    return clicks


def test_selected_single_value_passes_submit_time_validation(tmp_path, monkeypatch):
    assert submit_html(tmp_path, monkeypatch) == [{'result': {'ok': True}, 'clicked': 'yes'}]


@pytest.mark.parametrize('change', [
    'empty', 'placeholder', 'search_only', 'nearby_text', 'no_marker', 'wrong_container',
    'nested_unowned', 'unbound_label', 'ambiguous', 'hidden_choice', 'empty_surrogate',
])
def test_unselected_or_unowned_choices_remain_missing_and_cannot_submit(tmp_path, monkeypatch, change):
    html = HTML
    choice = '<div class="select__single-value">No</div>'
    if change == 'empty': html = html.replace(choice, '')
    if change == 'placeholder': html = html.replace(choice, '<div class="select__placeholder">Select...</div>')
    if change == 'search_only': html = html.replace(choice, '').replace('value="" style=', 'value="No" style=')
    if change == 'nearby_text': html = html.replace(choice, '<div>No</div>')
    if change == 'no_marker': html = html.replace(' select__value-container--has-value', '')
    if change == 'wrong_container':
        html = html.replace(choice, '').replace('</form>', '</form><div class="select">' + choice + '</div>')
    if change == 'nested_unowned': html = html.replace(choice, '<div class="select">' + choice + '</div>')
    if change == 'unbound_label': html = html.replace('for="answer"', 'for="other"')
    if change == 'ambiguous': html = html.replace(choice, choice + choice)
    if change == 'hidden_choice': html = html.replace('class="select__single-value"', 'class="select__single-value" hidden')
    if change == 'empty_surrogate':
        html = html.replace('</form>', '<input required tabindex="-1" style="opacity:0;width:1px;height:1px"></form>')
    page = inspect_html(html)
    assert page.missing_required
    with pytest.raises(PermissionError, match='required or invalid fields remain'):
        submit_html(tmp_path, monkeypatch, html)


def test_selected_choice_does_not_override_aria_invalid(tmp_path, monkeypatch):
    html = HTML.replace('aria-invalid="false"', 'aria-invalid="true"')
    page = inspect_html(html)
    assert not page.missing_required
    assert page.invalid_fields == ['Work authorization']
    with pytest.raises(PermissionError, match='required or invalid fields remain'):
        submit_html(tmp_path, monkeypatch, html)


def test_selected_choice_does_not_override_native_required_invalidity(tmp_path, monkeypatch):
    html = HTML.replace('aria-required="true"', 'aria-required="true" required')
    assert inspect_html(html).invalid_fields == ['Work authorization']
    with pytest.raises(PermissionError, match='required or invalid fields remain'):
        submit_html(tmp_path, monkeypatch, html)


def test_selected_choice_still_requires_final_approval(tmp_path, monkeypatch):
    with pytest.raises(PermissionError, match='submit requires explicit approval'):
        submit_html(tmp_path, monkeypatch, approved=False)


@pytest.mark.parametrize('change', ['challenge', 'wrong_role', 'wrong_employer'])
def test_selected_choice_cannot_bypass_preflight(tmp_path, monkeypatch, change):
    from jobhunter_auto_apply.cdp import CDPError
    html = HTML
    if change == 'challenge': html += '<iframe src="https://example.test/recaptcha" width="300" height="200"></iframe>'
    if change == 'wrong_role': html = html.replace('Lead Engineer', 'Other Engineer')
    if change == 'wrong_employer': html = html.replace('Example Company', 'Other Company')
    with pytest.raises(CDPError, match='verification blocker|exact role and employer'):
        submit_html(tmp_path, monkeypatch, html)


@pytest.mark.parametrize('host', ['job-boards.greenhouse.io', 'job-boards.eu.greenhouse.io', 'greenhouse.io.example.test'])
def test_selected_choice_is_limited_to_greenhouse_hosts(host):
    page = inspect_html(url=URL.replace('job-boards.greenhouse.io', host))
    assert bool(page.missing_required) is (host == 'greenhouse.io.example.test')


def test_five_independent_selected_widgets_and_populated_surrogate():
    widgets = ''.join(SELECT.replace('answer', 'answer' + str(index))
                      .replace('Work authorization', label).replace('>No</div>', '>' + value + '</div>')
                      for index, (label, value) in enumerate([
                          ('Country', '+999'), ('City', 'Example City'),
                          ('Work authorization', 'No'), ('English', 'Yes, fluent'), ('Consent', 'I agree')]))
    html = HTML.replace(SELECT, widgets)
    page = inspect_html(html)
    assert not page.missing_required and not page.invalid_fields
    fields = [item for item in page.inputs if item['id'].startswith('answer')]
    assert len(fields) == 5 and all(item['value_present'] for item in fields)
    # A populated native surrogate alone never proves a stored React choice.
    html = HTML.replace('<div class="select__single-value">No</div>', '').replace(
        '</form>', '<input required value="No" tabindex="-1" style="opacity:0;width:1px;height:1px"></form>')
    assert inspect_html(html).missing_required == ['Work authorization']
