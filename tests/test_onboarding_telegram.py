"""Candidate dialogue using the durable service; all external effects are mocked."""
import json
from unittest.mock import Mock

import pytest

from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.service import JobHunterService
from jobhunter_service.scheduling import next_run
from jobhunter_service.telegram import readable_fields


@pytest.fixture
def dialogue(tmp_path):
    client = Mock()
    client.send_message.return_value = {"message_id": 1}
    client.download_document.return_value = b"Candidate Example\nEngineer at Example Co, 2020-2024\nMaintained APIs."
    client.validate_destination.side_effect = lambda actor, destination: destination
    service = JobHunterService(tmp_path, 900, public_url="https://jobs.example.test", telegram_client=client)
    service.admin(900, "add", 11)
    planner = Mock(return_value={"operation": "reply", "reply": "What did you build in your first role?"})
    handler = ApplicationTelegramHandler(service, client, RestrictedHermesAssistant(planner))

    class Dialogue:
        counter = 0

        def message(self, text, **fields):
            self.counter += 1
            handler.handle_update({"update_id": self.counter, "message": {
                "message_id": self.counter, "from": {"id": 11}, "chat": {"id": 11, "type": "private"},
                "text": text, **fields}})

        def click(self, prefix, *, data=None):
            if data is None:
                data = next(button["callback_data"] for call in reversed(client.send_message.call_args_list)
                            if len(call.args) == 3 for row in call.args[2]["inline_keyboard"] for button in row
                            if button.get("callback_data", "").startswith(prefix))
            self.counter += 1
            handler.handle_update({"update_id": self.counter, "callback_query": {
                "id": "callback-" + str(self.counter), "from": {"id": 11}, "data": data,
                "message": {"message_id": self.counter, "from": {"id": 999, "is_bot": True},
                            "chat": {"id": 11, "type": "private"}}}})
            return data

        def output(self):
            return "\n".join(call.args[1] for call in client.send_message.call_args_list)

        def clear(self):
            client.send_message.reset_mock()

    result = Dialogue()
    result.service, result.client, result.planner = service, client, planner
    return result


def test_new_candidate_upload_confirmation_and_next_focused_step(dialogue):
    dialogue.message("/start")
    assert "Welcome" in dialogue.output() and "resume" in dialogue.output().lower()
    assert not dialogue.service.snapshot(11)["settings"]["schedule"]["enabled"]
    dialogue.message("", document={"file_id": "synthetic-file", "file_name": "resume.txt", "mime_type": "text/plain"})
    assert "unconfirmed draft" in dialogue.output()
    assert "first role" in dialogue.output()
    context = json.loads(dialogue.planner.call_args.args[0][1]["content"].split("\n", 1)[1])
    assert context["onboarding"]["next_step"] == "resume"
    assert "Maintained APIs" in context["resume_source"]["text"]
    dialogue.planner.return_value = {"operation": "propose", "patch": {"resume": {
        "name": "Candidate Example", "headline": "Engineer", "experience": [{"id": "exp-1", "title": "Engineer",
        "company": "Example Co", "dates": "2020-2024", "bullets": ["Maintained APIs."]}]}}}
    dialogue.clear()
    dialogue.message("Use these exact facts in my resume")
    assert "Maintained APIs." in dialogue.output()
    assert '"resume":' not in dialogue.output()
    assert not dialogue.service.snapshot(11)["settings"]["resume"]
    dialogue.click("jh:confirm:")
    assert dialogue.service.snapshot(11)["settings"]["resume"]["name"] == "Candidate Example"
    assert dialogue.service.onboarding_status(11)["next_step"] == "resume"
    dialogue.clear()
    dialogue.click("", data="jh:onboard:acknowledge:resume:" + str(dialogue.service.onboarding_status(11)["revision"]))
    state = dialogue.service.onboarding_status(11)
    assert state["next_step"] == "roles"
    assert state["next_question"] in dialogue.output()


def test_status_is_readable_and_does_not_show_source_or_history(dialogue):
    dialogue.message("/start")
    dialogue.service.record_turn(11, "Private interview answer", "Private follow-up")
    dialogue.clear()
    dialogue.message("/status")
    output = dialogue.output()
    assert "Your setup checklist" in output and "Searches: paused" in output
    assert '"settings"' not in output and "Private interview answer" not in output
    assert "Next:" in output


def test_stale_and_invalid_confirmations_explain_how_to_continue(dialogue):
    dialogue.message("/start")
    revision = dialogue.service.onboarding_status(11)["revision"]
    dialogue.service.onboarding_action(11, "skip", "tracker", revision)
    dialogue.clear()
    dialogue.click("", data=f"jh:onboard:acknowledge:resume:{revision}")
    assert "status" in dialogue.output() and "saved" not in dialogue.output().lower()
    dialogue.clear()
    dialogue.click("", data="jh:confirm:missing_action_12345678")
    assert "fresh preview" in dialogue.output()
    assert "/status" in dialogue.output()


def test_optional_unavailable_google_can_be_skipped_without_credentials(dialogue):
    dialogue.message("/start")
    state = dialogue.service.onboarding_status(11)
    tracker = next(step for step in state["steps"] if step["id"] == "tracker")
    assert tracker["state"] in {"pending", "blocked"} and "skip" in tracker["actions"]
    dialogue.clear()
    dialogue.click("", data=f"jh:onboard:skip:tracker:{state['revision']}")
    tracker = next(step for step in dialogue.service.onboarding_status(11)["steps"] if step["id"] == "tracker")
    assert tracker["state"] == "skipped"
    assert not dialogue.service.snapshot(11)["settings"]["schedule"]["enabled"]
    dialogue.client.download_document.assert_not_called()


def test_legacy_running_profile_status_and_start_preserve_schedule(dialogue):
    dialogue.service.authorize(11)
    proposal = dialogue.service.propose(11, {"resume": {"name": "Existing Candidate"},
        "search": {"keywords": ["engineer"], "markets": [{"name": "Tunisia", "locations": ["Tunisia"],
            "work_authorization": "authorized", "relocation_required": False}]}, "schedule": {"enabled": True}})
    dialogue.service.confirm(11, proposal["action_id"])
    original = dialogue.service.snapshot(11)["settings"]["schedule"]
    dialogue.message("/status")
    assert not dialogue.service.onboarding_status(11)["started"]
    assert "Roles: engineer" in dialogue.output()
    dialogue.message("/start")
    assert dialogue.service.snapshot(11)["settings"]["schedule"] == original
    assert "running" in dialogue.output().lower()


def test_help_explains_operations_and_support_never_messages_owner(dialogue):
    dialogue.message("/help")
    for command in ("/continue", "/pause", "/retry", "/jobs", "/apply", "/submit", "/tracker", "/logout"):
        assert command in dialogue.output()
    dialogue.message("/support")
    assert "has not been sent to anyone else" in dialogue.output()
    assert all(call.args[0] == 11 for call in dialogue.client.send_message.call_args_list)


def test_guided_schedule_waits_for_complete_review_and_activation_confirmation(dialogue):
    dialogue.message("/start")
    dialogue.planner.return_value = {"operation": "propose", "patch": {
        "resume": {"name": "Candidate Example", "experience": []},
        "search": {"keywords": ["engineer"], "markets": [{"name": "Tunisia", "locations": ["Tunisia"],
            "work_authorization": "authorized", "relocation_required": False}]},
        "schedule": {"time": "20:00", "timezone": "Africa/Tunis", "weekdays": [0, 1, 2, 3, 4], "enabled": True}}}
    dialogue.message("Use this background, search and weekday schedule")
    assert "enabled: No" in dialogue.output()
    dialogue.click("jh:confirm:")
    for step in ("resume", "roles", "markets", "schedule", "delivery"):
        assert dialogue.service.onboarding_status(11)["next_step"] == step
        dialogue.click("jh:onboard:acknowledge:" + step + ":")
    for step in ("linkedin", "gmail", "tracker"):
        assert dialogue.service.onboarding_status(11)["next_step"] == step
        dialogue.click("jh:onboard:skip:" + step + ":")
    assert "Final review" in dialogue.output()
    dialogue.click("jh:onboard:acknowledge:review:")
    assert not dialogue.service.snapshot(11)["settings"]["schedule"]["enabled"]
    dialogue.clear()
    expected_run = next_run({**dialogue.service.snapshot(11)["settings"]["schedule"], "enabled": True}).isoformat()
    dialogue.click("", data="jh:onboard:activate:review:" + str(dialogue.service.onboarding_status(11)["revision"]))
    assert "enabled: Yes" in dialogue.output()
    for fact in ("20:00", "Africa/Tunis", "Mon, Tue, Wed, Thu, Fri", "engineer", "Tunisia", expected_run):
        assert fact in dialogue.output()
    assert not dialogue.service.snapshot(11)["settings"]["schedule"]["enabled"]
    dialogue.click("jh:confirm:")
    assert dialogue.service.snapshot(11)["settings"]["schedule"]["enabled"]
    assert dialogue.service.onboarding_status(11)["complete"]
    assert "setup is complete" in dialogue.output()
    dialogue.clear()
    dialogue.message("/status")
    assert "Gmail:" in dialogue.output() and "Tracker:" in dialogue.output()


def test_explicit_check_probes_only_requested_candidate_account(dialogue):
    dialogue.service.check_connection = Mock()
    dialogue.message("/check tracker")
    dialogue.service.check_connection.assert_called_once_with(11, "tracker")
    assert "Your JobHunter status" in dialogue.output()
    dialogue.message("/check owner")
    assert dialogue.service.check_connection.call_count == 1
    assert "Use /check linkedin" in dialogue.output()


def test_readable_preview_preserves_list_replacements_and_every_public_fact():
    facts = ["Maintained APIs.", "Exact final fact with Unicode: Tunis — 東京", "First line\nSecond line"]
    result = readable_fields({"resume": {"name": "Candidate", "experience": [{"id": "exp-1", "bullets": facts}]},
                              "search": {"keywords": []}, "schedule": {"enabled": False}})
    assert "Replace the previous list" in result and "with no entries" in result
    assert all(fact in result for fact in facts)
    assert "enabled: No" in result and '"resume"' not in result
