"""Real DOM checks for manual ATS forms; no live uploads."""
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


@pytest.mark.parametrize('role,company', [
    ('SENIOR SOLUTION ARCHITECT | AZURE', 'Capitole'),
    ('SENIOR SOLUTION ARCHITECT | AZURE', 'Example Consulting | Part of Example Group'),
])
def test_linkedin_role_separator_is_distinguished_from_employer_separator(role, company):
    html = LINKEDIN.replace('Senior Java Developer', role).replace('Example Consulting', company)
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.application_context['role'] == role
    assert page.application_context['company'] == company
    assert page.verifies_application_for(role, company, job_id='li-123456')
    assert not page.verifies_application_for('SENIOR SOLUTION ARCHITECT', company, job_id='li-123456')
    assert not page.verifies_application_for(role, 'AZURE | ' + company, job_id='li-123456')


def test_infortec_reordered_seniority_still_requires_exact_role_and_employer():
    html = '''<!doctype html><title>Infortec Consultores | Full Stack Developer Senior</title>
    <h1>Full Stack Developer Senior</h1><form>
    <label>Full Name<input name="name"></label><label>Email<input type="email"></label>
    <button type="submit">Enviar candidatura</button></form>'''
    page = inspect_html(html, 'https://empleo.infortec.net/jobs/full-stack-developer-senior-example')
    assert page.verifies_application_for('Senior Full Stack Developer', 'Infortec Consultores')
    assert not page.verifies_application_for('Junior Full Stack Developer', 'Infortec Consultores')
    assert not page.verifies_application_for('Senior Full Stack Developer', 'Other Consultancy')
    assert not page.verifies_application_for('Senior Stack Full Developer', 'Infortec Consultores')


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


def linkedin_choices(*, checked=False, kind='radio'):
    options = ['Basic', 'Intermediate', 'Professional', 'Fluent', 'Native'] if kind == 'radio' else ['Acknowledge']
    rows = ''.join(f'''<div class="choice"><div><input id="choice-{i}" name="level" type="{kind}"
        aria-label="English level" style="width:0;height:0;appearance:none" {'checked' if checked and i == 3 else ''}>
        <label for="choice-{i}" style="display:inline-block;width:18px;height:18px"></label></div>
        <div><p>{option}</p></div></div>''' for i, option in enumerate(options))
    content = f'<div class="question"><p>English level*</p><fieldset role="radiogroup"><div>{rows}</div></fieldset></div><button>Back</button><button>Next</button>'
    return LINKEDIN.replace('<p>Select or upload a resume in PDF format</p><button>Upload resume</button><button>Next</button>', content)


def test_linkedin_zero_size_radios_use_owned_rendered_labels_and_group_question():
    page = inspect_html(linkedin_choices(), 'https://www.linkedin.com/jobs/view/123456/')
    assert page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    assert len(page.inputs) == 5
    assert [f['label'] for f in page.inputs] == ['Basic', 'Intermediate', 'Professional', 'Fluent', 'Native']
    assert all(f['question'] == 'English level*' and f['required'] and not f['value_present']
               and f['form'] == 'linkedin-easy-apply' for f in page.inputs)
    assert page.missing_required == ['English level*']
    assert not page.safe_to_continue
    assert any(s['reason'] == LINKEDIN_EASY_APPLY for s in page_hard_stops(page))
    assert not page.verifies_application_for('Other Engineer', 'Example Consulting', job_id='li-123456')
    assert not page.verifies_application_for('Senior Java Developer', 'Other Employer', job_id='li-123456')
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-999999')


@pytest.mark.parametrize('source', ['background', 'hidden_peer', 'disabled_peer', 'other_group'])
def test_linkedin_radio_group_cannot_be_satisfied_by_unowned_checked_peer(source):
    html = linkedin_choices()
    peer = '<input type="radio" name="level" checked>'
    if source == 'background':
        html += peer
    elif source == 'hidden_peer':
        html = html.replace('</fieldset>', peer.replace(' checked', ' style="display:none" checked') + '</fieldset>')
    elif source == 'disabled_peer':
        html = html.replace('</fieldset>', peer.replace(' checked', ' disabled checked') + '</fieldset>')
    else:
        html = html.replace('<button>Back</button>', '<fieldset>' + peer + '</fieldset><button>Back</button>')
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.missing_required == ['English level*']
    assert not any(f['value_present'] for f in page.inputs if f['id'].startswith('choice-'))


def test_native_same_name_radio_group_spans_fieldsets_in_one_form():
    html = '''<!doctype html><form>
    <fieldset><legend>First option</legend><label>Yes<input type="radio" name="answer" required></label></fieldset>
    <fieldset><legend>Second option</legend><label>No<input type="radio" name="answer" checked></label></fieldset>
    <button type="submit">Apply</button></form>'''
    page = inspect_html(html, 'https://careers.example.com/apply')
    assert len(page.inputs) == 2
    assert all(field['value_present'] for field in page.inputs)
    assert not page.missing_required
    assert not page.invalid_fields


def test_linkedin_owned_checked_radio_satisfies_only_its_question():
    page = inspect_html(linkedin_choices(checked=True), 'https://www.linkedin.com/jobs/view/123456/')
    assert all(f['value_present'] for f in page.inputs)
    assert not page.missing_required


@pytest.mark.parametrize('change', ['hidden_label', 'hidden_group', 'hidden_input', 'disabled', 'disabled_fieldset',
                                   'duplicate_id', 'extra_control', 'second_dialog', 'no_next', 'background_only'])
def test_linkedin_choice_proxies_do_not_make_hidden_or_ambiguous_forms_actionable(change):
    html = linkedin_choices()
    if change == 'hidden_label': html = html.replace('display:inline-block', 'display:none')
    if change == 'hidden_group': html = html.replace('<fieldset ', '<fieldset hidden ')
    if change == 'hidden_input': html = html.replace('width:0;', 'display:none;width:0;')
    if change == 'disabled': html = html.replace('<input id="choice-', '<input disabled id="choice-')
    if change == 'disabled_fieldset': html = html.replace('<fieldset ', '<fieldset disabled ')
    if change == 'duplicate_id': html += ''.join(f'<input hidden id="choice-{i}">' for i in range(5))
    if change == 'extra_control': html = html.replace('<div class="choice">', '<div class="choice"><input type="hidden">')
    if change == 'second_dialog': html += '<dialog open>Unrelated dialog</dialog>'
    if change == 'no_next': html = html.replace('<button>Next</button>', '')
    if change == 'background_only':
        start, end = html.index('<div class="question">'), html.index('<button>Back</button>')
        html = html[:start] + html[end:] + html[start:end]
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    if change not in {'second_dialog', 'no_next'}:
        assert not any(f['type'] in {'radio', 'checkbox'} for f in page.inputs)


@pytest.mark.parametrize('kind', ['radio', 'checkbox'])
@pytest.mark.parametrize('attribute', ['inert', 'aria-hidden="true"', 'hidden'])
@pytest.mark.parametrize('ancestor', [False, True])
def test_linkedin_choice_proxy_rejects_unavailable_label_ancestry(kind, attribute, ancestor):
    html = linkedin_choices(kind=kind)
    if ancestor:
        html = html.replace('<label for=', f'<span {attribute} style="display:block"><label for=').replace('</label>', '</label></span>')
    else:
        html = html.replace('<label for=', f'<label {attribute} for=')
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert not page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    assert not any(f['type'] in {'radio', 'checkbox'} for f in page.inputs)


@pytest.mark.parametrize('change', ['missing_star', 'hidden_prompt', 'inert_prompt', 'aria_hidden_prompt',
                                   'rendered_hidden_prompt', 'unrelated_prompt', 'ambiguous_prompt'])
def test_linkedin_required_inference_requires_one_visible_matching_question(change):
    html = linkedin_choices()
    if change == 'missing_star': html = html.replace('English level*</p>', 'English level</p>')
    if change == 'hidden_prompt': html = html.replace('<p>English level*', '<p hidden>English level*')
    if change == 'inert_prompt': html = html.replace('<p>English level*', '<p inert>English level*')
    if change == 'aria_hidden_prompt': html = html.replace('<p>English level*', '<p aria-hidden="true">English level*')
    if change == 'rendered_hidden_prompt': html = html.replace('<p>English level*', '<p hidden style="display:block">English level*')
    if change == 'unrelated_prompt': html = html.replace('<p>English level*', '<p>Other question*')
    if change == 'ambiguous_prompt': html = html.replace('<p>English level*', '<p>Another question*</p><p>English level*')
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert len(page.inputs) == 5
    assert not any(f['required'] for f in page.inputs)


@pytest.mark.parametrize('checked', [False, True])
def test_linkedin_zero_size_checkbox_retains_its_own_checked_validation(checked):
    html = linkedin_choices(kind='checkbox')
    if checked: html = html.replace('type="checkbox"', 'type="checkbox" checked')
    page = inspect_html(html, 'https://www.linkedin.com/jobs/view/123456/')
    assert page.verifies_application_for('Senior Java Developer', 'Example Consulting', job_id='li-123456')
    assert len(page.inputs) == 1
    assert page.inputs[0]['label'] == 'Acknowledge'
    assert page.inputs[0]['value_present'] is checked
    assert page.missing_required == ([] if checked else ['English level*'])


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


@pytest.mark.parametrize('host,tenant,company,brand,heading,role,submit', [
    ('career2.successfactors.eu', 'SF2013', 'Zurich Insurance', 'www.careers.zurich.com',
     'Career Opportunities: Lead Solution Architect', 'Lead Solution Architect', 'Apply'),
    ('career5.successfactors.eu', 'gfttechnol', 'GFT Technologies', 'www.gft.com',
     'Puestos vacantes: Desarrollador/a Java Microservicios', 'Desarrollador/a Java Microservicios', 'Solicitar'),
])
def test_successfactors_zurich_and_gft_registration_application_forms(host, tenant, company, brand, heading, role, submit):
    html = f'''<!doctype html><title>{heading} (123456)</title>
    <a href="https://{brand}/">Careers</a><form id="careerform">
    <h1>{heading} (123456)</h1>
    <input type="hidden" id="career_company" value="{tenant}">
    <input type="hidden" id="career_job_req_id" value="123456">
    <table><tr><th><input type="hidden" id="career_job_req_id" value="123456"></th></tr></table>
    <label>Correo electrónico: *<input id="fbclc_userName" type="text"></label>
    <label>Nombre: *<input id="fbclc_fName" type="text"></label>
    <label>Apellidos: *<input id="fbclc_lName" type="text"></label>
    <input type="submit" id="fbqa_apply" value="{submit}"></form>'''
    url = f'https://{host}/careers?company={tenant}'
    page = inspect_html(html, url)
    assert page.verifies_application_for(role, company)
    assert page.application_context['requisition'] == '123456'
    assert page.missing_required == ['Correo electrónico: *', 'Nombre: *', 'Apellidos: *']
    assert not page.safe_to_continue
    assert not page.verifies_application_for('Other Engineer', company)
    assert not page.verifies_application_for(role, 'Other Employer')

    changes = [
        (html, url.replace('company=' + tenant, 'company=OtherTenant')),
        (html, url + '&company=' + tenant),
        (html, url.replace('https:', 'http:')),
        (html, url.replace(host, host + '.example.test')),
        (html, url.replace(host, host + ':8443')),
        (html, url.replace('/careers?', '/profile?')),
        (html.replace('value="' + tenant + '"', 'value="OtherTenant"'), url),
        (html.replace('https://' + brand, 'https://' + brand + '.example.test'), url),
        (html.replace('<a href=', '<a hidden href='), url),
        (html.replace('id="career_job_req_id" value="123456"', 'id="career_job_req_id" value="999999"'), url),
        (html.replace('id="career_job_req_id" value="123456"', 'id="career_job_req_id" value="999999"', 1), url),
        (html + '<form><input type="hidden" id="career_job_req_id" value="123456"></form>', url),
        (html.replace('<h1>', '<h1 hidden>'), url),
        (html.replace('</form>', '<h2>Other Engineer (999999)</h2></form>'), url),
        (html.replace('</form>', '<input type="hidden" id="jobReqId" value="999999"></form>'), url),
        (html.replace('id="fbqa_apply"', 'id="save"').replace('type="submit"', 'type="button"'), url),
        (html.replace('<h1>', '</form><h1>').replace('</h1>', '</h1><form>'), url),
    ]
    for changed_html, changed_url in changes:
        assert not inspect_html(changed_html, changed_url).verifies_application_for(role, company), changed_url


CLARIVATE = '''<!doctype html><title>Apply</title>
<form id="application"><h1>Lead Software Engineer (JREQ136480)</h1>
<label>First Name<input name="firstName" required></label>
<label>Email<input type="email" required></label>
<button type="submit">Next</button></form><h2>follow us</h2>'''
CLARIVATE_URL = 'https://careers.clarivate.com/apply?jobSeqNo=CLACLAGBJREQ136480EXTERNALENGB&step=1'


def test_clarivate_rendered_requisition_form_without_separate_brand_heading():
    page = inspect_html(CLARIVATE, CLARIVATE_URL)
    assert page.headings == ['Lead Software Engineer (JREQ136480)', 'follow us']
    assert page.verifies_application_for('Lead Software Engineer', 'Clarivate')
    assert page.missing_required == ['First Name', 'Email']
    assert not page.safe_to_continue


@pytest.mark.parametrize('change', ['hidden_heading', 'wrong_role', 'wrong_reference', 'wrong_host',
                                   'missing_brand', 'missing_query', 'no_form'])
def test_clarivate_rendered_unbound_form_is_not_verified(change):
    html = CLARIVATE
    url = CLARIVATE_URL
    if change == 'hidden_heading': html = html.replace('<h1>', '<h1 hidden>')
    if change == 'wrong_role': html = html.replace('Lead Software Engineer', 'Senior Software Engineer')
    if change == 'wrong_reference': html = html.replace('JREQ136480', 'JREQ999999')
    if change == 'wrong_host': url = url.replace('careers.clarivate.com', 'careers.clarivate.com.example.test')
    if change == 'missing_brand': url = url.replace('careers.clarivate.com', 'jobs.phenom.com')
    if change == 'missing_query': url = url.split('?')[0]
    if change == 'no_form': html = html.replace('<button type="submit">Next</button>', '')
    page = inspect_html(html, url)
    assert not page.verifies_application_for('Lead Software Engineer', 'Clarivate')


@pytest.mark.parametrize('step,name,content', [
    ('2', 'workAndEducation', '<label>Job Title<input id="experienceData[0].title" required></label><label>School<input id="educationData[0].schoolName" required></label><button id="next">Next Step</button>'),
    ('3', 'jobSpecificQuestions', '<label>Work authorization<select id="jsqData.test" required><option>No</option></select></label><button id="next">Next Step</button>'),
    ('4', 'voluntaryInformation', '<label>Consent<input id="agreementCheck" type="checkbox" required></label><button id="next">Next Step</button>'),
    ('5', 'applicationReview', '<h2>My Information</h2><h2>My Experience</h2><h3>Work Experience</h3><h3>Education</h3><button type="submit">Submit</button>'),
])
def test_clarivate_later_steps_require_exact_step_identity_and_owned_controls(step, name, content):
    html = f'<!doctype html><title>Apply</title><form><h1>Lead Software Engineer (JREQ136480)</h1>{content}<button id="previous" type="button">Previous Step</button></form>'
    url = CLARIVATE_URL.split('&step=')[0] + f'&step={step}&stepname={name}'
    page = inspect_html(html, url)
    assert page.verifies_application_for('Lead Software Engineer', 'Clarivate')
    assert not page.verifies_application_for('Other Engineer', 'Clarivate')
    assert not inspect_html(html, url.replace('JREQ136480EXTERNAL', 'JREQ999EXTERNAL')).verifies_application_for('Lead Software Engineer', 'Clarivate')
    assert not inspect_html(html.replace('id="previous"', 'id="unrelated"'), url).verifies_application_for('Lead Software Engineer', 'Clarivate')


def test_clarivate_dom_snapshot_captures_bad_prefill_without_contact_details():
    from jobhunter_auto_apply.prefill import audit
    html = '''<!doctype html><title>Apply</title><h1>Lead Software Engineer (JREQ136480)</h1>
      <form><input id="experienceData[0].companyName" value="Enterprise Product">
      <input id="experienceData[0].title" value="Engineer">
      <input id="experienceData[0].fromTo.startDate" value="08/2019">
      <input id="experienceData[0].fromTo.endDate" value="09/2020">
      <input id="experienceData[0].fromTo.currentlyWorkHere" type="checkbox">
      <input name="email" type="email" value="private@example.test"><button>Next Step</button></form>'''
    page = inspect_html(html, CLARIVATE_URL)
    assert 'private@example.test' not in json.dumps(page.prefill_snapshot)
    result = audit(page.prefill_snapshot, {'experience': [{'company': 'Consultancy', 'title': 'Engineer',
                   'dates': '08/2019 - 09/2020'}]}, {})
    assert result['issues'] == ['parsed_employer_mismatch']


def test_clarivate_empty_select_placeholders_are_not_parsed_education_facts():
    html = '<form><input id="educationData[0].schoolName"><select id="educationData[0].degree"><option value="">Please select</option></select><select id="educationData[0].fieldOfStudy"><option value="">Please select</option></select></form>'
    page = inspect_html(html, CLARIVATE_URL)
    row = page.prefill_snapshot['education'][0]
    assert row['degree'] == row['field'] == ''
