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
    dialogue.planner.return_value = {"operation": "reply", "reply": "How did you test those APIs?"}
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
    dialogue.planner.return_value = {"operation": "reply", "reply": "Do you have any other experience to review?"}
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


def test_upload_announces_wait_before_download_and_model_then_requests_answer(dialogue):
    dialogue.message("/start")
    dialogue.clear()

    def download(document):
        assert "Resume received" in dialogue.output() and "Please wait" in dialogue.output()
        assert "Done reviewing" not in dialogue.output()
        assert dialogue.planner.call_count == 0
        return b"Candidate Example\nEngineer at Example Co\nBuilt APIs."

    def plan(messages, schema):
        assert "Please wait" in dialogue.output()
        assert "Your turn:" not in dialogue.output()
        assert not dialogue.service.snapshot(11)["settings"]["resume"]
        return {"operation": "reply", "reply": "What were your responsibilities at Example Co?"}

    dialogue.client.download_document.side_effect = download
    dialogue.planner.side_effect = plan
    dialogue.message("", document={"file_id": "synthetic", "file_name": "resume.txt"})
    messages = [call.args[1] for call in dialogue.client.send_message.call_args_list]
    assert len(messages) == 2
    assert messages[-1] == "Your turn:\nWhat were your responsibilities at Example Co?"


@pytest.mark.parametrize("text_confirmation", [False, True])
def test_confirmed_resume_automatically_asks_once_using_saved_facts(dialogue, text_confirmation):
    dialogue.message("/start")
    proposal = dialogue.service.propose(11, {"resume": {"name": "Candidate Example", "experience": [{
        "id": "exp-1", "title": "Engineer", "company": "Example Co", "dates": "2020-2024",
        "bullets": ["Maintained APIs."]}]}})

    def next_question(messages, schema):
        assert "Your changes are saved" in dialogue.output() and "Please wait" in dialogue.output()
        context = json.loads(messages[1]["content"].split("\n", 1)[1])
        assert context["settings"]["resume"]["experience"][0]["bullets"] == ["Maintained APIs."]
        assert schema["properties"]["operation"]["enum"] == ["reply"]
        return {"operation": "reply", "reply": "How did you test those APIs?"}

    dialogue.planner.side_effect = next_question
    dialogue.clear()
    if text_confirmation:
        dialogue.message("/confirm " + proposal["action_id"])
    else:
        dialogue.click("", data="jh:confirm:" + proposal["action_id"])
    assert "Your turn:\nHow did you test those APIs?" in dialogue.output()
    assert dialogue.planner.call_count == 1
    revision = dialogue.service.snapshot(11)["revision"]
    dialogue.click("", data="jh:confirm:" + proposal["action_id"])
    assert dialogue.planner.call_count == 1
    assert dialogue.service.snapshot(11)["revision"] == revision
    dialogue.click("jh:onboard:acknowledge:resume:")
    assert dialogue.service.onboarding_status(11)["next_step"] == "roles"
    assert dialogue.planner.call_count == 1


def test_followup_failure_preserves_confirmed_facts_and_retry_continues_interview(dialogue):
    from jobhunter_service.recovery import get_recovery
    dialogue.message("/start")
    proposal = dialogue.service.propose(11, {"resume": {"name": "Candidate Example"}})
    dialogue.planner.side_effect = RuntimeError("synthetic provider failure")
    dialogue.click("", data="jh:confirm:" + proposal["action_id"])
    revision = dialogue.service.snapshot(11)["revision"]
    assert dialogue.service.snapshot(11)["settings"]["resume"]["name"] == "Candidate Example"
    assert get_recovery(dialogue.service, 11)["kind"] == "resume_followup"
    assert "Your turn: Tap Retry" in dialogue.output()
    dialogue.planner.side_effect = None
    dialogue.planner.return_value = {"operation": "reply", "reply": "What else should we know about your experience?"}
    dialogue.clear()
    dialogue.message("/retry")
    assert "Please wait" in dialogue.output() and "Your turn:" in dialogue.output()
    assert get_recovery(dialogue.service, 11) is None
    assert dialogue.service.snapshot(11)["revision"] == revision
    assert "first experience" not in dialogue.planner.call_args.args[0][-1]["content"]


def test_followup_retry_does_not_reopen_completed_resume_step(dialogue):
    from jobhunter_service.recovery import get_recovery
    dialogue.message("/start")
    proposal = dialogue.service.propose(11, {"resume": {"name": "Candidate Example"}})
    dialogue.planner.side_effect = RuntimeError("synthetic failure")
    dialogue.click("", data="jh:confirm:" + proposal["action_id"])
    dialogue.message("/continue")
    dialogue.click("jh:onboard:acknowledge:resume:")
    calls = dialogue.planner.call_count
    dialogue.clear()
    dialogue.message("/retry")
    assert dialogue.planner.call_count == calls
    assert "Your turn — Next:" in dialogue.output()
    assert dialogue.service.onboarding_status(11)["next_step"] == "roles"
    assert get_recovery(dialogue.service, 11) is None


def test_automatic_followup_cannot_propose_another_mutation(dialogue):
    from jobhunter_service.recovery import get_recovery
    dialogue.message("/start")
    proposal = dialogue.service.propose(11, {"resume": {"name": "Candidate Example"}})
    dialogue.planner.return_value = {"operation": "propose", "patch": {"resume": {"name": "Wrong Name"}}}
    dialogue.clear()
    dialogue.click("", data="jh:confirm:" + proposal["action_id"])
    assert dialogue.service.snapshot(11)["settings"]["resume"]["name"] == "Candidate Example"
    assert "Wrong Name" not in dialogue.output()
    assert "Review the complete proposed changes" not in dialogue.output()
    assert get_recovery(dialogue.service, 11)["kind"] == "resume_followup"


def test_failed_wait_notice_does_not_call_model_or_create_model_recovery(dialogue):
    from jobhunter_service.recovery import get_recovery
    from jobhunter_service.telegram import TelegramAPIError
    dialogue.message("/start")
    dialogue.client.send_message.side_effect = TelegramAPIError("synthetic network failure")
    with pytest.raises(TelegramAPIError):
        dialogue.message("I built APIs in my previous role")
    dialogue.planner.assert_not_called()
    assert get_recovery(dialogue.service, 11) is None


def test_download_failure_ends_wait_with_an_actionable_error(dialogue):
    from jobhunter_service.resumes import ResumeImportError
    dialogue.message("/start")
    dialogue.clear()
    dialogue.client.download_document.side_effect = ResumeImportError("Upload a readable resume.")
    dialogue.message("", document={"file_id": "synthetic", "file_name": "resume.txt"})
    assert "Please wait" in dialogue.output()
    assert dialogue.client.send_message.call_args.args[1].startswith("Your turn: Upload a readable resume.")
    dialogue.planner.assert_not_called()
