"""Notification filtering must never turn a receipt heading into a lost offer."""

import argparse
import base64
from contextlib import closing
from html.parser import HTMLParser
import json
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_integrations import gmail_monitor as monitor, gmail_watcher as watcher
from test_google_integrations import make_application_db


@pytest.mark.parametrize('routine', [
    {'acknowledgement': True}, {'notice': 'account_registered'}, {'notice': 'welcome'},
])
def test_routine_only_run_stays_silent_but_checkpoints_message(tmp_path, monkeypatch, routine):
    state = {'seen_message_ids': ['routine']}
    monkeypatch.setattr(watcher, 'collect_mail', Mock(return_value=([routine], state)))
    args = argparse.Namespace(state_path=tmp_path/'seen.json', max_messages=25)
    send = Mock()
    monitor.run_monitor(args, send)
    send.assert_not_called()
    assert json.loads(args.state_path.read_text()) == state
    assert not args.state_path.with_suffix('.outbox.json').exists()


@pytest.mark.parametrize('outcome', list(watcher.OUTCOME_ALERTS))
def test_genuine_outcome_wins_over_routine_flags_even_without_application_match(outcome):
    batches = monitor.notification_batches([{
        'outcome': outcome, 'acknowledgement': True, 'notice': 'account_registered',
        'subject': 'Welcome to Example Careers', 'from': 'Recruiter <recruiter@example.test>',
    }])
    assert len(batches) == 1
    assert watcher.OUTCOME_ALERTS[outcome][1] in batches[0]
    assert 'Couldn’t link this email' in batches[0]


@pytest.mark.parametrize('body,outcome', [
    ('We would like to offer you the position of Technical Lead.', 'offer_received'),
    ('We would like to offer you the position of Technical Lead, if you accept please sign the documents.', 'offer_received'),
    ('We would like to offer you the position of Technical Lead if you agree to the terms.', 'offer_received'),
    ('Your job offer has been accepted.', 'offer_received'),
    ('We have received your signed offer.', 'offer_received'),
    ('Your application has been successful.', 'application_progressed'),
    ('Your application for Technical Lead has been accepted.', 'application_progressed'),
    ('We have accepted your application.', 'application_progressed'),
    ('We would like to schedule an interview.', 'interview_invited'),
])
@pytest.mark.parametrize('subject', ['Welcome to Example Careers', 'Application received', 'Account creation'])
def test_positive_reply_under_routine_subject_is_classified_and_notified(tmp_path, monkeypatch, body, outcome, subject):
    message = {'subject': subject, 'body_text': 'We received your application. '+body,
               'text': 'Example Company '+body}
    jobs = [{'id': 'example', 'company': 'Example Company', 'title': 'Technical Lead', 'stage': 'submitted'}]
    writer = Mock(return_value={'status': 'updated', 'tracker_synced': True})
    monkeypatch.setattr(watcher, 'record_application_outcome', writer)
    watcher.process_application_outcome(message, jobs, tmp_path/'unused.db')
    assert message['outcome'] == outcome
    writer.assert_called_once()
    assert 'notice' not in message
    alert, = monitor.notification_batches([message])
    assert watcher.OUTCOME_ALERTS[outcome][1] in alert
    assert '<b>Example Company</b>\nTechnical Lead' in alert
    assert '<b>Next:</b>' in alert
    assert 'Account registered' not in alert
    assert 'Application received</b>' not in alert


@pytest.mark.parametrize('body', [
    'Your application has been successfully submitted.',
    "You've successfully applied to the role.",
    "We'll review your application carefully and get back to you.",
    'We are currently processing your application.',
    'We will read and review your submitted application.',
    'We received your application. If selected, we will send an employment contract.',
    'We received your application. If selected, we would like to offer you the position of Technical Lead.',
])
def test_successfully_applied_and_conditional_promises_are_quiet(tmp_path, body):
    message = {'subject': 'Application update', 'body_text': body, 'text': body}
    watcher.process_application_outcome(message, [], tmp_path/'unused.db')
    assert message['acknowledgement']
    assert not monitor.notification_batches([message])


def test_uncertain_hiring_update_is_reviewed_instead_of_hidden_as_welcome(tmp_path):
    body = 'Your account has been created. Please review the employment agreement and confirm your start date.'
    message = {'subject': 'Welcome to Example Company', 'body_text': body, 'text': body,
               'snippet': body+' Password: test-only-secret; code: 123456; https://example.test/private'}
    watcher.process_application_outcome(message, [], tmp_path/'unused.db')
    assert 'outcome' not in message
    assert message['review_reason']
    alert, = monitor.notification_batches([message])
    assert 'Possible offer or hiring update' in alert
    assert 'Review needed' in alert
    assert 'test-only-secret' not in alert
    assert '123456' not in alert
    assert 'https://' not in alert


def test_untracked_offer_and_offer_mentioning_drive_are_relevant():
    body = 'We would like to offer you the position of Technical Lead. Review the document in Google Drive.'
    assert watcher.is_relevant(body, [], sender='Recruiter <recruiter@example.test>',
                               subject='Welcome', body_text=body)[0]
    assert not watcher.is_relevant(body, [], sender='drive-shares-dm-noreply@google.com',
                                   subject='Spreadsheet shared with you', body_text=body)[0]


@pytest.mark.parametrize('delivery_fails', [False, True])
def test_complete_monitor_suppresses_receipts_and_retries_offer_delivery(tmp_path, monkeypatch, delivery_fails):
    db = make_application_db(tmp_path)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("UPDATE jobs SET company='Example Company',title='Technical Lead'")
    service = Mock()
    copies = {
        'account': 'Your candidate account has been created.',
        'receipt': 'Your application has been successfully submitted.',
        'offer': 'We would like to offer you the position of Technical Lead. Please verify your email to view the offer.',
    }
    service.users().messages().list().execute.return_value = {'messages': [{'id': key} for key in copies]}
    def message(**kwargs):
        return Mock(execute=lambda: {'payload': {'mimeType': 'text/plain', 'headers': [
            {'name': 'Subject', 'value': 'Welcome to Example Company Careers'},
            {'name': 'From', 'value': 'Recruiter <recruiter@example.test>'}],
            'body': {'data': base64.urlsafe_b64encode(copies[kwargs['id']].encode()).decode()}}})
    service.users().messages().get.side_effect = message
    monkeypatch.setattr(watcher, 'gmail_service', lambda _: service)
    sync = Mock(return_value=True)
    args = argparse.Namespace(google_token=tmp_path/'fake-token', db_path=db, state_path=tmp_path/'seen.json',
                              max_messages=25, query='newer_than:30d', tracker_sync=sync)
    send = Mock(return_value=not delivery_fails)
    if delivery_fails:
        with pytest.raises(RuntimeError, match='remains queued'):
            monitor.run_monitor(args, send)
        assert not args.state_path.exists()
        pending = json.loads(args.state_path.with_suffix('.outbox.json').read_text())
        assert len(pending['notifications']) == 1
        retry = Mock(return_value=True)
        monitor.run_monitor(args, retry)
        retry.assert_called_once_with(pending['notifications'][0])
    else:
        monitor.run_monitor(args, send)
    send.assert_called_once()
    alert = send.call_args.args[0]
    assert 'Job offer received' in alert
    assert 'We would like to offer you the position' in alert
    assert 'Account registered' not in alert and 'Application received</b>' not in alert
    assert json.loads(args.state_path.read_text())['seen_message_ids'] == ['account', 'offer', 'receipt']
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute('SELECT stage,submitted_at FROM applications').fetchone() == (
            'offer_received', '2026-07-10T15:21:41+00:00')
    sync.assert_called_once()
    service.users().messages().get.assert_any_call(userId='me', id='offer', format='full')
    assert service.users().messages().get.call_count == 3
    service.users().messages().modify.assert_not_called()


def test_offer_first_cards_are_escaped_complete_and_valid_telegram_html():
    class BalancedHTML(HTMLParser):
        def __init__(self):
            super().__init__(); self.stack = []
        def handle_starttag(self, tag, attrs):
            assert tag == 'b' and not attrs; self.stack.append(tag)
        def handle_endtag(self, tag):
            assert self.stack.pop() == tag
    message = {'matched_job': {'company': 'Example & <Company>', 'title': 'Technical Lead <Java>'},
               'outcome_detail': 'Review the offer <today> & confirm your decision.',
               'application_update': {'status': 'updated', 'tracker_synced': True}}
    batches = monitor.notification_batches([
        {**message, 'outcome': 'rejected'}, {**message, 'outcome': 'offer_received'},
        {'acknowledgement': True}, {'notice': 'welcome'},
    ])
    alert, = batches
    assert alert.index('Job offer received') < alert.index('Application rejected')
    assert '<b>Example &amp; &lt;Company&gt;</b>' in alert
    assert 'Technical Lead &lt;Java&gt;' in alert
    assert 'Review the offer terms and respond' in alert
    assert 'Database and spreadsheet updated' in alert
    assert len(alert) <= 3500
    parser = BalancedHTML(); parser.feed(alert); parser.close(); assert parser.stack == []
