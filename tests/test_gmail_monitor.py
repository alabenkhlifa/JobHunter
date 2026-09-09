import argparse
import fcntl
import json
from unittest.mock import Mock

import pytest

from jobhunter_integrations import gmail_monitor as monitor


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    args = argparse.Namespace(state_path=tmp_path / "seen.json", max_messages=25)
    args.state_path.write_text('{"seen_message_ids":[]}')
    messages = [{"subject": f"Application reply {n}", "snippet": "Interview invitation"} for n in range(7)]
    state = {"seen_message_ids": [f"m{n}" for n in range(7)]}
    collector = Mock(return_value=(messages, state))
    monkeypatch.setattr(monitor.gmail_watcher, "collect_mail", collector)
    return args, messages, state, collector


def test_delivery_failure_preserves_outbox_and_retries_only_unsent_batches(mailbox):
    args, messages, state, collector = mailbox
    send = Mock(side_effect=[True, False])
    with pytest.raises(RuntimeError, match="remains queued"):
        monitor.run_monitor(args, send)
    assert json.loads(args.state_path.read_text())["seen_message_ids"] == []
    outbox = args.state_path.with_suffix(".outbox.json")
    remaining = json.loads(outbox.read_text())["notifications"]
    assert len(remaining) == 1
    assert outbox.stat().st_mode & 0o777 == 0o600
    retry = Mock(return_value=True)
    monitor.run_monitor(args, retry)
    retry.assert_called_once_with(remaining[0])
    collector.assert_called_once()
    assert json.loads(args.state_path.read_text()) == state
    assert not outbox.exists()


def test_checkpoint_failure_after_delivery_does_not_resend(mailbox, monkeypatch):
    args, _, state, collector = mailbox
    original = monitor.write_private_json

    def write(path, value):
        if path == args.state_path:
            raise OSError("test checkpoint failure")
        original(path, value)

    monkeypatch.setattr(monitor, "write_private_json", write)
    with pytest.raises(OSError):
        monitor.run_monitor(args, Mock(return_value=True))
    monkeypatch.setattr(monitor, "write_private_json", original)
    retry = Mock(return_value=True)
    monitor.run_monitor(args, retry)
    retry.assert_not_called()
    collector.assert_called_once()
    assert json.loads(args.state_path.read_text()) == state


def test_no_relevant_mail_is_silent_and_advances_ledger(mailbox):
    args, _, state, collector = mailbox
    collector.return_value = ([], state)
    send = Mock()
    monitor.run_monitor(args, send)
    send.assert_not_called()
    assert json.loads(args.state_path.read_text()) == state


def test_concurrent_run_does_not_inspect_or_send(mailbox):
    args, _, _, collector = mailbox
    send = Mock()
    with args.state_path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        monitor.run_monitor(args, send)
    collector.assert_not_called()
    send.assert_not_called()


def test_batches_include_all_replies_and_bound_escaped_headers(mailbox):
    _, messages, _, _ = mailbox
    batches = monitor.notification_batches(messages)
    assert len(batches) == 2
    for message in messages:
        assert sum(message["subject"] in batch for batch in batches) == 1
    noisy = {key: "&" * 4000 for key in ("subject", "from", "date", "snippet")}
    noisy["reasons"] = ["&" * 4000] * 10
    assert all(len(batch) <= 3500 for batch in monitor.notification_batches([noisy] * 6))


def test_corrupt_outbox_fails_without_sending_or_losing_it(mailbox):
    args, _, _, collector = mailbox
    outbox = args.state_path.with_suffix(".outbox.json")
    outbox.write_text("broken")
    with pytest.raises(RuntimeError):
        monitor.run_monitor(args, Mock())
    assert outbox.read_text() == "broken"
    collector.assert_not_called()


@pytest.mark.parametrize("sync_success", [True, False])
def test_scheduled_check_retries_tracker_without_new_mail(mailbox, monkeypatch, capsys, sync_success):
    args, _, state, collector = mailbox
    collector.return_value = ([], state)
    monkeypatch.setattr(monitor.gmail_watcher, "parse_args", lambda argv: args)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "test-chat")
    monkeypatch.setenv("JOBHUNTER_AUTO_SYNC_TRACKER", "true")
    sync = Mock(return_value=sync_success)
    send = Mock()
    monkeypatch.setattr("scraper.sync_application_tracker_if_enabled", sync)
    monkeypatch.setattr("scraper.send_telegram", send)
    assert monitor.main([]) == (0 if sync_success else 1)
    sync.assert_called_once_with()
    send.assert_not_called()
    assert json.loads(args.state_path.read_text()) == state
    output = capsys.readouterr().out
    assert output == "" if sync_success else "tracker sync failed" in output
