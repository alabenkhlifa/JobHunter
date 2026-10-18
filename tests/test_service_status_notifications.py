"""Completed reviews survive Telegram outages; private status retries are durable."""
import json
import sqlite3

import pytest
import requests

from jobhunter_service.diagnostics import error_category
from jobhunter_service.state import Store, private_json
from jobhunter_service.telegram import TelegramAPIError
from test_service_core import confirm
from test_service_orchestration import system, queue


def no_approved_jobs(system):
    scheduler = system[1]
    child = scheduler._child

    def prepare(*args):
        if args[0] == 'prepare-delivery':
            from pathlib import Path
            private_json(Path(args[args.index('--output') + 1]),
                         {'selected_ids': [], 'queued_count': 4, 'availability': {}})
        else:
            child(*args)

    scheduler._child = prepare
    return scheduler


def test_successful_review_is_complete_while_private_status_delivery_retries(system, monkeypatch):
    service, _, client, _, _ = system
    scheduler = no_approved_jobs(system)
    queue(system)
    assert scheduler.run_next()
    # Collection/review do not attempt direct Telegram delivery.
    assert client.sends == []
    monkeypatch.setattr(scheduler.delivery, '_preflight', lambda *args: pytest.fail('Status cannot depend on source checks'))
    client.fail_chats.add('123')
    assert scheduler.delivery.drain() == {'sent': 0, 'pending_or_failed': 1}
    with service.store.connect() as db:
        run = db.execute('SELECT status,error FROM runs WHERE user_id=123').fetchone()
        assert tuple(run) == ('complete', None)
        row = db.execute('SELECT chat_id,kind,status,attempts,last_error,message FROM deliveries').fetchone()
        assert tuple(row)[:5] == ('123', 'notice', 'pending', 1, 'RuntimeError')
        assert 'completed' in row['message'] and '4 remain queued' in row['message']
        assert 'could not complete' not in row['message']
        assert db.execute('SELECT count(*) FROM delivery_jobs').fetchone()[0] == 0
    client.fail_chats.clear()
    assert scheduler.delivery.drain() == {'sent': 1, 'pending_or_failed': 0}
    assert scheduler.delivery.drain() == {'sent': 0, 'pending_or_failed': 0}
    assert {chat for chat, _ in client.sends} == {'123'}
    with service.store.connect() as db:
        assert tuple(db.execute('SELECT status,attempts,last_error FROM deliveries').fetchone()) == ('sent', 2, None)


def test_notice_private_delivery_is_independent_of_configured_channel(system):
    service, _, client, _, _ = system
    confirm(service, 123, {'telegram': {'destinations': [{'kind': 'channel', 'chat_id': '-1000', 'label': 'Jobs'}]}})
    scheduler = no_approved_jobs(system)
    queue(system)
    assert scheduler.run_next()
    assert scheduler.delivery.drain()['sent'] == 1
    assert [chat for chat, _ in client.sends] == ['123']


def test_empty_collection_uses_durable_notice_without_model_call(system):
    service, scheduler, client, _, _ = system
    def empty(*args):
        from pathlib import Path
        assert args[0] == 'collect'
        private_json(Path(args[args.index('--output') + 1]), {'candidates': []})
    scheduler._child = empty
    queue(system)
    assert scheduler.run_next()
    scheduler.planner.assert_not_called()
    assert client.sends == []
    assert scheduler.delivery.drain()['sent'] == 1
    assert 'search completed' in client.sends[-1][1]


def test_actual_run_failure_retains_safe_stage_and_retryable_notice(system, caplog):
    service, scheduler, client, _, _ = system
    def failed(*args):
        raise RuntimeError('PRIVATE_TOKEN_AND_RESUME_CONTENT')
    scheduler._child = failed
    queue(system)
    assert scheduler.run_next()
    assert client.sends == []
    with service.store.connect() as db:
        row = db.execute('SELECT status,error FROM runs WHERE user_id=123').fetchone()
        assert row['status'] == 'failed'
        assert json.loads(row['error']) == {'stage': 'collection', 'category': 'RuntimeError'}
        assert 'PRIVATE' not in row['error']
    assert 'PRIVATE' not in caplog.text
    assert 'collection' in caplog.text
    assert scheduler.delivery.drain()['sent'] == 1
    assert 'search could not complete' in client.sends[-1][1]


def test_notice_is_idempotent_and_cancelled_after_settings_change(system):
    service, _, client, _, _ = system
    scheduler = no_approved_jobs(system)
    queue(system)
    scheduler.run_next()
    with service.store.connect() as db:
        run_id = db.execute('SELECT id FROM runs WHERE user_id=123').fetchone()[0]
    scheduler.delivery.enqueue_notice(123, run_id, 'Duplicate replay')
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 1
    confirm(service, 123, {'search': {'keywords': ['new preference']}})
    assert scheduler.delivery.drain()['sent'] == 0
    assert client.sends == []
    with service.store.connect() as db:
        assert db.execute('SELECT status FROM deliveries').fetchone()[0] == 'cancelled'


def test_notice_cannot_use_another_candidates_run(system):
    service, scheduler, _, _, _ = system
    queue(system)
    with service.store.connect() as db:
        run_id = db.execute('SELECT id FROM runs WHERE user_id=123').fetchone()[0]
    with pytest.raises(PermissionError):
        scheduler.delivery.enqueue_notice(456, run_id, 'Foreign status')
    with service.store.connect() as db:
        assert db.execute('SELECT count(*) FROM deliveries').fetchone()[0] == 0


def test_digest_controls_survive_multipart_delivery_without_reaching_channels(system):
    service, scheduler, client, roots, _ = system
    queue(system)
    with service.store.connect() as db:
        run_id = db.execute('SELECT id FROM runs WHERE user_id=123').fetchone()[0]
    with sqlite3.connect(roots[123] / 'jobs.db') as db:
        db.execute("INSERT INTO jobs SELECT 'second-id',notified,status,title,company,location,url,ai_verdict FROM jobs WHERE id='same-id'")
    scheduler.delivery.enqueue(123, run_id, 'x' * 6500, ['second-id', 'same-id'])
    with service.store.connect() as db:
        assert [tuple(row) for row in db.execute('SELECT job_id,position FROM delivery_jobs ORDER BY position')] == [('second-id', 1), ('same-id', 2)]
        rows = db.execute('SELECT chat_id,part,reply_markup FROM deliveries ORDER BY chat_id,part').fetchall()
        private = [row for row in rows if row['chat_id'] == '123']
        assert len(private) > 1 and all(row['reply_markup'] is None for row in private[:-1])
        buttons = json.loads(private[-1]['reply_markup'])['inline_keyboard']
        assert buttons[0][0]['callback_data'] == 'jh:job:details:second-id'
        assert all(row['reply_markup'] is None for row in rows if row['chat_id'] == '-1000')
    recorded = []
    real_send = client.send_message
    def send(chat, message, reply_markup=None):
        recorded.append((str(chat), reply_markup))
        return real_send(chat, message, reply_markup=reply_markup)
    client.send_message = send
    assert scheduler.delivery.drain()['pending_or_failed'] == 0
    assert len([markup for chat, markup in recorded if markup]) == 1
    assert all(markup is None for chat, markup in recorded if chat == '-1000')


def test_wrapped_telegram_timeout_has_safe_diagnostic_category():
    try:
        try:
            raise requests.ReadTimeout('secret token URL and candidate text')
        except requests.RequestException:
            raise TelegramAPIError('Public-safe transport error') from None
    except TelegramAPIError as error:
        assert error_category(error) == 'timeout'


def test_existing_outbox_upgrade_preserves_receipts_and_is_repeatable(tmp_path):
    registry = tmp_path / 'old.sqlite3'
    with sqlite3.connect(registry) as db:
        db.executescript('''
            CREATE TABLE deliveries(id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,user_id INTEGER NOT NULL,
                chat_id TEXT NOT NULL,part INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',message_id INTEGER,attempts INTEGER NOT NULL DEFAULT 0,
                UNIQUE(run_id,chat_id,part));
            CREATE TABLE delivery_jobs(run_id TEXT NOT NULL,user_id INTEGER NOT NULL,job_id TEXT NOT NULL,
                PRIMARY KEY(run_id,job_id));
            INSERT INTO deliveries VALUES(1,'old-run',123,'123',0,'Synthetic digest','sent',742,1);
            INSERT INTO delivery_jobs VALUES('old-run',123,'job-1');
        ''')
    for _ in range(2):
        store = Store(registry)
        with store.connect() as db:
            row = db.execute('SELECT * FROM deliveries').fetchone()
            assert row['message_id'] == 742 and row['status'] == 'sent' and row['attempts'] == 1
            assert row['kind'] == 'digest' and row['reply_markup'] is None and row['last_error'] is None
            assert db.execute('SELECT position FROM delivery_jobs').fetchone()[0] == 0
