"""Synthetic Telegram onboarding through real durable service and delivery code.

No Telegram, Google, LinkedIn, scraping, or browser account is contacted.
"""

import copy
from datetime import datetime, timezone
import json
import sqlite3

from jobhunter_service.delivery import DeliveryQueue
from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.service import JobHunterService
from jobhunter_service.telegram import TelegramAPIError, TelegramHandler


class TelegramTransport:
    def __init__(self):
        self.messages = []
        self.fail_destinations = set()

    def send_message(self, chat_id, text, reply_markup=None):
        if str(chat_id) in self.fail_destinations:
            raise TelegramAPIError("Synthetic delivery failure")
        result = {"message_id": len(self.messages) + 1}
        self.messages.append({"chat_id": str(chat_id), "text": text, "reply_markup": reply_markup, **result})
        return result

    def answer_callback(self, callback_id, text=""):
        return None

    def download_document(self, document):
        return b"Candidate Eleven\nBackend engineer; Python APIs\n2020-2024"

    def validate_destination(self, actor_id, destination):
        expected = {11: "-100111111", 22: "-100222222"}
        if destination["kind"] == "channel" and destination["chat_id"] != expected[actor_id]:
            raise PermissionError("This candidate does not control that channel.")
        return dict(destination)


class Conversation:
    def __init__(self, root):
        self.transport = TelegramTransport()
        self.service = JobHunterService(root, 1, telegram_client=self.transport)
        # OAuth itself is covered separately; this synthetic conversation treats
        # configured enabled Google accounts as having completed a mocked grant.
        self.service.connection_status = self.connection_status
        self.next_plan = {"operation": "reply", "reply": "What did you build in your first experience?"}
        self.handler = TelegramHandler(self.service, self.transport, RestrictedHermesAssistant(self.planner))
        self.counter = 0

    def planner(self, messages, schema):
        return copy.deepcopy(self.next_plan)

    def connection_status(self, actor):
        settings = json.loads(self.service.store.member(actor)["settings"])
        return {provider: {"status": "connected" if provider != "linkedin" and settings["accounts"][provider]["enabled"] else "unavailable",
                           "message": "Synthetic connection evidence."} for provider in ("linkedin", "gmail", "tracker")}

    def message(self, actor, text, **fields):
        self.counter += 1
        update = {"update_id": self.counter, "message": {"from": {"id": actor, "is_bot": False},
                  "chat": {"id": actor, "type": "private"}, "text": text, **fields}}
        self.handler.handle_update(update)
        return update

    def action(self, actor):
        return next(message["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
                    for message in reversed(self.transport.messages)
                    if message["chat_id"] == str(actor) and message.get("reply_markup"))

    def confirm(self, actor, action=None):
        self.counter += 1
        self.handler.handle_update({"update_id": self.counter, "callback_query": {
            "id": "synthetic-callback-" + str(self.counter), "from": {"id": actor},
            "data": action or self.action(actor),
            "message": {"chat": {"id": actor, "type": "private"}, "from": {"id": 1000, "is_bot": True}}}})


def patch_for(user):
    return {
        "resume": {"name": f"Candidate {user}", "headline": "Backend Engineer", "experience": [], "skills": {"Languages": ["Python"]}},
        "search": {"matching": {"preset": "generic", "preferred_roles": ["backend engineer"], "preferred_technologies": ["python"]},
                   "keywords": ["backend engineer"],
                   "markets": [{"name": "Tunisia" if user == 11 else "France", "locations": ["Tunisia" if user == 11 else "France"],
                                "work_authorization": "authorized", "relocation_required": False},
                               {"name": "Germany" if user == 11 else "Canada", "locations": ["Germany" if user == 11 else "Canada"],
                                "work_authorization": "sponsorship_required", "relocation_required": True}]},
        "schedule": {"timezone": "Africa/Tunis" if user == 11 else "Europe/Paris",
                     "time": "09:00" if user == 11 else "20:00", "weekdays": [0, 1, 2, 3, 4], "enabled": True},
        "telegram": {"destinations": [{"chat_id": str(user), "kind": "private", "label": "Private jobs"}]
                     + ([{"chat_id": "-100111111", "kind": "channel", "label": "My job channel"}] if user == 11 else [])},
        "accounts": {"gmail": {"enabled": False, "account": f"jobs{user}@example.test"},
                     "tracker": {"enabled": user == 11, "account": f"jobs{user}@example.test", "viewer_email": f"personal{user}@example.test"}},
    }


def invite_and_configure(conversation, user):
    conversation.message(1, "/jobhunter add " + str(user))
    assert conversation.service.store.member(user)["status"] == "pending"
    conversation.message(user, "/start")
    assert conversation.service.store.member(user)["status"] == "active"
    conversation.next_plan = {"operation": "propose", "patch": patch_for(user)}
    conversation.message(user, "Configure my confirmed resume, preferences, accounts and schedule")
    assert conversation.service.snapshot(user)["settings"]["schedule"]["enabled"] is False
    conversation.confirm(user)
    finish_guided_setup(conversation, user)
    assert conversation.service.snapshot(user)["settings"]["schedule"]["enabled"] is True


def finish_guided_setup(conversation, user):
    for _ in range(12):
        state = conversation.service.onboarding_status(user)
        if state["complete"]:
            return
        step = next(item for item in state["steps"] if item["id"] == state["next_step"])
        operation = "activate" if "activate" in step["actions"] else "acknowledge" if "acknowledge" in step["actions"] else "skip"
        assert operation in step["actions"]
        conversation.confirm(user, f"jh:onboard:{operation}:{step['id']}:{state['revision']}")
        if operation == "activate":
            assert not conversation.service.snapshot(user)["settings"]["schedule"]["enabled"]
            conversation.confirm(user)
    raise AssertionError("Guided setup did not finish")


def test_two_candidates_onboard_confirm_schedule_and_retry_separate_deliveries(tmp_path, monkeypatch):
    conversation = Conversation(tmp_path / "data")
    owner_profile = conversation.service.root / "master-profile.json"
    owner_profile.write_text('{"name":"Synthetic Owner"}')
    original_owner = owner_profile.read_bytes()

    conversation.message(1, "/jobhunter add 11")
    conversation.message(11, "/start")
    conversation.message(11, "", document={"file_id": "synthetic-file", "file_name": "resume.txt", "mime_type": "text/plain"})
    source_path = conversation.service.root / "u11" / "state" / "resume_source.json"
    assert json.loads(source_path.read_text())["untrusted"] is True
    assert not (conversation.service.root / "u11" / "master-profile.json").exists()
    assert conversation.service.snapshot(11)["history"]

    conversation.next_plan = {"operation": "propose", "patch": patch_for(11)}
    conversation.message(11, "Configure my confirmed resume, preferences, accounts and schedule")
    first_action = conversation.action(11)
    conversation.message(1, "/jobhunter add 22")
    conversation.message(22, "/start")
    conversation.confirm(22, first_action)
    assert conversation.service.snapshot(11)["settings"]["resume"] == {}
    assert conversation.service.snapshot(22)["settings"]["resume"] == {}
    conversation.confirm(11, first_action)

    conversation.next_plan = {"operation": "propose", "patch": patch_for(22)}
    conversation.message(22, "Configure my own resume and evening search")
    conversation.confirm(22)
    finish_guided_setup(conversation, 11)
    finish_guided_setup(conversation, 22)
    first = conversation.service.snapshot(11)["settings"]
    second = conversation.service.snapshot(22)["settings"]
    assert first["schedule"]["time"] == "09:00" and second["schedule"]["time"] == "20:00"
    assert first["schedule"]["timezone"] != second["schedule"]["timezone"]
    assert first["search"]["markets"][0]["work_authorization"] == "authorized"
    assert first["search"]["markets"][1]["work_authorization"] == "sponsorship_required"
    assert second["search"]["markets"][0]["name"] == "France"
    assert first["accounts"]["gmail"]["enabled"] is False
    assert first["accounts"]["tracker"]["viewer_email"] == "personal11@example.test"
    assert second["accounts"]["tracker"]["account"] == "jobs22@example.test"
    assert owner_profile.read_bytes() == original_owner

    scheduler = Scheduler(conversation.service, conversation.planner, conversation.transport)
    monkeypatch.setattr(scheduler.delivery, '_preflight', lambda *args: 'open')
    morning = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
    evening = datetime(2026, 9, 9, 18, 0, tzinfo=timezone.utc)
    assert scheduler.queue_due(morning) == 1
    assert scheduler.queue_due(morning) == 0
    assert scheduler.queue_due(evening) == 1
    with conversation.service.store.connect() as registry:
        runs = {row["user_id"]: row["id"] for row in registry.execute("SELECT * FROM runs")}
    assert set(runs) == {11, 22}
    for user in (11, 22):
        with sqlite3.connect(conversation.service.root / f"u{user}" / "jobs.db") as jobs:
            jobs.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, notified INTEGER, status TEXT)")
            jobs.execute("INSERT INTO jobs VALUES('same-posting',0,'new')")
        scheduler.delivery.enqueue(user, runs[user], f"Private candidate {user} digest", ["same-posting"])

    before = len(conversation.transport.messages)
    conversation.transport.fail_destinations.add("-100111111")
    assert scheduler.delivery.drain() == {"sent": 2, "pending_or_failed": 1}
    deliveries = conversation.transport.messages[before:]
    assert {(item["chat_id"], item["text"]) for item in deliveries} == {
        ("11", "Private candidate 11 digest"), ("22", "Private candidate 22 digest")}
    conversation.transport.fail_destinations.clear()
    assert scheduler.delivery.drain() == {"sent": 1, "pending_or_failed": 0}
    assert conversation.transport.messages[-1]["chat_id"] == "-100111111"
    assert conversation.transport.messages[-1]["text"] == "Private candidate 11 digest"
    assert scheduler.delivery.drain() == {"sent": 0, "pending_or_failed": 0}
    assert len(conversation.transport.messages) == before + 3
    for user in (11, 22):
        with sqlite3.connect(conversation.service.root / f"u{user}" / "jobs.db") as jobs:
            assert jobs.execute("SELECT notified,status FROM jobs").fetchone() == (1, "new")
    assert owner_profile.read_bytes() == original_owner


def test_revocation_cancels_pending_delivery_and_member_cannot_reinvite(tmp_path):
    conversation = Conversation(tmp_path / "data")
    invite_and_configure(conversation, 11)
    scheduler = Scheduler(conversation.service, conversation.planner, conversation.transport)
    scheduler.queue_due(datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc))
    with conversation.service.store.connect() as db:
        run_id = db.execute("SELECT id FROM runs").fetchone()[0]
    scheduler.delivery.enqueue(11, run_id, "Synthetic digest", [])
    conversation.message(1, "/jobhunter revoke 11")
    sent_before = len(conversation.transport.messages)
    assert scheduler.delivery.drain() == {"sent": 0, "pending_or_failed": 0}
    assert len(conversation.transport.messages) == sent_before
    with conversation.service.store.connect() as db:
        assert {row[0] for row in db.execute("SELECT status FROM deliveries")} == {"cancelled"}
    conversation.message(11, "/jobhunter add 11")
    assert conversation.service.store.member(11)["status"] == "revoked"


def test_duplicate_telegram_update_preserves_one_pending_proposal(tmp_path):
    conversation = Conversation(tmp_path / "data")
    conversation.message(1, "/jobhunter add 11")
    conversation.message(11, "/start")
    event = conversation.message(11, "/roles Backend Engineer")
    before = len(conversation.transport.messages)
    conversation.handler.handle_update(event)
    with conversation.service.store.connect() as db:
        assert db.execute("SELECT count(*) FROM actions WHERE user_id=11 AND consumed=0").fetchone()[0] == 1
    assert len(conversation.transport.messages) == before
