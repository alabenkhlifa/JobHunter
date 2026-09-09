"""Real outbox preflight with synthetic public-listing evidence and Telegram."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest

import jobhunter_availability as availability
from jobhunter_service import delivery
from jobhunter_service.service import JobHunterService
from test_service_core import Telegram, ready


class Checker:
    def __init__(self, state='open'):
        self.state, self.calls = state, []

    def __call__(self, job):
        self.calls.append(dict(job))
        if isinstance(self.state, Exception):
            raise self.state
        listing = availability._listing(job)
        return {'job_id': job['id'], 'state': self.state,
                'reason': {'open': 'application_control', 'closed': 'closed_marker', 'unknown': 'request_failed'}[self.state],
                'url': listing.url, 'source_job_id': listing.source_id,
                'matched': self.state != 'unknown', 'checked_at': datetime.now(timezone.utc).isoformat()}


@pytest.fixture
def setup(tmp_path):
    client = Telegram()
    service = JobHunterService(tmp_path, 900, telegram_client=client)
    ready(service, channels=True)
    checker = Checker()
    queue = delivery.DeliveryQueue(service, client, checker=checker)
    return service, client, queue, checker


def enqueue(service, queue, *, user=123, run='run1', count=1, message='Immutable digest'):
    member = service.store.member(user)
    with service.store.connect() as db:
        db.execute('INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)',
                   (run, user, run, member['revision'], 'complete', None, 0, 0))
    path = service.profile_dir(member) / 'jobs.db'
    ids = [f'li-{i + 123}' for i in range(count)]
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('''CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, source TEXT, title TEXT,
            company TEXT, location TEXT, description TEXT, url TEXT, date_posted TEXT, date_scraped TEXT,
            score INTEGER, tech_required TEXT, tech_nice_to_have TEXT, min_experience INTEGER,
            notified INTEGER, status TEXT, ai_verdict TEXT, ai_sponsorship TEXT, ai_rank INTEGER)''')
        for index, job_id in enumerate(ids):
            db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (job_id, 'LinkedIn', 'Frontend Developer', f'Candidate {user} company', 'France',
                        'Build React applications and work with a collaborative software engineering team.',
                        f'https://www.linkedin.com/jobs/view/{index + 123}', datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(), 80, 'react', '', 5, 0, 'new', 'send', 'offered', index + 1))
    queue.enqueue(user, run, message, ids)
    return path


def states(service, run='run1'):
    with service.store.connect() as db:
        return [row['status'] for row in db.execute('SELECT status FROM deliveries WHERE run_id=? ORDER BY id', (run,))]


def job_state(path):
    with closing(sqlite3.connect(path)) as db:
        return db.execute('SELECT notified,status FROM jobs ORDER BY id').fetchall()


def change(path, sql, parameters=()):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(sql, parameters)


def test_expiration_after_enqueue_cancels_every_pending_part_without_network(setup):
    service, client, queue, checker = setup
    path = enqueue(service, queue, message='x' * 6500)
    change(path, 'UPDATE jobs SET date_posted=?', ((datetime.now(timezone.utc) - timedelta(days=8)).isoformat(),))
    assert queue.drain() == {'sent': 0, 'pending_or_failed': 0}
    assert set(states(service)) == {'cancelled'}
    assert checker.calls == client.sends == []
    assert job_state(path) == [(0, 'new')]


@pytest.mark.parametrize('state', ['unknown', TimeoutError('synthetic timeout')])
def test_unknown_or_timeout_defers_all_parts_and_channels_then_checks_fresh_on_retry(setup, state):
    service, client, queue, checker = setup
    path = enqueue(service, queue, message='x' * 6500)
    checker.state = state
    first = queue.drain()
    assert first['sent'] == 0 and first['pending_or_failed'] == len(states(service))
    assert set(states(service)) == {'pending'}
    assert client.sends == [] and len(checker.calls) == 1
    assert job_state(path) == [(0, 'delivery_pending')]
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT state FROM job_availability').fetchone()[0] == 'unknown'
    checker.state = 'open'
    assert queue.drain()['sent'] == 6
    assert len(checker.calls) == 2
    assert job_state(path) == [(1, 'new')]


def test_fresh_check_is_committed_even_for_unknown_and_no_cached_open_is_reused(setup):
    service, client, queue, checker = setup
    path = enqueue(service, queue)
    client.fail_chats = {'123', '-1000'}
    assert queue.drain()['sent'] == 0
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT state FROM job_availability').fetchone()[0] == 'open'
    client.fail_chats.clear()
    checker.state = 'unknown'
    before = len(client.sends)
    assert queue.drain()['sent'] == 0
    assert len(client.sends) == before and len(checker.calls) == 2
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT state FROM job_availability').fetchone()[0] == 'unknown'
    assert job_state(path) == [(0, 'delivery_pending')]


def test_closure_after_one_channel_acknowledges_cancels_only_unsent_remainder(setup):
    service, client, queue, checker = setup
    path = enqueue(service, queue)
    client.fail_chats.add('-1000')
    assert queue.drain() == {'sent': 1, 'pending_or_failed': 1}
    assert job_state(path) == [(1, 'new')]
    checker.state = 'closed'
    client.fail_chats.clear()
    before = len(client.sends)
    assert queue.drain() == {'sent': 0, 'pending_or_failed': 0}
    assert states(service) == ['sent', 'cancelled']
    assert len(client.sends) == before and len(checker.calls) == 2
    assert job_state(path) == [(1, 'unavailable')]


def test_closure_after_only_one_part_does_not_fabricate_complete_acknowledgement(setup):
    service, client, queue, checker = setup
    path = enqueue(service, queue, message='x' * 6500)
    real_send = client.send_message
    def first_only(chat, message):
        if client.sends:
            raise RuntimeError('Synthetic outage')
        return real_send(chat, message)
    client.send_message = first_only
    assert queue.drain()['sent'] == 1
    assert job_state(path) == [(0, 'delivery_pending')]
    checker.state = 'closed'
    client.send_message = real_send
    assert queue.drain()['sent'] == 0
    assert states(service).count('sent') == 1
    assert states(service).count('cancelled') == 5
    assert job_state(path) == [(0, 'unavailable')]


def test_same_job_id_in_sibling_profile_uses_only_own_listing_and_evidence(setup):
    service, client, queue, checker = setup
    first = enqueue(service, queue)
    ready(service, 456)
    second = enqueue(service, queue, user=456, run='run2')
    def by_owner(job):
        checker.state = 'closed' if job['company'] == 'Candidate 123 company' else 'open'
        return checker(job)
    queue.checker = by_owner
    assert queue.drain()['sent'] == 1
    assert job_state(first) == [(0, 'unavailable')]
    assert job_state(second) == [(1, 'new')]
    assert [chat for chat, _ in client.sends] == ['456']
    assert {job['company'] for job in checker.calls} == {'Candidate 123 company', 'Candidate 456 company'}


@pytest.mark.parametrize('message', ['Digest', 'x' * 6500])
def test_unknown_older_profile_cannot_starve_another_profile_under_check_budget(setup, monkeypatch, message):
    service, client, queue, checker = setup
    first = enqueue(service, queue, run='older', message=message)
    ready(service, 456)
    second = enqueue(service, queue, user=456, run='newer', message=message)
    monkeypatch.setattr(delivery, 'MAX_DRAIN_CHECKS', 1)

    def by_owner(job):
        checker.state = 'unknown' if job['company'] == 'Candidate 123 company' else 'open'
        return checker(job)

    queue.checker = by_owner
    assert queue.drain()['sent'] == 0
    with service.store.connect() as db:
        assert {row['attempts'] for row in db.execute("SELECT attempts FROM deliveries WHERE run_id='older'")} == {1}
        # No source check was attempted for the later profile. Its priority
        # must remain ahead of an actual inconclusive retry on the next drain.
        assert {row['attempts'] for row in db.execute("SELECT attempts FROM deliveries WHERE run_id='newer'")} == {0}
        newer_parts = db.execute("SELECT COUNT(*) FROM deliveries WHERE run_id='newer'").fetchone()[0]
    assert queue.drain()['sent'] == newer_parts
    assert set(states(service, 'newer')) == {'sent'}
    assert set(states(service, 'older')) == {'pending'}
    assert [job['company'] for job in checker.calls] == ['Candidate 123 company', 'Candidate 456 company']
    assert [chat for chat, _ in client.sends] == ['456'] * newer_parts
    assert job_state(first) == [(0, 'delivery_pending')]
    assert job_state(second) == [(1, 'new')]
    with service.store.connect() as db:
        assert {row['attempts'] for row in db.execute("SELECT attempts FROM deliveries WHERE run_id='older'")} == {1}
    # The first profile remains retryable after the other profile finishes.
    assert queue.drain()['sent'] == 0
    assert [job['company'] for job in checker.calls][-1] == 'Candidate 123 company'


def test_database_symlink_cannot_borrow_sibling_listing_or_acknowledge_it(setup):
    service, client, queue, checker = setup
    first = enqueue(service, queue)
    ready(service, 456)
    second = enqueue(service, queue, user=456, run='run2')
    first.rename(first.with_name('own-original.db'))
    first.symlink_to(second)
    assert queue.drain()['sent'] == 1
    assert states(service) == ['cancelled', 'cancelled']
    assert len(checker.calls) == 1 and checker.calls[0]['company'] == 'Candidate 456 company'
    assert [chat for chat, _ in client.sends] == ['456']


@pytest.mark.parametrize('sql, parameters', [
    ('DELETE FROM jobs', ()),
    ("UPDATE jobs SET ai_verdict='hold'", ()),
    ("UPDATE jobs SET status='rejected'", ()),
    ("UPDATE jobs SET location='Elsewhere'", ()),
    ("UPDATE jobs SET date_posted='',date_scraped=''", ()),
])
def test_missing_or_currently_ineligible_job_cancels_immutable_message(setup, sql, parameters):
    service, client, queue, checker = setup
    path = enqueue(service, queue)
    change(path, sql, parameters)
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


def test_no_url_is_unknown_in_production_checker_and_never_skips_preflight(setup):
    service, client, queue, _ = setup
    path = enqueue(service, queue)
    change(path, "UPDATE jobs SET url=''")
    queue.checker = None
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'pending'}
    assert client.sends == []
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('SELECT state,reason FROM job_availability').fetchone() == ('unknown', 'unsupported_listing')


def test_wrong_listing_evidence_cannot_authorize_a_sibling_job(setup):
    service, client, queue, checker = setup
    path = enqueue(service, queue)
    def wrong_job(job):
        result = checker(job)
        result['job_id'] = 'li-999'
        return result
    queue.checker = wrong_job
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'pending'}
    assert client.sends == [] and job_state(path) == [(0, 'delivery_pending')]


def test_changed_revision_cancels_whole_run_before_checking_or_sending(setup):
    service, client, queue, checker = setup
    enqueue(service, queue)
    action = service.propose(123, {'search': {'max_job_age_days': 3}})
    service.confirm(123, action['action_id'])
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


@pytest.mark.parametrize('sql', [
    "UPDATE jobs SET title='Backend Developer'",  # Below this profile's role-weighted score threshold.
    "UPDATE jobs SET title='Frontend Developer Intern'",
    'UPDATE jobs SET min_experience=9',
])
def test_current_role_score_exclusion_and_seniority_filters_are_explicitly_rechecked(setup, sql):
    service, client, queue, checker = setup
    action = service.propose(123, {'search': {'matching': {
        'preferred_roles': ['frontend developer'], 'excluded_roles': ['intern'],
        'seniority': {'max_years': 8},
        'weights': {'stack': 0, 'role': 100, 'seniority': 0, 'employer': 0, 'freshness': 0},
    }}})
    service.confirm(123, action['action_id'])
    path = enqueue(service, queue)
    change(path, sql)
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


def test_personal_freshness_limit_is_used_instead_of_a_hardcoded_seven_days(setup):
    service, client, queue, checker = setup
    action = service.propose(123, {'search': {'max_job_age_days': 3}})
    service.confirm(123, action['action_id'])
    path = enqueue(service, queue)
    change(path, 'UPDATE jobs SET date_posted=?', ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(),))
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


@pytest.mark.parametrize('authorization,sql', [
    ('unknown', "UPDATE jobs SET ai_sponsorship='offered'"),
    ('sponsorship_required', "UPDATE jobs SET ai_sponsorship='implied'"),
    ('sponsorship_required', "UPDATE jobs SET description='No visa sponsorship.'"),
])
def test_fresh_open_listing_cannot_override_current_work_authorization(setup, authorization, sql):
    service, client, queue, checker = setup
    action = service.propose(123, {'search': {'markets': [{'name': 'France', 'locations': ['France'],
        'work_authorization': authorization, 'relocation_required': False}]}})
    service.confirm(123, action['action_id'])
    path = enqueue(service, queue)
    change(path, sql)
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


def test_run_and_global_check_budgets_leave_unchecked_digests_pending(setup, monkeypatch):
    service, client, queue, checker = setup
    enqueue(service, queue, count=3)
    monkeypatch.setattr(delivery, 'MAX_DRAIN_CHECKS', 2)
    assert queue.drain()['sent'] == 0
    assert len(checker.calls) == 2 and client.sends == []
    assert set(states(service)) == {'pending'}


def test_expired_run_time_budget_does_not_send_using_partial_checks(setup, monkeypatch):
    service, client, queue, checker = setup
    enqueue(service, queue, count=2)
    times = iter([0, 0, 121])
    monkeypatch.setattr(delivery.time, 'monotonic', lambda: next(times))
    assert queue.drain()['sent'] == 0
    assert len(checker.calls) == 1 and client.sends == []
    assert set(states(service)) == {'pending'}


def test_digest_over_twelve_jobs_cancels_without_unbounded_network(setup):
    service, client, queue, checker = setup
    enqueue(service, queue, count=13)
    assert queue.drain()['sent'] == 0
    assert set(states(service)) == {'cancelled'}
    assert client.sends == checker.calls == []


def test_concurrent_drains_reread_pending_rows_under_the_profile_lock(setup, monkeypatch):
    service, client, first, checker = setup
    enqueue(service, first, message='x' * 6500)
    second = delivery.DeliveryQueue(service, client, checker=checker)
    checking, release, second_waiting = threading.Event(), threading.Event(), threading.Event()
    original_mutation = service.mutation

    @contextmanager
    def mutation(actor):
        if threading.current_thread().name.endswith('_1'):
            second_waiting.set()
        with original_mutation(actor):
            yield

    def blocked_check(job):
        checking.set()
        assert release.wait(5)
        return checker(job)

    first.checker = blocked_check
    monkeypatch.setattr(service, 'mutation', mutation)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix='outbox') as executor:
        first_result = executor.submit(first.drain)
        assert checking.wait(5)
        second_result = executor.submit(second.drain)
        try:
            assert second_waiting.wait(5)  # Both drains have captured pending rows.
        finally:
            release.set()
        assert first_result.result(5)['sent'] + second_result.result(5)['sent'] == 6
    assert len(client.sends) == 6
    assert set(states(service)) == {'sent'}
