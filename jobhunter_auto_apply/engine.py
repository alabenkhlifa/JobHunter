"""Approval-gated auto-apply engine for JobHunter.

The engine is intentionally conservative. It can prepare and inspect application
pages, record state, and perform explicitly-approved actions. It must not guess
legal/visa/salary answers, bypass anti-bot controls, or submit without an
approval flag from the caller.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import scraper

from .cdp import CDPClient, CDPError, connect_first_page


DEFAULT_BLOCKLIST_TERMS = (
    "captcha",
    "recaptcha",
    "hcaptcha",
    "phone verification",
    "verify your phone",
    "one-time passcode",
    "identity verification",
)

SENSITIVE_QUESTION_TERMS = (
    "work authorization",
    "visa",
    "sponsorship",
    "salary",
    "compensation",
    "criminal",
    "disability",
    "gender",
    "ethnicity",
    "veteran",
    "medical",
    "background check",
    "terms and conditions",
    "privacy notice",
    "privacy policy",
    "certify",
    "true and correct",
)


@dataclass
class ApplyConfig:
    """Runtime config for one application attempt."""

    db_path: str = "data/jobs.db"
    output_dir: str = "data/output"
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    evidence_enabled: bool = True
    submit_requires_approval: bool = True
    upload_requires_approval: bool = True
    blocklist_terms: tuple[str, ...] = DEFAULT_BLOCKLIST_TERMS


@dataclass
class PageInspection:
    url: str
    title: str
    text_excerpt: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    buttons: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    sensitive_questions: list[str] = field(default_factory=list)

    @property
    def safe_to_continue(self) -> bool:
        return not self.blockers and not self.sensitive_questions


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    scraper.init_application_tracking(conn)
    return conn


def _job_output_dir(base: str, job_id: str) -> Path:
    safe_job = "".join(c if c.isalnum() or c in "-_." else "_" for c in job_id)
    path = Path(base) / safe_job / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _record(config: ApplyConfig, job_id: str, stage: str, **kwargs: Any) -> int:
    conn = _connect_db(config.db_path)
    try:
        return scraper.record_application_stage(conn, job_id, stage, **kwargs)
    finally:
        conn.close()


def inspect_page(client: CDPClient, *, blocklist_terms: tuple[str, ...] = DEFAULT_BLOCKLIST_TERMS) -> PageInspection:
    """Return a compact, non-secret summary of the current browser page."""

    expression = r"""
(() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width >= 0 && rect.height >= 0;
  };
  const labelFor = (el) => {
    const labels = el.labels ? [...el.labels].map(l => l.innerText.trim()).filter(Boolean) : [];
    if (labels.length) return labels.join(' ');
    const aria = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
    return aria.trim();
  };
  return {
    url: location.href,
    title: document.title,
    text: document.body ? document.body.innerText.slice(0, 7000) : '',
    inputs: [...document.querySelectorAll('input, select, textarea')].filter(visible).slice(0, 80).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      id: el.id || '',
      name: el.name || '',
      label: labelFor(el),
      required: !!el.required,
      value_present: !!(el.value || '').trim(),
      options: el.tagName === 'SELECT' ? [...el.options].slice(0, 25).map(o => o.text.trim()).filter(Boolean) : []
    })),
    buttons: [...document.querySelectorAll('button, input[type=submit], input[type=button]')].filter(visible).slice(0, 50).map((el) => ({
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
      id: el.id || '',
      name: el.name || '',
      disabled: !!el.disabled
    })),
    links: [...document.querySelectorAll('a[href]')].filter(visible).slice(0, 80).map((el) => ({
      text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 120),
      href: el.href
    }))
  };
})()
"""
    data = client.evaluate(expression)
    text = data.get("text") or ""
    lower = text.lower()
    blockers = [term for term in blocklist_terms if term.lower() in lower]

    sensitive = []
    lines = [" ".join(line.split()) for line in text.splitlines()]
    for line in lines:
        low = line.lower()
        if any(term in low for term in SENSITIVE_QUESTION_TERMS):
            if line and line not in sensitive:
                sensitive.append(line[:240])
        if len(sensitive) >= 10:
            break

    return PageInspection(
        url=data.get("url", ""),
        title=data.get("title", ""),
        text_excerpt=text[:2000],
        inputs=data.get("inputs") or [],
        buttons=data.get("buttons") or [],
        links=data.get("links") or [],
        blockers=blockers,
        sensitive_questions=sensitive,
    )


class AutoApplyEngine:
    """Coordinates safe browser apply actions and DB status updates."""

    def __init__(self, config: ApplyConfig | None = None, client: CDPClient | None = None):
        self.config = config or ApplyConfig()
        self.client = client

    def connect(self) -> CDPClient:
        if self.client is None:
            self.client = connect_first_page(self.config.cdp_host, self.config.cdp_port)
        return self.client

    def inspect(self, job_id: str, *, stage: str = "draft_inspected") -> PageInspection:
        client = self.connect()
        inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
        evidence_path = None
        if self.config.evidence_enabled:
            evidence_path = str(_job_output_dir(self.config.output_dir, job_id) / f"{stage}.png")
            try:
                client.screenshot(evidence_path)
            except Exception:
                evidence_path = None
        status = "blocked_unknown_questions" if inspection.sensitive_questions else stage
        if inspection.blockers:
            status = "blocked_site_challenge"
        _record(
            self.config,
            job_id,
            status,
            platform=_platform_from_url(inspection.url),
            application_url=inspection.url,
            evidence_path=evidence_path,
            notes=json.dumps(
                {
                    "title": inspection.title,
                    "safe_to_continue": inspection.safe_to_continue,
                    "blockers": inspection.blockers,
                    "sensitive_questions": inspection.sensitive_questions,
                },
                ensure_ascii=False,
            ),
            error="; ".join(inspection.blockers or inspection.sensitive_questions) or None,
        )
        return inspection

    def upload_file(self, job_id: str, selector: str, file_path: str, *, approved: bool = False) -> PageInspection:
        if self.config.upload_requires_approval and not approved:
            _record(
                self.config,
                job_id,
                "blocked_resume_upload_approval",
                notes=f"Upload requested for selector {selector}; approval required.",
                error="resume_upload_requires_approval",
            )
            raise PermissionError("file upload requires explicit approval")
        if not Path(file_path).is_file():
            raise FileNotFoundError(file_path)
        client = self.connect()
        client.upload_file(selector, str(Path(file_path).resolve()))
        time.sleep(1)
        _record(self.config, job_id, "resume_uploaded", package_path=str(Path(file_path).resolve()))
        return self.inspect(job_id, stage="after_upload")

    def click_submit(self, job_id: str, selector: str, *, approved: bool = False) -> PageInspection:
        if self.config.submit_requires_approval and not approved:
            _record(
                self.config,
                job_id,
                "blocked_submit_approval",
                notes=f"Submit requested for selector {selector}; approval required.",
                error="submit_requires_approval",
            )
            raise PermissionError("submit requires explicit approval")
        client = self.connect()
        client.evaluate(
            f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return {{ok:false, reason:'selector not found'}};
  el.click();
  return {{ok:true}};
}})()
"""
        )
        time.sleep(2)
        _record(self.config, job_id, "submitted")
        return self.inspect(job_id, stage="submission_result")


def _platform_from_url(url: str) -> str | None:
    host = (url or "").lower()
    if "linkedin." in host:
        return "LinkedIn"
    if "emirates" in host or "avature" in host:
        return "Avature/Emirates"
    if host:
        return "External ATS"
    return None


def inspection_to_markdown(inspection: PageInspection) -> str:
    """Render a compact review message suitable for Telegram/Markdown."""

    lines = [
        "## Application page inspection",
        "",
        f"| Field | Value |",
        f"|---|---|",
        f"| Title | {inspection.title or 'N/A'} |",
        f"| URL | `{inspection.url or 'N/A'}` |",
        f"| Safe to continue | {'Yes' if inspection.safe_to_continue else 'No'} |",
    ]
    if inspection.blockers:
        lines += ["", "### Blockers", *[f"- {b}" for b in inspection.blockers]]
    if inspection.sensitive_questions:
        lines += ["", "### Sensitive / approval-gated text", *[f"- {q}" for q in inspection.sensitive_questions]]
    required = [i for i in inspection.inputs if i.get("required")]
    if required:
        lines += ["", "### Required fields seen", *[f"- {i.get('label') or i.get('name') or i.get('id') or i.get('tag')}" for i in required[:20]]]
    return "\n".join(lines)
