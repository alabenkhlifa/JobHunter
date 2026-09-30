import importlib
import json
import os
import sqlite3
from pathlib import Path

import pytest


def load_callback_handler(monkeypatch, *, stub_cover_package=True):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    import callback_handler
    handler = importlib.reload(callback_handler)
    monkeypatch.setattr(handler, "application_already_submitted", lambda job_id: False)
    if stub_cover_package:
        monkeypatch.setattr(handler, "current_package_includes_cover_letter", lambda job_id: False)
    return handler


def test_handle_interested_marks_job_and_sends_research_brief(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Lead Backend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
        "score": 27,
        "url": "https://example.com/job",
        "tech_required": "Java",
        "tech_nice_to_have": "AWS",
        "description": "Build backend services.",
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler.interest_flow, "research_job", lambda job: callback_handler.interest_flow.build_default_research(job))
    monkeypatch.setattr(callback_handler, "mark_interested", lambda job_id: calls.append(("mark", job_id)))
    monkeypatch.setattr(callback_handler, "record_research_delivery", lambda job_id: calls.append(("research_delivered", job_id)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True)

    callback_handler.handle_interested("li-1", "callback-1")

    assert ("mark", "li-1") in calls
    assert ("research_delivered", "li-1") in calls
    assert ("answer", "callback-1", "✓ Research brief sent") in calls
    sent = [(call[1], call[2]) for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Research" in sent[0][0]
    assert "No published range; no ExampleCo pay data" in sent[0][0]
    assert "Ask:</b> Base salary, currency, pay period and bonus/equity terms" in sent[0][0]
    assert "OpenClaw" not in sent[0][0]
    buttons = [button for row in sent[0][1]["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} >= {"✅ Apply", "🚫 Ignore", "📄 Details"}


def test_text_reply_actions_use_the_same_handlers_without_polling(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(callback_handler, "handle_interested", lambda job_id: calls.append(("interested", job_id)) or True)
    monkeypatch.setattr(callback_handler, "handle_apply", lambda job_id, **kwargs: calls.append(("apply", job_id, kwargs)) or True)
    monkeypatch.setattr(callback_handler, "handle_proceed_apply", lambda job_id: calls.append(("proceed", job_id)) or True)
    monkeypatch.setattr(callback_handler, "poll_updates", lambda: calls.append(("poll",)))

    assert callback_handler.main(["--interested", "li-1"]) == 0
    assert callback_handler.main(["--apply", "li-1"]) == 0
    assert callback_handler.main(["--proceed-apply", "li-1"]) == 0
    assert calls == [("interested", "li-1"), ("apply", "li-1", {"include_cover_letter": False}),
                     ("proceed", "li-1")]


def test_interested_does_not_report_success_when_research_card_delivery_fails(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "mark_interested", lambda job_id: calls.append(("mark", job_id)))
    monkeypatch.setattr(callback_handler, "record_research_delivery", lambda job_id: calls.append(("research_delivered", job_id)))
    monkeypatch.setattr(callback_handler.interest_flow, "research_job", lambda job: callback_handler.interest_flow.build_default_research(job))
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: False)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))

    assert callback_handler.handle_interested("li-1", "callback-1") is False
    assert ("mark", "li-1") in calls
    assert ("research_delivered", "li-1") not in calls
    assert ("answer", "callback-1", "Research delivery failed") in calls
    assert callback_handler.main(["--interested", "li-1"]) == 1


def test_research_gate_is_persisted_and_reset_by_a_new_interest_or_pause(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    db_path = tmp_path / "feedback.db"
    monkeypatch.setattr(callback_handler, "get_db", lambda: sqlite3.connect(db_path))

    assert callback_handler.has_delivered_research("li-1") is False
    callback_handler.record_feedback("li-1", "interested")
    assert callback_handler.has_delivered_research("li-1") is False
    callback_handler.record_research_delivery("li-1")
    assert callback_handler.has_delivered_research("li-1") is True


def test_document_gate_tracks_the_latest_package_attempt(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    db_path = tmp_path / "feedback.db"
    monkeypatch.setattr(callback_handler, "get_db", lambda: sqlite3.connect(db_path))

    assert callback_handler.has_delivered_documents("li-1") is False
    callback_handler.record_document_delivery_step("li-1", callback_handler.PACKAGE_DELIVERY_STARTED_ACTION)
    assert callback_handler.has_delivered_documents("li-1") is False
    callback_handler.record_document_delivery_step("li-1", callback_handler.DOCUMENTS_DELIVERED_ACTION)
    assert callback_handler.has_delivered_documents("li-1") is True
    callback_handler.record_document_delivery_step("li-1", callback_handler.PACKAGE_DELIVERY_STARTED_ACTION)
    assert callback_handler.has_delivered_documents("li-1") is False
    callback_handler.record_feedback("li-1", "paused")
    assert callback_handler.has_delivered_research("li-1") is False
    callback_handler.record_feedback("li-1", "interested")
    assert callback_handler.has_delivered_research("li-1") is False
    callback_handler.record_research_delivery("li-1")
    assert callback_handler.has_delivered_research("li-1") is True


def test_send_message_requires_telegram_acknowledgement(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)

    class Response:
        status_code = 200

        def json(self):
            return {"ok": False}

    monkeypatch.setattr(callback_handler.requests, "post", lambda *args, **kwargs: Response())

    assert callback_handler.send_message("research card") is False


def test_send_document_requires_telegram_acknowledgement_and_omits_response_text(monkeypatch, tmp_path, caplog):
    callback_handler = load_callback_handler(monkeypatch)
    document = tmp_path / "Resume.pdf"
    document.write_bytes(b"%PDF-1.4\n")

    class Response:
        status_code = 200

        def json(self):
            return {"ok": False, "description": "secret response content"}

    def fake_post(url, **kwargs):
        assert url.endswith("/sendDocument")
        assert kwargs["files"]["document"][0] == "Resume.pdf"
        assert kwargs["files"]["document"][1].read() == b"%PDF-1.4\n"
        return Response()

    monkeypatch.setattr(callback_handler.requests, "post", fake_post)

    assert callback_handler.send_document(document, "Tailored resume") is False
    assert "secret response content" not in caplog.text


def test_callback_cli_validates_payload_before_dispatch(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id} if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "handle_apply", lambda job_id, callback_id=None, **kwargs: calls.append((job_id, callback_id, kwargs)) or True)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("rejected", text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("message", text)) or True)

    assert callback_handler.main(["--callback-data", "apply:li-1", "--callback-query-id", "callback-1"]) == 0
    assert callback_handler.main(["--callback-data", "apply_cover:li-1", "--callback-query-id", "callback-cover"]) == 3
    assert calls[:2] == [("li-1", "callback-1", {}), ("rejected", "Reviewed cover draft required")]
    assert "--cover-draft" in calls[2][1]
    assert callback_handler.main(["--callback-data", "unknown:li-1", "--callback-query-id", "callback-2"]) == 1
    assert callback_handler.main(["--callback-data", "apply:../li-1", "--callback-query-id", "callback-3"]) == 1
    assert callback_handler.main(["--callback-data", "skip_reason:other:li-1", "--callback-query-id", "callback-4"]) == 1
    assert calls == [
        ("li-1", "callback-1", {}),
        ("rejected", "Reviewed cover draft required"),
        ("message", callback_handler.COVER_DRAFT_REQUIRED_MESSAGE),
        ("rejected", "Unknown action"),
        ("rejected", "Invalid job ID"),
        ("rejected", "Invalid skip reason"),
    ]
    assert callback_handler.main(["--callback-data", "apply:li-1"]) == 0
    assert calls[-1] == ("li-1", None, {})


def test_apply_requires_acknowledged_research_before_generating_package(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: False)
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", lambda *args, **kwargs: calls.append("package"))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(text))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("message", text)) or True)

    assert callback_handler.handle_apply("li-1", "callback-1") is False
    assert callback_handler.main(["--apply", "li-1"]) == 1
    assert callback_handler.main(["--callback-data", "apply:li-1"]) == 3
    assert "Open the research brief first" in calls
    assert any(call[0] == "message" and "Choose Interested before Apply" in call[1] for call in calls if isinstance(call, tuple))
    assert "package" not in calls


def test_submitted_application_cannot_restart_interested_or_apply(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "application_already_submitted", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "mark_interested", lambda job_id: calls.append("interest"))
    monkeypatch.setattr(
        callback_handler.interest_flow,
        "prepare_application_package",
        lambda *args, **kwargs: calls.append("package"),
    )
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(text) or True)

    assert callback_handler.handle_interested("li-1") is False
    assert callback_handler.handle_apply("li-1") is False
    assert "interest" not in calls
    assert "package" not in calls
    assert sum("already submitted" in message.lower() for message in calls) == 2


def test_handle_apply_generates_package_and_sends_final_apply_cta(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Lead Backend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
        "score": 27,
        "url": "https://example.com/job",
        "source": "LinkedIn",
    }
    package = callback_handler.interest_flow.ApplicationPackage(
        job_id="li-1",
        package_dir=tmp_path / "pkg",
        resume_json=tmp_path / "pkg" / "resume.json",
        cover_json=None,
        resume_pdf=tmp_path / "pkg" / "Resume.pdf",
        cover_pdf=None,
    )

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda job_id, action: calls.append(("delivery_state", action)))
    package_calls = []
    monkeypatch.setattr(
        callback_handler.interest_flow,
        "prepare_application_package",
        lambda job_id, **kwargs: package_calls.append((job_id, kwargs)) or package,
    )
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_document", lambda path, caption: calls.append(("document", Path(path).name, caption)) or True)
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True)

    callback_handler.handle_apply("li-1", "callback-1")

    assert package_calls == [
        (
            "li-1",
            {
                "db_path": callback_handler.DB_PATH,
                "profile_path": callback_handler.PROFILE_PATH,
                "output_dir": callback_handler.OUTPUT_DIR,
                "include_cover_letter": False,
            },
        )
    ]
    assert ("answer", "callback-1", "✓ Documents delivered") in calls
    assert [call[0] for call in calls if call[0] in {"document", "send"}] == ["document", "send"]
    assert ("delivery_state", callback_handler.PACKAGE_DELIVERY_STARTED_ACTION) in calls
    assert ("delivery_state", callback_handler.DOCUMENTS_DELIVERED_ACTION) in calls
    sent = [(call[1], call[2]) for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Application resume delivered" in sent[0][0]
    assert "cover letter" not in sent[0][0].lower()
    assert str(tmp_path) not in sent[0][0]
    buttons = [button for row in sent[0][1]["inline_keyboard"] for button in row]
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} >= {"proceed_apply:li-1", "pause:li-1"}


def test_explicit_cover_request_generates_and_delivers_both_documents(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}
    package = callback_handler.interest_flow.ApplicationPackage(
        job_id="li-1", package_dir=tmp_path, resume_json=tmp_path / "resume.json",
        cover_json=tmp_path / "cover.json", resume_pdf=tmp_path / "Resume.pdf",
        cover_pdf=tmp_path / "CoverLetter.pdf",
    )
    calls = []
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda *args: None)
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package",
                        lambda job_id, **kwargs: calls.append(("prepare", kwargs)) or package)
    monkeypatch.setattr(callback_handler, "send_document",
                        lambda path, caption: calls.append(("document", Path(path).name)) or True)
    monkeypatch.setattr(callback_handler, "send_message",
                        lambda text, reply_markup=None: calls.append(("message", text)) or True)

    draft = {"paragraphs": ["Opening", "Example", "Closing"], "evidence_ids": ["E1"], "review_flags": []}
    draft_path = tmp_path / "cover-drafts" / "li-1.json"
    draft_path.parent.mkdir()
    draft_path.write_text(json.dumps(draft), encoding="utf-8")
    assert callback_handler.main(["--apply", "li-1", "--cover-letter", "--cover-draft", str(draft_path)]) == 0
    assert next(call[1] for call in calls if call[0] == "prepare")["cover_letter_draft"] == draft
    assert next(call[1] for call in calls if call[0] == "prepare")["include_cover_letter"] is True
    assert [call[1] for call in calls if call[0] == "document"] == ["Resume.pdf", "CoverLetter.pdf"]
    assert "resume and cover letter" in next(call[1] for call in calls if call[0] == "message")


def test_cover_request_requires_reviewed_draft_before_delivery_state_changes(monkeypatch, capsys):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda *args: calls.append("delivery state"))
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package",
                        lambda *args, **kwargs: calls.append("package"))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("message", text)) or True)

    assert callback_handler.handle_apply("li-1", include_cover_letter=True) is False
    assert calls == [("message", callback_handler.COVER_DRAFT_REQUIRED_MESSAGE)]
    with pytest.raises(SystemExit) as error:
        callback_handler.main(["--apply", "li-1", "--cover-letter"])
    assert error.value.code == 2
    assert "--cover-draft" in capsys.readouterr().err
    assert calls == [("message", callback_handler.COVER_DRAFT_REQUIRED_MESSAGE)]


def test_resume_apply_cannot_replace_existing_cover_package(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch, stub_cover_package=False)
    calls = []
    output_dir = tmp_path / "output"
    package_dir = output_dir / "li-1-exampleco-backend-engineer"
    package_dir.mkdir(parents=True)
    (package_dir / "tailoring_manifest.json").write_text(
        json.dumps({"job_id": "li-1", "cover_letter_included": True}), encoding="utf-8"
    )
    db_path = tmp_path / "applications.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE applications (id INTEGER PRIMARY KEY, job_id TEXT, package_path TEXT)")
        db.execute(
            "INSERT INTO applications (job_id, package_path) VALUES (?, ?)",
            ("li-1", str(package_dir)),
        )
    monkeypatch.setattr(callback_handler, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(callback_handler, "get_db", lambda: sqlite3.connect(db_path))
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda *args: calls.append("delivery state"))
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package",
                        lambda *args, **kwargs: calls.append("package"))
    monkeypatch.setattr(callback_handler, "answer_callback",
                        lambda callback_id, text=None: calls.append(("answer", text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("message", text)) or True)

    assert callback_handler.dispatch_callback_data("apply:li-1", "callback-1") == 3
    assert calls[0] == ("answer", "Cover retry needs the same draft")
    assert "--cover-letter --cover-draft" in calls[1][1]
    assert "delivery state" not in calls
    assert "package" not in calls
    (package_dir / "tailoring_manifest.json").write_text(
        json.dumps({"job_id": "li-1", "cover_letter_included": False}), encoding="utf-8"
    )
    assert callback_handler.current_package_includes_cover_letter("li-1") is False


def test_handle_apply_pauses_when_resume_refinement_is_required(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Software Architect",
        "company": "AIQU",
        "location": "Dubai",
        "url": "https://example.com/job",
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda job_id, action: None)

    def block_package(*args, **kwargs):
        raise callback_handler.interest_flow.TailoringReadinessError(
            "Complete the Software Architect resume variant before generating the package."
        )

    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", block_package)
    monkeypatch.setattr(
        callback_handler,
        "answer_callback",
        lambda callback_id, text=None: calls.append(("answer", callback_id, text)),
    )
    monkeypatch.setattr(
        callback_handler,
        "send_message",
        lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True,
    )

    callback_handler.handle_apply("li-1", "callback-1")

    assert ("answer", "callback-1", "Resume refinement needed") in calls
    sent = [(call[1], call[2]) for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Resume package paused" in sent[0][0]
    buttons = [button for row in sent[0][1]["inline_keyboard"] for button in row]
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} == {
        "resume_refine:li-1",
        "pause:li-1",
    }
    assert any(button.get("url") == job["url"] for button in buttons)


def test_apply_returns_failure_when_package_card_is_not_delivered(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}
    package = callback_handler.interest_flow.ApplicationPackage(
        job_id="li-1",
        package_dir=tmp_path,
        resume_json=tmp_path / "resume.json",
        cover_json=tmp_path / "cover.json",
        resume_pdf=tmp_path / "resume.pdf",
        cover_pdf=tmp_path / "cover.pdf",
    )
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda job_id, action: None)
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", lambda *args, **kwargs: package)
    monkeypatch.setattr(callback_handler, "send_document", lambda path, caption: True)
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: False)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(text))

    assert callback_handler.handle_apply("li-1", "callback-1") is False
    assert callback_handler.main(["--apply", "li-1"]) == 1
    assert "Documents sent; card delivery failed" in calls


def test_cover_card_delivery_failure_requires_the_same_draft_for_retry(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo"}
    package = callback_handler.interest_flow.ApplicationPackage(
        job_id="li-1", package_dir=tmp_path, resume_json=tmp_path / "resume.json",
        cover_json=tmp_path / "cover.json", resume_pdf=tmp_path / "resume.pdf",
        cover_pdf=tmp_path / "cover.pdf",
    )
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda *args: None)
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", lambda *args, **kwargs: package)
    monkeypatch.setattr(callback_handler, "send_document", lambda path, caption: True)
    monkeypatch.setattr(callback_handler, "send_message",
                        lambda text, reply_markup=None: calls.append((text, reply_markup)) or reply_markup is None)
    monkeypatch.setattr(callback_handler, "answer_callback",
                        lambda callback_id, text=None: calls.append(("answer", text)))

    assert callback_handler.handle_apply(
        "li-1", "callback-1", include_cover_letter=True, cover_letter_draft={"paragraphs": ["reviewed"]}
    ) is False
    assert ("answer", "Documents sent; card delivery failed") in calls
    assert any("--cover-letter --cover-draft" in text for text, _ in calls if text != "answer")


@pytest.mark.parametrize("failed_document", ["resume.pdf", "cover.pdf"])
def test_apply_never_offers_proceed_when_a_document_fails(monkeypatch, tmp_path, failed_document):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}
    package = callback_handler.interest_flow.ApplicationPackage(
        job_id="li-1",
        package_dir=tmp_path,
        resume_json=tmp_path / "resume.json",
        cover_json=tmp_path / "cover.json",
        resume_pdf=tmp_path / "resume.pdf",
        cover_pdf=tmp_path / "cover.pdf",
    )
    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "has_delivered_research", lambda job_id: True)
    monkeypatch.setattr(callback_handler, "record_document_delivery_step", lambda job_id, action: calls.append(("state", action)))
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", lambda *args, **kwargs: package)
    monkeypatch.setattr(callback_handler, "send_document", lambda path, caption: calls.append(("document", Path(path).name)) or Path(path).name != failed_document)
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(("message", text, reply_markup)) or True)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", text)))

    assert callback_handler.handle_apply("li-1", "callback-1", include_cover_letter=True,
                                         cover_letter_draft={"paragraphs": ["reviewed"]}) is False
    assert ("state", callback_handler.DOCUMENTS_DELIVERED_ACTION) not in calls
    assert not any(call[0] == "message" and call[2] for call in calls)
    assert any(call[0] == "message" and "delivery failed" in call[1] for call in calls)
    assert any(call[0] == "message" and "--cover-letter --cover-draft" in call[1] for call in calls)


def test_proceed_requires_a_generated_package(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []

    class Database:
        def execute(self, query, params):
            calls.append(("query", params))
            return self

        def fetchone(self):
            return ("interested", None)

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "get_db", Database)
    monkeypatch.setattr(callback_handler.scraper, "record_application_stage", lambda *args, **kwargs: calls.append(("stage",)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))

    assert callback_handler.handle_proceed_apply("li-1", "callback-1") is False
    assert ("answer", "callback-1", "Generate the package first") in calls
    assert ("stage",) not in calls


def test_proceed_requires_delivery_of_generated_documents(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []

    class Database:
        def execute(self, query, params):
            return self

        def fetchone(self):
            return ("package_generated", str(tmp_path))

        def close(self):
            pass

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: {"id": job_id})
    monkeypatch.setattr(callback_handler, "get_db", Database)
    monkeypatch.setattr(callback_handler, "has_delivered_documents", lambda job_id: False)
    monkeypatch.setattr(callback_handler.scraper, "record_application_stage", lambda *args, **kwargs: calls.append("stage"))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("message", text)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(text))

    assert callback_handler.handle_proceed_apply("li-1", "callback-1") is False
    assert "Deliver the documents first" in calls
    assert "stage" not in calls
    assert any(call[0] == "message" and "Retry Apply" in call[1] for call in calls if isinstance(call, tuple))


def test_proceed_records_approval_after_package_generation(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}

    class Database:
        def execute(self, query, params):
            return self

        def fetchone(self):
            return ("package_generated", str(tmp_path))

        def close(self):
            calls.append(("close",))

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "get_db", Database)
    monkeypatch.setattr(callback_handler, "has_delivered_documents", lambda job_id: True)
    monkeypatch.setattr(callback_handler.scraper, "record_application_stage", lambda db, job_id, stage, **kwargs: calls.append(("stage", job_id, stage)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("send", text)) or True)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))

    assert callback_handler.handle_proceed_apply("li-1", "callback-1") is True
    assert ("stage", "li-1", "approved_to_prepare_apply") in calls
    assert ("answer", "callback-1", "✓ Apply prep approved") in calls
    assert any(call[0] == "send" and "Apply prep approved" in call[1] for call in calls)


def test_proceed_notice_failure_can_be_retried_without_rewriting_stage(monkeypatch, tmp_path):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    stage = ["package_generated"]
    deliveries = [False, True]
    job = {"id": "li-1", "title": "Backend Engineer", "company": "ExampleCo", "location": "Dubai"}

    class Database:
        def execute(self, query, params):
            return self

        def fetchone(self):
            return (stage[0], str(tmp_path))

        def close(self):
            pass

    def record_stage(db, job_id, new_stage, **kwargs):
        calls.append(("stage", new_stage))
        stage[0] = new_stage

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job)
    monkeypatch.setattr(callback_handler, "get_db", Database)
    monkeypatch.setattr(callback_handler, "has_delivered_documents", lambda job_id: True)
    monkeypatch.setattr(callback_handler.scraper, "record_application_stage", record_stage)
    monkeypatch.setattr(callback_handler, "send_message", lambda text: deliveries.pop(0))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", text)))

    assert callback_handler.handle_proceed_apply("li-1", "callback-1") is False
    assert ("answer", "Approval saved; notice delivery failed") in calls
    assert callback_handler.handle_proceed_apply("li-1", "callback-2") is True
    assert calls.count(("stage", "approved_to_prepare_apply")) == 1


def test_resume_refine_and_pause_callbacks_record_feedback(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Software Architect",
        "company": "AIQU",
        "location": "Dubai",
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(
        callback_handler,
        "record_feedback",
        lambda job_id, action, reason=None: calls.append(("feedback", job_id, action, reason)),
    )
    monkeypatch.setattr(
        callback_handler,
        "answer_callback",
        lambda callback_id, text=None: calls.append(("answer", callback_id, text)),
    )
    monkeypatch.setattr(
        callback_handler,
        "send_message",
        lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True,
    )

    callback_handler.handle_resume_refine("li-1", "callback-refine")
    callback_handler.handle_pause("li-1", "callback-pause")

    assert (
        "feedback",
        "li-1",
        "resume_refine",
        "resume refinement requested after tailoring gate",
    ) in calls
    assert ("feedback", "li-1", "paused", "user paused application workflow") in calls
    assert ("answer", "callback-refine", "Resume refinement instructions ready") in calls
    assert ("answer", "callback-pause", "✓ Paused") in calls
    assert any(call[0] == "send" and "Software Architect" in call[1] for call in calls)


def test_runtime_paths_are_project_anchored_outside_repo(monkeypatch, tmp_path):
    monkeypatch.setenv("JOBHUNTER_DB_PATH", "data/test-jobs.db")
    monkeypatch.setenv("JOBHUNTER_PROFILE_PATH", "data/test-profile.json")
    monkeypatch.setenv("JOBHUNTER_OUTPUT_DIR", "data/test-output")
    monkeypatch.chdir(tmp_path)

    callback_handler = load_callback_handler(monkeypatch)
    project_dir = Path(callback_handler.__file__).resolve().parent

    assert callback_handler.DB_PATH == project_dir / "data" / "test-jobs.db"
    assert callback_handler.PROFILE_PATH == project_dir / "data" / "test-profile.json"
    assert callback_handler.OUTPUT_DIR == project_dir / "data" / "test-output"


def test_handle_details_records_feedback_and_sends_details(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Lead Backend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
        "score": 27,
        "url": "https://example.com/job",
        "description": "Build backend services.",
        "tech_required": "Java",
        "tech_nice_to_have": "AWS",
        "salary": "AED 40k",
        "work_model": "hybrid",
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "record_feedback", lambda job_id, action, reason=None: calls.append(("feedback", job_id, action, reason)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("send", text)) or True)

    callback_handler.handle_details("li-1", "callback-1")

    assert ("feedback", "li-1", "details", "user requested details") in calls
    assert ("answer", "callback-1", "Opening details") in calls
    sent = [call[1] for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Build backend services." in sent[0]
    assert "https://example.com/job" in sent[0]


def test_handle_skip_with_reason_records_specific_feedback(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    calls = []
    job = {
        "id": "li-1",
        "title": "Junior Frontend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "mark_skipped", lambda job_id, reason=None: calls.append(("skip", job_id, reason)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))

    callback_handler.handle_skip("li-1", "callback-1", reason_code="too_junior")

    assert ("skip", "li-1", "too junior / low seniority") in calls
    assert ("answer", "callback-1", "✓ Skipped: too junior / low seniority") in calls
