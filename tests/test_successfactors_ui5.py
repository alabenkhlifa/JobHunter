"""Approval, upload ownership, continuation and uncertain-submit guards."""
import sqlite3
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import AutoApplyEngine, ApplyConfig, _inspection_from_data, GateRefused
from jobhunter_auto_apply.cdp import BrowserTimeoutError, CDPError
from jobhunter_auto_apply.shadow import element_expression

URL = "https://career55.sapsf.eu/portalcareer"


def page(step="Review", missing=False):
    return _inspection_from_data({
        "url": URL, "title": "Product Architect", "text": "Product Architect",
        "application_context": {"kind": "successfactors_ui5", "tenant": "denodosl",
            "requisition": "2521", "role": "Product Architect", "brand_host": "jobs.denodo.com",
            "form": "careerform", "step": step, "actionable": True},
        "inputs": ([{"type": "text", "label": "Required", "required": True, "value_present": False}]
                   if missing else []),
        "buttons": [{"text": "Submit Application", "selector": "#submit", "disabled": False},
                    {"text": "Next", "selector": "#next", "disabled": False}],
    }, ())


def engine(tmp_path):
    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY,title TEXT,company TEXT)")
        conn.execute("INSERT INTO jobs VALUES('fixture','Product Architect','Denodo')")
    client = Mock()
    client.timeout = 1
    client.evaluate.return_value = URL
    app = AutoApplyEngine(ApplyConfig(db_path=str(db), expected_page_url=URL,
                                     evidence_enabled=False, tracker_sync=False), client=client)
    app._verify_before_mutation = Mock(return_value=page())
    return app, client


@pytest.mark.parametrize("change", ["approval", "wrong_selector", "not_review", "missing", "changed_url"])
def test_submit_refuses_unapproved_incomplete_or_wrong_step(tmp_path, change):
    app, client = engine(tmp_path)
    if change == "not_review": app._verify_before_mutation.return_value = page("Profile Information")
    if change == "missing": app._verify_before_mutation.return_value = page(missing=True)
    if change == "changed_url": client.evaluate.return_value = URL + "?changed"
    with pytest.raises((PermissionError, CDPError)):
        app.click_submit("fixture", "#next" if change == "wrong_selector" else "#submit",
                         approved=change != "approval")
    client.click.assert_not_called()


def test_uncertain_ui5_submit_is_recorded_before_click_and_never_repeated(tmp_path):
    app, client = engine(tmp_path)
    def click(selector):
        with sqlite3.connect(app.config.db_path) as conn:
            assert conn.execute("SELECT stage FROM applications").fetchone()[0] == "submission_attempted"
        raise BrowserTimeoutError("Synthetic unknown outcome")
    client.click.side_effect = click
    with pytest.raises(BrowserTimeoutError):
        app.click_submit("fixture", "#submit", approved=True)
    with pytest.raises(GateRefused, match="never resubmitted"):
        app.click_submit("fixture", "#submit", approved=True)
    assert client.click.call_count == 1


def test_ui5_upload_must_be_owned_by_the_verified_step(tmp_path):
    app, client = engine(tmp_path)
    app._verify_before_mutation.return_value = page("Getting Started *")
    file = tmp_path / "synthetic.txt"
    file.write_text("Synthetic fixture")
    with pytest.raises(CDPError, match="inspected form"):
        app.upload_file("fixture", "#unrelated-file", str(file), approved=True)
    client.upload_file.assert_not_called()


def test_ui5_advance_reinspects_a_new_step_at_the_same_url(tmp_path, monkeypatch):
    import jobhunter_auto_apply.engine as module
    app, client = engine(tmp_path)
    app._verify_before_mutation.return_value = page("Getting Started *")
    next_page = page("Profile Information")
    monkeypatch.setattr(module, "inspect_page", lambda *a, **k: next_page)
    app.inspect = Mock(return_value=next_page)
    assert app.advance("fixture", "#next", approved=True) is next_page
    client.click.assert_called_once_with("#next")


def test_ui5_advance_cannot_click_a_final_submit_control(tmp_path):
    app, client = engine(tmp_path)
    with pytest.raises(PermissionError, match="final submission"):
        app.advance("fixture", "#submit", approved=True)
    client.click.assert_not_called()


def test_ui5_form_support_does_not_enable_unattended_mutation(tmp_path, monkeypatch):
    import jobhunter_auto_apply.engine as module
    app, client = engine(tmp_path)
    app.config.policy_run = True
    monkeypatch.setattr(module, "inspect_page", lambda *a, **k: page())
    with pytest.raises(GateRefused, match="owner-driven"):
        AutoApplyEngine._verify_before_mutation(app, "fixture", client)


def test_shadow_path_rejects_empty_segments():
    with pytest.raises(ValueError):
        element_expression("#host >>> ")
