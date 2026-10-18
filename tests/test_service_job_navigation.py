"""Candidate text/buttons reach only their own application backend."""
from contextlib import closing
import json
import sqlite3
import time
from unittest.mock import Mock

import pytest

from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.presentation import digest_markup
from jobhunter_service.telegram_ingress import TelegramIngress
from test_service_applications import app


def message(text, *, actor=11, update_id=1):
    return {'update_id': update_id, 'message': {'message_id': update_id + 1,
        'from': {'id': actor, 'is_bot': False}, 'chat': {'id': actor, 'type': 'private'}, 'text': text}}


def callback(data, *, actor=11, update_id=1):
    return {'update_id': update_id, 'callback_query': {'id': f'button-{update_id}', 'data': data,
        'from': {'id': actor, 'is_bot': False}, 'message': {'message_id': 99,
        'from': {'id': 999, 'is_bot': True}, 'chat': {'id': actor, 'type': 'private'}}}}


def navigation(app, planner=None):
    facade, service, roots, pages, browser, client = app
    planner = planner or Mock(return_value={'operation': 'reply', 'reply': 'What would you like to change?'})
    handler = ApplicationTelegramHandler(service, client, RestrictedHermesAssistant(planner))
    handler.applications = facade
    return handler, planner


def add_job(root, job_id, title, company, status='hold'):
    with closing(sqlite3.connect(root / 'jobs.db')) as db, db:
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)', (job_id, title, company, 'Remote',
                    f'https://jobs.example.test/{job_id}', 'Synthetic public listing', status))


def delivered(service, root, ids, *, actor=11, run_id='digest-1', sent=True, legacy=False, artifact=True):
    now = time.time()
    with service.store.connect() as db:
        member = service._member(actor)
        db.execute('INSERT INTO runs(id,user_id,local_slot,revision,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                   (run_id, actor, run_id, member['revision'], 'complete', now, now))
        db.execute('INSERT INTO deliveries(run_id,user_id,chat_id,part,message,status,message_id) VALUES(?,?,?,?,?,?,?)',
                   (run_id, actor, str(actor), 0, 'Synthetic digest', 'sent' if sent else 'pending', 10 if sent else None))
        columns = {row['name'] for row in db.execute('PRAGMA table_info(delivery_jobs)')}
        for number, job_id in enumerate(ids, 1):
            if 'position' in columns:
                db.execute('INSERT INTO delivery_jobs(run_id,user_id,job_id,position) VALUES(?,?,?,?)',
                           (run_id, actor, job_id, 0 if legacy else number))
            else:
                db.execute('INSERT INTO delivery_jobs VALUES(?,?,?)', (run_id, actor, job_id))
    if artifact:
        path = root / 'state' / 'runs' / run_id / 'delivery-output.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'selected_ids': ids}))


def test_natural_job_request_uses_only_candidate_database_without_the_model(app):
    handler, planner = navigation(app)
    handler.handle_update(message('details job same-job'), use_offset=False)
    client = app[-1]
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'Company 11' in text and 'Company 22' not in text
    markup = client.send_message.call_args.kwargs['reply_markup']
    assert markup['inline_keyboard'][0][2]['callback_data'] == 'jh:job:prepare:same-job'
    planner.assert_not_called()


def test_numbers_follow_latest_acknowledged_digest_order_not_database_row_order(app):
    handler, planner = navigation(app)
    _, service, roots, _, _, client = app
    add_job(roots[11], 'second-job', 'Different role', 'Own company')
    delivered(service, roots[11], ['second-job', 'same-job'])
    delivered(service, roots[11], ['same-job'], run_id='pending-newer', sent=False)
    handler.handle_update(message('show details for job 1'), use_offset=False)
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'Different role' in text and 'Role for 11' not in text
    planner.assert_not_called()


def test_legacy_numbers_require_validated_original_order_or_clarification(app):
    handler, _ = navigation(app)
    facade, service, roots, _, _, client = app
    delivered(service, roots[11], ['same-job'], legacy=True, artifact=False)
    handler.handle_update(message('prepare job 1'), use_offset=False)
    facade._run.assert_not_called()
    assert 'Choose the exact role' in client.send_message.call_args.args[1]
    path = roots[11] / 'state/runs/digest-1/delivery-output.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'selected_ids': ['another-candidates-job']}))
    handler.handle_update(message('prepare job 1', update_id=2), use_offset=False)
    facade._run.assert_not_called()


def test_repeated_titles_ask_for_a_specific_job_without_mutation(app):
    handler, planner = navigation(app)
    facade, _, roots, _, _, client = app
    add_job(roots[11], 'duplicate-role', 'Role for 11', 'Another company')
    handler.handle_update(message('prepare Role for 11'), use_offset=False)
    facade._run.assert_not_called()
    planner.assert_not_called()
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'Choose the exact role' in text and 'same-job' in text and 'duplicate-role' in text
    assert all(button['text'] == 'Prepare' for row in client.send_message.call_args.kwargs['reply_markup']['inline_keyboard'] for button in row if button['callback_data'].startswith('jh:job:prepare:'))


def test_foreign_candidate_job_callback_never_reaches_backend(app):
    handler, planner = navigation(app)
    facade, _, roots, _, _, client = app
    add_job(roots[22], 'private-other', 'Other candidate role', 'Private company')
    handler.handle_update(callback('jh:job:prepare:private-other'), use_offset=False)
    facade._run.assert_not_called()
    planner.assert_not_called()
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'not in your own collected jobs' in text
    assert 'Private company' not in text


@pytest.mark.parametrize('operation', ['submit', 'upload', 'shell', 'confirm'])
def test_navigation_callbacks_cannot_skip_application_approvals(app, operation):
    handler, _ = navigation(app)
    facade = app[0]
    handler.handle_update(callback(f'jh:job:{operation}:same-job'), use_offset=False)
    facade._run.assert_not_called()


@pytest.mark.parametrize('text', ['yes', 'confirm', 'I am interested in Role for 11',
    'show my settings', 'show my schedule', 'show status', 'show my tracker', 'show my Gmail connection'])
def test_profile_answers_and_settings_conversation_do_not_create_application_actions(app, text):
    handler, planner = navigation(app)
    handler.handle_update(message(text), use_offset=False)
    app[0]._run.assert_not_called()
    planner.assert_called_once()


def test_nonexplicit_role_reference_during_resume_review_remains_a_conversation(app, monkeypatch):
    handler, planner = navigation(app)
    monkeypatch.setattr(handler.service, 'onboarding_status', lambda *args, **kwargs: {
        'started': True, 'complete': False, 'next_step': 'resume'})
    handler.handle_update(message('interested in Role for 11'), use_offset=False)
    app[0]._run.assert_not_called()
    planner.assert_called_once()


def test_show_collected_jobs_includes_held_roles_without_changing_number_anchor(app):
    handler, planner = navigation(app)
    _, service, roots, _, _, client = app
    delivered(service, roots[11], ['same-job'])
    add_job(roots[11], 'held-role', 'Held iOS role', 'Needs evidence')
    handler.handle_update(message('show my collected jobs'), use_offset=False)
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'Held iOS role' in text and 'Role for 11' in text
    assert '1. Held' not in text
    assert client.send_message.call_args.kwargs['reply_markup']['inline_keyboard'][0][0]['text'] == 'Details'
    client.reset_mock()
    handler.handle_update(message('details job 1', update_id=2), use_offset=False)
    text = '\n'.join(call.args[1] for call in client.send_message.call_args_list)
    assert 'Role for 11' in text and 'Held iOS role' not in text
    planner.assert_not_called()


def test_synthetic_end_to_end_natural_text_then_digest_button_prepares_only_own_resume(app):
    handler, planner = navigation(app)
    facade, service, roots, _, browser, client = app
    delivered(service, roots[11], ['same-job'])
    ingress = TelegramIngress(service, handler)
    assert ingress.enqueue(message('show my jobs'))['accepted'] is True
    assert ingress.drain_one()
    markup = client.send_message.call_args.kwargs['reply_markup']
    assert markup == digest_markup(['same-job'])
    prepare_button = markup['inline_keyboard'][0][2]['callback_data']
    ingress.enqueue(callback(prepare_button, update_id=2))
    assert ingress.drain_one()
    assert facade._run.call_args.args[1]['operation'] == 'prepare'
    assert facade._run.call_args.args[1]['include_cover_letter'] is False
    assert client.send_document.call_args.args[0] == 11
    assert client.send_document.call_args.args[1].is_relative_to(roots[11])
    assert not list((roots[22] / 'output').iterdir())
    with service.store.connect() as db:
        assert [row['status'] for row in db.execute('SELECT status FROM telegram_ingress ORDER BY id')] == ['done', 'done']
        assert db.execute('SELECT COUNT(*) FROM tokens WHERE purpose=?', ('application_approval',)).fetchone()[0] == 0
    browser.status.assert_not_called()
    planner.assert_not_called()
