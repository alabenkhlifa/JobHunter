"""Durable, private candidate intent recovery; no external services."""
from concurrent.futures import ThreadPoolExecutor
import time
from types import SimpleNamespace

import pytest

from jobhunter_service.recovery import (
    RESUME_RETRY_INTENT, begin_retry, callback_retry_id, clear_recovery, get_recovery, save_recovery,
)
from jobhunter_service.service import JobHunterService


@pytest.fixture
def service(tmp_path):
    result = JobHunterService(tmp_path, 1)
    for actor in (11, 22):
        result.admin(1, 'add', actor)
        result.authorize(actor)
    return result


def test_failure_survives_restart_and_settings_changes_without_replaying_a_patch(service):
    saved = save_recovery(service, 11, 'Move my search to the morning')
    assert set(saved) == {'reference', 'attempts', 'category'}
    assert saved['category'] == 'model_unavailable' and saved['attempts'] == 0
    proposal = service.propose(11, {'schedule': {'time': '14:00'}})
    service.confirm(11, proposal['action_id'])
    restarted = JobHunterService(service.store.path.parent.parent, 1)
    assert restarted.store.path == service.store.path
    request = begin_retry(restarted, 11, request_id=100)
    assert request['text'] == 'Move my search to the morning'
    assert request['reference'] == saved['reference'] and request['attempts'] == 1
    assert service.snapshot(11)['settings']['schedule']['time'] == '14:00'
    assert service.snapshot(11)['revision'] == 1


def test_retry_counts_are_bounded_and_redelivery_does_not_spend_another_attempt(service):
    saved = save_recovery(service, 11, 'Help refine my work experience')
    for attempt in range(1, 4):
        assert begin_retry(service, 11, request_id=attempt)['attempts'] == attempt
        assert begin_retry(service, 11, request_id=attempt)['attempts'] == attempt
        failed = save_recovery(service, 11, 'Help refine my work experience')
        assert failed['reference'] == saved['reference'] and failed['attempts'] == attempt
    with pytest.raises(ValueError, match='retry limit'):
        begin_retry(service, 11, request_id=4)
    assert begin_retry(service, 11, request_id=1)['attempts'] == 3


def test_concurrent_delivery_of_one_retry_message_spends_only_one_attempt(service):
    save_recovery(service, 11, 'Help with my destination preferences')
    with ThreadPoolExecutor(max_workers=6) as pool:
        attempts = list(pool.map(lambda _: begin_retry(service, 11, request_id=101)['attempts'], range(6)))
    assert attempts == [1] * 6
    assert get_recovery(service, 11)['attempts'] == 1


def test_callback_redelivery_is_one_attempt_but_distinct_button_clicks_count(service):
    save_recovery(service, 11, 'A private candidate request')
    for count in range(1, 4):
        request_id = callback_retry_id('synthetic-callback-' + str(count))
        assert begin_retry(service, 11, request_id=request_id)['attempts'] == count
        assert begin_retry(service, 11, request_id=request_id)['attempts'] == count
    with pytest.raises(ValueError, match='retry limit'):
        begin_retry(service, 11, request_id=callback_retry_id('synthetic-callback-4'))


def test_clear_is_bound_to_actor_and_exact_intent_reference(service):
    first = save_recovery(service, 11, 'First candidate request')
    second = save_recovery(service, 22, 'Second candidate request')
    assert not clear_recovery(service, 22, first['reference'])
    assert get_recovery(service, 22)['text'] == 'Second candidate request'
    newer = save_recovery(service, 11, 'Replacement request')
    with pytest.raises(ValueError, match='older request'):
        begin_retry(service, 11, request_id=100, reference=first['reference'])
    assert get_recovery(service, 11)['attempts'] == 0
    assert not clear_recovery(service, 11, first['reference'])
    assert clear_recovery(service, 11, newer['reference'])
    assert get_recovery(service, 11) is None
    assert get_recovery(service, 22)['reference'] == second['reference']


@pytest.mark.parametrize('operation', ['suspend', 'revoke'])
def test_removed_access_cannot_recover_old_intent_after_reinvitation(service, operation):
    saved = save_recovery(service, 11, 'Old private request')
    service.admin(1, operation, 11)
    with pytest.raises(PermissionError):
        get_recovery(service, 11)
    service.admin(1, 'add', 11)
    service.authorize(11)
    assert get_recovery(service, 11) is None
    assert save_recovery(service, 11, 'Old private request')['reference'] != saved['reference']


def test_expired_intent_cannot_be_retried(service, monkeypatch):
    import jobhunter_service.recovery as recovery
    clock = [time.time()]
    monkeypatch.setattr(recovery, 'time', SimpleNamespace(time=lambda: clock[0]))
    save_recovery(service, 11, 'An expired request')
    clock[0] += recovery.RECOVERY_TTL_SECONDS + 1
    assert get_recovery(service, 11) is None
    with pytest.raises(ValueError, match='no pending request'):
        begin_retry(service, 11)


def test_resume_recovery_uses_only_fixed_intent_and_keeps_source_out_of_record(service):
    save_recovery(service, 11, 'Private uploaded document body', kind='resume')
    assert get_recovery(service, 11)['text'] == RESUME_RETRY_INTENT
    with service.store.connect() as db:
        row = db.execute('SELECT * FROM onboarding_recovery WHERE user_id=11').fetchone()
    assert 'Private uploaded document body' not in repr(dict(row))


@pytest.mark.parametrize('text', ['', 'x' * 6001, 'password: synthetic-secret'])
def test_sensitive_or_unbounded_intent_is_not_saved(service, text):
    with pytest.raises(ValueError):
        save_recovery(service, 11, text)
    assert get_recovery(service, 11) is None


def test_unregistered_actor_cannot_read_or_create_recovery(service):
    with pytest.raises(PermissionError):
        save_recovery(service, 99, 'A request')
    with pytest.raises(PermissionError):
        get_recovery(service, 99)
