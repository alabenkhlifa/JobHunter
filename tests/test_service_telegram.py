from unittest.mock import Mock

import pytest
import requests

from jobhunter_service.hermes import RestrictedHermesAssistant
from jobhunter_service.resumes import ResumeImportError
from jobhunter_service.telegram import TelegramAPIError, TelegramClient, TelegramHandler, message_chunks, polling_lock

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmn"
ACTION = "opaque_action_123456789"


class Service:
    def __init__(self):
        self.offset = 0
        self.allowed = {11, 22}
        self.calls = []
        self.preview = '{"schedule": {"enabled": false}}'

    def authorize(self, actor):
        self.calls.append(("authorize", actor))
        if actor not in self.allowed:
            raise PermissionError("not allowed")

    def admin(self, actor, operation, target_id=None):
        if actor != 1:
            raise PermissionError("owner only")
        self.calls.append(("admin", actor, operation, target_id))
        return {"status": "pending"}

    def snapshot(self, actor):
        self.authorize(actor)
        return {"config": {"schedule": {"enabled": True}}, "resume_source": {"text": "private source"}}

    def propose(self, actor, patch):
        self.calls.append(("propose", actor, patch))
        return {"action_id": ACTION, "preview": self.preview}

    def confirm(self, actor, action):
        if actor != 11 or action != ACTION:
            raise PermissionError("another candidate or stale action")
        self.calls.append(("confirm", actor, action))
        return {"saved": True}

    def stage_resume(self, actor, source):
        self.calls.append(("stage_resume", actor, source))
        return {"status": "draft"}

    def connect(self, actor, provider, purpose):
        self.calls.append(("connect", actor, provider, purpose))
        return "https://jobs.example.test/connect/one-use-token"

    def get_update_offset(self):
        return self.offset

    def acknowledge_update(self, update_id):
        self.offset = update_id + 1

    def record_turn(self, actor, user_text, assistant_text):
        self.calls.append(("record_turn", actor, user_text, assistant_text))


def update(actor=11, text="/pause", update_id=1, **message_fields):
    return {"update_id": update_id, "message": {"from": {"id": actor, "is_bot": False},
                                                "chat": {"id": actor, "type": "private"},
                                                "text": text, **message_fields}}


def callback(actor=11, action=ACTION, update_id=2):
    return {"update_id": update_id, "callback_query": {"id": "callback-query",
            "from": {"id": actor}, "data": "jh:confirm:" + action,
            "message": {"from": {"id": 123456789, "is_bot": True}, "chat": {"id": actor, "type": "private"}}}}


@pytest.fixture
def handler():
    return TelegramHandler(Service(), Mock())


def test_profile_changes_are_previewed_and_require_explicit_confirmation(handler):
    handler.handle_update(update())
    assert ("propose", 11, {"schedule": {"enabled": False}}) in handler.service.calls
    assert not any(call[0] == "confirm" for call in handler.service.calls)
    markup = handler.client.send_message.call_args.args[2]
    assert markup["inline_keyboard"][0][0]["callback_data"] == "jh:confirm:" + ACTION
    handler.handle_update(callback())
    assert ("confirm", 11, ACTION) in handler.service.calls


def test_duplicate_updates_do_not_repeat_mutations_or_messages(handler):
    handler.handle_update(update())
    calls = list(handler.service.calls)
    sends = handler.client.send_message.call_count
    handler.handle_update(update())
    assert handler.service.calls == calls
    assert handler.client.send_message.call_count == sends


@pytest.mark.parametrize("mutation", [
    {"chat": {"id": -100111111, "type": "supergroup"}},
    {"chat": {"id": 22, "type": "private"}},
    {"from": {"id": "11"}},
    {"from": {"id": True}},
    {"from": {"id": 11, "is_bot": True}},
])
def test_only_authenticated_sender_own_private_chat_is_accepted(handler, mutation):
    handler.handle_update(update(**mutation))
    assert not handler.service.calls
    handler.client.send_message.assert_not_called()
    assert handler.service.offset == 2


def test_unknown_user_cannot_access_snapshot_or_create_profile(handler):
    handler.handle_update(update(actor=99))
    assert handler.service.calls == [("authorize", 99)]
    assert "99" in handler.client.send_message.call_args.args[1]


def test_member_cannot_register_users(handler):
    handler.handle_update(update(text="/jobhunter add 33"))
    assert not any(call[0] == "admin" for call in handler.service.calls)
    assert "do not have access" in handler.client.send_message.call_args.args[1]


def test_owner_can_register_without_becoming_candidate(handler):
    handler.handle_update(update(actor=1, text="/jobhunter add 33"))
    assert handler.service.calls == [("admin", 1, "add", 33)]


def test_forwarded_confirmation_cannot_confirm_another_candidate(handler):
    handler.handle_update(callback(actor=22))
    assert not any(call[0] == "confirm" for call in handler.service.calls)


def test_callback_actor_comes_from_callback_sender_not_bot_message(handler):
    handler.handle_update(callback())
    assert ("confirm", 11, ACTION) in handler.service.calls


def test_text_confirmation_is_explicit_and_scoped(handler):
    handler.handle_update(update(text="/confirm " + ACTION))
    assert ("confirm", 11, ACTION) in handler.service.calls


def test_plain_yes_cannot_confirm_pending_changes(handler):
    handler.handle_update(update(text="yes"))
    assert not any(call[0] == "confirm" for call in handler.service.calls)


def test_complete_large_preview_precedes_confirmation_button(handler):
    handler.service.preview = "x" * 9_000 + "LAST EXACT FACT"
    handler.handle_update(update())
    messages = handler.client.send_message.call_args_list
    assert len(messages) == 5
    assert all(len(call.args) == 2 for call in messages[:-1])
    assert "LAST EXACT FACT" in messages[-2].args[1]
    assert "inline_keyboard" in messages[-1].args[2]


def test_failed_preview_delivery_never_sends_confirmation_button_or_acks(handler):
    handler.service.preview = "x" * 5_000
    handler.client.send_message.side_effect = [{"message_id": 1}, TelegramAPIError("network failure")]
    with pytest.raises(TelegramAPIError):
        handler.handle_update(update())
    assert not any(len(call.args) > 2 for call in handler.client.send_message.call_args_list)
    assert handler.service.offset == 0


def test_oversized_preview_has_no_confirmation_button(handler):
    handler.service.preview = "x" * 25_000
    handler.handle_update(update())
    assert all(len(call.args) == 2 for call in handler.client.send_message.call_args_list)
    assert "too large" in handler.client.send_message.call_args.args[1]


def test_uploaded_resume_is_staged_without_confirming_evidence(handler):
    handler.client.download_document.return_value = b"Candidate Example\nPython Engineer"
    handler.handle_update(update(text="", document={"file_id": "file_1", "file_name": "resume.txt", "mime_type": "text/plain"}))
    staged = next(call for call in handler.service.calls if call[0] == "stage_resume")
    assert staged[1] == 11
    assert staged[2]["confirmation"] == "unconfirmed"
    assert staged[2]["trusted"] is False
    assert not any(call[0] == "confirm" for call in handler.service.calls)


def test_unsupported_upload_is_rejected_before_download(handler):
    handler.handle_update(update(text="", document={"file_id": "file_1", "file_name": "attack.py"}))
    handler.client.download_document.assert_not_called()


def test_unauthorized_upload_is_never_downloaded(handler):
    handler.handle_update(update(actor=99, text="", document={"file_id": "file_1", "file_name": "resume.txt"}))
    handler.client.download_document.assert_not_called()


def test_credentials_are_not_sent_to_planner_or_persisted(handler):
    planner = Mock()
    handler.assistant = RestrictedHermesAssistant(planner)
    handler.handle_update(update(text="password: synthetic-secret"))
    planner.assert_not_called()
    assert not any(call[0] == "record_turn" for call in handler.service.calls)


def test_conversation_answers_are_persisted_for_resumption(handler):
    handler.assistant = RestrictedHermesAssistant(lambda messages, schema: {"operation": "reply", "reply": "What dates did you work there?"})
    handler.handle_update(update(text="I maintained the API"))
    assert ("record_turn", 11, "I maintained the API", "What dates did you work there?") in handler.service.calls


def test_connect_uses_backend_link_and_does_not_accept_credentials(handler):
    handler.handle_update(update(text="/connect gmail"))
    assert ("connect", 11, "google", "gmail") in handler.service.calls
    markup = handler.client.send_message.call_args.args[2]
    assert markup["inline_keyboard"][0][0]["url"].startswith("https://jobs.example.test/")


def test_status_omits_raw_uploaded_resume_source(handler):
    handler.handle_update(update(text="/status"))
    assert "private source" not in handler.client.send_message.call_args.args[1]


def response(result, status=200):
    result_response = Mock(status_code=status, headers={})
    result_response.json.return_value = {"ok": True, "result": result}
    return result_response


def test_bot_api_uses_fixed_url_and_disables_redirects():
    session = Mock()
    session.post.return_value = response({"message_id": 1})
    TelegramClient(TOKEN, session).send_message(11, "test")
    assert session.post.call_args.args[0] == "https://api.telegram.org/bot" + TOKEN + "/sendMessage"
    assert session.post.call_args.kwargs["allow_redirects"] is False


def test_transport_failure_never_contains_token_or_url():
    session = Mock()
    session.post.side_effect = requests.Timeout("https://api.telegram.org/bot" + TOKEN + "/getUpdates")
    with pytest.raises(TelegramAPIError) as error:
        TelegramClient(TOKEN, session).get_updates(0)
    assert TOKEN not in str(error.value)
    assert "api.telegram.org" not in str(error.value)


@pytest.mark.parametrize("metadata", [
    {"file_path": "https://evil.test/data"},
    {"file_path": "../secret"},
    {"file_path": "documents/../../secret"},
    {"file_path": "documents/file.txt?token=secret"},
    {"file_path": "documents/file.txt", "file_size": 9 * 1024 * 1024},
])
def test_attachment_download_rejects_external_paths_and_oversize(metadata):
    session = Mock()
    session.post.return_value = response(metadata)
    with pytest.raises((TelegramAPIError, ResumeImportError)):
        TelegramClient(TOKEN, session).download_document({"file_id": "file_1"})
    session.get.assert_not_called()


def test_attachment_stream_cannot_exceed_limit_when_metadata_lies():
    session = Mock()
    session.post.return_value = response({"file_path": "documents/file_1.txt", "file_size": 2})
    download = response({})
    download.iter_content.return_value = [b"aa", b"bbb"]
    session.get.return_value = download
    with pytest.raises(ResumeImportError, match="limit"):
        TelegramClient(TOKEN, session).download_document({"file_id": "file_1"}, max_bytes=4)
    assert session.get.call_args.kwargs["allow_redirects"] is False
    download.close.assert_called_once()


def test_channel_requires_candidate_control_and_bot_posting_rights():
    session = Mock()
    session.post.side_effect = [response({"id": -100111111, "type": "channel", "title": "Jobs"}),
                                response({"status": "administrator"}), response({"id": 123456789}),
                                response({"status": "administrator", "can_post_messages": True})]
    result = TelegramClient(TOKEN, session).validate_destination(11, {"chat_id": "-100111111", "kind": "channel"})
    assert result["label"] == "Jobs"


def test_channel_member_is_not_authorized_by_membership():
    session = Mock()
    session.post.side_effect = [response({"id": -100111111, "type": "channel"}), response({"status": "member"})]
    with pytest.raises(PermissionError):
        TelegramClient(TOKEN, session).validate_destination(11, {"chat_id": "-100111111", "kind": "channel"})
    assert session.post.call_count == 2


def test_private_delivery_cannot_target_another_user():
    with pytest.raises(PermissionError):
        TelegramClient(TOKEN, Mock()).validate_destination(11, {"chat_id": "22", "kind": "private"})


def test_local_lock_rejects_a_second_poller(tmp_path):
    path = tmp_path / "telegram.lock"
    with polling_lock(path):
        with pytest.raises(RuntimeError, match="Another"):
            with polling_lock(path):
                pytest.fail("second poller acquired lock")


def test_unicode_preview_chunks_preserve_all_content():
    text = "🙂" * 5_000 + "last fact"
    chunks = message_chunks(text)
    assert "".join(chunks) == text
    assert all(len(chunk.encode("utf-16-le")) // 2 <= 3000 for chunk in chunks)


def test_document_upload_uses_fixed_multipart_endpoint_and_acknowledgement(tmp_path):
    document = tmp_path.resolve() / "resume.pdf"
    document.write_bytes(b"synthetic PDF content")
    session = Mock()
    session.post.return_value = response({"message_id": 123})
    result = TelegramClient(TOKEN, session).send_document(11, document, "Your resume")
    assert result == {"message_id": 123}
    assert session.post.call_args.args[0] == "https://api.telegram.org/bot" + TOKEN + "/sendDocument"
    assert session.post.call_args.kwargs["allow_redirects"] is False
    assert session.post.call_args.kwargs["files"]["document"][0] == "resume.pdf"


def test_document_upload_rejects_symlinked_parent_before_network(tmp_path):
    root = tmp_path.resolve()
    (root / "actual").mkdir()
    (root / "actual" / "document.pdf").write_bytes(b"synthetic")
    (root / "link").symlink_to(root / "actual", target_is_directory=True)
    session = Mock()
    with pytest.raises(ValueError, match="unsafe"):
        TelegramClient(TOKEN, session).send_document(11, root / "link" / "document.pdf")
    session.post.assert_not_called()


def test_document_upload_redacts_network_exception(tmp_path):
    document = tmp_path.resolve() / "resume.pdf"
    document.write_bytes(b"synthetic")
    session = Mock()
    session.post.side_effect = requests.Timeout("https://api.telegram.org/bot" + TOKEN + "/sendDocument")
    with pytest.raises(TelegramAPIError) as error:
        TelegramClient(TOKEN, session).send_document(11, document)
    assert TOKEN not in str(error.value)
