"""Guided setup persistence, explicit decisions, invalidation and activation."""
import copy
import json
from unittest.mock import Mock

import pytest

from jobhunter_service.service import JobHunterService
from test_service_core import OWNER, Telegram, activate, confirm, ready


@pytest.fixture
def service(tmp_path):
    result = JobHunterService(tmp_path, OWNER, telegram_client=Telegram())
    activate(result)
    return result


def stage(service, step, action='acknowledge', actor=123):
    current = service.onboarding_status(actor)
    return service.onboarding_action(actor, action, step, current['revision'])


def step_state(service, step, actor=123):
    return next(row for row in service.onboarding_status(actor)['steps'] if row['id'] == step)


def configure(service, actor=123):
    service.onboarding_status(actor, start=True)
    return confirm(service, actor, {'resume': {'name': 'Synthetic Candidate'},
        'search': {'keywords': ['software engineer'], 'markets': [{'name': 'France',
            'locations': ['France'], 'work_authorization': 'unknown', 'relocation_required': True}]},
        'schedule': {'timezone': 'Europe/Paris', 'time': '08:30', 'enabled': True}})


def review(service, *, connected=()):
    configure(service)
    for step in ('resume', 'roles', 'markets', 'schedule', 'delivery'):
        stage(service, step)
    for step in ('linkedin', 'gmail', 'tracker'):
        stage(service, step, 'acknowledge' if step in connected else 'skip')
    return stage(service, 'review')


def statuses(**overrides):
    return {provider: {'status': overrides.get(provider, 'unavailable'), 'message': 'Synthetic connection status.'}
            for provider in ('linkedin', 'gmail', 'tracker')}


def test_status_alone_never_starts_or_changes_legacy_settings(service):
    before = service.store.member(123)
    state = service.onboarding_status(123)
    assert not state['started'] and state['next_step'] == 'resume'
    assert state['ready'] is False and 'confirmed resume' in state['missing']
    assert service.store.member(123) == before
    assert service.store.checkpoint('onboarding:123') is None
    assert service.snapshot(123)['connections']['gmail']['status'] == 'unavailable'


def test_start_and_progress_do_not_pause_or_change_running_legacy_schedule(service):
    ready(service)
    before = service.store.member(123)
    state = service.onboarding_status(123, start=True)
    assert state['started'] and state['schedule_enabled']
    assert service.store.member(123) == before
    stage(service, 'resume')
    assert service.store.member(123) == before
    with pytest.raises(ValueError, match='pause'):
        service.propose(123, {'search': {'keywords': ['different role']}})
    pause = service.propose(123, {'schedule': {'enabled': False}})
    assert service.snapshot(123)['settings']['schedule']['enabled']
    service.confirm(123, pause['action_id'])
    assert not service.snapshot(123)['settings']['schedule']['enabled']


def test_started_progress_survives_restart_and_is_idempotent(service):
    configure(service)
    first = stage(service, 'resume')
    restarted = JobHunterService(service.root, OWNER, telegram_client=Telegram())
    second = restarted.onboarding_status(123, start=True)
    assert second == first
    assert second['next_step'] == 'roles'


def test_name_only_resume_requires_separate_explicit_experience_review(service):
    configure(service)
    assert step_state(service, 'resume')['state'] == 'pending'
    assert not service.onboarding_status(123)['ready_for_activation']
    assert 'no employment experience' in step_state(service, 'resume')['question']
    stage(service, 'resume')
    assert step_state(service, 'resume')['state'] == 'complete'
    assert service.snapshot(123)['settings']['resume'] == {'name': 'Synthetic Candidate'}


def test_missing_resume_or_roles_cannot_be_acknowledged_or_skipped(service):
    service.onboarding_status(123, start=True)
    for step in ('resume', 'roles', 'markets'):
        with pytest.raises(ValueError):
            stage(service, step)
        with pytest.raises(ValueError):
            stage(service, step, 'skip')
    assert not service.onboarding_status(123)['ready_for_activation']


def test_effective_experience_limits_are_explicit_and_not_called_unlimited(service):
    configure(service)
    row = step_state(service, 'roles')
    assert 'Minimum years: 0' in row['detail'] and 'Maximum years: 30' in row['detail']
    assert 'Preferred minimum: 0' in row['detail'] and 'Preferred maximum: 30' in row['detail']
    assert 'unlimited' not in row['detail'] and 'no bound' not in row['detail']
    assert row['state'] == 'pending'
    stage(service, 'roles')
    assert step_state(service, 'roles')['state'] == 'complete'


def test_acknowledged_unknown_work_authorization_remains_unknown(service):
    configure(service)
    stage(service, 'markets')
    assert 'matches remain held' in step_state(service, 'markets')['detail']
    assert service.snapshot(123)['settings']['search']['markets'][0]['work_authorization'] == 'unknown'


def test_fresh_combined_model_proposal_cannot_enable_schedule(service):
    result = configure(service)
    assert result['settings']['schedule']['enabled'] is False
    proposal = service.propose(123, {'schedule': {'enabled': True}})
    assert proposal['patch'] == {'schedule': {'enabled': False}}
    assert 'paused' in proposal['preview']
    service.confirm(123, proposal['action_id'])
    assert not service.snapshot(123)['settings']['schedule']['enabled']


def test_activation_is_a_readable_proposal_then_explicit_confirmation(service):
    status = review(service)
    assert status['next_step'] == 'review' and status['ready_for_activation'] and not status['complete']
    proposal = stage(service, 'review', 'activate')
    assert proposal['patch'] == {'schedule': {'enabled': True}}
    assert 'Europe/Paris' in proposal['preview'] and 'work authorization unknown' in proposal['preview']
    assert 'Next run:' in proposal['preview']
    assert proposal['next_run'] and 'Europe/Paris' in proposal['summary']
    assert not service.snapshot(123)['settings']['schedule']['enabled']
    confirmed = service.confirm(123, proposal['action_id'])
    assert confirmed['settings']['schedule']['enabled']
    assert confirmed['onboarding']['complete'] and confirmed['onboarding']['next_step'] is None
    replay = service.confirm(123, proposal['action_id'])
    assert replay['revision'] == confirmed['revision']


def test_final_review_distinguishes_roles_technologies_and_search_keywords(service):
    configure(service)
    confirm(service, 123, {'search': {'matching': {'preferred_roles': ['Backend developer'],
        'excluded_roles': ['Support agent'], 'preferred_technologies': ['Java'], 'excluded_technologies': ['PHP']}}})
    summary = service.onboarding_status(123)['summary']
    assert 'Search keywords: software engineer' in summary
    assert 'Preferred roles: Backend developer' in summary and 'Excluded roles: Support agent' in summary
    assert 'Preferred technologies: Java' in summary and 'Excluded technologies: PHP' in summary
    assert 'collection pages per query: 2' in summary


def test_preferred_roles_without_keywords_ask_for_search_phrases_instead_of_roles_again(service):
    service.onboarding_status(123, start=True)
    confirm(service, 123, {'resume': {'name': 'Synthetic'},
                         'search': {'matching': {'preferred_roles': ['Backend developer']}}})
    stage(service, 'resume')
    current = service.onboarding_status(123)
    assert current['next_step'] == 'roles'
    assert 'preferred roles are saved' in current['next_question']
    assert 'search keywords: Backend developer' in current['next_question']
    assert 'acknowledge' not in step_state(service, 'roles')['actions']


def test_pause_preview_exposes_readable_next_run_and_normalized_patch(service):
    configure(service)
    proposal = service.propose(123, {'schedule': {'enabled': True},
        'accounts': {'gmail': {'account': 'Jobs@Example.Test'}}})
    assert proposal['next_run'] is None
    assert proposal['patch']['schedule']['enabled'] is False
    assert proposal['patch']['accounts']['gmail']['account'] == 'jobs@example.test'


def test_old_legacy_enable_proposal_cannot_bypass_new_guided_gate(service):
    proposal = service.propose(123, {'resume': {'name': 'Synthetic'},
        'search': {'keywords': ['engineer'], 'markets': [{'name': 'France', 'locations': ['France'],
            'work_authorization': 'authorized', 'relocation_required': False}]},
        'schedule': {'enabled': True}})
    service.onboarding_status(123, start=True)
    with pytest.raises(ValueError, match='guided activation'):
        service.confirm(123, proposal['action_id'])
    assert not service.snapshot(123)['settings']['schedule']['enabled']


def test_progress_revision_invalidates_old_buttons_without_changing_settings_revision(service):
    configure(service)
    before = service.onboarding_status(123)
    after = stage(service, 'resume')
    assert after['settings_revision'] == before['settings_revision']
    assert after['revision'] != before['revision']
    with pytest.raises(ValueError, match='stale'):
        service.onboarding_action(123, 'acknowledge', 'roles', before['revision'])


def test_acknowledgements_do_not_resurrect_when_settings_are_changed_back(service):
    review(service)
    original = service.snapshot(123)['settings']['search']['keywords']
    confirm(service, 123, {'search': {'keywords': ['other role']}})
    assert step_state(service, 'roles')['state'] == 'pending'
    assert step_state(service, 'resume')['state'] == 'complete'
    confirm(service, 123, {'search': {'keywords': original}})
    assert step_state(service, 'roles')['state'] == 'pending'
    assert not service.onboarding_status(123)['ready_for_activation']


@pytest.mark.parametrize('step,patch', [
    ('resume', {'resume': {'summary': 'Candidate-confirmed summary'}}),
    ('markets', {'search': {'markets': [{'name': 'Germany', 'locations': ['Germany'],
         'work_authorization': 'sponsorship_required', 'relocation_required': True}]}}),
    ('schedule', {'schedule': {'time': '10:00'}}),
    ('delivery', {'telegram': {'presentation': {'style': 'compact'}}}),
    ('gmail', {'accounts': {'gmail': {'account': 'new@example.test'}}}),
    ('tracker', {'accounts': {'tracker': {'viewer_email': 'viewer@example.test'}}}),
])
def test_confirmed_subject_change_invalidates_just_its_step_and_final_review(service, step, patch):
    review(service)
    confirm(service, 123, patch)
    assert step_state(service, step)['state'] not in {'complete', 'skipped'}
    assert step_state(service, 'review')['state'] != 'complete'
    assert step_state(service, 'roles')['state'] == 'complete'


def test_new_resume_source_invalidates_review_even_before_settings_change(service):
    review(service)
    before = service.snapshot(123)['settings']
    service.stage_resume(123, {'text': 'New synthetic source', 'filename': 'synthetic.txt'})
    assert step_state(service, 'resume')['state'] == 'pending'
    assert service.snapshot(123)['settings'] == before


def test_reopen_invalidates_pending_activation_even_if_acknowledged_again(service):
    review(service)
    proposal = stage(service, 'review', 'activate')
    stage(service, 'resume', 'reopen')
    stage(service, 'resume')
    stage(service, 'review')
    with pytest.raises(ValueError, match='progress changed'):
        service.confirm(123, proposal['action_id'])


def test_optional_accounts_are_independent_and_unavailable_can_be_skipped(service):
    configure(service)
    for provider in ('linkedin', 'gmail', 'tracker'):
        with pytest.raises(ValueError):
            stage(service, provider)
        stage(service, provider, 'skip')
        assert step_state(service, provider)['state'] == 'skipped'
    assert not service.snapshot(123)['settings']['accounts']['gmail']['enabled']
    assert not service.snapshot(123)['settings']['accounts']['tracker']['enabled']


def test_skipping_enabled_integration_requires_exact_disable_confirmation(service):
    configure(service)
    confirm(service, 123, {'accounts': {'gmail': {'enabled': True, 'account': 'jobs@example.test'},
                                      'tracker': {'enabled': True, 'account': 'tracker@example.test'}}})
    proposal = stage(service, 'gmail', 'skip')
    assert proposal['patch'] == {'accounts': {'gmail': {'enabled': False}}}
    assert service.snapshot(123)['settings']['accounts']['gmail']['enabled']
    service.confirm(123, proposal['action_id'])
    assert step_state(service, 'gmail')['state'] == 'skipped'
    assert not service.snapshot(123)['settings']['accounts']['gmail']['enabled']
    assert service.snapshot(123)['settings']['accounts']['tracker']['enabled']


def test_connection_ttl_expiry_requires_recheck_before_activation_but_preserves_ack(service):
    current = statuses(linkedin='connected')
    service.connection_status = Mock(side_effect=lambda actor: copy.deepcopy(current))
    review(service, connected=('linkedin',))
    current['linkedin']['status'] = 'unverified'
    status = service.onboarding_status(123)
    assert step_state(service, 'linkedin')['state'] == 'complete'
    assert status['next_step'] == 'linkedin' and not status['ready_for_activation']
    def check(actor, provider):
        current[provider]['status'] = 'connected'
        return current
    service.check_connection = Mock(side_effect=check)
    stage(service, 'linkedin', 'check')
    assert service.onboarding_status(123)['ready_for_activation']
    proposal = stage(service, 'review', 'activate')
    service.confirm(123, proposal['action_id'])
    current['linkedin']['status'] = 'unverified'
    assert service.onboarding_status(123)['complete']
    assert 'check' in step_state(service, 'linkedin')['actions']


def test_activation_confirmation_rechecks_current_connection_readiness(service):
    current = statuses(linkedin='connected')
    service.connection_status = Mock(side_effect=lambda actor: current)
    review(service, connected=('linkedin',))
    proposal = stage(service, 'review', 'activate')
    current['linkedin']['status'] = 'error'
    with pytest.raises(ValueError, match='guided setup'):
        service.confirm(123, proposal['action_id'])


def test_stale_check_cannot_probe_and_revoked_actor_cannot_change_progress(service):
    configure(service)
    old = service.onboarding_status(123)['revision']
    stage(service, 'resume')
    service.check_connection = Mock()
    with pytest.raises(ValueError):
        service.onboarding_action(123, 'check', 'linkedin', old)
    service.check_connection.assert_not_called()
    service.admin(OWNER, 'revoke', 123)
    with pytest.raises(PermissionError):
        service.onboarding_status(123, start=True)


def test_each_candidate_has_independent_progress_and_activation_tokens(service):
    activate(service, 456)
    review(service)
    second_before = service.store.member(456)
    assert service.onboarding_status(456)['started'] is False
    proposal = stage(service, 'review', 'activate')
    with pytest.raises(ValueError):
        service.confirm(456, proposal['action_id'])
    assert service.store.member(456) == second_before
    assert service.store.checkpoint('onboarding:456') is None
    with service.store.connect() as db:
        persisted = db.execute("SELECT value FROM checkpoints WHERE key='onboarding:123'").fetchone()[0]
    assert 'Synthetic Candidate' not in persisted and 'France' not in persisted
