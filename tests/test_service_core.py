"""Real service state transitions with only Telegram's external API mocked."""
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

import pytest

from jobhunter_service.delivery import DeliveryQueue
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.scheduling import due_slot, next_run
from jobhunter_service.service import JobHunterService


OWNER = 900


class Telegram:
    def __init__(self):
        self.validations, self.sends = [], []
        self.fail_chats = set()
        self.missing_ack = False

    def validate_destination(self, actor_id, destination):
        self.validations.append((actor_id, dict(destination)))
        if destination["chat_id"] == "-999":
            raise PermissionError("Actor does not control that channel")
        return destination

    def send_message(self, chat_id, message):
        chat_id = str(chat_id)
        self.sends.append((chat_id, message))
        if chat_id in self.fail_chats:
            raise RuntimeError("Temporary Telegram failure")
        return {} if self.missing_ack else {"message_id": len(self.sends)}


@pytest.fixture
def service(tmp_path, monkeypatch):
    # These fixtures intentionally contain only outbox state columns. Full
    # listing eligibility and live evidence are tested in delivery_availability.
    monkeypatch.setattr(DeliveryQueue, '_preflight', lambda *args: 'open')
    return JobHunterService(tmp_path, OWNER, telegram_client=Telegram())


def activate(service, user_id=123):
    service.admin(OWNER, "add", user_id)
    service.authorize(user_id)
    return service.snapshot(user_id)


def confirm(service, user_id, patch):
    action = service.propose(user_id, patch)
    return service.confirm(user_id, action["action_id"])


def ready(service, user_id=123, *, zone="UTC", clock="09:00", channels=False):
    activate(service, user_id)
    patch = {"resume": {"name": f"Candidate {user_id}"},
             "search": {"keywords": ["frontend developer"],
                        "markets": [{"name": "France", "locations": ["France"],
                                     "work_authorization": "authorized", "relocation_required": False}]},
             "schedule": {"timezone": zone, "time": clock, "enabled": True}}
    if channels:
        patch["telegram"] = {"destinations": [
            {"chat_id": str(user_id), "kind": "private", "label": "Private"},
            {"chat_id": "-1000", "kind": "channel", "label": "Jobs"}]}
    return confirm(service, user_id, patch)


def create_run(service, user_id=123, run_id="run1"):
    member = service.store.member(user_id)
    with service.store.connect() as db:
        db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
                   (run_id, user_id, "slot-" + run_id, member["revision"], "complete", None, 1, 1))
    root = service.profile_dir(member)
    with closing(sqlite3.connect(root / "jobs.db")) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, notified INTEGER, status TEXT)")
        db.execute("INSERT OR IGNORE INTO jobs VALUES('same-id',0,'new')")
    return root


def job_state(root):
    with closing(sqlite3.connect(root / "jobs.db")) as db, db:
        return db.execute("SELECT notified,status FROM jobs WHERE id='same-id'").fetchone()


def test_registration_requires_owner_invitation_and_first_private_identity(service):
    with pytest.raises(PermissionError):
        service.authorize(123)
    with pytest.raises(PermissionError):
        service.admin(123, "add", 456)
    invitation = service.admin(OWNER, "add", 123)
    assert invitation["status"] == "pending"
    with pytest.raises(PermissionError):
        service.snapshot(123)
    service.authorize(123)
    assert service.store.member(123)["status"] == "active"
    snapshot = service.snapshot(123)
    assert snapshot["profile_id"] == "u123"
    assert snapshot["settings"]["telegram"]["destinations"][0]["chat_id"] == "123"
    assert snapshot["settings"]["search"]["matching"]["preset"] == "generic"
    assert snapshot["settings"]["schedule"]["enabled"] is False


def test_proposals_are_private_unapplied_and_require_the_right_actor(service):
    activate(service, 123)
    activate(service, 456)
    action = service.propose(123, {"search": {"keywords": ["designer"]}})
    assert service.snapshot(123)["settings"]["search"]["keywords"] == []
    with pytest.raises(ValueError):
        service.confirm(456, action["action_id"])
    changed = service.confirm(123, action["action_id"])
    assert changed["settings"]["search"]["keywords"] == ["designer"]
    assert changed["revision"] == 1
    assert service.snapshot(456)["settings"]["search"]["keywords"] == []
    replayed = service.confirm(123, action["action_id"])
    assert replayed["revision"] == 1
    with service.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE operation='confirm_settings'").fetchone()[0] == 1


def test_concurrent_proposals_cannot_overwrite_a_newer_revision(service):
    activate(service)
    first = service.propose(123, {"search": {"keywords": ["designer"]}})
    stale = service.propose(123, {"schedule": {"timezone": "Africa/Tunis"}})
    service.confirm(123, first["action_id"])
    with pytest.raises(ValueError):
        service.confirm(123, stale["action_id"])
    current = service.snapshot(123)
    assert current["revision"] == 1
    assert current["settings"]["schedule"]["timezone"] == "UTC"


def test_expired_proposal_cannot_be_confirmed(service):
    activate(service)
    action = service.propose(123, {"search": {"keywords": ["designer"]}})
    with service.store.connect() as db:
        db.execute("UPDATE actions SET expires_at=0 WHERE id=?", (action["action_id"],))
    with pytest.raises(ValueError):
        service.confirm(123, action["action_id"])
    assert service.snapshot(123)["revision"] == 0


@pytest.mark.parametrize("patch", [
    {"search": {"db_path": "/other/jobs.db"}},
    {"search": {"matching": {"preset": "legacy"}}},
    {"schedule": {"command": "reboot"}},
    {"telegram": {"destinations": [{"chat_id": "456", "kind": "private"}]}},
    {"telegram": {"destinations": [{"chat_id": "-999", "kind": "channel"}]}},
])
def test_user_cannot_configure_owner_tools_paths_or_other_private_chats(service, patch):
    activate(service)
    with pytest.raises((ValueError, PermissionError)):
        service.propose(123, patch)
    assert service.snapshot(123)["revision"] == 0


def test_resume_upload_stays_untrusted_until_exact_fact_confirmation(service):
    activate(service, 123)
    activate(service, 456)
    uploaded = service.stage_resume(123, {"text": "Candidate draft, React developer", "filename": "resume.pdf", "sha256": "a" * 64})
    assert uploaded["status"] == "resume_received"
    snapshot = service.snapshot(123)
    assert snapshot["settings"]["resume"] == {}
    assert snapshot["resume_source"]["untrusted"] is True
    assert "resume_source" not in service.snapshot(456)
    root = service.profile_dir(service.store.member(123))
    assert not (root / "master-profile.json").exists()
    proposal = service.propose(123, {"resume": {"name": "Confirmed candidate"}})
    assert not (root / "master-profile.json").exists()
    service.confirm(123, proposal["action_id"])
    assert json.loads((root / "master-profile.json").read_text())["name"] == "Confirmed candidate"
    assert service.snapshot(123)["resume_source"]["untrusted"] is True


def test_enabling_schedule_requires_resume_and_search_configuration(service):
    activate(service)
    with pytest.raises(ValueError):
        service.propose(123, {"schedule": {"enabled": True}})
    assert service.snapshot(123)["settings"]["schedule"]["enabled"] is False


def test_candidate_can_confirm_salary_guidance_for_one_destination(service):
    activate(service)
    target = {"amount": 60000, "currency": "EUR", "period": "year"}
    result = confirm(service, 123, {"search": {"markets": [
        {"name": "France", "locations": ["France"], "work_authorization": "authorized",
         "relocation_required": False, "salary_target": target},
        {"name": "Germany", "locations": ["Germany"], "work_authorization": "sponsorship_required",
         "relocation_required": True},
    ]}})
    markets = result["settings"]["search"]["markets"]
    assert markets[0]["salary_target"] == target
    assert "salary_target" not in markets[1]


def test_independent_timezones_queue_each_user_at_their_local_time(service):
    ready(service, 123, zone="Africa/Tunis")
    ready(service, 456, zone="America/New_York")
    scheduler = Scheduler(service, None, service.telegram_client)
    assert scheduler.queue_due(datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)) == 1
    assert scheduler.queue_due(datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)) == 0
    assert scheduler.queue_due(datetime(2026, 9, 9, 13, 0, tzinfo=timezone.utc)) == 1
    with service.store.connect() as db:
        rows = db.execute("SELECT user_id,local_slot FROM runs ORDER BY user_id").fetchall()
    assert [(r["user_id"], r["local_slot"].split("@")[1]) for r in rows] == [(123, "Africa/Tunis"), (456, "America/New_York")]


def test_dst_fall_repeated_clock_time_queues_once(service):
    ready(service, zone="America/New_York", clock="01:30")
    scheduler = Scheduler(service, None, service.telegram_client)
    assert scheduler.queue_due(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)) == 1
    assert scheduler.queue_due(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)) == 0
    schedule = service.snapshot(123)["settings"]["schedule"]
    upcoming = next_run(schedule, datetime(2026, 11, 1, 5, 45, tzinfo=timezone.utc))
    assert upcoming.astimezone(timezone.utc) == datetime(2026, 11, 2, 6, 30, tzinfo=timezone.utc)


def test_dst_spring_nonexistent_time_is_skipped():
    schedule = {"enabled": True, "timezone": "America/New_York", "time": "02:30", "weekdays": list(range(7))}
    assert due_slot(schedule, datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)) is None
    upcoming = next_run(schedule, datetime(2026, 3, 8, 5, 0, tzinfo=timezone.utc))
    assert upcoming.astimezone(timezone.utc) == datetime(2026, 3, 9, 6, 30, tzinfo=timezone.utc)


def test_outbox_partial_failure_retries_only_failed_destination(service):
    ready(service, channels=True)
    root = create_run(service)
    client = service.telegram_client
    queue = DeliveryQueue(service, client)
    queue.enqueue(123, "run1", "Digest", ["same-id"])
    assert job_state(root) == (0, "delivery_pending")
    assert client.sends == []
    client.fail_chats.add("-1000")
    first = queue.drain()
    assert first == {"sent": 1, "pending_or_failed": 1}
    assert job_state(root)[0] == 1  # At least one destination acknowledged.
    client.fail_chats.clear()
    assert queue.drain() == {"sent": 1, "pending_or_failed": 0}
    assert [chat for chat, _ in client.sends] == ["123", "-1000", "-1000"]
    assert queue.drain() == {"sent": 0, "pending_or_failed": 0}
    assert all(actor == 123 for actor, _ in client.validations)


def test_missing_telegram_receipt_keeps_job_pending_until_acknowledged(service):
    ready(service)
    root = create_run(service)
    client = service.telegram_client
    queue = DeliveryQueue(service, client)
    queue.enqueue(123, "run1", "Digest", ["same-id"])
    client.missing_ack = True
    assert queue.drain() == {"sent": 0, "pending_or_failed": 1}
    assert job_state(root) == (0, "delivery_pending")
    client.missing_ack = False
    assert queue.drain() == {"sent": 1, "pending_or_failed": 0}
    assert job_state(root) == (1, "new")


@pytest.mark.parametrize("message_id", [True, 0, -1, "1"])
def test_invalid_telegram_message_id_is_not_delivery_acknowledgement(service, monkeypatch, message_id):
    ready(service)
    root = create_run(service)
    client = service.telegram_client
    queue = DeliveryQueue(service, client)
    queue.enqueue(123, "run1", "Digest", ["same-id"])
    monkeypatch.setattr(client, "send_message", lambda *args: {"message_id": message_id})
    assert queue.drain() == {"sent": 0, "pending_or_failed": 1}
    assert job_state(root) == (0, "delivery_pending")


def test_outbox_data_and_same_job_ids_remain_scoped(service):
    ready(service, 123)
    ready(service, 456)
    first = create_run(service, 123, "run1")
    second = create_run(service, 456, "run2")
    queue = DeliveryQueue(service, service.telegram_client)
    with pytest.raises(PermissionError):
        queue.enqueue(456, "run1", "Foreign digest", ["same-id"])
    queue.enqueue(123, "run1", "Own digest", ["same-id"])
    queue.drain()
    assert job_state(first)[0] == 1
    assert job_state(second) == (0, "new")
    assert all(chat == "123" for chat, _ in service.telegram_client.sends)


def test_revocation_cancels_tokens_proposals_schedule_and_pending_delivery(service):
    ready(service)
    root = create_run(service)
    queue = DeliveryQueue(service, service.telegram_client)
    queue.enqueue(123, "run1", "Digest", ["same-id"])
    token = service.store.token(123, "google_connect")
    action = service.propose(123, {"schedule": {"time": "11:00"}})
    service.admin(OWNER, "revoke", 123)
    with pytest.raises(PermissionError):
        service.authorize(123)
    with pytest.raises(PermissionError):
        service.store.read_token(token, "google_connect")
    assert queue.drain() == {"sent": 0, "pending_or_failed": 0}
    assert service.telegram_client.sends == []
    assert job_state(root)[0] == 0
    stored = json.loads(service.store.member(123)["settings"])
    assert stored["schedule"]["enabled"] is False
    service.admin(OWNER, "add", 123)
    service.authorize(123)
    with pytest.raises(ValueError):
        service.confirm(123, action["action_id"])


def test_resume_and_snapshot_cannot_follow_a_sibling_profile_symlink(service):
    activate(service, 123)
    activate(service, 456)
    own = service.root / "u123"
    other = service.root / "u456"
    # Simulate a misplaced filesystem entry without deleting user state.
    own.rename(service.root / "u123-original")
    own.symlink_to(other, target_is_directory=True)
    with pytest.raises(PermissionError):
        service.stage_resume(123, {"text": "Should not reach another profile"})
    assert not (other / "state" / "resume_source.json").exists()
