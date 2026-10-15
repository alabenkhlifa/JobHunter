import hashlib
import json

import pytest

from jobhunter_auto_apply import receipt as module
from jobhunter_auto_apply.cdp import CDPError, CDPTarget
from jobhunter_auto_apply.engine import PageInspection


JOB = {"id": "job-1", "title": "Senior Backend Engineer", "company": "Acme Labs S.L.", "location": "Madrid, Spain"}


class PageClient:
    def __init__(self, payload):
        self.payload = payload
        self.shots = []

    def evaluate(self, expression, **kwargs):
        if expression.strip() == "location.href":
            return self.payload["url"]
        return self.payload

    def screenshot(self, path):
        with open(path, "wb") as stream:
            stream.write(b"png-bytes")
        self.shots.append(path)
        return path


def _page(text, *, headings=(), title="Careers", url="https://jobs.example.test/acme/confirmation"):
    return {"url": url, "title": title, "text": text, "headings": list(headings), "inputs": [], "buttons": []}


@pytest.mark.parametrize("text", [
    "Thank you for applying to Senior Backend Engineer at Acme Labs!",
    "Thanks for your application to Acme Labs",
    "Your application has been received. Senior Backend Engineer",
    "Application submitted — Acme Labs",
    "¡Gracias por tu candidatura! Acme Labs revisará tu perfil.",
    "Solicitud enviada: Senior Backend Engineer",
    "Hemos recibido tu candidatura para Senior Backend Engineer",
    "Tu candidatura se ha enviado correctamente a Acme Labs",
])
def test_markers_with_job_or_company_verify(tmp_path, text):
    client = PageClient(_page(text))
    shot = tmp_path / "evidence" / "receipt.png"
    receipt = module.verify_receipt(client, JOB, screenshot_path=shot, page_url="https://jobs.example.test/acme/apply")
    assert receipt["verified"] is True, receipt
    assert receipt["schema"] == "jobhunter.receipt/1"
    assert receipt["markers"]
    assert receipt["page_url"] == "https://jobs.example.test/acme/apply"
    assert receipt["final_url"] == "https://jobs.example.test/acme/confirmation"
    assert receipt["evidence_path"] == str(shot)
    assert receipt["sha256"] == hashlib.sha256(b"png-bytes").hexdigest()
    assert set(receipt) == {"schema", "job_id", "page_url", "final_url", "checked_at", "verified", "markers",
                            "title_match", "company_match", "evidence_path", "sha256"}


@pytest.mark.parametrize("text", [
    "Thank you for applying!",                                  # marker, no job/company
    "Senior Backend Engineer at Acme Labs — Apply now",          # job, no marker
    "Thank you for applying to Staff Data Scientist at Other Co",  # other job
    "Gracias por tu candidatura a Acme Labs. Este campo es obligatorio",  # still erroring
])
def test_unverified_receipts(tmp_path, text):
    receipt = module.verify_receipt(PageClient(_page(text)), JOB, screenshot_path=tmp_path / "r.png")
    assert receipt["verified"] is False


def test_heading_title_match_and_inspection_input(tmp_path):
    shot = tmp_path / "r.png"
    shot.write_bytes(b"x")
    page = PageInspection("https://jobs.ashbyhq.com/acmelabs/1/application", "Acme", "Application submitted",
                          headings=["Senior Backend Engineer – Madrid (Hybrid)"])
    receipt = module.verify_receipt(page, JOB, screenshot_path=shot)
    assert receipt["verified"] and receipt["title_match"] and receipt["company_match"]
    assert receipt["sha256"] == hashlib.sha256(b"x").hexdigest()


def test_missing_screenshot_has_no_hash(tmp_path):
    page = PageInspection("https://x.test/done", "Acme Labs", "Application submitted")
    receipt = module.verify_receipt(page, JOB, screenshot_path=tmp_path / "missing.png")
    assert receipt["evidence_path"] is None and receipt["sha256"] is None and receipt["verified"]


def test_write_and_read_receipt(tmp_path):
    receipt = module.verify_receipt(PageInspection("https://x.test", "", "Solicitud enviada Acme Labs"), JOB,
                                    screenshot_path=None)
    path = module.write_receipt(receipt, tmp_path / "a" / "receipt.json")
    assert module.read_receipt(path) == receipt
    (tmp_path / "bad.json").write_text(json.dumps({"schema": "other"}))
    with pytest.raises(ValueError):
        module.read_receipt(tmp_path / "bad.json")


def test_connect_receipt_page_follows_navigation_on_same_host(monkeypatch):
    targets = [CDPTarget("a", "", "https://jobs.example.test/acme/confirmation", "page", "ws://h/a"),
               CDPTarget("b", "", "https://other.test/", "page", "ws://h/b")]
    monkeypatch.setattr(module, "list_targets", lambda *args: targets)
    monkeypatch.setattr(module, "CDPClient", lambda url, timeout: url)
    assert module.connect_receipt_page("127.0.0.1", 9333, "https://jobs.example.test/acme/apply") == "ws://h/a"
    targets.append(CDPTarget("c", "", "https://jobs.example.test/acme/other", "page", "ws://h/c"))
    with pytest.raises(CDPError, match="no unique tab"):
        module.connect_receipt_page("127.0.0.1", 9333, "https://jobs.example.test/acme/apply")
    targets[:] = [CDPTarget(i, "", "https://jobs.example.test/acme/apply", "page", f"ws://h/{i}") for i in "xy"]
    with pytest.raises(CDPError, match="multiple tabs"):
        module.connect_receipt_page("127.0.0.1", 9333, "https://jobs.example.test/acme/apply")
