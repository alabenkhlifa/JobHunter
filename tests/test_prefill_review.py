"""Untrusted ATS extraction cannot replace approved employment or current location."""
import copy
import json
import sqlite3

import pytest

from jobhunter_auto_apply import prefill
from jobhunter_auto_apply.cdp import BrowserTimeoutError, CDPError
from jobhunter_auto_apply.engine import ApplyConfig, AutoApplyEngine


URL = 'https://careers.clarivate.com/apply?jobSeqNo=CLACLAGBJREQ123EXTERNALENGB&step=2&stepname=workAndEducation'
RESUME = {'experience': [
    {'company': 'Consultancy One', 'title': 'Lead Engineer', 'dates': 'October 2020 - Present',
     'bullets': ['Delivered services for Example Client.']},
    {'company': 'Consultancy Two', 'title': 'Software Engineer', 'dates': '08/2019 - 09/2020',
     'bullets': ['Built an enterprise product.']}],
    'education': [{'school': 'Example School', 'degree': "Master's Degree"}]}
PROFILE = {'phone': '+216 20 123 456', 'location': 'Relocating to Spain',
           'application_defaults': {'current_work_location': 'Tunis, Tunisia'},
           'experience': [{'company': 'Consultancy One', 'engagements': [{'client': 'Example Client'}]},
                          {'company': 'Consultancy Two'}],
           'education': [{'school': 'Example School', 'degree': "Master's Degree"}]}


def snapshot():
    return {'adapter': 'clarivate-v1', 'url': URL, 'country': None, 'phone_country': None,
            'work': [{'prefix': 'experienceData[0]', 'company': 'Consultancy One', 'title': 'Lead Engineer',
                      'start': '10/2020', 'end': None, 'current': True},
                     {'prefix': 'experienceData[1]', 'company': 'Consultancy Two', 'title': 'Software Engineer',
                      'start': '08/2019', 'end': '09/2020', 'current': False}],
            'education': [{'prefix': 'educationData[0]', 'school': 'Example School', 'degree': "Master's Degree"}],
            'errors': []}


def test_correct_extraction_is_accepted_without_rewriting_sources():
    before = copy.deepcopy((RESUME, PROFILE))
    result = prefill.audit(snapshot(), RESUME, PROFILE)
    assert result['supported'] and result['issues'] == []
    assert (RESUME, PROFILE) == before


def test_client_and_product_names_are_reconciled_to_employers_by_confirmed_period():
    data = snapshot()
    duplicate = {**data['work'][0], 'prefix': 'experienceData[2]', 'company': 'Example Client', 'title': ''}
    data['work'].insert(1, duplicate)
    data['work'][2]['company'] = 'Enterprise Product'
    result = prefill.audit(data, RESUME, PROFILE)
    assert result['manual_review'] == []
    assert set(result['issues']) == {'client_parsed_as_duplicate_employer', 'parsed_employer_mismatch'}
    assert any(op['kind'] == 'remove' for op in result['operations'])
    assert any(op.get('value') == 'Consultancy Two' for op in result['operations'])


def test_current_country_and_phone_override_target_location_without_changing_legal_answers():
    data = snapshot()
    data.update(country='Spain', phone_country='Spain (+34)', phone_options=['Spain (+34)', 'Tunisia (+216)'],
                previous_employee='Yes')
    result = prefill.audit(data, RESUME, PROFILE, {'previous_clarivate_employee_or_contractor': False})
    assert result['manual_review'] == []
    assert {op['value'] for op in result['operations']} == {'Tunisia', 'Tunisia (+216)', 'No'}
    assert all('jsqData' not in op['selector'] and 'agreement' not in op['selector'] for op in result['operations'])


def test_relocation_text_is_never_treated_as_confirmed_residence():
    data = snapshot(); data['country'] = 'Spain'
    profile = {**PROFILE, 'application_defaults': {}}
    result = prefill.audit(data, RESUME, profile)
    assert result['manual_review'] == ['confirmed_current_country_missing']
    assert result['operations'] == []


@pytest.mark.parametrize('change,expected', [('extra_employer', 'parsed_employer_requires_review'),
    ('unconfirmed_duplicate', 'duplicate_work_entry_requires_review'),
    ('new_school', 'different_school_requires_review'), ('unknown_error', 'site_validation_errors_visible')])
def test_uncertain_parser_output_stays_for_review(change, expected):
    data = snapshot()
    if change == 'extra_employer':
        data['work'].append({'prefix': 'experienceData[2]', 'company': 'Unknown Employer'})
    if change == 'unconfirmed_duplicate':
        data['work'].append({**data['work'][0], 'prefix': 'experienceData[2]', 'company': 'Unknown Employer'})
    if change == 'new_school': data['education'][0]['school'] = 'Different University'
    if change == 'unknown_error': data['errors'] = ['Confirm a legal declaration']
    assert expected in prefill.audit(data, RESUME, PROFILE)['manual_review']


def test_empty_school_and_extra_blank_row_are_repairable_but_degree_is_never_invented():
    data = snapshot(); data['education'][0].update(school='', degree='')
    data['education'].append({'prefix': 'educationData[1]', 'school': '', 'degree': '', 'gpa': ''})
    result = prefill.audit(data, RESUME, PROFILE)
    assert not result['manual_review']
    assert set(result['issues']) == {'parsed_school_missing', 'parsed_degree_mismatch', 'empty_parser_education_entry'}
    unknown = {**RESUME, 'education': [{'school': 'Example School', 'degree': 'Engineering Diploma'}]}
    result = prefill.audit(data, unknown, {**PROFILE, 'education': []})
    assert 'degree_level_requires_confirmation' in result['manual_review']


def test_description_limit_uses_only_complete_approved_bullets():
    data = snapshot(); data['work'][0]['description_length'] = 3000
    resume = copy.deepcopy(RESUME)
    resume['experience'][0]['bullets'] = ['A'*1200, 'B'*1100, 'C'*100]
    result = prefill.audit(data, resume, PROFILE)
    operation = next(op for op in result['operations'] if 'description' in op['selector'])
    assert operation['value'] == 'A'*1200+'\n'+'C'*100


@pytest.mark.parametrize('url', ['https://careers.clarivate.com.example.test/apply',
                               'http://careers.clarivate.com/apply'])
def test_untrusted_origins_do_not_get_repairs(url):
    data = snapshot(); data['url'] = url
    assert not prefill.audit(data, RESUME, PROFILE)['supported']


class Browser:
    def __init__(self, data):
        self.data = data
        self.changes = []
        self.frame_path = ()

    def evaluate(self, expression, **kwargs):
        if expression.strip() == 'location.href': return URL
        if expression == prefill.SNAPSHOT_JS: return copy.deepcopy(self.data)
        return {'url': URL, 'title': 'Apply', 'headings': ['Lead Engineer (JREQ123)'],
                'inputs': [{'tag': 'input', 'type': 'email', 'label': 'Email', 'form': 'apply'},
                           {'tag': 'input', 'type': 'text', 'label': 'Name', 'form': 'apply'}],
                'buttons': [{'text': 'Next Step', 'type': 'submit', 'form': 'apply'}],
                'prefill_snapshot': copy.deepcopy(self.data)}

    def fill(self, selector, value):
        self.changes.append((selector, value))
        if 'companyName' in selector: self.data['work'][1]['company'] = value
        else: raise AssertionError('unexpected field repair')

    def click(self, selector):
        raise AssertionError('no continuation or final-submit action is permitted in this fixture')


def engine(tmp_path, data):
    root = tmp_path/'package'; root.mkdir()
    (root/'resume.json').write_text(json.dumps(RESUME))
    (root/'tailoring_manifest.json').write_text(json.dumps({'job_id': 'job-1'}))
    profile = tmp_path/'profile.json'; profile.write_text(json.dumps(PROFILE))
    db = tmp_path/'jobs.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,company TEXT)')
        conn.execute("INSERT INTO jobs VALUES('job-1','Lead Engineer','Clarivate')")
        import scraper
        scraper.record_application_stage(conn, 'job-1', 'package_generated', package_path=str(root), sync=False)
    browser = Browser(data)
    result = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path/'output'),
        profile_path=str(profile), expected_page_url=URL, evidence_enabled=False, tracker_sync=False), client=browser)
    return result, browser, root


def test_inspection_blocks_bad_prefill_and_approved_repair_leaves_documents_unchanged(tmp_path):
    data = snapshot(); data['work'][1]['company'] = 'Enterprise Product'
    service, browser, root = engine(tmp_path, data)
    original = (root/'resume.json').read_bytes()
    page = service.inspect('job-1')
    assert page.prefill_issues == ['parsed_employer_mismatch']
    assert not page.safe_to_continue
    assert service.reconcile_prefill('job-1')['issues'] and not browser.changes
    result = service.reconcile_prefill('job-1', approved=True)
    assert not result['issues'] and len(browser.changes) == 1
    assert (root/'resume.json').read_bytes() == original
    assert list((tmp_path/'output').rglob('prefill-before-*.json'))


def test_uncertain_prefill_prevents_next_or_submit_before_any_action(tmp_path):
    data = snapshot(); data['work'][1]['company'] = 'Enterprise Product'
    service, browser, _ = engine(tmp_path, data)
    with pytest.raises(CDPError, match='CV parser output'):
        service.advance('job-1', '#next', approved=True)
    with pytest.raises(CDPError, match='CV parser output'):
        service.click_submit('job-1', 'button[type=submit]', approved=True)
    assert not browser.changes


def test_uncertain_submit_is_persisted_and_cannot_be_repeated(tmp_path):
    service, browser, _ = engine(tmp_path, snapshot())
    original = browser.evaluate
    def uncertain(expression, **kwargs):
        if 'el.click()' in expression: raise BrowserTimeoutError('outcome unknown')
        return original(expression, **kwargs)
    browser.evaluate = uncertain
    with pytest.raises(BrowserTimeoutError):
        service.click_submit('job-1', 'button[type=submit]', approved=True)
    with sqlite3.connect(service.config.db_path) as conn:
        assert conn.execute('SELECT stage FROM applications').fetchone()[0] == 'submission_attempted'
    service.inspect('job-1')
    with sqlite3.connect(service.config.db_path) as conn:
        stage, notes = conn.execute('SELECT stage,notes FROM applications').fetchone()
        assert stage == 'submission_attempted'
        assert json.loads(notes)['submit_outcome_unknown'] is True
    with pytest.raises(PermissionError, match='never resubmitted'):
        service.click_submit('job-1', 'button[type=submit]', approved=True)
