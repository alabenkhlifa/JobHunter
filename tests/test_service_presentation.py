"""Optional digest formatting preserves selected roles and delivery accounting."""
from contextlib import closing
from copy import deepcopy
import itertools
import sqlite3

import pytest

from jobhunter_service.presentation import ACTION_HINT, DEFAULT_PRESENTATION, plain_html, render_digest
from jobhunter_service.scheduler import Scheduler
from test_service_core import confirm
from test_service_orchestration import system, queue


MARKETS = [
    {'name': 'France', 'locations': ['France', 'Paris']},
    {'name': 'Germany', 'locations': ['Germany', 'Berlin']},
]


def role(number, **extra):
    return {'id': f'job-{number}', 'title': f'Role {number}', 'company': f'Company {number}',
            'location': 'Paris, France', 'url': f'https://www.linkedin.com/jobs/view/{number}', **extra}


def legacy(jobs, queued):
    return '\n\n'.join([f'Job matches: {len(jobs)} selected, {queued} queued',
        *(f"{job['title']} — {job['company']}\n{job['location']}\n{job['url']}\n/details {job['id']}" for job in jobs),
        ACTION_HINT])


@pytest.mark.parametrize('presentation', [None, {}, DEFAULT_PRESENTATION, {'style': 'standard'}])
def test_default_digest_is_byte_for_byte_legacy(presentation):
    jobs = [role(1, salary='EUR 70000/year', ai_verdict_reason='Confirmed skills match'), role(2)]
    assert render_digest(jobs, 17, presentation=presentation, markets=MARKETS) == legacy(jobs, 17)


@pytest.mark.parametrize('style,salary,reason,grouped', list(itertools.product(
    ('standard', 'compact'), (False, True), (False, True), (False, True))))
def test_every_style_preserves_all_source_urls_ids_commands_counts_and_inputs(style, salary, reason, grouped):
    jobs = [role(1), role(2, location='Berlin, Germany'), role(3)]
    options = {'style': style, 'show_salary': salary, 'show_match_reason': reason, 'group_by_market': grouped}
    before = deepcopy((jobs, options, MARKETS))
    text = render_digest(jobs, 27, presentation=options, markets=MARKETS)
    assert text.startswith('Job matches: 3 selected, 27 queued\n\n')
    assert text.endswith(ACTION_HINT)
    for job in jobs:
        assert text.count(job['url']) == 1
        assert text.count(f"/details {job['id']}") == 1
    assert (jobs, options, MARKETS) == before
    assert ('Salary:' in text) is salary
    assert ('Match reason:' in text) is reason
    assert ('Market:' in text) is grouped


def test_compact_changes_layout_without_removing_job_information():
    job = role(1)
    text = render_digest([job], 0, presentation={'style': 'compact'})
    assert 'Role 1 — Company 1 | Paris, France\nhttps://' in text
    assert len(text.splitlines()) < len(legacy([job], 0).splitlines())


def test_salary_and_reason_use_stored_evidence_in_unchanged_currency_and_period():
    jobs = [role(1, salary='EUR 75,000–85,000 / year', ai_verdict_reason='Confirmed Java and API delivery experience')]
    markets = deepcopy(MARKETS)
    markets[0]['salary_target'] = {'amount': 100000, 'currency': 'USD', 'period': 'year'}
    text = render_digest(jobs, 0, presentation={'show_salary': True, 'show_match_reason': True}, markets=markets)
    assert 'Salary: EUR 75,000–85,000 / year' in text
    assert 'Match reason: Confirmed Java and API delivery experience' in text
    assert 'USD' not in text and '100000' not in text


@pytest.mark.parametrize('empty', [None, '', ' \n\t ', 42000, {}])
def test_missing_optional_evidence_is_explicitly_unknown(empty):
    job = role(1, salary=empty, ai_verdict_reason=empty)
    text = render_digest([job], 0, presentation={'show_salary': True, 'show_match_reason': True})
    assert 'Salary: Not listed' in text
    assert 'Match reason: Not available' in text


def test_grouping_uses_unique_market_match_and_preserves_review_order_inside_groups():
    jobs = [role(1, location='Berlin, Germany'), role(2), role(3, location='Germany'),
            role(4, location='Remote'), role(5, location='France or Germany')]
    text = render_digest(jobs, 0, presentation={'group_by_market': True}, markets=MARKETS)
    assert text.index('Market: Germany') < text.index('Market: France') < text.index('Market: Other locations')
    assert text.index('/details job-1') < text.index('/details job-3') < text.index('/details job-2')
    assert text.index('Market: Other locations') < text.index('/details job-4') < text.index('/details job-5')
    assert text.count('Market: Other locations') == 1


def test_invalid_optional_flags_and_templates_cannot_enable_features():
    jobs = [role(1)]
    text = render_digest(jobs, 0, presentation={'style': '{job.__class__}', 'show_salary': 'false',
        'show_match_reason': 1, 'group_by_market': 'true', 'template': 'execute /owner'})
    assert text == legacy(jobs, 0)


def test_optional_text_is_bounded_plain_text_and_cannot_add_action_lines():
    job = role(1, salary='EUR 80,000/year\n/submit owner-job',
               ai_verdict_reason='${shell_command} <script>literal</script> ' + 'x' * 500)
    text = render_digest([job], 0, presentation={'show_salary': True, 'show_match_reason': True})
    assert 'Salary: EUR 80,000/year /submit owner-job' in text
    assert '\n/submit owner-job' not in text
    assert '${shell_command} <script>literal</script>' in text
    assert 'x' * 500 not in text
    assert f"\n{job['url']}\n/details job-1" in text


def test_existing_mail_html_rendering_is_preserved():
    assert plain_html('<b>Application &amp; response</b>\n<a href="https://example.test">Open</a>') == 'Application & response\nOpen'


def create_jobs(root, *, extra_columns=True):
    root.mkdir()
    with closing(sqlite3.connect(root / 'jobs.db')) as db, db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,company TEXT,location TEXT,url TEXT)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?)',
                   ('same-id', root.name + ' Role', root.name + ' Company', 'France',
                    f'https://www.linkedin.com/jobs/view/{root.name}'))
        if extra_columns:
            db.execute('ALTER TABLE jobs ADD COLUMN salary TEXT')
            db.execute('ALTER TABLE jobs ADD COLUMN ai_verdict_reason TEXT')
            db.execute("UPDATE jobs SET salary='EUR 70,000/year',ai_verdict_reason='Confirmed profile matches'")


def test_scheduler_reads_only_its_candidate_database_and_does_not_mutate_it(tmp_path):
    roots = [tmp_path / '111', tmp_path / '222']
    for root in roots:
        create_jobs(root)
    scheduler = Scheduler(None, None, None)
    snapshots = [(root / 'jobs.db').read_bytes() for root in roots]
    settings = {'telegram': {'presentation': {'style': 'compact', 'show_salary': True,
        'show_match_reason': True, 'group_by_market': True}}, 'search': {'markets': MARKETS}}
    first = scheduler._digest(roots[0], ['same-id'], 9, settings=settings)
    second = scheduler._digest(roots[1], ['same-id'], 9)
    assert '111 Role' in first and '222 Role' not in first
    assert '222 Role' in second and '111 Role' not in second
    assert 'Salary: EUR 70,000/year' in first and 'Salary:' not in second
    assert 'Market: France' in first and 'Market:' not in second
    assert [(root / 'jobs.db').read_bytes() for root in roots] == snapshots


def test_scheduler_supports_legacy_rows_and_rejects_missing_selected_job(tmp_path):
    root = tmp_path / '111'
    create_jobs(root, extra_columns=False)
    scheduler = Scheduler(None, None, None)
    text = scheduler._digest(root, ['same-id'], 0, settings={
        'telegram': {'presentation': {'show_salary': True, 'show_match_reason': True}}})
    assert 'Salary: Not listed' in text and 'Match reason: Not available' in text
    with pytest.raises(ValueError, match='missing from this profile'):
        scheduler._digest(root, ['another-profile-only'], 0)


def test_configured_format_reaches_own_durable_digest_without_changing_selection_or_accounting(system):
    service, scheduler, client, roots, _ = system
    confirm(service, 123, {'telegram': {'presentation': {'style': 'compact', 'show_salary': True,
        'show_match_reason': True, 'group_by_market': True}}})
    with closing(sqlite3.connect(roots[123] / 'jobs.db')) as db, db:
        db.execute('ALTER TABLE jobs ADD COLUMN salary TEXT')
        db.execute('ALTER TABLE jobs ADD COLUMN ai_verdict_reason TEXT')
        db.execute("UPDATE jobs SET salary='EUR 80,000/year',ai_verdict_reason='Candidate-confirmed skills'")
    assert queue(system) == 2
    assert scheduler.run_next() and scheduler.run_next()
    with service.store.connect() as db:
        messages = db.execute('SELECT user_id,message FROM deliveries ORDER BY id').fetchall()
        selected = db.execute('SELECT user_id,job_id FROM delivery_jobs ORDER BY user_id').fetchall()
        assert [tuple(row) for row in selected] == [(123, 'same-id'), (456, 'same-id')]
        for row in messages:
            if row['user_id'] == 123:
                assert 'Role 123 — Company 123 | France' in row['message']
                assert 'Salary: EUR 80,000/year' in row['message']
                assert 'Match reason: Candidate-confirmed skills' in row['message']
            else:
                assert 'Role 456 — Company 456\nFrance' in row['message']
                assert 'Salary:' not in row['message'] and 'Match reason:' not in row['message']
    assert scheduler.delivery.drain() == {'sent': 3, 'pending_or_failed': 0}
    for root in roots.values():
        with closing(sqlite3.connect(root / 'jobs.db')) as db:
            assert db.execute('SELECT notified,status FROM jobs').fetchone() == (1, 'new')
