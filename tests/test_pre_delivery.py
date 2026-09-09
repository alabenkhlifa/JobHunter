"""Actual source evidence, approved queue refill and final Telegram boundaries."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import json
import sqlite3
import pytest
import requests

import jobhunter_availability as availability
from jobhunter_availability import check as check_source
from jobhunter_delivery import select_available
import scraper
from test_job_availability import Transport, Response, page


@pytest.fixture
def database(tmp_path, monkeypatch):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config.update(db_path=str(tmp_path / 'jobs.db'), score_threshold=45)
    monkeypatch.setattr(scraper, 'CONFIG', config)
    conn = scraper.init_db()
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def insert(conn, number, location='Dubai', *, status='new', notified=0, verdict='send', score=80, days=2, sponsorship='implied'):
    job_id = f'li-{number}'
    date = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn.execute('INSERT INTO jobs(id,title,company,location,url,source,score,date_scraped,date_posted,description,status,notified,ai_verdict,ai_sponsorship,ai_rank) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (job_id, 'Backend Engineer', 'Example Team', location, f'https://www.linkedin.com/jobs/view/{number}',
         'LinkedIn', score, date, date, 'Java Spring backend architecture with 5 years experience.', status, notified, verdict, sponsorship, 1))
    conn.commit()
    return dict(conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())


def source_responses(monkeypatch, *, closed=(), unknown=()):
    calls = []
    def check(job):
        calls.append(job['id'])
        number = job['id'].split('-')[-1]
        response = (Response(status=429) if job['id'] in unknown else
                    Response(page(identifier=number, status='No longer accepting applications' if job['id'] in closed else '')))
        return check_source(job, Transport(response))
    monkeypatch.setattr(availability, 'check', check)
    return calls


def test_today_dubai_jobs_backfill_all_other_markets_from_approved_queue(database, monkeypatch):
    for number in range(100, 103):
        insert(database, number, days=0)
    for number, market in enumerate(('Abu Dhabi', 'Jeddah', 'Riyadh', 'Zurich, Switzerland'), 200):
        insert(database, number, market, days=5, score=60)
    calls = source_responses(monkeypatch)
    sent = []
    monkeypatch.setattr(scraper, 'send_telegram', lambda token, chat, text: sent.append(text) or True)
    written = scraper.record_review(database, [{'job_id': f'li-{number}', 'verdict': 'send', 'sponsorship': 'implied',
                                              'reason': 'Confirmed match', 'rank': number - 99} for number in range(100, 103)])
    report = scraper.send_reviewed_digest('synthetic', 'private', database, written)
    assert report['sent'] == 7 and len(calls) == 7
    assert 'No matches' not in sent[0]
    assert all(name in sent[0] for name in ('DUBAI', 'ABU DHABI', 'JEDDAH', 'RIYADH', 'SWITZERLAND'))
    assert database.execute('SELECT COUNT(*) FROM jobs WHERE notified=1').fetchone()[0] == 7


def test_explicitly_closed_choice_is_replaced_with_older_open_job_in_same_market(database, monkeypatch):
    closed = insert(database, 100, 'Abu Dhabi', days=0)
    replacement = insert(database, 101, 'Abu Dhabi', days=4, score=60)
    source_responses(monkeypatch, closed=[closed['id']])
    selected, report = select_available(database, scraper.reviewed_queue(database), per_market=1, cap=1)
    assert [job['id'] for job in selected] == [replacement['id']]
    assert report['closed'] == 1
    assert tuple(database.execute("SELECT status,notified FROM jobs WHERE id='li-100'").fetchone()) == ('unavailable', 0)
    assert database.execute("SELECT state FROM job_availability WHERE job_id='li-100'").fetchone()[0] == 'closed'


def test_inaccessible_queue_job_is_held_without_claiming_closed_or_sending(database, monkeypatch):
    job = insert(database, 100)
    source_responses(monkeypatch, unknown=[job['id']])
    sent = []
    monkeypatch.setattr(scraper, 'send_telegram', lambda *args: sent.append(args) or True)
    result = scraper.send_reviewed_digest('test', 'private', database)
    assert result['sent'] == 0 and result['unknown'] == 1 and sent == []
    assert tuple(database.execute('SELECT status,notified,ai_verdict FROM jobs').fetchone()) == ('new', 0, 'send')
    source_responses(monkeypatch)
    assert scraper.send_reviewed_digest('test', 'private', database)['sent'] == 1


def test_backfill_never_promotes_holds_rejects_notified_old_or_filtered_rows(database, monkeypatch):
    insert(database, 100, verdict='hold')
    insert(database, 101, status='rejected', verdict='reject')
    insert(database, 102, notified=1)
    insert(database, 103, days=9)
    insert(database, 104, location='Bengaluru, India')
    insert(database, 105, sponsorship='doubtful')
    calls = source_responses(monkeypatch)
    monkeypatch.setattr(scraper, 'send_telegram', lambda *args: pytest.fail('Nothing may be sent'))
    assert scraper.send_reviewed_digest('test', 'private', database)['sent'] == 0
    assert calls == []


def test_queue_more_reopens_listings_and_refills_skipped_closed_job(database, monkeypatch):
    insert(database, 100)
    insert(database, 101, 'Riyadh', score=60)
    calls = source_responses(monkeypatch, closed=['li-100'])
    assert scraper.list_queued_jobs(database, limit=1, revalidate=True)[0]['id'] == 'li-101'
    assert calls == ['li-100', 'li-101']


def test_direct_digest_and_individual_cards_cannot_bypass_availability(database, monkeypatch):
    closed = insert(database, 100)
    uncertain = insert(database, 101)
    source_responses(monkeypatch, closed=[closed['id']], unknown=[uncertain['id']])
    monkeypatch.setattr(scraper, 'send_telegram', lambda *args, **kwargs: pytest.fail('No known-open listing'))
    with pytest.raises(RuntimeError, match='confirmed open'):
        scraper.send_digest('test', 'private', database, [closed, uncertain])
    assert scraper.notify_new_jobs('test', 'private', [uncertain], conn=database) == []


def test_unknown_region_is_reported_separately_from_no_matches(database, monkeypatch):
    insert(database, 100)
    insert(database, 101, 'Riyadh')
    source_responses(monkeypatch, unknown=['li-101'])
    sent = []
    monkeypatch.setattr(scraper, 'send_telegram', lambda token, chat, text: sent.append(text) or True)
    assert scraper.send_reviewed_digest('test', 'private', database)['sent'] == 1
    assert 'Availability not confirmed: Riyadh' in sent[0]
    no_matches = next(line for line in sent[0].splitlines() if 'No matches:' in line)
    assert 'Riyadh' not in no_matches


def test_availability_budget_leaves_unchecked_jobs_pending(database, monkeypatch):
    for number in range(100, 106):
        insert(database, number)
    calls = source_responses(monkeypatch, unknown=[f'li-{number}' for number in range(100, 106)])
    selected, report = select_available(database, scraper.reviewed_queue(database), max_checks=2)
    assert selected == [] and len(calls) == 2 and report['unchecked'] == 4
    assert database.execute("SELECT COUNT(*) FROM jobs WHERE status='new' AND notified=0").fetchone()[0] == 6


def test_scoped_delivery_refills_from_same_context_reviewed_backlog(tmp_path, monkeypatch):
    from jobhunter_service import runner
    from jobhunter_service.state import private_json
    from test_service_runner import create_profile, put_job, envelope
    import json
    directory, store = create_profile(tmp_path, 123)
    settings = json.loads(store.member(123)['settings'])
    settings['search']['delivery'] = {'per_market': 1, 'cap': 1}
    with store.connect() as db:
        db.execute('UPDATE members SET settings=? WHERE user_id=123', (json.dumps(settings),))
    private_json(directory / 'config.json', settings['search'])
    def scrape(profile, root, pages):
        put_job(profile, root)
        with closing(sqlite3.connect(directory / 'jobs.db')) as db, db:
            db.execute("UPDATE jobs SET url='https://www.linkedin.com/jobs/view/10001' WHERE id='same-id'")
            exists = db.execute("SELECT 1 FROM jobs WHERE id='zz-queued-other'").fetchone()
            if not exists:
                db.row_factory = sqlite3.Row
                other = dict(db.execute("SELECT * FROM jobs WHERE id='same-id'").fetchone())
                other.update(id='zz-queued-other', url='https://www.linkedin.com/jobs/view/10002', score=70)
                columns = ','.join(other)
                db.execute(f"INSERT INTO jobs({columns}) VALUES ({','.join('?' for _ in other)})", list(other.values()))
    monkeypatch.setattr(runner, '_scrape', scrape)
    first = runner.collect('u123', tmp_path, 'first')
    runner.apply_review('u123', tmp_path, envelope(first))
    # The older approval falls outside today's tiny model batch but remains
    # tied to the same confirmed profile; it can fill a newly closed slot.
    monkeypatch.setattr(runner, 'MAX_CANDIDATES', 1)
    second = runner.collect('u123', tmp_path, 'second')
    assert second['candidate_ids'] == ['same-id']
    runner.apply_review('u123', tmp_path, envelope(second))
    def check(job):
        identifier = job['url'].rsplit('/', 1)[1]
        body = page(identifier=identifier, status='No longer accepting applications' if identifier == '10001' else '')
        body = body.replace('Backend Engineer', 'Frontend Developer').replace('Example Team', 'Example')
        return check_source(job, Transport(Response(body)))
    monkeypatch.setattr(availability, 'check', check)
    result = runner.prepare_delivery('u123', tmp_path, 'second', revision=1)
    assert result['selected_ids'] == ['zz-queued-other']
    assert result['availability']['closed'] == 1
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        assert dict(db.execute('SELECT id,notified FROM jobs')) == {'same-id': 0, 'zz-queued-other': 0}
        assert db.execute("SELECT status FROM jobs WHERE id='same-id'").fetchone()[0] == 'unavailable'


def test_generic_backfill_does_not_reuse_approval_from_another_resume(database, monkeypatch):
    import jobhunter_matching
    item = insert(database, 100, 'France', sponsorship='excluded')
    generic = jobhunter_matching.validate_config({'matching': {'preset': 'generic', 'preferred_roles': ['backend engineer'],
        'preferred_technologies': ['java']}, 'keywords': ['backend engineer'],
        'markets': [{'name': 'France', 'locations': ['France'], 'work_authorization': 'authorized', 'relocation_required': False}],
        'score_threshold': 0})
    monkeypatch.setattr(scraper, 'CONFIG', {**scraper.DEFAULT_CONFIG, **generic})
    database.execute('CREATE TABLE jobhunter_review_context(job_id TEXT PRIMARY KEY,context_digest TEXT NOT NULL)')
    database.execute('INSERT INTO jobhunter_review_context VALUES(?,?)', (item['id'], 'old-resume-digest'))
    assert scraper.reviewed_queue(database, context_digest='new-resume-digest') == []
    assert [job['id'] for job in scraper.reviewed_queue(database, context_digest='old-resume-digest')] == [item['id']]


@pytest.fixture
def ranked_service_run(tmp_path, monkeypatch):
    """One older approval and a new batch whose AI rank opposes model score."""
    from jobhunter_service import runner
    from jobhunter_service.state import private_json
    from test_service_runner import create_profile, put_job, envelope

    directory, store = create_profile(tmp_path, 123)
    settings = json.loads(store.member(123)['settings'])
    settings['search']['delivery'] = {'per_market': 1, 'cap': 2}
    with store.connect() as db:
        db.execute('UPDATE members SET settings=? WHERE user_id=123', (json.dumps(settings),))
    private_json(directory / 'config.json', settings['search'])
    monkeypatch.setattr(runner, '_scrape', lambda *args: None)

    def seed(number, company='Example'):
        put_job('u123', tmp_path)
        with closing(sqlite3.connect(directory / 'jobs.db')) as db, db:
            db.execute("UPDATE jobs SET id=?,url=?,company=? WHERE id='same-id'",
                       (f'li-{number}', f'https://www.linkedin.com/jobs/view/{number}', company))

    seed(10001)
    first = runner.collect('u123', tmp_path, 'older')
    runner.apply_review('u123', tmp_path, envelope(first))
    seed(10002)
    seed(10003, 'Example Recruitment')
    monkeypatch.setattr(runner, 'MAX_CANDIDATES', 2)
    current = runner.collect('u123', tmp_path, 'current')
    assert current['candidate_ids'] == ['li-10002', 'li-10003']
    scores = {job['id']: job['score'] for job in current['candidates']}
    assert scores['li-10002'] > scores['li-10003']
    review = envelope(current)
    for verdict in review['verdicts']:
        verdict['rank'] = 1 if verdict['job_id'] == 'li-10003' else 2
    reviewed = runner.apply_review('u123', tmp_path, review)
    assert reviewed['selected_ids'] == ['li-10003', 'li-10002']
    return tmp_path, directory, current


def scoped_source_responses(monkeypatch, *, closed=(), unknown=()):
    calls = []
    def check(job):
        calls.append(job['id'])
        identifier = job['url'].rsplit('/', 1)[1]
        body = page(identifier=identifier,
                    status='No longer accepting applications' if job['id'] in closed else '')
        body = body.replace('Backend Engineer', job['title']).replace('Example Team', job['company'])
        response = Response(status=429) if job['id'] in unknown else Response(body)
        return check_source(job, Transport(response))
    monkeypatch.setattr(availability, 'check', check)
    return calls


def test_pre_delivery_keeps_current_ai_order_ahead_of_higher_score_old_approval(ranked_service_run, monkeypatch):
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    calls = scoped_source_responses(monkeypatch)
    result = runner.prepare_delivery('u123', root, 'current', revision=1)
    assert result['selected_ids'] == ['li-10003', 'li-10002']
    assert calls == ['li-10003', 'li-10002']
    assert result['availability']['unchecked'] == result['queued_count'] == 1
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        # Ranks belong to their original review batches, never the temporary
        # merged delivery ordering. Old rank 1 cannot outrank current rank 1.
        assert db.execute('SELECT id,ai_rank,notified FROM jobs ORDER BY id').fetchall() == [
            ('li-10001', 1, 0), ('li-10002', 2, 0), ('li-10003', 1, 0)]


def test_pre_delivery_backfill_follows_surviving_current_ai_choice(ranked_service_run, monkeypatch):
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    calls = scoped_source_responses(monkeypatch, closed=['li-10003'])
    result = runner.prepare_delivery('u123', root, 'current', revision=1)
    assert result['selected_ids'] == ['li-10002', 'li-10001']
    assert calls == ['li-10003', 'li-10002', 'li-10001']
    assert result['availability']['closed'] == 1
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        assert db.execute("SELECT status,notified FROM jobs WHERE id='li-10003'").fetchone() == ('unavailable', 0)
        assert db.execute('SELECT SUM(notified) FROM jobs').fetchone()[0] == 0


@pytest.mark.parametrize('invalid_id, expected', [
    ('li-10001', ['li-10002']), ('li-10002', ['li-10001']),
])
def test_manifest_membership_does_not_bypass_current_review_context(ranked_service_run, monkeypatch, invalid_id, expected):
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    with closing(sqlite3.connect(directory / 'jobs.db')) as db, db:
        db.execute('UPDATE jobhunter_review_context SET context_digest=? WHERE job_id=?', ('another-profile-snapshot', invalid_id))
    calls = scoped_source_responses(monkeypatch, closed=['li-10003'])
    result = runner.prepare_delivery('u123', root, 'current', revision=1)
    assert result['selected_ids'] == expected
    assert invalid_id not in calls


def test_pre_delivery_rejects_a_different_run_result_before_availability_writes(ranked_service_run, monkeypatch):
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    run_directory = directory / 'state' / 'runs'
    (run_directory / 'current' / 'result.json').write_bytes((run_directory / 'older' / 'result.json').read_bytes())
    calls = scoped_source_responses(monkeypatch)
    with pytest.raises(ValueError, match='completed review'):
        runner.prepare_delivery('u123', root, 'current', revision=1)
    assert calls == []
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        assert db.execute('SELECT SUM(notified) FROM jobs').fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='job_availability'").fetchone()[0] == 0


@pytest.mark.parametrize('filename', ['result.json', 'manifest.json'])
def test_pre_delivery_rejects_run_file_symlink_even_to_same_profile(ranked_service_run, monkeypatch, filename):
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    run_directory = directory / 'state' / 'runs'
    path = run_directory / 'current' / filename
    path.rename(path.with_suffix('.original'))
    path.symlink_to(run_directory / 'older' / filename)
    calls = scoped_source_responses(monkeypatch)
    with pytest.raises(PermissionError, match='symlink'):
        runner.prepare_delivery('u123', root, 'current', revision=1)
    assert calls == []


@pytest.mark.parametrize('unknown, expected_ids', [((), ['li-10003']), (('li-10003',), [])])
def test_scoped_pre_delivery_budget_sends_only_verified_rows_and_preserves_pending_queue(ranked_service_run, monkeypatch, unknown, expected_ids):
    import jobhunter_delivery
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    calls = scoped_source_responses(monkeypatch, unknown=unknown)
    real_select = jobhunter_delivery.select_available
    def one_check(*args, **kwargs):
        return real_select(*args, **kwargs, max_checks=1)
    monkeypatch.setattr(jobhunter_delivery, 'select_available', one_check)
    result = runner.prepare_delivery('u123', root, 'current', revision=1)
    assert result['selected_ids'] == expected_ids
    assert calls == ['li-10003']
    assert result['availability']['checked'] == 1
    assert result['availability']['unchecked'] == 2
    assert result['availability']['unknown'] == int(bool(unknown))
    assert result['queued_count'] == 3 - len(expected_ids)
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status='new' AND notified=0 AND ai_verdict='send'").fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM job_availability').fetchone()[0] == 1


def test_pre_delivery_time_budget_does_not_treat_unchecked_as_open(ranked_service_run, monkeypatch):
    import jobhunter_delivery
    from jobhunter_service import runner
    root, directory, _ = ranked_service_run
    calls = scoped_source_responses(monkeypatch)
    real_select = jobhunter_delivery.select_available
    def expired_budget(*args, **kwargs):
        return real_select(*args, **kwargs, max_seconds=0)
    monkeypatch.setattr(jobhunter_delivery, 'select_available', expired_budget)
    result = runner.prepare_delivery('u123', root, 'current', revision=1)
    assert result['selected_ids'] == []
    assert result['availability']['checked'] == 0 and result['availability']['unchecked'] == 3
    assert result['queued_count'] == 3 and calls == []
    with closing(sqlite3.connect(directory / 'jobs.db')) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs WHERE status='new' AND notified=0").fetchone()[0] == 3
