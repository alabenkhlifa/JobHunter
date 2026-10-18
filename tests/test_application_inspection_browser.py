"""Opt-in DOM regression for CV-first application methods; no real site or uploads."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import inspect_page


pytestmark = pytest.mark.skipif(os.environ.get('JOBHUNTER_BROWSER_TESTS') != '1',
                                reason='requires Node Playwright and Chromium')


@pytest.mark.parametrize('reveal,disabled,continue_button,expected', [
    (False, False, True, False), (True, False, True, True),
    (True, True, True, False), (True, 'fieldset', True, False), (True, False, False, False),
])
def test_cv_step_requires_presented_file_control_and_its_own_continue(reveal, disabled, continue_button, expected):
    html = '''<!doctype html><title>Applying to Tech Leader | BNP Paribas</title>
    <h1>BNP Paribas</h1><h2>TECH LEADER</h2><h3>Choose your application method</h3>
    <form id="login"><label>EMAIL<input name="username"></label>
      <label>PASSWORD<input type="password"></label><button type="submit">LOG IN</button></form>
    <a id="method-cv" role="button" onclick="document.querySelector('fieldset').hidden=false">Upload your CV</a>
    <form id="cv"><fieldset hidden FIELDSET_DISABLED><label for="resumeFile">Please upload your CV</label>
      <input type="file" id="resumeFile" name="resumeFile" aria-required="true" style="display:none" DISABLED>
      CONTINUE
    </fieldset></form>'''.replace('FIELDSET_DISABLED', 'disabled' if disabled == 'fieldset' else '').replace(
        'DISABLED', 'disabled' if disabled is True else '').replace(
        'CONTINUE', '<button type="submit">Continue</button>' if continue_button else '')

    def evaluate(expression):
        result = subprocess.run(['node', str(Path(__file__).with_name('browser_application_inspection.cjs'))],
                                input=json.dumps({'html': html, 'expression': expression, 'reveal': reveal}),
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    client = Mock()
    client.evaluate.side_effect = evaluate
    inspection = inspect_page(client)
    assert inspection.verifies_application_for('Tech Leader', 'BNP Paribas') is expected
    if expected:
        upload = next(item for item in inspection.inputs if item['type'] == 'file')
        assert upload['form'] == 'cv' and upload['required']
        assert inspection.missing_required == ['Please upload your CV']
        assert not inspection.verifies_application_for('Other Role', 'BNP Paribas')
        assert not inspection.verifies_application_for('Tech Leader', 'Another Bank')
    if not reveal:
        assert not any(item['type'] == 'file' for item in inspection.inputs)
