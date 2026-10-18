import base64
import datetime as dt
import json
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply import email_verification as ev
from jobhunter_integrations.gmail_verification import extract_verification_code, find_message

URL = "https://job-boards.eu.greenhouse.io/example/jobs/123"
NOW = dt.datetime.now(dt.timezone.utc)
CODE = "Ab3dEf9X"  # Synthetic test data, never a candidate's code.


@pytest.mark.parametrize("text,subject,expected", [
    ("Your verification code is Ab3dEf9X", "", CODE),
    ("Security code:\nAb3dEf9X\nExpires soon.", "", CODE),
    ("Ab3dEf9X", "Verify with this code", CODE),
    ("Your code is 12345678", "", "12345678"),
    ("Your code is ABCDEFGH", "", "ABCDEFGH"),
    ("Your code is Ab3dEf9X. Another code is Cd4eFg8Y", "", None),
    ("Ab3dEf9X", "Application received", None),
    ("Verification code 2024", "", None),
    ("Thank you. Your application is received.", "", None),
    ("Your code is Ab3dEf9XTOOLONG", "", None),
])
def test_extract_code_preserves_case_and_rejects_ambiguous_copy(text, subject, expected):
    assert extract_verification_code(text, subject=subject) == expected


def mail(mid, text, *, seconds=1, domain="eu.greenhouse-mail.io", recipient="jobs@example.com", mime="text/plain"):
    return {"id": mid, "internalDate": str(int((NOW-dt.timedelta(seconds=seconds)).timestamp()*1000)),
            "payload": {"mimeType": mime, "headers": [
                {"name": "From", "value": "noreply@"+domain}, {"name": "To", "value": recipient}],
                "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()}}}


def test_lookup_skips_newer_ack_and_html_attributes_without_mutating_mail():
    items = [mail("ack", "Application received", seconds=1),
             mail("html", '<style>code Cd4eFg8Y</style><p>Your verification code:</p><strong>Ab3dEf9X</strong>', seconds=2, mime="text/html"),
             mail("wrong", "Code: Cd4eFg8Y", domain="evil.eu.greenhouse-mail.io"),
             mail("recipient", "Code: Cd4eFg8Y", recipient="personal@example.com"),
             mail("stale", "Code: Cd4eFg8Y", seconds=500),
             mail("future", "Code: Cd4eFg8Y", seconds=-1)]
    service = Mock()
    service.users().messages().list().execute.return_value = {"messages": [{"id": i["id"]} for i in items]}
    service.users().messages().get().execute.side_effect = items
    result = find_message(service, account="jobs@example.com", sender_domain="eu.greenhouse-mail.io",
                          after=NOW-dt.timedelta(seconds=30), now=NOW, code_length=8)
    assert result["message_id"] == "html" and result["code"] == CODE
    service.users().messages().modify.assert_not_called()


class Client:
    def __init__(self, **state):
        self.state = {"url": URL, "present": True, "ready": True, "email_prompt": True,
                      "manual": False, "recipient": "jobs@example.com", **state}
        self.typed = []
        self.clicks = 0
        self.cleared = False

    def evaluate(self, expression):
        if "email_prompt:" in expression:
            return dict(self.state)
        if "el.click()" in expression:
            self.clicks += 1
            return True
        if "el.value=''" in expression:
            self.cleared = True
            return None
        if "el.focus()" in expression:
            return True
        raise AssertionError("Unexpected browser operation")

    def call(self, method, params):
        assert method == "Input.insertText"
        self.typed.append(params["text"])


def run(client, *, approved=True, lookup=None, **kwargs):
    return ev.complete_email_verification(client, page_url=URL, requested_at=NOW,
        approved=approved, service_factory=Mock(), lookup=lookup or Mock(return_value={"code": CODE}),
        account_resolver=lambda: "jobs@example.com", sleep=Mock(), **kwargs)


def test_approved_email_handoff_types_case_preserved_code_and_one_continuation():
    c = Client()
    lookup = Mock(side_effect=[None, {"code": CODE}])
    assert run(c, lookup=lookup, attempts=2)
    assert "".join(c.typed) == CODE and c.clicks == 1 and c.cleared
    assert lookup.call_count == 2
    assert lookup.call_args.kwargs["sender_domain"] == "eu.greenhouse-mail.io"
    assert lookup.call_args.kwargs["after"] == NOW


@pytest.mark.parametrize("state", [
    {"manual": True}, {"ready": False}, {"email_prompt": False},
    {"url": "https://evil.example/apply"}, {"recipient": "personal@example.com"},
])
def test_unrecognized_challenge_or_wrong_mailbox_never_reads_or_types(state):
    c, lookup = Client(**state), Mock()
    with pytest.raises(PermissionError):
        run(c, lookup=lookup)
    lookup.assert_not_called()
    assert not c.typed and not c.clicks


def test_no_approval_never_reads_or_types():
    c, lookup = Client(), Mock()
    with pytest.raises(PermissionError, match="approved"):
        run(c, approved=False, lookup=lookup)
    lookup.assert_not_called()
    assert not c.typed and not c.clicks


def test_poll_exhaustion_is_pending_without_repeating_submission():
    c, lookup = Client(), Mock(return_value=None)
    with pytest.raises(PermissionError, match="pending"):
        run(c, lookup=lookup, attempts=2)
    assert lookup.call_count == 2 and c.clicks == 0 and not c.typed


def test_provider_errors_are_sanitized():
    c = Client()
    with pytest.raises(PermissionError) as error:
        run(c, lookup=Mock(side_effect=PermissionError("secret-token-and-mail-body")))
    assert "secret-token" not in str(error.value) and not c.typed


def test_unsupported_ats_does_not_read_mail_or_click():
    c, service = Mock(), Mock()
    assert not ev.complete_email_verification(c, page_url="https://another.example/apply",
        requested_at=NOW, approved=True, service_factory=service)
    service.assert_not_called()
    c.evaluate.assert_not_called()


def test_changed_page_during_mail_lookup_does_not_receive_code():
    c = Client()
    def lookup(*args, **kwargs):
        c.state["url"] = "https://evil.example/apply"
        return {"code": CODE}
    with pytest.raises(PermissionError, match="changed"):
        run(c, lookup=lookup)
    assert not c.typed and c.clicks == 0


def test_submit_invokes_reader_only_after_approved_initial_click_and_stays_attempted(tmp_path, monkeypatch):
    from jobhunter_auto_apply import engine as module
    c = Mock()
    c.evaluate.side_effect = [URL, {"ok": True}]
    engine = module.AutoApplyEngine(module.ApplyConfig(db_path=str(tmp_path/'jobs.db'), tracker_sync=False,
        evidence_enabled=False, expected_page_url=URL), c)
    engine._verify_before_mutation = Mock()
    engine.inspect = Mock(return_value=module.PageInspection(URL+'/confirmation', 'Thank you', 'Received'))
    monkeypatch.setattr(module.time, "sleep", Mock())
    def handoff(client, **kwargs):
        assert c.evaluate.call_count == 2 and kwargs['approved'] is True
        with sqlite3.connect(engine.config.db_path) as db:
            stage, notes = db.execute('SELECT stage,notes FROM applications').fetchone()
        assert stage == 'submission_attempted'
        assert json.loads(notes)['email_code_requested_at'] == kwargs['requested_at'].isoformat()
        return True
    monkeypatch.setattr(module, "complete_email_verification", handoff)
    engine.click_submit('synthetic-job', 'button[type=submit]', approved=True)
    with sqlite3.connect(engine.config.db_path) as db:
        assert db.execute('SELECT stage FROM applications').fetchone()[0] == 'submission_attempted'


def test_resume_email_reads_recorded_request_without_initial_submit(tmp_path, monkeypatch):
    from jobhunter_auto_apply import engine as module
    config = module.ApplyConfig(db_path=str(tmp_path/'jobs.db'), tracker_sync=False,
                               evidence_enabled=False, expected_page_url=URL)
    module._record(config, 'synthetic-job', 'submission_attempted', application_url=URL,
        notes=json.dumps({'submit_clicked': True, 'exact_role_form_verified': True,
                          'email_code_requested_at': dt.datetime.now(dt.timezone.utc).isoformat()}))
    c, helper = Mock(), Mock(return_value=True)
    engine = module.AutoApplyEngine(config, c)
    engine.inspect = Mock()
    monkeypatch.setattr(module, 'complete_email_verification', helper)
    with pytest.raises(PermissionError):
        engine.verify_email('synthetic-job')
    helper.assert_not_called()
    engine.verify_email('synthetic-job', approved=True)
    helper.assert_called_once()
    c.evaluate.assert_not_called()
    module._record(config, 'synthetic-job', 'submitted')
    with pytest.raises(PermissionError):
        engine.verify_email('synthetic-job', approved=True)
    assert helper.call_count == 1


def test_resume_cli_requires_exact_application_url():
    from jobhunter_auto_apply.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(['verify-email', '--job-id', 'synthetic-job', '--approved'])
    args = build_parser().parse_args(['verify-email', '--job-id', 'synthetic-job', '--page-url', URL, '--approved'])
    assert args.command == 'verify-email' and args.approved
