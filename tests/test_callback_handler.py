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
    assert "generate tailored resume and cover letter" in message
    assert "<b>Job ID:</b> li-1" in message


def test_handle_interested_marks_job_and_sends_hermes_message(monkeypatch):
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
    }

    monkeypatch.setattr(callback_handler, "get_job", lambda job_id: job if job_id == "li-1" else None)
    monkeypatch.setattr(callback_handler, "mark_interested", lambda job_id: calls.append(("mark", job_id)))
    monkeypatch.setattr(callback_handler, "answer_callback", lambda callback_id, text=None: calls.append(("answer", callback_id, text)))
    monkeypatch.setattr(callback_handler, "send_message", lambda text: calls.append(("send", text)) or True)

    callback_handler.handle_interested("li-1", "callback-1")

    assert ("mark", "li-1") in calls
    assert ("answer", "callback-1", "✓ Marked as interested") in calls
    sent = [call[1] for call in calls if call[0] == "send"]
    assert len(sent) == 1
    assert "HERMES JOBHUNTER ACTION" in sent[0]
    assert "OpenClaw" not in sent[0]


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
