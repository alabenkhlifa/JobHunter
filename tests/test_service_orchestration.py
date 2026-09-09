"""Cross-module scheduler, delivery, account-state and private command contracts."""

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_service.account_state import connection_matches, reconcile_account_settings
from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.service import JobHunterService
from jobhunter_service.state import private_json
from test_service_core import Telegram, ready, confirm


@pytest.fixture
def system(tmp_path, monkeypatch):
    client = Telegram()
    service = JobHunterService(tmp_path, 900, telegram_client=client)
    ready(service, 123, clock="09:00", channels=True)
    ready(service, 456, zone="Asia/Dubai", clock="13:00")
    roots = {}
    for actor in (123, 456):
        root = service.profile_dir(service._member(actor))
        roots[actor] = root
        with closing(sqlite3.connect(root / "jobs.db")) as db, db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,notified INTEGER,status TEXT,title TEXT,company TEXT,location TEXT,url TEXT,ai_verdict TEXT)")
            db.execute("INSERT INTO jobs VALUES(?,0,'new',?,?,?,?,NULL)",
                       ("same-id", f"Role {actor}", f"Company {actor}", "France", f"https://example.test/{actor}"))
    manifests = {}
    child_calls = []
    def planner(messages, schema):
        content = json.loads(messages[1]["content"])
        actor = int(content["candidate_confirmed_profile"]["name"].split()[-1])
        manifest = manifests[actor]
        assert content["candidates"][0]["title"] == f"Role {actor}"
        return {**{key: manifest[key] for key in ("profile_id", "run_id", "revision", "config_digest", "manifest_digest")},
                "verdicts": [{"job_id": "same-id", "verdict": "send", "sponsorship": "excluded", "reason": "Confirmed skills match", "rank": 1}]}
    scheduler = Scheduler(service, Mock(side_effect=planner), client)
    monkeypatch.setattr(scheduler.delivery, '_preflight', lambda *args, **kwargs: 'open')
    def child(*args):
        child_calls.append(args)
        actor = int(args[args.index("--profile") + 1][1:])
        root = roots[actor]
        output = Path(args[args.index("--output") + 1])
        assert output.is_relative_to(root / "state/runs")
        assert args[args.index("--data-root") + 1] == str(service.root)
        if args[0] == "collect":
            member = service._member(actor)
            settings = json.loads(member["settings"])
            run_id = args[args.index("--run-id") + 1]
            manifest = {"profile_id": member["profile_id"], "run_id": run_id, "revision": member["revision"],
                        "config_digest": f"config-{actor}", "manifest_digest": f"manifest-{actor}",
                        "master_profile": settings["resume"], "policy": settings["search"],
                        "candidates": [{"id": "same-id", "title": f"Role {actor}"}]}
            manifests[actor] = manifest
            private_json(output, manifest)
        elif args[0] == 'prepare-delivery':
            private_json(output, {'selected_ids': ['same-id'], 'queued_count': 0, 'availability': {'unknown': 0}})
        else:
            envelope = json.loads(Path(args[args.index("--input") + 1]).read_text())
            assert envelope["profile_id"] == f"u{actor}"
            assert envelope["config_digest"] == f"config-{actor}"
            assert envelope["verdicts"][0]["job_id"] == "same-id"
            with closing(sqlite3.connect(root / "jobs.db")) as db, db:
                db.execute("UPDATE jobs SET ai_verdict='send' WHERE id='same-id'")
            private_json(output, {"selected_ids": ["same-id"], "queued_count": 0})
    scheduler._child = child
    return service, scheduler, client, roots, child_calls


def queue(system):
    return system[1].queue_due(datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc))


def test_scheduler_review_reaches_durable_per_candidate_delivery_then_independent_retry(system):
    service, scheduler, client, roots, calls = system
    assert queue(system) == 2 and queue(system) == 0
    assert scheduler.run_next() and scheduler.run_next() and not scheduler.run_next()
    assert [call[0] for call in calls] == ["collect", "review", "prepare-delivery", "collect", "review", "prepare-delivery"]
    assert scheduler.planner.call_count == 2
    with service.store.connect() as db:
        rows = db.execute("SELECT user_id,chat_id,message FROM deliveries ORDER BY id").fetchall()
        assert len(rows) == 3
        assert all(f"Role {row['user_id']}" in row["message"] for row in rows)
        assert all(f"Company {456 if row['user_id'] == 123 else 123}" not in row["message"] for row in rows)
        assert {row[0] for row in db.execute("SELECT status FROM runs")} == {"complete"}
    client.fail_chats.add("-1000")
    assert scheduler.delivery.drain() == {"sent": 2, "pending_or_failed": 1}
    with closing(sqlite3.connect(roots[123] / "jobs.db")) as db:
        assert db.execute("SELECT ai_verdict,notified FROM jobs").fetchone() == ("send", 1)
    count = len(client.sends)
    client.fail_chats.clear()
    assert scheduler.delivery.drain()["sent"] == 1
    assert [chat for chat, _ in client.sends[count:]] == ["-1000"]


def test_revision_change_after_review_prevents_digest_delivery(system):
    service, scheduler, client, _, _ = system
    queue(system)
    original = scheduler._child
    def child(*args):
        original(*args)
        if args[0] == "review":
            confirm(service, 123, {"search": {"keywords": ["changed role"]}})
    scheduler._child = child
    scheduler.run_next()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0
        assert db.execute("SELECT status FROM runs WHERE user_id=123").fetchone()[0] == "failed"
    assert not any("Role 123" in text for _, text in client.sends)


def test_revision_change_at_enqueue_boundary_cannot_deliver_stale_review(system):
    service, scheduler, _, _, _ = system
    queue(system)
    original = scheduler.delivery.enqueue
    def enqueue(*args):
        confirm(service, 123, {"search": {"keywords": ["changed after final check"]}})
        return original(*args)
    scheduler.delivery.enqueue = enqueue
    scheduler.run_next()
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0] == 0


def test_configured_collection_page_limit_is_passed_to_child(system):
    service, scheduler, _, _, calls = system
    confirm(service, 123, {"search": {"max_pages": 3}})
    queue(system)
    scheduler.run_next()
    args = calls[0]
    assert "--max-pages" in args
    assert args[args.index("--max-pages") + 1] == "3"


def connect_tracker(system, actor, *, gmail=False):
    service, _, _, roots, _ = system
    account = f"tracker{actor}@example.com"
    patch = {"accounts": {"tracker": {"enabled": True, "account": account}}}
    if gmail:
        patch["accounts"]["gmail"] = {"enabled": True, "account": f"monitor{actor}@example.com"}
    confirm(service, actor, patch)
    info = {"spreadsheet_id": f"sheet-{actor}", "sheet_id": actor,
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/sheet-{actor}",
            "account": account, "configured_spreadsheet_id": "", "viewer_email": ""}
    private_json(roots[actor] / "state/tracker_connection.json", info)
    private_json(roots[actor] / "secrets/tracker_token.json", {"token": "synthetic-tracker"})
    if gmail:
        private_json(roots[actor] / "secrets/gmail_token.json", {"token": "synthetic-gmail"})
    return info


def test_sync_candidate_uses_actual_tracker_argument_contract_and_own_account(system, monkeypatch):
    _, scheduler, _, roots, _ = system
    connect_tracker(system, 123, gmail=True)
    monkeypatch.setenv("JOBHUNTER_GMAIL_ACCOUNT", "owner@example.com")
    monkeypatch.setenv("JOBHUNTER_TRACKER_GOOGLE_TOKEN_PATH", "/owner/token.json")
    sync = Mock()
    monkeypatch.setattr("jobhunter_integrations.google_tracker.sync_tracker", sync)
    assert scheduler.sync_candidate(123) is True
    args = sync.call_args.args[0]
    assert args.account == "tracker123@example.com"
    assert args.spreadsheet_id == "sheet-123" and args.sheet_id == 123
    assert args.google_token == roots[123] / "secrets/tracker_token.json"
    assert args.db_path == roots[123] / "jobs.db"
    assert args.repo_root == args.candidate_root == roots[123]
    assert args.drive_state == roots[123] / "state/tracker_drive_files.json"


def test_tracker_command_uses_only_the_authenticated_candidate(system):
    service, scheduler, client, _, _ = system
    connect_tracker(system, 123)
    connect_tracker(system, 456)
    scheduler.sync_candidate = Mock(return_value=True)
    handler = ApplicationTelegramHandler(service, client, scheduler=scheduler)
    handler.handle_update({"update_id": 1, "message": {"from": {"id": 456},
        "chat": {"id": 456, "type": "private"}, "text": "/tracker"}})
    scheduler.sync_candidate.assert_called_once_with(456)
    assert client.sends[-1][0] == "456" and "sheet-456" in client.sends[-1][1]
    assert "sheet-123" not in client.sends[-1][1]


def test_disabled_mail_and_tracker_never_invoke_integrations(system, monkeypatch):
    _, scheduler, _, _, _ = system
    monitor = Mock()
    sync = Mock()
    monkeypatch.setattr("jobhunter_integrations.gmail_monitor.run_candidate_monitor", monitor)
    monkeypatch.setattr("jobhunter_integrations.google_tracker.sync_tracker", sync)
    scheduler.monitor_due(datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc))
    monitor.assert_not_called()
    sync.assert_not_called()


def test_mail_only_candidate_receives_no_tracker_callback(system, monkeypatch):
    service, scheduler, _, roots, _ = system
    confirm(service, 123, {"accounts": {"gmail": {"enabled": True, "account": "jobs123@example.com"}}})
    private_json(roots[123] / "secrets/gmail_token.json", {"token": "synthetic"})
    monitor = Mock()
    monkeypatch.setattr("jobhunter_integrations.gmail_monitor.run_candidate_monitor", monitor)
    scheduler.sync_candidate = Mock()
    scheduler.monitor_due(datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc))
    assert monitor.call_args.kwargs["account"] == "jobs123@example.com"
    assert monitor.call_args.kwargs["candidate_root"] == roots[123]
    assert monitor.call_args.kwargs["tracker_sync"] is None
    scheduler.sync_candidate.assert_not_called()


def test_tracker_only_account_gets_periodic_retry_without_enabling_mail(system, monkeypatch):
    _, scheduler, _, _, _ = system
    connect_tracker(system, 123)
    monitor = Mock()
    monkeypatch.setattr("jobhunter_integrations.gmail_monitor.run_candidate_monitor", monitor)
    scheduler.sync_candidate = Mock(return_value=True)
    now = datetime(2026, 9, 10, 11, 0, tzinfo=timezone.utc)
    scheduler.sync_due(now)
    scheduler.monitor_due(now)
    scheduler.sync_candidate.assert_called_once_with(123)
    monitor.assert_not_called()


def test_confirmed_tracker_account_replacement_archives_old_connection_before_sync(system, monkeypatch):
    service, scheduler, _, roots, _ = system
    connect_tracker(system, 123)
    confirm(service, 123, {"accounts": {"tracker": {"account": "replacement@example.com"}}})
    sync = Mock()
    monkeypatch.setattr("jobhunter_integrations.google_tracker.sync_tracker", sync)
    assert scheduler.sync_candidate(123) is False
    sync.assert_not_called()
    assert not (roots[123] / "secrets/tracker_token.json").exists()
    assert len(list((roots[123] / "state/account_history").rglob("tracker_token.json"))) == 1
    assert len(list((roots[123] / "state/account_history").rglob("tracker_connection.json"))) == 1


def test_sync_rejects_connection_metadata_from_an_old_account(system, monkeypatch):
    _, scheduler, _, roots, _ = system
    info = connect_tracker(system, 123)
    info["account"] = "other-account@example.com"
    private_json(roots[123] / "state/tracker_connection.json", info)
    sync = Mock()
    monkeypatch.setattr("jobhunter_integrations.google_tracker.sync_tracker", sync)
    with pytest.raises(ValueError):
        scheduler.sync_candidate(123)
    sync.assert_not_called()


def settings(gmail="old-mail@example.com", tracker="old-tracker@example.com", spreadsheet="", viewer="personal@example.com"):
    return {"accounts": {"gmail": {"account": gmail},
            "tracker": {"account": tracker, "spreadsheet_id": spreadsheet, "viewer_email": viewer}}}


def seed_state(root):
    from jobhunter_service.account_state import GMAIL, TRACKER_AUTH, TRACKER_DATA
    for index, path in enumerate((*GMAIL, *TRACKER_AUTH, *TRACKER_DATA)):
        private_json(root / path, {"original": index})


def test_account_change_archives_prior_grants_ledgers_and_history_independently(tmp_path):
    seed_state(tmp_path)
    previous = settings()
    changed = settings(gmail="new-mail@example.com")
    reconcile_account_settings(tmp_path, previous, changed)
    assert not (tmp_path / "secrets/gmail_token.json").exists()
    assert not (tmp_path / "state/gmail_seen.outbox.json").exists()
    assert (tmp_path / "secrets/tracker_token.json").exists()
    archived = list((tmp_path / "state/account_history").rglob("gmail_token.json"))
    assert len(archived) == 1 and archived[0].stat().st_mode & 0o777 == 0o600
    private_json(tmp_path / "secrets/gmail_token.json", {"new": "synthetic-grant"})
    reconcile_account_settings(tmp_path, previous, changed)
    assert json.loads((tmp_path / "secrets/gmail_token.json").read_text()) == {"new": "synthetic-grant"}


def test_tracker_sheet_change_preserves_account_grant_but_reconnects_sheet(tmp_path):
    seed_state(tmp_path)
    reconcile_account_settings(tmp_path, settings(), settings(spreadsheet="new-sheet"))
    assert (tmp_path / "secrets/tracker_token.json").exists()
    assert not (tmp_path / "state/tracker_connection.json").exists()
    assert not (tmp_path / "state/tracker_drive_files.json").exists()
    assert len(list((tmp_path / "state/account_history").rglob("tracker_drive_files.json"))) == 1


def test_viewer_change_requires_reconnect_but_keeps_existing_sheet_and_folder(tmp_path):
    seed_state(tmp_path)
    reconcile_account_settings(tmp_path, settings(), settings(viewer="new-personal@example.com"))
    assert not (tmp_path / "state/tracker_connection.json").exists()
    assert (tmp_path / "state/tracker_setup.json").exists()
    assert (tmp_path / "state/tracker_drive_files.json").exists()


def test_account_change_crash_replay_cannot_archive_a_newly_connected_token(tmp_path, monkeypatch):
    from jobhunter_service import account_state
    seed_state(tmp_path)
    real_write = account_state.private_json
    def interrupted(path, value):
        if path.name == "account_state.json":
            raise OSError("simulated interrupted checkpoint")
        real_write(path, value)
    monkeypatch.setattr(account_state, "private_json", interrupted)
    with pytest.raises(OSError):
        reconcile_account_settings(tmp_path, settings(), settings(gmail="new@example.com"))
    private_json(tmp_path / "secrets/gmail_token.json", {"new": "synthetic"})
    monkeypatch.setattr(account_state, "private_json", real_write)
    reconcile_account_settings(tmp_path, settings(), settings(gmail="new@example.com"))
    assert json.loads((tmp_path / "secrets/gmail_token.json").read_text()) == {"new": "synthetic"}


def test_repeated_account_roundtrip_creates_distinct_archives(tmp_path):
    original, changed = settings(), settings(gmail="new@example.com")
    seed_state(tmp_path)
    reconcile_account_settings(tmp_path, original, changed)
    private_json(tmp_path / "secrets/gmail_token.json", {"new": 2})
    reconcile_account_settings(tmp_path, changed, original)
    private_json(tmp_path / "secrets/gmail_token.json", {"new": 3})
    reconcile_account_settings(tmp_path, original, changed)
    assert len(list((tmp_path / "state/account_history").rglob("gmail_token.json"))) == 3
    assert not (tmp_path / "secrets/gmail_token.json").exists()


def test_first_materialization_without_previous_settings_is_safe(tmp_path):
    reconcile_account_settings(tmp_path, None, settings())
    assert not (tmp_path / "state/account_history").exists()


def test_active_mail_monitor_prevents_archiving_its_ledger_mid_run(tmp_path):
    seed_state(tmp_path)
    path = tmp_path / "state/gmail_seen.lock"
    with path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="operation is running"):
            reconcile_account_settings(tmp_path, settings(), settings(gmail="new@example.com"))
    assert (tmp_path / "state/gmail_seen.json").exists()
    assert (tmp_path / "secrets/gmail_token.json").exists()
    assert not (tmp_path / "state/account_history").exists()


def test_connection_metadata_cannot_match_another_account_sheet_or_viewer():
    configured = settings()["accounts"]["tracker"]
    info = {"account": configured["account"], "configured_spreadsheet_id": "", "viewer_email": configured["viewer_email"],
            "spreadsheet_id": "managed-sheet", "sheet_id": 0}
    assert connection_matches(info, configured)
    for field, changed in (("account", "other@example.com"), ("spreadsheet_id", "other-sheet"), ("viewer_email", "other@example.com")):
        assert not connection_matches(info, {**configured, field: changed})
    assert not connection_matches({"spreadsheet_id": "legacy-without-owner", "sheet_id": 0}, configured)
