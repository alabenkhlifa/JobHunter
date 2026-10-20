"""Real DOM checks for manual Easy Apply and SuccessFactors; no live uploads."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import inspect_page
from jobhunter_auto_apply.answers import page_hard_stops, LINKEDIN_EASY_APPLY


pytestmark = pytest.mark.skipif(os.environ.get('JOBHUNTER_BROWSER_TESTS') != '1',
                                reason='requires Node Playwright and Chromium')


def inspect_html(html, url):
    def evaluate(expression):
        result = subprocess.run(['node', str(Path(__file__).with_name('browser_application_inspection.cjs'))],
                                input=json.dumps({'html': html, 'url': url, 'expression': expression}),
                                text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    client = Mock()
    client.evaluate.side_effect = evaluate
    return inspect_page(client)


LINKEDIN = '''<!doctype html><title>Senior Java Developer | Example Consulting | LinkedIn</title>
<h1>Unrelated recommended role</h1><input name="backgroundEmail" type="email" required>
<dialog open><header><h2>Apply to Example Consulting</h2></header>
<div data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply">
<p>Select or upload a resume in PDF format</p><button>Upload resume</button><button>Next</button>
</div></dialog><input type="file" accept="application/pdf" style="display:none">'''


def test_linkedin_resume_step_binds_exact_job_company_dialog_and_external_file():
    page = inspect_html(LINKEDIN, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    assert not page.verifies_application_for('Junior Java Developer', 'Example Consulting', job_id='li-123456')
    assert not page.verifies_application_for('Senior Java Developer', 'Other Company', job_id='li-123456')
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-999999')
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting')
    assert [field['type'] for field in page.inputs] == ['file']
    assert page.inputs[0]['form'] == 'linkedin-easy-apply'
    assert page.inputs[0]['label'] == 'Resume'
    assert page.headings == ['Apply to Example Consulting']
    assert not page.missing_required  # Background required fields are outside the application.
    assert any(stop['reason'] == LINKEDIN_EASY_APPLY for stop in page_hard_stops(page))


def test_linkedin_ambiguous_external_file_controls_are_not_exposed_as_uploads():
    page = inspect_html(LINKEDIN + '<input type="file" accept="application/pdf" style="display:none">',
                        'https://www.linkedin.com/jobs/view/123456/')
    assert not any(field['type'] == 'file' for field in page.inputs)


@pytest.mark.parametrize('company', ['Example Consulting | Part of Example Group', 'Example España&amp;Portugal'])
def test_linkedin_employer_with_separator_or_encoded_ampersand(company):
    html = LINKEDIN.replace('Example Consulting', company)
    if '&amp;' in company:
        # Some LinkedIn page titles display an HTML entity literally, while the dialog decodes it.
        html = html.replace('<title>Senior Java Developer | ' + company,
                            '<title>Senior Java Developer | ' + company.replace('&amp;', '&amp;amp;'))
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.verifies_application_for('Senior Java Developer', company.replace('&amp;', '&'), job_id='li-123456')


@pytest.mark.parametrize('change', ['different_company', 'no_easy_apply_marker', 'hidden_dialog',
                                   'second_dialog', 'wrong_host', 'wrong_path', 'no_continuation'])
def test_linkedin_does_not_accept_source_pages_or_unbound_modals(change):
    html = LINKEDIN
    url = 'https://www.linkedin.com/jobs/view/123456/'
    if change == 'different_company': html = html.replace('Apply to Example Consulting', 'Apply to Other Company')
    if change == 'no_easy_apply_marker': html = html.replace('com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply', 'settings')
    if change == 'hidden_dialog': html = html.replace('<dialog open>', '<dialog open hidden>')
    if change == 'second_dialog': html += '<dialog open>Another application</dialog>'
    if change == 'wrong_host': url = 'https://linkedin.example.test/jobs/view/123456/'
    if change == 'wrong_path': url = 'https://www.linkedin.com/jobs/search/'
    if change == 'no_continuation': html = html.replace('<button>Next</button>', '<button>Dismiss</button>')
    page = inspect_html(html, url)
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')


@pytest.mark.parametrize('phase', ['contact', 'questions', 'review'])
def test_linkedin_contact_questions_and_final_review_remain_bound_to_job(phase):
    content = {
        'contact': '<label>Email<input type="email" required></label><label>Phone<input type="tel" required></label><button>Next</button>',
        'questions': '<label>Java experience<input type="number" required></label><button>Review</button>',
        'review': '<h3>Review your application</h3><p>Resume.pdf</p><button>Submit application</button>',
    }[phase]
    html = LINKEDIN.replace('<p>Select or upload a resume in PDF format</p><button>Upload resume</button><button>Next</button>', content)
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    assert not any(field['type'] == 'file' for field in page.inputs)
    assert bool(page.missing_required) is (phase != 'review')


SWISS = '''<!doctype html><title>Career Opportunities: Apply for Lead Software Engineer (123456)</title>
<a href="https://careers.swissre.com/">Swiss Re</a>
<form id="careerform"><h1>Lead Software Engineer (123456)</h1>
<input type="hidden" id="career_company" value="SwissRe">
<input type="hidden" id="career_job_req_id" value="123456">
<input type="hidden" id="jobReqId" value="123456">
<label>First Name<input name="firstName" aria-required="true"></label>
<label>Mobile Phone<input name="cellPhone" aria-required="true"></label>
<span role="button" id="apply">Apply</span></form>'''


def test_successfactors_requisition_heading_requires_matching_tenant_and_brand():
    page = inspect_html(SWISS, 'https://career2.successfactors.eu/portalcareer')
    assert page.verifies_application_for('Lead Software Engineer', 'Swiss Re')
    assert not page.verifies_application_for('Senior Software Engineer', 'Swiss Re')
    assert not page.verifies_application_for('Lead Software Engineer', 'Other Insurer')
    assert page.missing_required == ['First Name', 'Mobile Phone']
    assert next(button for button in page.buttons if button['text'] == 'Apply')['form'] == 'careerform'


@pytest.mark.parametrize('change', ['wrong_tenant', 'mismatched_requisition', 'missing_brand',
                                   'hidden_brand', 'wrong_host', 'no_apply'])
def test_successfactors_unverified_tenant_or_requisition_stays_blocked(change):
    html = SWISS
    url = 'https://career2.successfactors.eu/portalcareer'
    if change == 'wrong_tenant': html = html.replace('value="SwissRe"', 'value="OtherTenant"')
    if change == 'mismatched_requisition': html = html.replace('id="jobReqId" value="123456"', 'id="jobReqId" value="999999"')
    if change == 'missing_brand': html = html.replace('careers.swissre.com', 'example.test')
    if change == 'hidden_brand': html = html.replace('<a href=', '<a hidden href=')
    if change == 'wrong_host': url = 'https://successfactors.example.test/portalcareer'
    if change == 'no_apply': html = html.replace('>Apply</span>', '>Save</span>')
    page = inspect_html(html, url)
    assert not page.verifies_application_for('Lead Software Engineer', 'Swiss Re')
