import json
import sqlite3
from contextlib import closing
from unittest.mock import Mock

import pytest
import scraper

from jobhunter_auto_apply import cli, engine as engine_module, receipt as receipt_module
from jobhunter_auto_apply.engine import ApplyConfig, AutoApplyEngine, GateRefused


URL = "https://jobs.example.test/acme/senior-backend/apply"
JOB_ID = "es-123"
RUN_ID = "2026-10-15"


def _db(tmp_path, *, stage=None, submitted_at=None):
    db = tmp_path / "jobs.db"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT, company TEXT, status TEXT, location TEXT)")
        conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?)",
                     (JOB_ID, "Senior Backend Engineer", "Acme", "interested", "Madrid, Spain"))
        if stage:
            scraper.record_application_stage(conn, JOB_ID, stage, sync=False)
            if submitted_at:
                conn.execute("UPDATE applications SET submitted_at=?", (submitted_at,))
    return db


def _form(*, heading="Senior Backend Engineer", button="Submit application", text=None):
    return {
        "url": URL, "title": "Acme | Careers",
        "text": text if text is not None else (
            "Senior Backend Engineer\nExpected salary?\nDo you need visa sponsorship?\n"
            "I have read the privacy policy\nI certify that this is true and correct"),
        "headings": [heading],
        "inputs": [
            {"tag": "input", "type": "text", "id": "first_name", "name": "first_name", "label": "First name",
             "required": True, "value_present": False},
            {"tag": "input", "type": "email", "id": "email", "name": "email", "label": "Email",
             "required": True, "value_present": False},
            {"tag": "input", "type": "file", "id": "resume", "name": "resume", "label": "Resume/CV",
             "required": True, "value_present": False},
            {"tag": "input", "type": "text", "id": "salary", "name": "salary",
             "label": "Expected salary (EUR gross per year)", "required": True, "value_present": False},
            {"tag": "input", "type": "checkbox", "id": "gdpr", "name": "gdpr",
             "label": "I have read the privacy policy", "required": True, "value_present": False},
        ],
        "buttons": [{"text": button, "type": "submit"}],
    }


CONFIRMATION = {"url": URL + "/thanks", "title": "Acme", "text": "Thank you for applying to Acme!",
                "headings": ["Application submitted"], "inputs": [], "buttons": []}


class Browser:
    """Fake CDP page; clicking submit navigates to the confirmation page."""

    def __init__(self, page=None, after_click=None):
        self.page = page or _form()
        self.after_click = after_click
        self.uploads, self.clicked, self.shots = [], [], []

    def evaluate(self, expression, **kwargs):
        if expression.strip() == "location.href":
            return self.page["url"]
        if "el.click()" in expression:
            self.clicked.append(expression)
            if self.after_click:
                self.page = self.after_click
            return {"ok": True}
        return self.page

    def upload_file(self, selector, path):
        self.uploads.append((selector, path))

    def screenshot(self, path):
        with open(path, "wb") as stream:
            stream.write(b"png")
        self.shots.append(path)
        return path


class Gate:
    def __init__(self, *, refuse=None, mode="live"):
        self.calls = []
        self.refuse = refuse
        self.mode = mode

    def __call__(self, db_path, job_id, action, *, page_url, **kwargs):
        self.calls.append((job_id, action, page_url))
        if self.refuse and action in self.refuse:
            raise PermissionError(self.refuse[action])
        return {"run_id": RUN_ID, "mode": self.mode}


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(engine_module.time, "sleep", lambda _: None)
    tracker = Mock(return_value=True)
    monkeypatch.setattr(scraper, "sync_application_tracker_if_enabled", tracker)
    return tracker


def _engine(tmp_path, db, browser, gate, **kwargs):
    config = ApplyConfig(db_path=str(db), output_dir=str(tmp_path / "out"), expected_page_url=URL,
                         policy_run=True, gate_authorizer=gate, verify_submission=True, **kwargs)
    return AutoApplyEngine(config, client=browser)


def _row(db):
    with closing(sqlite3.connect(db)) as conn:
        return conn.execute("SELECT stage, notes, error, submitted_at FROM applications ORDER BY id DESC").fetchone()


# --------------------------------------------------------------------------- inspect

def test_policy_inspect_classifies_fields_instead_of_page_text(tmp_path, quiet):
    db = _db(tmp_path)
    gate = Gate()
    answers = {"first_name": {"value": "Ala"}, "email": {"value": "jobs@example.com"},
               "salary_expectation": {"value": "62000"}, "consent": {"value": "Yes"}}
    engine = _engine(tmp_path, db, Browser(), gate, tracker_sync=True, answers=answers)
    assert engine.config.tracker_sync is False
    inspection = engine.inspect(JOB_ID)
    assert inspection.sensitive_questions  # legacy signal still computed …
    stage, notes, error, _ = _row(db)
    assert stage == "draft_inspected" and error is None  # … but does not block a policy run
    assert f"autopilot {RUN_ID}" in notes and json.loads(notes)["hard_stops"] == []
    assert gate.calls == [(JOB_ID, "inspect", URL)]
    quiet.assert_not_called()
    assert {item["answer_key"] for item in engine.last_plan["fill"]} >= {"consent"}


def test_non_policy_inspect_keeps_blocked_unknown_questions(tmp_path, quiet):
    db = _db(tmp_path)
    engine = AutoApplyEngine(ApplyConfig(db_path=str(db), output_dir=str(tmp_path), evidence_enabled=False,
                                         tracker_sync=False), client=Browser())
    engine.inspect(JOB_ID)
    stage, notes, _, _ = _row(db)
    assert stage == "blocked_unknown_questions" and "autopilot" not in notes


def test_policy_inspect_records_visible_challenge(tmp_path):
    db = _db(tmp_path)
    page = {**_form(), "challenge_visible": True}
    _engine(tmp_path, db, Browser(page), Gate()).inspect(JOB_ID)
    stage, _, error, _ = _row(db)
    assert stage == "blocked_site_challenge" and error == "captcha"


def test_cli_inspect_policy_json_adds_question_plan(tmp_path, monkeypatch, capsys, quiet):
    db = _db(tmp_path)
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"first_name": {"value": "Ala", "source": "profile", "kind": "text"},
                                   "email": {"value": "jobs@example.com", "source": "jobs_mailbox", "kind": "email"},
                                   "salary_expectation": {"value": None, "source": "missing", "kind": "number"},
                                   "consent": {"value": "Yes", "source": "policy", "kind": "checkbox"}}))
    browser = Browser()
    monkeypatch.setattr(engine_module, "connect_first_page", lambda *a, **k: browser)
    gate = Gate()
    monkeypatch.setattr(engine_module, "_default_gate_authorizer", gate)
    rc = cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "--cdp-port", "9333", "inspect",
                   "--job-id", JOB_ID, "--page-url", URL, "--json", "--policy-run", "--answers", str(answers)])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["hard_stops"] == [{"label": "Expected salary (EUR gross per year)",
                                      "reason": "unanswerable:Expected salary (EUR gross per year)"}]
    assert payload["question_plan"]["hard_stops"] == payload["hard_stops"]
    assert {item["answer_key"] for item in payload["question_plan"]["fill"]} == {"first_name", "email", "consent"}
    assert payload["run_id"] == RUN_ID and "page_text" not in payload
    quiet.assert_not_called()


def test_cli_inspect_without_policy_has_no_question_plan(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)
    monkeypatch.setattr(engine_module, "connect_first_page", lambda *a, **k: Browser())
    rc = cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "inspect", "--job-id", JOB_ID,
                   "--page-url", URL, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0 and "question_plan" not in payload and "page_text" not in payload
    assert set(payload) == {"url", "title", "text_excerpt", "inputs", "buttons", "links", "headings", "blockers",
                            "sensitive_questions", "missing_required", "invalid_fields", "frame_path", "prefill_issues"}


@pytest.mark.parametrize("command", [
    ["inspect", "--job-id", JOB_ID, "--page-url", URL],
    ["upload", "--job-id", JOB_ID, "--page-url", URL, "--selector", "#resume", "--file", "RESUME"],
    ["submit", "--job-id", JOB_ID, "--page-url", URL, "--selector", "button[type=submit]"],
    ["verify-email", "--job-id", JOB_ID, "--page-url", URL],
])
def test_cli_gate_refusal_exits_4_with_one_line_reason(tmp_path, monkeypatch, capsys, command):
    db = _db(tmp_path)
    resume = tmp_path / "out" / JOB_ID / "resume.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF")
    browser = Browser()
    monkeypatch.setattr(engine_module, "connect_first_page", lambda *a, **k: browser)
    action = {"verify-email": "verify-email"}.get(command[0], command[0])
    monkeypatch.setattr(engine_module, "_default_gate_authorizer",
                        Gate(refuse={action: "autopilot disabled\n(enable it from Telegram)"}))
    argv = ["--db", str(db), "--output-dir", str(tmp_path / "out"),
            *[str(resume) if part == "RESUME" else part for part in command], "--policy-run"]
    rc = cli.main(argv)
    err = capsys.readouterr().err
    assert rc == 4
    assert err.strip() == "Refused: autopilot disabled (enable it from Telegram)"
    assert browser.clicked == [] and browser.uploads == []
    with closing(sqlite3.connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='applications'").fetchone()[0] == 0 \
            or conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0


@pytest.mark.parametrize("command", ["upload", "submit", "verify-email", "credential"])
def test_policy_run_and_approved_are_mutually_exclusive(command):
    extra = {"upload": ["--selector", "s", "--file", "f"], "submit": ["--selector", "s"], "verify-email": [],
             "credential": ["--ats-key", "h", "--email-selector", "e", "--password-selector", "p"]}[command]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([command, "--job-id", "j", "--page-url", URL, *extra,
                                       "--approved", "--policy-run"])


def test_missing_gate_module_is_a_refusal(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_gate(name, *args, **kwargs):
        if name.startswith("jobhunter_autopilot"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_gate)
    with pytest.raises(GateRefused, match="not installed"):
        engine_module._default_gate_authorizer("db", JOB_ID, "inspect", page_url=URL)


# --------------------------------------------------------------------------- submit

def test_rehearsal_gate_refuses_submit_without_click_and_keeps_evidence(tmp_path):
    db = _db(tmp_path, stage="resume_uploaded")
    browser = Browser()
    gate = Gate(refuse={"submit": "rehearsal mode: final submit is not allowed"}, mode="rehearsal")
    engine = _engine(tmp_path, db, browser, gate)
    with pytest.raises(GateRefused, match="rehearsal"):
        engine.click_submit(JOB_ID, "button[type=submit]")
    assert browser.clicked == []
    assert browser.shots and browser.shots[0].endswith("submit_refused.png")
    assert _row(db)[0] == "resume_uploaded"


@pytest.mark.parametrize("stage,submitted_at", [("submission_attempted", None), ("submitted", None),
                                                ("draft_inspected", "2026-10-14T10:00:00+00:00")])
def test_policy_submit_never_resubmits(tmp_path, stage, submitted_at):
    db = _db(tmp_path, stage=stage, submitted_at=submitted_at)
    browser, gate = Browser(), Gate()
    with pytest.raises(GateRefused, match="never resubmitted"):
        _engine(tmp_path, db, browser, gate).click_submit(JOB_ID, "button[type=submit]")
    assert browser.clicked == [] and gate.calls == []


def test_live_policy_submit_clicks_authorizes_email_and_writes_receipt(tmp_path, monkeypatch, quiet):
    db = _db(tmp_path, stage="resume_uploaded")
    browser = Browser(after_click=CONFIRMATION)
    seen = {}

    def email_step(client, **kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(engine_module, "complete_email_verification", email_step)
    gate = Gate()
    engine = _engine(tmp_path, db, browser, gate)
    engine.click_submit(JOB_ID, "button[type=submit]")
    assert len(browser.clicked) == 1
    assert seen["approved"] is True
    assert gate.calls == [(JOB_ID, "submit", URL)]
    stage, notes, _, _ = _row(db)
    assert stage == "submission_attempted" and f"autopilot {RUN_ID}" in notes
    quiet.assert_not_called()
    receipt = json.loads((tmp_path / "out" / JOB_ID / "evidence" / "receipt.json").read_text())
    assert receipt["verified"] is True and receipt["company_match"] is True
    assert engine.last_receipt == receipt


def test_policy_submit_email_pending_exits_3(tmp_path, monkeypatch, capsys):
    from jobhunter_auto_apply.email_verification import EmailVerificationError

    db = _db(tmp_path, stage="resume_uploaded")
    browser = Browser()
    monkeypatch.setattr(engine_module, "connect_first_page", lambda *a, **k: browser)
    monkeypatch.setattr(engine_module, "_default_gate_authorizer", Gate())

    def pending(client, **kwargs):
        raise EmailVerificationError("Email verification is pending")

    monkeypatch.setattr(engine_module, "complete_email_verification", pending)
    rc = cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "submit", "--job-id", JOB_ID,
                   "--page-url", URL, "--selector", "button[type=submit]", "--policy-run"])
    assert rc == 3 and "Pending" in capsys.readouterr().err
    assert _row(db)[0] == "submission_attempted"


def test_policy_verify_email_uses_gate_instead_of_approval(tmp_path, monkeypatch):
    db = _db(tmp_path, stage="draft_inspected")
    gate = Gate()
    engine = _engine(tmp_path, db, Browser(), gate)
    with pytest.raises(PermissionError, match="No verified pending"):
        engine.verify_email(JOB_ID)
    assert gate.calls == [(JOB_ID, "verify-email", URL)]


# --------------------------------------------------------------------------- upload

def test_policy_upload_requires_package_directory_and_gate(tmp_path):
    db = _db(tmp_path)
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"x")
    browser, gate = Browser(), Gate()
    engine = _engine(tmp_path, db, browser, gate)
    with pytest.raises(PermissionError, match="package output directory"):
        engine.upload_file(JOB_ID, "#resume", str(outside))
    assert gate.calls == [] and browser.uploads == []
    inside = tmp_path / "out" / JOB_ID / "resume.pdf"
    inside.parent.mkdir(parents=True)
    inside.write_bytes(b"%PDF")
    engine.upload_file(JOB_ID, "#resume", str(inside))
    assert browser.uploads == [("#resume", str(inside.resolve()))]
    assert gate.calls == [(JOB_ID, "upload", URL)]
    assert f"autopilot {RUN_ID}" in _row(db)[1]


# --------------------------------------------------------------------------- Spanish form

def test_spanish_form_with_location_suffix_is_verified(tmp_path):
    db = _db(tmp_path)
    page = _form(heading="Senior Backend Engineer – Madrid (Híbrido)", button="Enviar candidatura",
                 text="Senior Backend Engineer\nPretensiones salariales")
    _engine(tmp_path, db, Browser(page), Gate()).inspect(JOB_ID)
    assert _row(db)[0] == "draft_inspected"


# --------------------------------------------------------------------------- verify-receipt CLI

@pytest.mark.parametrize("page,code", [(CONFIRMATION, 0), (_form(), 3)])
def test_cli_verify_receipt_exit_codes(tmp_path, monkeypatch, capsys, page, code):
    db = _db(tmp_path)
    browser = Browser(page)
    monkeypatch.setattr(receipt_module, "connect_receipt_page", lambda host, port, url: browser)
    out = tmp_path / "custom" / "receipt.json"
    rc = cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "--cdp-port", "9333",
                   "verify-receipt", "--job-id", JOB_ID, "--page-url", URL, "--out", str(out)])
    assert rc == code
    stored = json.loads(out.read_text())
    assert stored == json.loads(capsys.readouterr().out)
    assert stored["schema"] == "jobhunter.receipt/1" and stored["verified"] is (code == 0)
    assert stored["evidence_path"].endswith(f"{JOB_ID}/evidence/receipt.png") and stored["sha256"]


def test_cli_verify_receipt_default_path(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(receipt_module, "connect_receipt_page", lambda *a: Browser(CONFIRMATION))
    assert cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "verify-receipt",
                     "--job-id", JOB_ID, "--page-url", URL]) == 0
    assert (tmp_path / "out" / JOB_ID / "evidence" / "receipt.json").is_file()


def test_help_documents_pi_port_and_duplicate_tabs():
    text = cli.build_parser().format_help()
    assert "9333" in text and "two tabs share" in text
