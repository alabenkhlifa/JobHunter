from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_service.applications import ApplicationService, PURPOSE, _selector, run_worker, worker
from jobhunter_service.dispatch import ApplicationTelegramHandler
from jobhunter_service.service import JobHunterService


def configure_cover_planner(monkeypatch, facade, *, review_flags=()):
    import jobhunter_interest_flow as flow
    context = {"job_title": "Role for 11", "public_evidence": [
        {"id": "fact-1", "text": "Built APIs"}, {"id": "fact-2", "text": "Improved delivery"}]}
    draft = {"paragraphs": ["First paragraph.", "Second paragraph.", "Third paragraph."],
             "evidence_ids": ["fact-1", "fact-2"], "review_flags": list(review_flags)}
    revised = {**draft, "paragraphs": ["Revised first paragraph.", "Revised second paragraph.",
                                       "Revised third paragraph."]}
    review = {"verdict": "ready", "criticisms": ["The first draft repeated a generic opening."],
              "blocking_evidence_issues": [], "revised_draft": revised}
    monkeypatch.setattr(flow, "cover_letter_draft_context", lambda profile, job: context)
    def validate(value, evidence):
        if value not in (draft, revised) or evidence != context:
            raise ValueError("invalid draft")
        return value
    monkeypatch.setattr(flow, "validate_cover_letter_draft", validate)
    facade.planner = Mock(side_effect=[draft, review])
    return context, draft, revised, review


@pytest.fixture
def app(tmp_path):
    service = JobHunterService(tmp_path / "data", 1)
    roots = {}
    for actor in (11, 22):
        service.admin(1, "add", actor)
        service.authorize(actor)
        root = service.profile_dir(service._member(actor))
        roots[actor] = root
        with closing(sqlite3.connect(root / "jobs.db")) as db, db:
            db.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,company TEXT,location TEXT,url TEXT,description TEXT,status TEXT)")
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", ("same-job", f"Role for {actor}", f"Company {actor}", "Remote",
                                                                 f"https://jobs.example.com/{actor}", f"Description {actor}", "new"))
        (root / "master-profile.json").write_text("{}")
    pages = {actor: {"url": f"https://jobs.example.com/{actor}", "title": f"Apply for {actor}", "target_id": f"tab-{actor}",
                     "blockers": [], "sensitive_questions": [], "missing_required": False,
                     "upload_selector": '[id="resume"]', "submit_selector": '[id="submit"]'} for actor in roots}
    browser, telegram = Mock(), Mock()
    browser.status.side_effect = lambda profile: {"profile_id": profile, "status": "running", "cdp_port": 9500 + int(profile[1:])}
    facade = ApplicationService(service, browser, telegram)

    def fake_worker(root, request):
        actor = int(root.name[1:])
        if request["operation"] == "observe":
            return dict(pages[actor])
        if request["operation"] == "prepare":
            import scraper
            package = root / "output" / "same-job-package"
            package.mkdir(exist_ok=True)
            names = ("Resume.pdf", "CoverLetter.pdf") if request.get("include_cover_letter") else ("Resume.pdf",)
            for name in names:
                (package / name).write_bytes(b"%PDF-1.7\nprivate candidate document\n")
            (package / "tailoring_manifest.json").write_text(json.dumps({"job_id": "same-job", "profile_sha256": hashlib.sha256(b"{}").hexdigest()}))
            with closing(sqlite3.connect(root / "jobs.db")) as db, db:
                scraper.record_application_stage(db, "same-job", "package_generated", package_path=str(package), sync=False)
            return {"resume_pdf": str(package / "Resume.pdf"),
                    "cover_pdf": str(package / "CoverLetter.pdf") if request.get("include_cover_letter") else None}
        return {"status": "completed"}

    facade._run = Mock(side_effect=fake_worker)
    return facade, service, roots, pages, browser, telegram


def test_same_job_id_resolves_only_the_actors_database_and_private_chat(app):
    facade, _, _, _, _, telegram = app
    assert facade.details(11, "same-job")["company"] == "Company 11"
    assert facade.details(22, "same-job")["company"] == "Company 22"
    assert [call.args[0] for call in telegram.send_message.call_args_list] == [11, 22]
    assert "Company 22" not in telegram.send_message.call_args_list[0].args[1]


@pytest.mark.parametrize("operation", ["details", "interested", "prepare", "prepare_with_cover", "inspect", "propose_upload", "propose_submit"])
def test_unknown_jobs_fail_without_worker_browser_or_messages(app, operation):
    facade, _, roots, _, browser, telegram = app
    before = sorted(str(path.relative_to(roots[11])) for path in roots[11].rglob("*"))
    with pytest.raises(ValueError, match="not in"):
        getattr(facade, operation)(11, "unknown-job")
    facade._run.assert_not_called()
    browser.status.assert_not_called()
    telegram.send_message.assert_not_called()
    assert sorted(str(path.relative_to(roots[11])) for path in roots[11].rglob("*")) == before


def test_prepare_sends_only_own_documents_to_own_private_chat(app):
    facade, _, roots, _, _, telegram = app
    assert facade.prepare(11, "same-job")["status"] == "package_generated"
    assert len(telegram.send_document.call_args_list) == 1
    assert facade._run.call_args.args[1]["include_cover_letter"] is False
    assert not (roots[11] / "output/same-job-package/CoverLetter.pdf").exists()
    for call in telegram.send_document.call_args_list:
        assert call.args[0] == 11
        assert call.args[1].is_relative_to(roots[11] / "output")
    assert not list((roots[22] / "output").iterdir())


def test_explicit_cover_request_sends_both_documents(app, monkeypatch):
    facade, _, roots, _, _, telegram = app
    context, draft, revised, _ = configure_cover_planner(monkeypatch, facade)
    assert facade.prepare_with_cover(11, "same-job")["status"] == "package_generated"
    assert facade._run.call_args.args[1]["include_cover_letter"] is True
    assert facade._run.call_args.args[1]["cover_letter_draft"] == revised
    assert facade.planner.call_count == 2
    messages, schema = facade.planner.call_args_list[0].args
    assert messages[0]["role"] == "system" and messages[1]["role"] == "user"
    assert json.dumps(context, sort_keys=True) in messages[1]["content"]
    assert schema["type"] == "object"
    review_messages, review_schema = facade.planner.call_args_list[1].args
    assert review_messages[0]["role"] == "system"
    assert len(review_messages) == 2
    assert json.loads(review_messages[1]["content"].split("\n", 1)[1]) == {"context": context, "draft": draft}
    assert review_schema["properties"]["verdict"]["enum"] == ["ready", "blocked"]
    assert [call.args[1].name for call in telegram.send_document.call_args_list] == ["Resume.pdf", "CoverLetter.pdf"]
    assert all(call.args[1].is_relative_to(roots[11] / "output") for call in telegram.send_document.call_args_list)
    assert "resume and cover letter" in telegram.send_message.call_args.args[1]


def test_cover_fit_flags_reach_candidate_before_application_steps(app, monkeypatch):
    facade, _, _, _, _, telegram = app
    configure_cover_planner(
        monkeypatch, facade,
        review_flags=("Seven-person leadership is requested but only four-person coordination is confirmed.",),
    )

    facade.prepare_with_cover(11, "same-job")

    message = telegram.send_message.call_args.args[1]
    assert "Cover-letter fit points to review:" in message
    assert "Seven-person leadership is requested" in message
    assert message.index("fit points") < message.index("/inspect")


def test_explicit_cover_request_rejects_missing_cover_before_delivery(app, monkeypatch):
    facade, _, roots, _, _, telegram = app
    configure_cover_planner(monkeypatch, facade)
    facade._run.return_value = {"resume_pdf": str(roots[11] / "output/Resume.pdf"), "cover_pdf": None}
    facade._run.side_effect = None
    with pytest.raises(ValueError, match="incomplete"):
        facade.prepare_with_cover(11, "same-job")
    telegram.send_document.assert_not_called()


def test_cover_request_without_planner_does_not_generate_or_deliver(app):
    facade, _, _, _, _, telegram = app
    with pytest.raises(ValueError, match="drafting is unavailable"):
        facade.prepare_with_cover(11, "same-job")
    facade._run.assert_not_called()
    telegram.send_document.assert_not_called()


@pytest.mark.parametrize("failure", ["model", "invalid_draft", "readiness_rejection"])
def test_cover_request_fails_closed_when_model_or_draft_is_invalid(app, monkeypatch, failure):
    facade, _, _, _, _, telegram = app
    _, draft, _, _ = configure_cover_planner(monkeypatch, facade)
    if failure == "model":
        facade.planner.side_effect = RuntimeError("private model details")
    elif failure == "readiness_rejection":
        from jobhunter_interest_flow import TailoringReadinessError
        monkeypatch.setattr("jobhunter_interest_flow.validate_cover_letter_draft",
                            Mock(side_effect=TailoringReadinessError("unverified evidence")))
    else:
        facade.planner.side_effect = [{**draft, "paragraphs": ["generic"]}]
    with pytest.raises(ValueError, match="drafting is unavailable|did not pass evidence checks") as error:
        facade.prepare_with_cover(11, "same-job")
    assert "private model details" not in str(error.value)
    facade._run.assert_not_called()
    telegram.send_document.assert_not_called()


@pytest.mark.parametrize("review_change", [
    {"verdict": "blocked", "blocking_evidence_issues": ["Outcome lacks public evidence."]},
    {"verdict": "ready", "blocking_evidence_issues": ["Outcome lacks public evidence."]},
    {"verdict": "ready", "criticisms": []},
    {"verdict": "ready", "revised_draft": {"paragraphs": ["Generic."]}},
    {"verdict": "ready", "revised_draft": "not a draft"},
    {"verdict": "ready", "unexpected": True},
])
def test_cover_critic_rejection_or_malformed_response_fails_closed(app, monkeypatch, review_change):
    facade, _, _, _, _, telegram = app
    _, draft, _, review = configure_cover_planner(monkeypatch, facade)
    facade.planner.side_effect = [draft, {**review, **review_change}]

    with pytest.raises(ValueError, match="review did not approve"):
        facade.prepare_with_cover(11, "same-job")

    facade._run.assert_not_called()
    telegram.send_document.assert_not_called()


def test_cover_critic_model_failure_fails_closed(app, monkeypatch):
    facade, _, _, _, _, telegram = app
    _, draft, _, _ = configure_cover_planner(monkeypatch, facade)
    facade.planner.side_effect = [draft, RuntimeError("private model details")]

    with pytest.raises(ValueError, match="review is unavailable") as error:
        facade.prepare_with_cover(11, "same-job")

    assert "private model details" not in str(error.value)
    facade._run.assert_not_called()
    telegram.send_document.assert_not_called()


def test_candidate_cover_command_routes_only_to_explicit_cover_generation(app):
    facade, service, _, _, _, telegram = app
    handler = ApplicationTelegramHandler(service, telegram)
    handler.applications = Mock()
    handler._handle_private(11, {"text": "/apply same-job"}, None)
    handler._handle_private(11, {"text": "/cover same-job"}, None)
    handler.applications.prepare.assert_called_once_with(11, "same-job")
    handler.applications.prepare_with_cover.assert_called_once_with(11, "same-job")


def test_candidate_cover_route_receives_restricted_planner(app):
    _, service, _, _, _, telegram = app
    planner = Mock()
    handler = ApplicationTelegramHandler(service, telegram, assistant=Mock(planner=planner))
    assert handler.applications.planner is planner


@pytest.mark.parametrize("stage", ["submitted", "submission_attempted"])
def test_worker_does_not_replace_terminal_or_uncertain_application_package(app, monkeypatch, stage):
    facade, _, roots, _, _, _ = app
    import scraper
    with closing(sqlite3.connect(roots[11] / "jobs.db")) as db, db:
        scraper.record_application_stage(db, "same-job", stage, sync=False)
    generate = Mock()
    monkeypatch.setattr("jobhunter_interest_flow.prepare_application_package", generate)
    with pytest.raises(ValueError, match="submission|attempt"):
        worker({"root": str(roots[11]), "job_id": "same-job", "operation": "prepare",
                "include_cover_letter": True})
    generate.assert_not_called()


def test_worker_passes_reviewed_cover_draft_to_package_generator(app, monkeypatch):
    facade, _, roots, _, _, _ = app
    generated = Mock(return_value=Mock(resume_pdf=roots[11] / "output/Resume.pdf",
                                       cover_pdf=roots[11] / "output/CoverLetter.pdf"))
    monkeypatch.setattr("jobhunter_interest_flow.prepare_application_package", generated)
    draft = {"paragraphs": ["One", "Two", "Three"], "evidence_ids": ["fact-1"], "review_flags": []}
    result = worker({"root": str(roots[11]), "job_id": "same-job", "operation": "prepare",
                     "include_cover_letter": True, "cover_letter_draft": draft})
    assert result["cover_pdf"].endswith("CoverLetter.pdf")
    assert generated.call_args.kwargs["cover_letter_draft"] == draft
    assert generated.call_args.kwargs["include_cover_letter"] is True


def test_worker_rejects_cover_without_reviewed_draft(app, monkeypatch):
    facade, _, roots, _, _, _ = app
    generate = Mock()
    monkeypatch.setattr("jobhunter_interest_flow.prepare_application_package", generate)
    with pytest.raises(ValueError, match="reviewed cover-letter draft"):
        worker({"root": str(roots[11]), "job_id": "same-job", "operation": "prepare",
                "include_cover_letter": True})
    generate.assert_not_called()


def test_preparing_an_updated_package_invalidates_old_application_approval(app, monkeypatch):
    facade, service, *_ = app
    configure_cover_planner(monkeypatch, facade)
    token = ready_upload(app)
    facade.prepare_with_cover(11, "same-job")
    with pytest.raises(PermissionError):
        service.store.read_token(token, PURPOSE, consume=False)


def test_failed_render_does_not_announce_or_record_package(app):
    facade, _, roots, _, _, telegram = app
    facade._run.side_effect = ValueError("PDF rendering failed")
    with pytest.raises(ValueError, match="PDF rendering"):
        facade.prepare(11, "same-job")
    telegram.send_document.assert_not_called()
    telegram.send_message.assert_not_called()
    with closing(sqlite3.connect(roots[11] / "jobs.db")) as db, db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='applications'").fetchone()[0] == 0


def test_foreign_generated_document_path_is_rejected(app):
    facade, _, roots, _, _, telegram = app
    foreign = roots[22] / "output" / "Resume.pdf"
    foreign.write_bytes(b"private other candidate")
    facade._run.return_value = {"resume_pdf": str(foreign), "cover_pdf": str(foreign)}
    facade._run.side_effect = None
    with pytest.raises(PermissionError, match="belong"):
        facade.prepare(11, "same-job")
    telegram.send_document.assert_not_called()


def ready_upload(app):
    facade, *_ = app
    facade.prepare(11, "same-job")
    facade.inspect(11, "same-job")
    return facade.propose_upload(11, "same-job")["token"]


def test_confirmation_is_actor_bound_one_use_and_exact_document(app):
    facade, service, _, _, _, telegram = app
    token = ready_upload(app)
    with pytest.raises(PermissionError, match="another user"):
        facade.execute(22, token)
    assert service.store.read_token(token, PURPOSE, consume=False)[0] == 11
    facade.execute(11, token)
    request = facade._run.call_args.args[1]
    assert request["operation"] == "upload" and request["approved"] is True
    assert request["document_sha256"] and request["url"] == "https://jobs.example.com/11"
    assert telegram.send_message.call_args.args[0] == 11
    with pytest.raises(PermissionError):
        facade.execute(11, token)


def test_approved_action_serializes_revocation_only_for_its_own_actor(app):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    facade, service, *_ = app
    token = ready_upload(app)
    entered, release, revoke_started = Event(), Event(), Event()
    original = facade._run.side_effect

    def blocked_worker(root, request):
        if request["operation"] == "upload":
            entered.set()
            assert release.wait(3), "The test did not release the application worker."
        return original(root, request)

    def revoke_actor():
        revoke_started.set()
        return service.admin(1, "revoke", 11)

    facade._run.side_effect = blocked_worker
    with ThreadPoolExecutor(max_workers=3) as pool:
        action = pool.submit(facade.execute, 11, token)
        try:
            assert entered.wait(3)
            revoke = pool.submit(revoke_actor)
            assert revoke_started.wait(3)
            # Another candidate remains independently configurable while this
            # actor's approved external action is protected against revocation.
            pool.submit(service.admin, 1, "revoke", 22).result(timeout=3)
            assert not revoke.done()
            assert service._member(11)["status"] == "active"
        finally:
            release.set()
        assert action.result(timeout=3)["status"] == "resume_uploaded"
        revoke.result(timeout=3)
    with pytest.raises(PermissionError):
        service._member(11)


@pytest.mark.parametrize("change", ["profile", "page", "browser", "document", "expired", "revoked"])
def test_changed_or_stale_context_cannot_execute_approval(app, change):
    facade, service, roots, pages, browser, _ = app
    token = ready_upload(app)
    if change == "profile":
        with service.store.connect() as db:
            db.execute("UPDATE members SET revision=revision+1 WHERE user_id=11")
    elif change == "page":
        pages[11]["url"] = "https://jobs.example.com/different-role"
    elif change == "browser":
        browser.status.side_effect = lambda profile: {"profile_id": profile, "status": "running", "cdp_port": 9900}
    elif change == "document":
        (roots[11] / "output/same-job-package/Resume.pdf").write_bytes(b"changed after approval")
    elif change == "expired":
        with service.store.connect() as db:
            db.execute("UPDATE tokens SET expires_at=0")
    else:
        service.admin(1, "revoke", 11)
    facade._run.reset_mock()
    with pytest.raises((ValueError, PermissionError)):
        facade.execute(11, token)
    assert not any(call.args[1]["operation"] in {"upload", "submit"} for call in facade._run.call_args_list)


def test_submit_is_separately_approved_and_reports_attempt_not_success(app):
    facade, _, _, _, _, telegram = app
    facade.inspect(11, "same-job")
    token = facade.propose_submit(11, "same-job")["token"]
    assert not any(call.args[1]["operation"] == "submit" for call in facade._run.call_args_list)
    result = facade.execute(11, token)
    assert result["status"] == "submission_attempted"
    assert "receipt" in telegram.send_message.call_args.args[1]
    with pytest.raises(ValueError, match="unresolved"):
        facade.propose_submit(11, "same-job")


def test_sibling_proposals_are_invalidated_after_one_is_executed(app):
    facade, service, *_ = app
    first = ready_upload(app)
    second = facade.propose_upload(11, "same-job")["token"]
    facade.execute(11, first)
    with pytest.raises(PermissionError):
        service.store.read_token(second, PURPOSE, consume=False)


def test_stale_prepared_profile_cannot_be_uploaded(app):
    facade, _, roots, *_ = app
    facade.prepare(11, "same-job")
    (roots[11] / "master-profile.json").write_text('{"name":"Updated confirmed profile"}')
    facade.inspect(11, "same-job")
    with pytest.raises(ValueError, match="older resume"):
        facade.propose_upload(11, "same-job")


def test_lost_submit_result_cannot_be_retried_blindly(app):
    facade, _, _, _, _, _ = app
    facade.inspect(11, "same-job")
    token = facade.propose_submit(11, "same-job")["token"]
    original = facade._run.side_effect
    def fail_submit(root, request):
        if request["operation"] == "submit":
            raise ValueError("Browser action outcome unknown")
        return original(root, request)
    facade._run.side_effect = fail_submit
    with pytest.raises(ValueError, match="unknown"):
        facade.execute(11, token)
    with pytest.raises(ValueError, match="unresolved"):
        facade.propose_submit(11, "same-job")


def test_page_different_from_posting_is_explicitly_shown_before_confirmation(app):
    facade, _, _, pages, _, telegram = app
    pages[11]["url"] = "https://ats.example.com/application-form"
    facade.inspect(11, "same-job")
    text = telegram.send_message.call_args.args[1]
    assert "Role for 11" in text and "differs from the collected posting" in text
    facade.propose_submit(11, "same-job")
    assert "https://ats.example.com/application-form" in telegram.send_message.call_args.args[1]


@pytest.mark.parametrize("blocked", ["blockers", "sensitive_questions", "missing_required"])
def test_unresolved_questions_or_verification_prevent_submit(app, blocked):
    facade, _, _, pages, _, _ = app
    facade.inspect(11, "same-job")
    pages[11][blocked] = True if blocked == "missing_required" else ["Candidate must answer"]
    with pytest.raises(ValueError):
        facade.propose_submit(11, "same-job")


def test_safe_selector_requires_one_observed_final_control():
    assert _selector([{"text": "Submit application", "id": "submit"}]) == '[id="submit"]'
    assert _selector([{"text": "Apply now", "id": "apply"}]) is None
    assert _selector([{"text": "Submit", "id": "one"}, {"text": "Submit", "id": "two"}]) is None
    assert _selector([{"type": "file", "id": "resume"}], upload=True) == '[id="resume"]'


def test_worker_rechecks_page_before_using_approved_engine(app, monkeypatch):
    facade, _, roots, pages, _, _ = app
    client = Mock()
    monkeypatch.setattr("jobhunter_service.applications._browser_page", lambda *_: (client, pages[11]))
    engine = Mock()
    monkeypatch.setattr("jobhunter_auto_apply.engine.AutoApplyEngine", engine)
    with pytest.raises(PermissionError):
        worker({"root": str(roots[11]), "job_id": "same-job", "operation": "submit", "cdp_port": 9511,
                "target_id": "tab-11", "approved": True, "url": "https://jobs.example.com/old-page", "selector": '[id="submit"]'})
    engine.assert_not_called()
    client.close.assert_called_once()


def test_approved_worker_upload_uses_container_document_path_and_candidate_db(app, monkeypatch):
    from jobhunter_auto_apply.engine import PageInspection
    facade, service, roots, pages, _, _ = app
    token = ready_upload(app)
    _, payload = service.store.read_token(token, PURPOSE, consume=False)
    client = Mock()
    client.evaluate.return_value = pages[11]["url"]
    monkeypatch.setattr("jobhunter_service.applications._browser_page", lambda *_: (client, pages[11]))
    monkeypatch.setattr("jobhunter_auto_apply.engine.time.sleep", lambda _: None)
    monkeypatch.setattr("jobhunter_auto_apply.engine.AutoApplyEngine.inspect", Mock(return_value=PageInspection(pages[11]["url"], "Apply", "")))
    worker({**payload, "root": str(roots[11]), "approved": True})
    client.upload_file.assert_called_once_with('[id="resume"]', "/documents/same-job-package/Resume.pdf")
    client.close.assert_called_once()
    with closing(sqlite3.connect(roots[11] / "jobs.db")) as db, db:
        assert db.execute("SELECT stage FROM applications").fetchone()[0] == "resume_uploaded"
    with closing(sqlite3.connect(roots[22] / "jobs.db")) as db, db:
        assert db.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='applications'").fetchone()[0] == 0


def test_real_interested_subprocess_cannot_run_owner_tracker_command(app, monkeypatch, tmp_path):
    _, _, roots, _, _, _ = app
    marker = tmp_path / "owner-tracker-ran"
    monkeypatch.setenv("JOBHUNTER_AUTO_SYNC_TRACKER", "true")
    monkeypatch.setenv("JOBHUNTER_TRACKER_SYNC_COMMAND", f"touch {marker}")
    result = run_worker(roots[11], {"operation": "interested", "job_id": "same-job"})
    assert result["status"] == "interested" and not marker.exists()
    with closing(sqlite3.connect(roots[11] / "jobs.db")) as db, db:
        assert db.execute("SELECT stage FROM applications").fetchone()[0] == "interested"
        assert db.execute("SELECT COUNT(*) FROM job_feedback").fetchone()[0] == 1
    with closing(sqlite3.connect(roots[22] / "jobs.db")) as db, db:
        assert db.execute("SELECT status FROM jobs").fetchone()[0] == "new"


@pytest.mark.parametrize("advertised", ["ws://localhost:9222/devtools/page/tab-11",
                                       "ws://127.0.0.1:9223/devtools/page/tab-11",
                                       "ws://127.0.0.1:49511/devtools/page/tab-11"])
def test_container_cdp_discovery_is_remapped_only_to_broker_port(monkeypatch, advertised):
    from jobhunter_auto_apply.cdp import CDPTarget
    from jobhunter_auto_apply.engine import PageInspection
    from jobhunter_service.applications import _browser_page
    target = CDPTarget("tab-11", "Apply", "https://jobs.example.com/11", "page", advertised)
    discover = Mock(return_value=[target])
    connect = Mock(return_value=Mock())
    monkeypatch.setattr("jobhunter_auto_apply.cdp.list_targets", discover)
    monkeypatch.setattr("jobhunter_auto_apply.cdp.CDPClient", connect)
    monkeypatch.setattr("jobhunter_auto_apply.engine.inspect_page", Mock(return_value=PageInspection(target.url, target.title, "")))
    _, page = _browser_page(49511, "tab-11")
    discover.assert_called_once_with("127.0.0.1", 49511)
    connect.assert_called_once_with("ws://127.0.0.1:49511/devtools/page/tab-11")
    assert page["target_id"] == "tab-11"


@pytest.mark.parametrize("advertised", ["ws://external.example:9222/devtools/page/tab-11",
                                       "ws://localhost:9222/devtools/page/another-tab",
                                       "ws://localhost:9222/devtools/page/tab-11?token=private",
                                       "ws://localhost:12345/devtools/page/tab-11",
                                       "ws://localhost:9222/devtools/page/../tab-11"])
def test_cdp_discovery_cannot_select_untrusted_hosts_ports_or_targets(monkeypatch, advertised):
    from jobhunter_auto_apply.cdp import CDPTarget
    from jobhunter_service.applications import _browser_page
    monkeypatch.setattr("jobhunter_auto_apply.cdp.list_targets", Mock(return_value=[
        CDPTarget("tab-11", "Apply", "https://jobs.example.com/11", "page", advertised)]))
    connect = Mock()
    monkeypatch.setattr("jobhunter_auto_apply.cdp.CDPClient", connect)
    with pytest.raises(PermissionError):
        _browser_page(49511, "tab-11")
    connect.assert_not_called()


def test_application_read_connections_close_without_waiting_for_garbage_collection(app, monkeypatch):
    from jobhunter_service.applications import _job
    facade, _, roots, *_ = app
    ready_upload(app)
    connections = []
    real_connect = sqlite3.connect

    class TrackedConnection(sqlite3.Connection):
        closed = False
        def close(self):
            self.closed = True
            super().close()

    def connect(*args, **kwargs):
        connection = real_connect(*args, factory=TrackedConnection, **kwargs)
        connections.append(connection)  # Keep strong references: no GC cleanup.
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    for _ in range(25):
        _job(roots[11], "same-job")
        facade._resume(roots[11], "same-job")
        facade._check_prior_submission(roots[11], "same-job")
    assert len(connections) == 75
    assert all(connection.closed for connection in connections)
