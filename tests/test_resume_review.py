import copy
import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
from http.server import ThreadingHTTPServer

import fitz
import pytest

from resume_blocks import build
from resume_review.server import Conflict, ReviewError, ReviewStore, compose, handler_for


@pytest.fixture
def source(tmp_path):
    catalog = {
        "version": 1, "enabled": True,
        "base": {"name": "Example Candidate", "email": "candidate@example.test",
                 "skills": {"Backend": ["Java", "PostgreSQL"]},
                 "additional": {"languages": "English (C1)"}},
        "flavors": [{"id": "engineer", "label": "Engineer", "confirmation": "candidate-confirmed",
                     "headline": "Software Engineer | Java", "summary": "Builds confirmed Java services."},
                    {"id": "architect", "label": "Architect", "confirmation": "draft", "headline": "", "summary": ""}],
        "projects": [{"id": "services", "label": "Service platform", "confirmation": "candidate-confirmed",
                      "company": "Example Company", "role": {"title": "Software Engineer", "dates": "2020 - Present"},
                      "entry": {"name": "Service platform", "dates": "2021 - 2024", "tech": "Java",
                                "bullets": ["Owned five **Java services**.", "Migrated eight services."]}}],
        "variants": [{"id": "one", "label": "One page", "flavor_id": "engineer", "confirmation": "candidate-confirmed",
                      "max_pages": 1, "project_ids": ["services"], "default_project_ids": ["services"]}]
    }
    path = tmp_path / "source.json"
    path.write_text(json.dumps({"resume_blocks": catalog, "private_notes": "Never copied to review state"}))
    return path


@pytest.fixture
def store(source, tmp_path):
    return ReviewStore(source, tmp_path / "review")


def save_body(state):
    return {"revision": state["revision"], "catalog": state["catalog"], "selection": state["selection"]}


def test_preview_composition_matches_production_without_promoting_drafts(store):
    state = store.read()
    expected = build({"resume_blocks": state["catalog"]}, state["selection"])
    assert compose(state["catalog"], state["selection"])[0] == expected
    state["catalog"]["flavors"][0]["confirmation"] = "draft"
    before = copy.deepcopy(state)
    assert compose(state["catalog"], state["selection"])[0] == expected
    assert state == before


def test_autosave_resumes_undoes_and_never_updates_source_or_confirms_wording(store, source):
    original = source.read_bytes()
    state = store.read()
    state["catalog"]["flavors"][0]["headline"] = "Revised title"
    saved = store.save(save_body(state))
    assert saved["catalog"]["flavors"][0]["confirmation"] == "draft"
    assert saved["catalog"]["projects"][0]["confirmation"] == "candidate-confirmed"
    assert "private_notes" not in saved
    resumed = ReviewStore(source, store.directory)
    assert resumed.read() == saved
    undone = resumed.undo({"revision": saved["revision"]})
    assert undone["catalog"] == saved["baseline"]
    assert source.read_bytes() == original


def test_confirmation_flags_cannot_be_elevated_by_a_client(store):
    state = store.read()
    state["catalog"]["flavors"][1].update(headline="Architect", summary="Draft", confirmation="candidate-confirmed")
    saved = store.save(save_body(state))
    assert saved["catalog"]["flavors"][1]["confirmation"] == "draft"


def test_stale_autosave_cannot_replace_another_tabs_changes(store):
    old = save_body(store.read())
    state = store.request({"revision": 0, "message": "Keep the scope precise", "path": None})
    with pytest.raises(Conflict):
        store.save(old)
    assert store.read() == state


def test_proposal_requires_acceptance_and_binds_to_exact_original_text(store, source):
    original = source.read_bytes()
    path = ["flavors", 0, "summary"]
    requested = store.request({"revision": 0, "path": path, "message": "Please shorten this"})
    request_id = requested["requests"][-1]["id"]
    proposed = store.propose({"revision": 1, "path": path, "after": "Builds Java services.",
                              "request_id": request_id, "rationale": "Shorter wording"})
    assert proposed["catalog"] == requested["catalog"]
    assert proposed["proposals"][-1]["status"] == "pending"
    accepted = store.decide({"revision": 2, "id": proposed["proposals"][-1]["id"], "decision": "accept"})
    assert accepted["catalog"]["flavors"][0]["summary"] == "Builds Java services."
    assert accepted["catalog"]["flavors"][0]["confirmation"] == "draft"
    assert accepted["requests"][-1]["status"] == "resolved"
    assert source.read_bytes() == original
    assert store.undo({"revision": 3})["catalog"] == requested["catalog"]


def test_stale_proposal_refuses_to_overwrite_new_user_wording(store):
    state = store.propose({"revision": 0, "path": ["flavors", 0, "summary"], "after": "Proposed text."})
    proposal_id = state["proposals"][-1]["id"]
    state["catalog"]["flavors"][0]["summary"] = "New user wording."
    saved = store.save(save_body(state))
    with pytest.raises(Conflict, match="changed after the proposal"):
        store.decide({"revision": saved["revision"], "id": proposal_id, "decision": "accept"})
    assert store.read() == saved


def test_rejecting_proposal_preserves_text_and_replayed_decision_is_refused(store):
    original = store.read()["catalog"]
    state = store.propose({"revision": 0, "path": ["projects", 0, "entry", "bullets", 0], "after": "Proposed bullet."})
    body = {"revision": 1, "id": state["proposals"][-1]["id"], "decision": "reject"}
    rejected = store.decide(body)
    assert rejected["catalog"] == original
    with pytest.raises(Conflict):
        store.decide({**body, "revision": 2})


@pytest.mark.parametrize("path", [["flavors", 0, "confirmation"], ["projects", 0, "id"],
                                 ["private_notes"], ["base", "skills"]])
def test_proposals_can_only_target_public_text(store, path):
    with pytest.raises(ReviewError):
        store.propose({"revision": 0, "path": path, "after": "Anything"})
    assert store.read()["revision"] == 0


def test_real_pdf_preview_and_approval_are_bound_to_saved_content(store, source):
    original = source.read_bytes()
    preview = store.preview()
    assert preview["pages"] == 1 and preview["audit_ok"] and not preview["warnings"]
    document = store.directory / "previews" / preview["id"] / "resume.pdf"
    with fitz.open(document) as pdf:
        text = " ".join(page.get_text() for page in pdf)
        assert "Software Engineer | Java" in text
        assert "Migrated eight services." in text
    with pytest.raises(Conflict, match="rendered version changed"):
        store.approval({"revision": 0, "content_digest": "stale"})
    approved = store.approval({"revision": 0, "content_digest": preview["content_digest"]})
    assert approved["approvals"][-1]["digest"] == preview["content_digest"]
    approved["catalog"]["flavors"][0]["summary"] = "Updated confirmed-scope wording."
    store.save(save_body(approved))
    assert store.preview()["content_digest"] != preview["content_digest"]
    assert source.read_bytes() == original


def test_overflow_preview_keeps_all_bullets_but_cannot_be_approved(store):
    state = store.read()
    bullets = [f"Built component {i} with Java, PostgreSQL and documented service integration tests across the complete application platform." for i in range(50)]
    state["catalog"]["projects"][0]["entry"]["bullets"] = bullets
    saved = store.save(save_body(state))
    preview = store.preview()
    assert preview["pages"] > 1 and preview["warnings"] and not preview["audit_ok"]
    assert "Built component 49" in preview["text"]
    with pytest.raises(ReviewError, match="warnings"):
        store.approval({"revision": saved["revision"], "content_digest": preview["content_digest"]})
    assert store.read()["approvals"] == []


def test_blank_draft_can_be_previewed_but_never_approved(store):
    state = store.read()
    state["catalog"]["variants"][0]["flavor_id"] = "architect"
    state["selection"]["flavor_id"] = "architect"
    saved = store.save(save_body(state))
    preview = store.preview()
    assert "This flavor has no summary yet." in preview["warnings"]
    assert saved["catalog"]["flavors"][1]["confirmation"] == "draft"
    with pytest.raises(ReviewError):
        store.approval({"revision": saved["revision"], "content_digest": preview["content_digest"]})


@pytest.fixture
def http_server(store):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join()
    server.server_close()


def request(server, method, path, body=None, headers=None):
    client = http.client.HTTPConnection("127.0.0.1", server.server_port)
    client.request(method, path, json.dumps(body) if body is not None else None, headers or {})
    response = client.getresponse()
    data = response.read()
    result = (response.status, response.headers, data)
    client.close()
    return result


def test_http_rejects_cross_origin_dns_rebinding_and_unmarked_mutations(http_server, store):
    status, headers, _ = request(http_server, "GET", "/api/state")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert request(http_server, "GET", "/api/state", headers={"Host": "hostile.example"})[0] == 400
    assert request(http_server, "GET", "/api/state", headers={"Origin": "https://hostile.example"})[0] == 400
    assert request(http_server, "POST", "/api/requests", {"revision": 0, "message": "Injected"})[0] == 400
    assert store.read()["revision"] == 0


def test_http_stale_revisions_and_asset_traversal(http_server):
    headers = {"Content-Type": "application/json", "X-Review-Client": "studio"}
    body = {"revision": 0, "message": "Review this summary", "path": ["flavors", 0, "summary"]}
    status, _, data = request(http_server, "POST", "/api/requests", body, headers)
    assert status == 200 and json.loads(data)["requests"][-1]["message"] == body["message"]
    assert request(http_server, "POST", "/api/requests", body, headers)[0] == 409
    assert request(http_server, "GET", "/previews/../../source.json")[0] == 404
    assert request(http_server, "GET", "/../source.json")[0] == 404


def test_failed_catalog_validation_is_atomic(store):
    original = store.read()
    changed = copy.deepcopy(original)
    changed["catalog"]["base"]["private_notes"] = "Not public"
    with pytest.raises(ValueError):
        store.save(save_body(changed))
    assert store.read() == original


@pytest.mark.skipif(os.environ.get("JOBHUNTER_BROWSER_TESTS") != "1",
                    reason="requires opt-in JOBHUNTER_BROWSER_TESTS=1, Node Playwright and Chromium")
def test_browser_skills_headings_and_reordering_preserve_open_sections(http_server, store, source):
    original = source.read_bytes()
    state = store.read()
    state["catalog"]["base"]["skills"]["Delivery"] = ["CI/CD", "Docker"]
    saved = store.save(save_body(state))
    preview = store.preview()
    approved = store.approval({"revision": saved["revision"], "content_digest": preview["content_digest"]})
    driver = Path(__file__).with_name("browser_resume_review_check.cjs")
    result = subprocess.run(["node", str(driver), f"http://127.0.0.1:{http_server.server_port}"],
                            capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    state = store.read()
    assert state["approvals"] == approved["approvals"]
    assert source.read_bytes() == original
    preview = store.preview()
    assert preview["audit_ok"] and not preview["warnings"]
    assert 'Platform & "Architecture"' in preview["text"]
    assert preview["content_digest"] != approved["approvals"][-1]["digest"]
