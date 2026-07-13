import importlib
import os


def load_callback_handler(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    import callback_handler
    return importlib.reload(callback_handler)


def test_build_interested_message_routes_to_hermes_without_openclaw(monkeypatch):
    callback_handler = load_callback_handler(monkeypatch)
    job = {
        "id": "li-1",
        "title": "Lead Backend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
        "score": 27,
        "url": "https://example.com/job",
        "tech_required": "Java, Spring Boot, AWS",
        "tech_nice_to_have": "Kubernetes",
    }

    message = callback_handler.build_interested_message(job)

    assert "HERMES JOBHUNTER ACTION" in message
    assert "OpenClaw" not in message
    assert "generate a tailored resume and cover letter" in message
    assert "detect Easy Apply vs external apply" in message
    assert "<b>Job ID:</b> li-1" in message


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
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True)

    callback_handler.handle_interested("li-1", "callback-1")

    assert ("mark", "li-1") in calls
    assert ("answer", "callback-1", "✓ Research brief ready") in calls
    sent = [(call[1], call[2]) for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Research" in sent[0][0]
    assert "No published range; no ExampleCo pay data" in sent[0][0]
    assert "Ask:</b> Fixed monthly salary and bonus/equity terms" in sent[0][0]
    assert "OpenClaw" not in sent[0][0]
    buttons = [button for row in sent[0][1]["inline_keyboard"] for button in row]
    assert {button["text"] for button in buttons} >= {"✅ Apply", "🚫 Ignore", "📄 Details"}


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
        cover_json=tmp_path / "pkg" / "cover.json",
        resume_pdf=tmp_path / "pkg" / "Resume.pdf",
        cover_pdf=tmp_path / "pkg" / "Cover.pdf",
    )

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler.interest_flow, "prepare_application_package", lambda job_id: package)
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text, reply_markup=None: calls.append(("send", text, reply_markup)) or True)

    callback_handler.handle_apply("li-1", "callback-1")

    assert ("answer", "callback-1", "✓ Package generated") in calls
    sent = [(call[1], call[2]) for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "Application package ready" in sent[0][0]
    buttons = [button for row in sent[0][1]["inline_keyboard"] for button in row]
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} >= {"proceed_apply:li-1", "ignore:li-1"}


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
