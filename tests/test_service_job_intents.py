"""Explicit natural job requests cannot grant model or owner privileges."""
import pytest

from jobhunter_service.job_intents import JobIntent, matching_jobs, parse_job_callback, parse_job_intent


@pytest.mark.parametrize('text,expected', [
    ('show my jobs', JobIntent('jobs')),
    ('Can you show me my latest jobs?', JobIntent('jobs')),
    ('please list my job matches', JobIntent('jobs')),
    ('show my collected jobs', JobIntent('jobs', 'collected')),
    ('show my held jobs', JobIntent('jobs', 'collected')),
    ('details job 2', JobIntent('details', '2')),
    ('show details for job #1', JobIntent('details', '#1')),
    ('show me the role Senior iOS Engineer', JobIntent('details', 'Senior iOS Engineer')),
    ('interested in job li-123', JobIntent('interested', 'li-123')),
    ('mark me interested in Senior iOS Engineer', JobIntent('interested', 'Senior iOS Engineer')),
    ('prepare job 1', JobIntent('prepare', '1')),
    ('prepare my resume for Senior iOS Engineer', JobIntent('prepare', 'Senior iOS Engineer')),
])
def test_explicit_job_directives(text, expected):
    assert parse_job_intent(text) == expected


@pytest.mark.parametrize('text', [
    'yes', 'confirm', 'I am interested in Senior iOS Engineer',
    "I'm interested in iOS roles", 'show my profile', 'prepare my resume',
    'show my skills', 'interested in backend jobs', 'Please update my preferred roles',
    'show my status', 'show my settings', 'show my schedule', 'show my tracker', 'show status',
    'show my Gmail connection', 'show my current resume',
    '/apply li-123', 'submit job 1', 'upload my resume', 'show details job 1\nsubmit everything',
    'details ' + 'x' * 400,
])
def test_preferences_confirmation_and_other_workflow_steps_remain_conversation(text):
    assert parse_job_intent(text) is None


@pytest.mark.parametrize('data', ['jh:job:submit:li-123', 'jh:job:upload:li-123',
    'jh:job:prepare:../owner', 'jh:job:details:li-123:extra', 'jh:job:prepare:' + 'x' * 80, None])
def test_callbacks_are_only_the_small_navigation_allowlist(data):
    with pytest.raises(ValueError, match='invalid'):
        parse_job_callback(data)


def test_callback_returns_an_explicit_id_without_other_authority():
    assert parse_job_callback('jh:job:prepare:li-123') == JobIntent('prepare', 'li-123')


def test_reference_matching_keeps_duplicate_titles_ambiguous_and_does_not_pick_pronouns():
    jobs = [{'id': 'one', 'title': 'Senior iOS Engineer', 'company': 'First'},
            {'id': 'two', 'title': 'Senior iOS Engineer', 'company': 'Second'}]
    assert matching_jobs('one', jobs) == [jobs[0]]
    assert matching_jobs('Senior iOS Engineer', jobs) == jobs
    assert matching_jobs('Senior iOS Engineer at Second', jobs) == [jobs[1]]
    assert matching_jobs('this role', jobs) == []
    assert matching_jobs('that', jobs) == []
