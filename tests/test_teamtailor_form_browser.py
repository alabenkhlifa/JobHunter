"""Offline Teamtailor DOM regressions; all browser requests are fulfilled locally."""
import pytest

from test_manual_application_forms_browser import inspect_html, pytestmark

EXTERNAL_REQUIRED_CONTROLS = [
    ('<label>Portfolio<input name="portfolio" required form="job-application-form" value="{value}"></label>', 'portfolio', 'Portfolio'),
    ('<div data-question-mandatory="true"><p>Architecture</p><input aria-label="Architecture" name="architecture" form="job-application-form" value="{value}"></div>', 'architecture', 'Architecture'),
]

URL = 'https://knowmadmood.teamtailor.com/jobs/8622923-technical-lead-java-sdd-remoto'
ROLE = 'Technical Lead Java SDD Remoto'
HTML = '''<!doctype html><title>Technical Lead Java SDD Remoto - knowmad mood</title>
<h1>Technical Lead Java SDD Remoto</h1>
<p>Supervisarás la integración continua y despliegues en entorno cloud.</p>
<form id="job-application-form" action="https://knowmadmood.teamtailor.com/applications" method="post">
<input type="hidden" id="candidate_job_applications_attributes_0_job_id"
 name="candidate[job_applications_attributes][0][job_id]" value="8622923">
<label>First name *<input name="candidate[first_name]" value="Example"></label>
<label>Email *<input type="email" name="candidate[email]" value="candidate@example.test"></label>
<div id="upload_resume_field" data-forms--inputs--upload-required-value="true" class="dz-max-files-reached">
<label for="candidate_resume_remote_url">Resume (Obligatorio)</label>
<div style="display:none"><input type="file" id="candidate_resume_remote_url"></div>
<div data-controller="forms--inputs--upload-preview" class="dz-success dz-complete">
<input type="text" style="display:none" id="candidate_resume_remote_url" name="candidate[resume_remote_url]"
 data-forms--inputs--upload-preview-target="urlInput" value="https://uploads.example.test/resume.pdf">
<a href="javascript:void(0);" data-forms--inputs--upload-preview-target="link">Resume.pdf</a></div></div>
<label>I accept the privacy policy<input type="checkbox" name="candidate[consent]" required checked></label>
<button type="submit" id="apply">Enviar candidatura</button></form>
<form id="employee-login"><label>Employee email<input name="full_email" type="email" required value="employee"></label>
<button type="submit">Log in</button></form>'''


def test_teamtailor_ignores_unrelated_employee_login_and_posting_prose():
    page = inspect_html(HTML, URL)
    assert page.verifies_application_for(ROLE, 'knowmad mood')
    assert not page.missing_required, page.missing_required
    assert not page.invalid_fields, page.invalid_fields
    assert not any('Supervisarás' in question for question in page.sensitive_questions)
    assert not any(item['name'] == 'full_email' for item in page.inputs)
    assert page.sensitive_questions == ['I accept the privacy policy']
    assert not page.verifies_application_for('Junior Java Developer', 'knowmad mood')
    assert not page.verifies_application_for(ROLE, 'Other Employer')


@pytest.mark.parametrize('markup,name,label', EXTERNAL_REQUIRED_CONTROLS)
@pytest.mark.parametrize('value', ['', 'Confirmed answer'])
def test_teamtailor_external_required_controls_are_inspected(markup, name, label, value):
    page = inspect_html(HTML + markup.format(value=value), URL)
    control = next((item for item in page.inputs if item['name'] == name), None)
    assert control is not None, page.inputs
    assert control['form'] == 'job-application-form'
    assert control['required'] is True
    assert control['value_present'] is bool(value)
    assert page.missing_required == ([] if value else [label])
    assert not any(item['name'] == 'full_email' for item in page.inputs)


@pytest.mark.parametrize('change', ['no_url', 'no_success', 'no_filename', 'duplicate_preview'])
def test_teamtailor_stale_or_ambiguous_resume_is_not_accepted(change):
    html = HTML
    if change == 'no_url': html = html.replace('value="https://uploads.example.test/resume.pdf"', 'value=""')
    if change == 'no_success': html = html.replace('class="dz-success dz-complete"', 'class=""')
    if change == 'no_filename': html = html.replace('>Resume.pdf</a>', '></a>')
    if change == 'duplicate_preview': html = html.replace('</a></div></div>', '</a></div><div data-controller="forms--inputs--upload-preview" class="dz-success dz-complete"><input type="text" style="display:none" name="candidate[resume_remote_url]" data-forms--inputs--upload-preview-target="urlInput" value="https://uploads.example.test/other.pdf"><a data-forms--inputs--upload-preview-target="link">Other.pdf</a></div></div>')
    assert 'Resume (Obligatorio)' in inspect_html(html, URL).missing_required


@pytest.mark.parametrize('peer', ['', 'hidden', 'disabled'])
def test_teamtailor_mandatory_radio_requires_presented_enabled_group_answer(peer):
    question = f'''<div data-question-mandatory="true"><fieldset><legend>Architecture *</legend>
    <label>Yes<input type="radio" name="architecture" value="yes"></label>
    <label>No<input type="radio" name="architecture" value="no" checked {peer}></label></fieldset></div>'''
    page = inspect_html(HTML.replace('<button type="submit" id="apply">', question + '<button type="submit" id="apply">'), URL)
    radios = [item for item in page.inputs if item['type'] == 'radio' and not item['disabled']]
    assert radios and all(item['required'] for item in radios)
    assert all(item['value_present'] is (not peer) for item in radios)


def test_teamtailor_required_labels_and_accepted_stored_resume():
    page = inspect_html(HTML, URL)
    resume = next(item for item in page.inputs if item['type'] == 'file')
    assert resume['required'] and resume['value_present']
    assert resume['uploaded_file_name'] == 'Resume.pdf'
    assert 'uploads.example.test' not in str(page.inputs)
    assert all(item['required'] for item in page.inputs)


@pytest.mark.parametrize('change,label', [
    ('name', 'First name *'), ('email', 'Email *'), ('resume', 'Resume (Obligatorio)'),
    ('consent', 'I accept the privacy policy'),
])
def test_teamtailor_missing_application_fields_stay_blocked(change, label):
    html = HTML
    if change == 'name': html = html.replace('value="Example"', 'value=""')
    if change == 'email': html = html.replace('value="candidate@example.test"', 'value=""')
    if change == 'resume': html = html.replace('class="dz-max-files-reached"', 'class=""')
    if change == 'consent': html = html.replace('required checked', 'required')
    page = inspect_html(html, URL)
    assert label in page.missing_required
    assert not page.safe_to_continue


@pytest.mark.parametrize('disabled', ['control', 'fieldset'])
def test_teamtailor_disabled_requisition_fails_closed(disabled):
    requisition = '''<input type="hidden" id="candidate_job_applications_attributes_0_job_id"
 name="candidate[job_applications_attributes][0][job_id]" value="8622923">'''
    replacement = (requisition.replace('<input ', '<input disabled ') if disabled == 'control'
                   else f'<fieldset disabled>{requisition}</fieldset>')
    page = inspect_html(HTML.replace(requisition, replacement), URL)
    assert not page.verifies_application_for(ROLE, 'knowmad mood')


@pytest.mark.parametrize('change', ['wrong_action', 'wrong_req', 'duplicate_form', 'hidden_form', 'no_form', 'duplicate_req'])
def test_teamtailor_unbound_form_fails_closed(change):
    html = HTML
    if change == 'wrong_action': html = html.replace('/applications"', '/login"')
    if change == 'wrong_req': html = html.replace('value="8622923"', 'value="99999"')
    if change == 'duplicate_form': html += '<form id="job-application-form">Other application</form>'
    if change == 'hidden_form': html = html.replace('id="job-application-form"', 'id="job-application-form" hidden')
    if change == 'no_form': html = html.replace('id="job-application-form"', 'id="other-form"')
    if change == 'duplicate_req': html += '<input type="hidden" name="candidate[job_applications_attributes][0][job_id]" value="8622923">'
    assert not inspect_html(html, URL).verifies_application_for(ROLE, 'knowmad mood')


@pytest.mark.parametrize('markup', [
    '<label>Salary expectation<input form="job-application-form" name="answer" value="50000"></label>',
    '<div data-question-mandatory="true"><p>Salary expectation</p><input form="job-application-form" name="answer" value="50000"></div>',
    '<div data-question-mandatory="false"><p>Salary expectation</p><input form="job-application-form" name="answer" value="50000"></div>',
])
@pytest.mark.parametrize('outside', [False, True])
def test_teamtailor_owned_sensitive_questions_include_containers_and_external_controls(markup, outside):
    html = (HTML + markup if outside else
            HTML.replace('<button type="submit" id="apply">', markup + '<button type="submit" id="apply">'))
    page = inspect_html(html, URL)
    assert 'Salary expectation' in page.sensitive_questions
    assert not page.safe_to_continue
    assert not any('Supervisarás' in question for question in page.sensitive_questions)


@pytest.mark.parametrize('question', ['Work authorization', 'Visa sponsorship', 'Salary expectation', 'Disability', 'Criminal background check'])
def test_teamtailor_genuine_sensitive_questions_remain_visible(question):
    html = HTML.replace('<button type="submit" id="apply">', f'<label>{question}<input name="answer"></label><button type="submit" id="apply">')
    page = inspect_html(html, URL)
    assert question in page.sensitive_questions
    assert not page.safe_to_continue
