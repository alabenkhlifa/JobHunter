"""Approval-gated auto-apply engine for JobHunter.

The engine is intentionally conservative. It can prepare and inspect application
pages, record state, and perform explicitly-approved actions. It must not guess
legal/visa/salary answers, bypass anti-bot controls, or submit without an
approval flag from the caller.

Policy runs (``ApplyConfig.policy_run``) replace the human approval with the
autopilot gate (``jobhunter_autopilot.gate.authorize``): every mutating action
is authorized per job, a refusal raises ``GateRefused``, the tracker is never
synced from here, resubmission is refused and form questions are classified per
field (``answers.plan_fields``) instead of by page-text terms.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit

import scraper

from .cdp import CDPClient, CDPError, connect_first_page
from .email_verification import complete_email_verification


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
    tracker_sync: bool = True
    verify_submission: bool = False
    expected_page_url: str | None = None
    # When Chromium runs in the managed container, output_dir is mounted here
    # read-only. Local validation keeps using host paths; only CDP sees this path.
    browser_output_dir: str | None = None
    # Unattended autopilot mode: approvals come from the policy gate.
    policy_run: bool = False
    # Called as gate_authorizer(db_path, job_id, action, page_url=..., [policy_path=...]) -> {"run_id","mode"};
    # raises PermissionError (PolicyRefused) to refuse. Default: jobhunter_autopilot.gate.authorize.
    gate_authorizer: Callable[..., dict[str, Any]] | None = None
    policy_path: str | None = None
    # Answers from answers.build_answers / the autopilot plan, used by policy-run inspections.
    answers: dict[str, Any] | None = None
    # Set after authorization; tags application notes as "autopilot <run_id>".
    run_id: str | None = None


class GateRefused(PermissionError):
    """The autopilot policy gate refused this action (CLI exit 4)."""


def _default_gate_authorizer(db_path: str, job_id: str, action: str, *, page_url: str | None,
                             **kwargs: Any) -> dict[str, Any]:
    try:
        from jobhunter_autopilot.gate import authorize
    except ImportError:
        raise GateRefused("the autopilot gate is not installed") from None
    return authorize(db_path, job_id, action, page_url=page_url, **kwargs)


@dataclass
class PageInspection:
    url: str
    title: str
    text_excerpt: str
    inputs: list[dict[str, Any]] = field(default_factory=list)
    buttons: list[dict[str, Any]] = field(default_factory=list)
    links: list[dict[str, Any]] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    sensitive_questions: list[str] = field(default_factory=list)
    missing_required: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)

    @property
    def loaded_application_page(self) -> bool:
        url = urlsplit(self.url)
        return url.scheme in {"http", "https"} and bool(url.netloc)

    @property
    def safe_to_continue(self) -> bool:
        return (
            self.actionable_application_form
            and not self.blockers
            and not self.sensitive_questions
            and not self.missing_required
            and not self.invalid_fields
        )

    @property
    def actionable_application_form(self) -> bool:
        if not self.loaded_application_page:
            return False
        applicant_fields = [item for item in self.inputs if (
            item.get("tag") in {"input", "select", "textarea"}
            and str(item.get("type") or "").casefold() not in
            {"hidden", "submit", "button", "reset", "image", "password"}
            and not item.get("disabled")
            and (
                str(item.get("type") or "").casefold() in {"email", "tel", "file"}
                or re.search(r"\b(?:name|email|phone|resume|curriculum vitae|cv)\b",
                             " ".join(str(item.get(key) or "") for key in ("label", "name", "id")), re.I)
            )
        )]
        def submit_control(item):
            return (str(item.get("type") or "").casefold() == "submit"
                    or _SUBMIT_LABEL_RX.fullmatch(" ".join(str(item.get("text") or "").split())))

        forms: dict[str, list[dict]] = {}
        for item in applicant_fields:
            forms.setdefault(item.get("form") or "", []).append(item)
        editable = any(
            len(fields) >= 2
            and any(str(item.get("type") or "").casefold() in {"tel", "file"}
                    or re.search(r"\b(?:name|phone|resume|curriculum vitae|cv)\b",
                                 " ".join(str(item.get(key) or "") for key in ("label", "name", "id")), re.I)
                    for item in fields)
            and any((button.get("form") or "") == form and submit_control(button) for button in self.buttons)
            for form, fields in forms.items()
        )
        # Some ATS flows collect just a CV before showing personal details.
        # Its continuation must belong to that form, not an adjacent login form.
        resume_first = any(
            str(item.get("type") or "").casefold() == "file"
            and re.search(r"\b(?:resume|curriculum vitae|cv)\b", str(item.get("label") or ""), re.I)
            and item.get("form")
            and any(button.get("form") == item["form"] and submit_control(button) for button in self.buttons)
            for item in applicant_fields
        )
        # Some ATS flows end with an input-free confirmation step. Require an
        # explicit final-submit notice and enabled final control in that same
        # rendered form; a source Apply link or a login button is insufficient.
        final_review = any(
            form.get("id") and _FINAL_REVIEW_RX.search(str(form.get("text") or ""))
            and not any(item.get("form") == form["id"]
                        and not item.get("disabled")
                        and str(item.get("type") or "").casefold() not in
                        {"hidden", "submit", "button", "reset", "image"}
                        for item in self.inputs)
            and any(button.get("form") == form["id"] and not button.get("disabled")
                    and _FINAL_SUBMIT_LABEL_RX.fullmatch(" ".join(str(button.get("text") or "").split()))
                    for button in self.buttons)
            for form in getattr(self, "forms", [])
        )
        return bool(editable or resume_first or final_review)

    def verifies_application_for(self, job_title: str, job_company: str | None = None,
                                 job_location: str | None = None) -> bool:
        """A source Apply link and an ATS shell cannot verify this application."""
        return bool(
            self.actionable_application_form
            and any(_heading_matches_job(heading, job_title, job_company, job_location) for heading in self.headings)
            and _page_identifies_company(self.url, self.title, self.headings, job_company)
        )


def _title_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


_SUBMIT_LABEL_RX = re.compile(
    r"(?:submit(?: (?:my |your )?application)?|send application|apply(?: now| for this job)?|continue|next"
    r"|enviar(?: (?:mi |la |tu )?(?:solicitud|candidatura|cv|curr[ií]culum))?|aplicar(?: ahora| a (?:esta )?oferta)?"
    r"|postular(?:me|se)?(?: ahora)?|post[uú]late(?: ahora)?|inscribirme|inscribirse|inscr[ií]bete"
    r"|solicitar(?: empleo)?|presentar candidatura|continuar|siguiente)", re.I)

_FINAL_SUBMIT_LABEL_RX = re.compile(
    r"(?:submit|send) (?:my |your )?application|enviar (?:mi |la |tu )?(?:solicitud|candidatura)"
    r"|presentar candidatura", re.I)
_FINAL_REVIEW_RX = re.compile(
    r"\bby clicking (?:below|submit) you will submit your application\b"
    r"|\breview (?:your|the) application before (?:submitting|submission)\b"
    r"|\brevisa (?:tu|la) (?:solicitud|candidatura) antes de enviarla\b", re.I)

_APPLICATION_HEADING_PREFIXES = (
    ("apply", "for"), ("application", "for"), ("job", "application", "for"),
    ("apply", "to", "be", "a"), ("apply", "to", "be", "an"),
    ("solicitud", "de", "empleo", "para"), ("solicitud", "para"), ("candidatura", "para"),
    ("candidatura", "a"), ("aplicar", "a"), ("aplica", "a"), ("postularme", "a"), ("postúlate", "a"),
    ("inscríbete", "en"), ("inscribirme", "en"), ("oferta", "de", "empleo"),
)
_COMPANY_FILLER = {"at", "with", "and", "the", "of", "inc", "llc", "ltd"}
_SUFFIX_FILLER = _COMPANY_FILLER | {"en", "de", "del", "y", "in", "or", "o", "for", "para", "a"}
# Heading suffix tags that do not change the role: location, work mode, (m/f/d).
# Seniority words are deliberately absent: "Backend Engineer – Junior" is another posting.
_SUFFIX_TAGS = {
    # work mode / contract
    "remote", "remoto", "remota", "hybrid", "híbrido", "hibrido", "híbrida", "hibrida", "onsite", "on", "site",
    "presencial", "teletrabajo", "fulltime", "full", "time", "permanent", "indefinido", "100", "first", "flexible",
    "office", "oficina",
    # gender tags
    "m", "f", "d", "w", "x", "h", "mfd", "mwd", "all", "genders", "diverse",
    # locations
    "madrid", "barcelona", "valencia", "valència", "spain", "españa", "espana", "sevilla", "seville", "málaga",
    "malaga", "bilbao", "zaragoza", "alicante", "murcia", "palma", "mallorca", "catalonia", "cataluña", "catalunya",
    "comunidad", "europe", "eu", "emea", "es", "esp", "worldwide", "anywhere", "iberia", "portugal", "lisbon",
    "lisboa", "city",
}
_COMPANY_LEGAL_SUFFIXES = {"inc", "llc", "ltd", "limited", "gmbh", "corp", "corporation",
                           "sl", "sa", "slu", "sau", "sll", "bv", "ag", "plc", "sas", "sarl"}
# Dotted Spanish forms tokenize to letters: "S.L." -> ("s", "l").
_COMPANY_LEGAL_TAILS = (("s", "l", "u"), ("s", "a", "u"), ("s", "l"), ("s", "a"))


def _strip_legal_suffix(words: list[str]) -> list[str]:
    words = list(words)
    changed = True
    while words and changed:
        changed = False
        if words[-1] in _COMPANY_LEGAL_SUFFIXES:
            words.pop()
            changed = True
            continue
        for tail in _COMPANY_LEGAL_TAILS:
            if len(words) > len(tail) and tuple(words[-len(tail):]) == tail:
                del words[-len(tail):]
                changed = True
                break
    return words


def _page_identifies_company(url: str, title: str, headings: list[str], company: str | None) -> bool:
    """Require employer evidence from the rendered page or its Ashby tenant."""
    words = _strip_legal_suffix(list(_title_tokens(company or "")))
    if not words:
        return False
    expected = tuple(words)
    for value in (title, *headings):
        actual = _title_tokens(value)
        if any(actual[index:index + len(expected)] == expected
               for index in range(len(actual) - len(expected) + 1)):
            return True
    parsed = urlsplit(url)
    if parsed.hostname == "jobs.ashbyhq.com":
        tenant = parsed.path.strip("/").split("/", 1)[0]
        return "".join(_title_tokens(tenant)) == "".join(expected)
    return False


def _heading_matches_job(heading: str, job_title: str, job_company: str | None,
                         job_location: str | None = None) -> bool:
    title = _title_tokens(job_title)
    if not title:
        return False
    tokens = _title_tokens(heading)
    for prefix in _APPLICATION_HEADING_PREFIXES:
        if tokens[:len(prefix)] == prefix:
            tokens = tokens[len(prefix):]
            break
    if tokens == title:
        return True
    company = {word for word in _title_tokens(job_company or "") if word not in _COMPANY_FILLER}
    tags = _SUFFIX_TAGS | set(_title_tokens(job_location or ""))
    if tokens[:len(title)] == title:
        suffix = {word for word in tokens[len(title):] if word not in _SUFFIX_FILLER}
        return bool(suffix) and suffix <= (company | tags)
    # The stored title may carry the tag (" - Madrid") that the rendered heading omits.
    if len(tokens) >= 2 and title[:len(tokens)] == tokens:
        extra = {word for word in title[len(tokens):] if word not in _SUFFIX_FILLER}
        return bool(extra) and extra <= tags
    return False


def _connect_db(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    scraper.init_application_tracking(conn)
    return conn


def _job_output_dir(base: str, job_id: str) -> Path:
    safe_job = "".join(c if c.isalnum() or c in "-_." else "_" for c in job_id)
    path = Path(base) / safe_job / "evidence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _autopilot_notes(notes: str | None, run_id: str | None) -> str:
    tag = f"autopilot {run_id or 'unknown'}"
    try:
        payload = json.loads(notes) if notes else {}
    except (TypeError, ValueError):
        payload = None
    if isinstance(payload, dict):
        payload["source"] = tag
        return json.dumps(payload, ensure_ascii=False)
    return f"{tag}: {notes}"


def _post_submission_stages() -> frozenset[str]:
    try:
        from jobhunter_autopilot.store import POST_SUBMISSION_STAGES
    except ImportError:  # pragma: no cover - the autopilot package ships with the engine
        return frozenset({"submission_attempted", "submitted"})
    return POST_SUBMISSION_STAGES


def _record(config: ApplyConfig, job_id: str, stage: str, **kwargs: Any) -> int:
    if config.policy_run:
        kwargs["notes"] = _autopilot_notes(kwargs.get("notes"), config.run_id)
        # record_application_stage updates one row in place. Once a submit was
        # clicked that row is the only proof, so a later inspection or blocker
        # may add notes and evidence but never lowers the stage again.
        latest = _latest_application(config.db_path, job_id)
        if latest and stage not in _post_submission_stages() and (
                latest[1] or (latest[0] or "") in _post_submission_stages()):
            stage = latest[0] or stage
    conn = _connect_db(config.db_path)
    try:
        return scraper.record_application_stage(conn, job_id, stage, **kwargs,
                                                **({"sync": False} if not config.tracker_sync else {}))
    finally:
        conn.close()


def _expected_job(db_path: str, job_id: str) -> tuple[str, str | None, str | None]:
    path = Path(db_path)
    if not path.is_file():
        raise CDPError("collected job is unavailable; application form was not verified")
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            columns = {item[1] for item in conn.execute("PRAGMA table_info(jobs)")}
            company = "company" if "company" in columns else "NULL"
            status = "status" if "status" in columns else "NULL"
            row = conn.execute(f"SELECT title, {company}, {status} FROM jobs WHERE id=?", (job_id,)).fetchone()
    except sqlite3.Error:
        row = None
    if not row or not isinstance(row[0], str) or not row[0].strip():
        raise CDPError("collected job is unavailable; application form was not verified")
    return row[0], row[1], row[2]


def _expected_location(db_path: str, job_id: str) -> str | None:
    try:
        with sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
            columns = {item[1] for item in conn.execute("PRAGMA table_info(jobs)")}
            if "location" not in columns:
                return None
            row = conn.execute("SELECT location FROM jobs WHERE id=?", (job_id,)).fetchone()
    except (sqlite3.Error, OSError):
        return None
    return row[0] if row and isinstance(row[0], str) else None


def _is_under(file_path: str, root: Path | str) -> bool:
    try:
        Path(file_path).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return False
    return True


def _upload_roots(config: ApplyConfig, job_id: str) -> list[Path]:
    """Where a policy upload may come from: the output dir however it was named, or the job's package."""
    roots = [Path(config.output_dir)]
    env_root = os.getenv("JOBHUNTER_OUTPUT_DIR", "").strip()
    if env_root:
        roots.append(Path(env_root))
    path = Path(config.db_path)
    if path.is_file():
        try:
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                row = conn.execute("SELECT package_path FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                                   (job_id,)).fetchone()
        except sqlite3.Error:
            row = None
        if row and row[0]:
            roots.append(Path(str(row[0])))
    return roots


def _latest_application(db_path: str, job_id: str) -> tuple[str | None, str | None] | None:
    """Return (stage, submitted_at) of the latest application row, if any."""
    path = Path(db_path)
    if not path.is_file():
        return None
    try:
        with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            return conn.execute("SELECT stage, submitted_at FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                                (job_id,)).fetchone()
    except sqlite3.Error:
        return None


def _record_unverified(config: ApplyConfig, job_id: str, *, url: str | None = None) -> None:
    try:
        _, _, job_status = _expected_job(config.db_path, job_id)
        if str(job_status or "").casefold() == "skipped":
            return
        with sqlite3.connect(Path(config.db_path).resolve().as_uri() + "?mode=ro", uri=True) as conn:
            try:
                row = conn.execute("SELECT stage FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                                   (job_id,)).fetchone()
            except sqlite3.Error:
                row = None
        if row and row[0] in {"submitted", "submission_attempted", "skipped"}:
            return
        _record(config, job_id, "blocked_application_unverified",
                application_url=url,
                notes="The rendered exact-role application form was not verified.",
                error="exact_role_form_not_verified")
    except (CDPError, OSError, sqlite3.Error):
        # Preserve the browser failure; an unavailable job database cannot
        # become positive or negative form evidence.
        pass


def inspect_page(client: CDPClient, *, blocklist_terms: tuple[str, ...] = DEFAULT_BLOCKLIST_TERMS) -> PageInspection:
    """Return a compact, non-secret summary of the current browser page."""

    expression = r"""
(() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  const labelFor = (el) => {
    const labels = el.labels ? [...el.labels].map(l => l.innerText.trim()).filter(Boolean) : [];
    if (labels.length) return labels.join(' ');
    const aria = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
    return aria.trim();
  };
  const presented = (el) => visible(el) || (
    el.type === 'file' && !el.matches(':disabled') && [...(el.labels || [])].some(visible)
  );
  const formFor = (el) => el.form ? (el.form.id || `form-${[...document.forms].indexOf(el.form)}`) : '';
  const questionFor = (el) => { try {
    const group = el.closest('fieldset, [role=radiogroup], [role=group]');
    if (group) {
      const legend = group.querySelector('legend');
      const labelled = group.getAttribute('aria-labelledby');
      const text = legend ? legend.innerText : (labelled && document.getElementById(labelled)
        ? document.getElementById(labelled).innerText : (group.getAttribute('aria-label') || ''));
      if (text && text.trim()) return text.trim().slice(0, 300);
    }
    if (el.type === 'radio' || el.type === 'checkbox') {
      const box = el.parentElement && el.parentElement.parentElement;
      const holder = box && box.parentElement;
      const label = holder && holder.querySelector('label:not(:has(input)), .label, [class*="question" i]');
      if (label && !label.contains(el) && label.innerText.trim()) return label.innerText.trim().slice(0, 300);
    }
    return '';
  } catch (e) { return ''; } };
  const valuePresent = (el) => {
    if (el.type === 'radio') {
      return [...document.querySelectorAll('input[type=radio]')].some(other => other.name === el.name && other.checked);
    }
    if (el.type === 'checkbox') return el.checked;
    return !!(el.value || '').trim();
  };
  return {
    url: location.href,
    title: document.title,
    text: document.body ? document.body.innerText.slice(0, 7000) : '',
    headings: [...document.querySelectorAll('h1, h2, h3, [role=heading]')]
      .filter(el => visible(el) && !el.closest('aside, nav, [class*="recommend" i], [class*="similar" i], [class*="related" i]'))
      .slice(0, 20)
      .map(el => (el.innerText || '').trim().slice(0, 200)).filter(Boolean),
    forms: [...document.forms].filter(visible).slice(0, 20).map(el => ({
      id: el.id || `form-${[...document.forms].indexOf(el)}`,
      text: (el.innerText || '').trim().slice(0, 4000)
    })),
    inputs: [...document.querySelectorAll('input, select, textarea')].filter(presented).slice(0, 80).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      id: el.id || '',
      name: el.name || '',
      label: labelFor(el),
      form: formFor(el),
      question: questionFor(el),
      autocomplete: el.getAttribute('autocomplete') || '',
      value: (el.type === 'radio' || el.type === 'checkbox') ? (el.value || '') : '',
      disabled: !!el.disabled || el.matches(':disabled'),
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      value_present: valuePresent(el),
      invalid: el.getAttribute('aria-invalid') === 'true' || !el.checkValidity(),
      options: el.tagName === 'SELECT' ? [...el.options].slice(0, 25).map(o => o.text.trim()).filter(Boolean) : []
    })),
    buttons: [...document.querySelectorAll('button, input[type=submit], input[type=button]')].filter(visible).slice(0, 50).map((el) => ({
      text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim(),
      type: el.type || '',
      id: el.id || '',
      name: el.name || '',
      form: formFor(el),
      disabled: !!el.disabled || el.matches(':disabled')
    })),
    links: [...document.querySelectorAll('a[href]')].filter(visible).slice(0, 80).map((el) => ({
      text: (el.innerText || el.getAttribute('aria-label') || '').trim().slice(0, 120),
      href: el.href
    })),
    challenge_visible: [...document.querySelectorAll('iframe')].some(el => {
      const src = el.getAttribute('src') || '';
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return /recaptcha|hcaptcha/i.test(src) && style.display !== 'none' && style.visibility !== 'hidden'
        && rect.width >= 120 && rect.height >= 100;
    })
  };
})()
"""
    data = client.evaluate(expression)
    if not isinstance(data, dict):
        raise CDPError("application page inspection returned no document")
    text = data.get("text") or ""
    lower = text.lower()
    blockers = [term for term in blocklist_terms if term.lower() in lower]
    if data.get("challenge_visible") and "human verification" not in blockers:
        blockers.append("human verification")

    inputs = data.get("inputs") or []
    missing_required = [
        item.get("label") or item.get("name") or item.get("id") or item.get("tag") or "field"
        for item in inputs if item.get("required") and not item.get("value_present")
    ]
    invalid_fields = [
        item.get("label") or item.get("name") or item.get("id") or item.get("tag") or "field"
        for item in inputs if item.get("invalid")
    ]

    sensitive = []
    lines = [" ".join(line.split()) for line in text.splitlines()]
    for line in lines:
        low = line.lower()
        if any(term in low for term in SENSITIVE_QUESTION_TERMS):
            if line and line not in sensitive:
                sensitive.append(line[:240])
        if len(sensitive) >= 10:
            break

    inspection = PageInspection(
        url=data.get("url", ""),
        title=data.get("title", ""),
        text_excerpt=text[:2000],
        inputs=inputs,
        buttons=data.get("buttons") or [],
        links=data.get("links") or [],
        headings=data.get("headings") or [],
        blockers=blockers,
        sensitive_questions=sensitive,
        missing_required=missing_required,
        invalid_fields=invalid_fields,
    )
    # Full text for per-field classification; deliberately not a dataclass field
    # so asdict()/JSON output of existing callers is unchanged.
    inspection.page_text = text
    # Form-local text verifies an input-free final step without expanding the
    # CLI's public inspection schema or exposing duplicate applicant details.
    inspection.forms = data.get("forms") or []
    return inspection


class AutoApplyEngine:
    """Coordinates safe browser apply actions and DB status updates."""

    def __init__(self, config: ApplyConfig | None = None, client: CDPClient | None = None):
        config = config or ApplyConfig()
        if config.policy_run and config.tracker_sync:
            # Only the autopilot's record-submitted syncs the tracker.
            config = replace(config, tracker_sync=False)
        self.config = config
        self.client = client
        self.run_mode: str | None = None
        self.last_plan: dict[str, Any] | None = None
        self.last_receipt: dict[str, Any] | None = None

    def _authorize(self, job_id: str, action: str) -> bool:
        """Policy runs: ask the autopilot gate. Returns True when authorized."""
        if not self.config.policy_run:
            return False
        authorizer = self.config.gate_authorizer or _default_gate_authorizer
        extra = {"policy_path": self.config.policy_path} if self.config.policy_path else {}
        try:
            result = authorizer(self.config.db_path, job_id, action, page_url=self.config.expected_page_url, **extra)
        except GateRefused:
            raise
        except PermissionError as exc:
            raise GateRefused(str(exc) or f"{action} refused by the autopilot policy") from None
        if not isinstance(result, dict) or not result.get("run_id"):
            raise GateRefused(f"{action} was not authorized by the autopilot gate")
        self.config.run_id = str(result["run_id"])
        self.run_mode = result.get("mode")
        return True

    def _refuse_resubmit(self, job_id: str) -> None:
        path = Path(self.config.db_path)
        done = False
        if path.is_file():
            try:
                from jobhunter_autopilot.store import already_submitted
                with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
                    done = already_submitted(conn, job_id)
            except (ImportError, sqlite3.Error):
                latest = _latest_application(self.config.db_path, job_id)
                done = bool(latest and (latest[0] in {"submission_attempted", "submitted"} or latest[1]))
        if done:
            raise GateRefused("this application was already submitted or attempted; it is never resubmitted")

    def _evidence(self, job_id: str, name: str) -> str | None:
        if not self.config.evidence_enabled:
            return None
        try:
            path = str(_job_output_dir(self.config.output_dir, job_id) / f"{name}.png")
            self.connect().screenshot(path)
            return path
        except Exception:
            return None

    def connect(self) -> CDPClient:
        if self.client is None:
            self.client = connect_first_page(
                self.config.cdp_host,
                self.config.cdp_port,
                expected_url=self.config.expected_page_url,
            )
        return self.client

    def _verify_before_mutation(self, job_id: str, client: CDPClient) -> None:
        """Check the current rendered form before sending a document or clicking submit."""
        try:
            inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
            title, company, _ = _expected_job(self.config.db_path, job_id)
        except (CDPError, OSError):
            _record_unverified(self.config, job_id)
            raise
        if not inspection.verifies_application_for(title, company, _expected_location(self.config.db_path, job_id)):
            _record_unverified(self.config, job_id, url=inspection.url)
            raise CDPError("exact role and employer application form were not verified; no browser action was taken")

    def inspect(self, job_id: str, *, stage: str = "draft_inspected") -> PageInspection:
        if self.config.policy_run and stage == "draft_inspected" and self.config.run_id is None:
            self._authorize(job_id, "inspect")
        try:
            client = self.connect()
            inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
        except (CDPError, OSError):
            _record_unverified(self.config, job_id)
            raise
        if not inspection.loaded_application_page:
            _record_unverified(self.config, job_id)
            raise CDPError("application page is blank or still loading; no draft state was recorded")
        if stage != "submission_attempted":
            title, company, _ = _expected_job(self.config.db_path, job_id)
            location = _expected_location(self.config.db_path, job_id)
            if not inspection.verifies_application_for(title, company, location):
                _record_unverified(self.config, job_id, url=inspection.url)
                raise CDPError("exact role and actionable application form were not verified; no draft state was recorded")
        evidence_path = None
        if self.config.evidence_enabled and not any(
            str(item.get("id", "")).startswith("security-input-") and item.get("value_present")
            for item in inspection.inputs
        ):
            evidence_path = str(_job_output_dir(self.config.output_dir, job_id) / f"{stage}.png")
            try:
                client.screenshot(evidence_path)
            except Exception:
                evidence_path = None
        if self.config.policy_run:
            return self._record_policy_inspection(job_id, inspection, stage, evidence_path)
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
                    "exact_role_form_verified": stage != "submission_attempted",
                    "blockers": inspection.blockers,
                    "sensitive_questions": inspection.sensitive_questions,
                    "missing_required": inspection.missing_required,
                    "invalid_fields": inspection.invalid_fields,
                },
                ensure_ascii=False,
            ),
            error="; ".join(inspection.blockers or inspection.sensitive_questions) or None,
        )
        return inspection

    def _record_policy_inspection(self, job_id: str, inspection: PageInspection, stage: str,
                                  evidence_path: str | None) -> PageInspection:
        """Per-field classification: only unanswerable fields and real challenges stop a policy run."""
        from . import answers as answers_module

        plan = answers_module.plan_fields(inspection, self.config.answers or {})
        self.last_plan = plan
        challenges = [item for item in plan["hard_stops"]
                      if item["reason"] in {answers_module.CAPTCHA, answers_module.PHONE_OR_IDENTITY}]
        _record(
            self.config,
            job_id,
            "blocked_site_challenge" if challenges else stage,
            platform=_platform_from_url(inspection.url),
            application_url=inspection.url,
            evidence_path=evidence_path,
            notes=json.dumps(
                {
                    "title": inspection.title,
                    "exact_role_form_verified": stage != "submission_attempted",
                    "hard_stops": plan["hard_stops"],
                    "fill": len(plan["fill"]),
                    "leave_blank": len(plan["leave_blank"]),
                    "uploads": len(plan["uploads"]),
                    "missing_required": inspection.missing_required,
                    "invalid_fields": inspection.invalid_fields,
                },
                ensure_ascii=False,
            ),
            error="; ".join(item["reason"] for item in challenges) or None,
        )
        return inspection

    def upload_file(self, job_id: str, selector: str, file_path: str, *, approved: bool = False) -> PageInspection:
        gate_ok = False
        if self.config.policy_run:
            if self.config.expected_page_url is None:
                raise PermissionError("the application page URL is required for a policy upload")
            if not Path(file_path).is_file():
                raise FileNotFoundError(file_path)
            if not any(_is_under(file_path, root) for root in _upload_roots(self.config, job_id)):
                raise PermissionError("Policy uploads must come from the application package output directory.")
            gate_ok = self._authorize(job_id, "upload")
        if self.config.upload_requires_approval and not (approved or gate_ok):
            _record(
                self.config,
                job_id,
                "blocked_resume_upload_approval",
                notes=f"Upload requested for selector {selector}; approval required.",
                error="resume_upload_requires_approval",
            )
            raise PermissionError("file upload requires explicit approval")
        if self.config.expected_page_url is None:
            raise PermissionError("the approved application page URL is required for upload")
        if not Path(file_path).is_file():
            raise FileNotFoundError(file_path)
        upload_path = str(Path(file_path).resolve())
        if self.config.browser_output_dir is not None:
            browser_root = PurePosixPath(self.config.browser_output_dir)
            if not browser_root.is_absolute() or ".." in browser_root.parts or browser_root == PurePosixPath("/"):
                raise PermissionError("The browser document mount is invalid.")
            try:
                relative = Path(file_path).resolve().relative_to(Path(self.config.output_dir).resolve())
            except ValueError:
                raise PermissionError("The approved document is outside this candidate's output directory.") from None
            upload_path = str(browser_root.joinpath(*relative.parts))
        client = self.connect()
        if self.config.expected_page_url is not None and client.evaluate("location.href") != self.config.expected_page_url:
            raise PermissionError("The approved application page changed before upload.")
        self._verify_before_mutation(job_id, client)
        if client.evaluate("location.href") != self.config.expected_page_url:
            raise PermissionError("The approved application page changed before upload.")
        client.upload_file(selector, upload_path)
        time.sleep(1)
        _record(self.config, job_id, "resume_uploaded")
        return self.inspect(job_id, stage="after_upload")

    def click_submit(self, job_id: str, selector: str, *, approved: bool = False) -> PageInspection:
        gate_ok = False
        if self.config.policy_run:
            if self.config.expected_page_url is None:
                raise PermissionError("the application page URL is required for a policy submission")
            self._refuse_resubmit(job_id)
            try:
                gate_ok = self._authorize(job_id, "submit")
            except GateRefused:
                # Rehearsal (or any refusal): keep evidence of the filled form, never click.
                self._evidence(job_id, "submit_refused")
                raise
        if self.config.submit_requires_approval and not (approved or gate_ok):
            _record(
                self.config,
                job_id,
                "blocked_submit_approval",
                notes=f"Submit requested for selector {selector}; approval required.",
                error="submit_requires_approval",
            )
            raise PermissionError("submit requires explicit approval")
        if self.config.expected_page_url is None:
            raise PermissionError("the approved application page URL is required for submission")
        client = self.connect()
        if client.evaluate("location.href") != self.config.expected_page_url:
            raise PermissionError("The approved application page changed before submission.")
        self._verify_before_mutation(job_id, client)
        requested_at = dt.datetime.now(dt.timezone.utc)
        result = client.evaluate(
            f"""
(() => {{
  if ({json.dumps(self.config.expected_page_url)} !== null && location.href !== {json.dumps(self.config.expected_page_url)}) return {{ok:false, reason:'page changed'}};
  if (!/^https?:$/.test(location.protocol) || !document.body) return {{ok:false, reason:'application page is not loaded'}};
  const visible = el => {{const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return !el.disabled && r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';}};
  const required = [...document.querySelectorAll('input[required],select[required],textarea[required]')].filter(visible);
  const missing = required.filter(el => {{
    if (el.type === 'radio') return ![...document.querySelectorAll('input[type=radio]')].some(other => other.name === el.name && other.checked);
    if (el.type === 'checkbox') {{
      const locationGroup = location.hostname === 'job-boards.eu.greenhouse.io' && el.name.endsWith('[]');
      const group = locationGroup ? required.filter(other => other.type === 'checkbox' && other.name === el.name && other.form === el.form) : [el];
      return !group.some(other => other.checked);
    }}
    return !(el.value || '').trim();
  }});
  if (missing.length || [...document.querySelectorAll('[aria-invalid=true]')].some(visible)) return {{ok:false, reason:'required or invalid fields remain'}};
  const challenge = [...document.querySelectorAll('iframe')].some(frame => {{
    const src = frame.getAttribute('src') || '';
    const rect = frame.getBoundingClientRect();
    const style = getComputedStyle(frame);
    return /recaptcha|hcaptcha/i.test(src) && style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width >= 120 && rect.height >= 100;
  }});
  if (challenge) return {{ok:false, reason:'human verification is open'}};
  const el = document.querySelector({json.dumps(selector)});
  if (!el || el.disabled) return {{ok:false, reason:'selector unavailable'}};
  el.click();
  return {{ok:true}};
}})()
"""
        )
        if not isinstance(result, dict) or result.get("ok") is not True:
            reason = result.get("reason", "submit control unavailable") if isinstance(result, dict) else "submit control unavailable"
            raise PermissionError(f"The application was not submitted: {reason}.")
        time.sleep(2)
        _record(self.config, job_id, "submission_attempted", application_url=self.config.expected_page_url,
                notes=json.dumps({"submit_clicked": True, "exact_role_form_verified": True,
                                  "email_code_requested_at": requested_at.isoformat(),
                                  "receipt_review_required": True}))
        complete_email_verification(client, page_url=self.config.expected_page_url,
                                    requested_at=requested_at, approved=approved or gate_ok)
        inspection = self.inspect(job_id, stage="submission_attempted")
        if self.config.policy_run and self.config.verify_submission:
            try:
                self.last_receipt = self.verify_receipt(job_id)
            except Exception:
                self.last_receipt = None
        return inspection

    def verify_email(self, job_id: str, *, approved: bool = False) -> PageInspection:
        """Resume a recorded code step without clicking the initial submit again."""
        if self.config.policy_run and self.config.expected_page_url is not None:
            approved = approved or self._authorize(job_id, "verify-email")
        if not approved or self.config.expected_page_url is None:
            raise PermissionError("Email verification requires approval and the exact application URL.")
        with sqlite3.connect(self.config.db_path) as conn:
            row = conn.execute("SELECT stage, application_url, notes FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
                               (job_id,)).fetchone()
        try:
            notes = json.loads(row[2]) if row else {}
            requested_at = dt.datetime.fromisoformat(notes["email_code_requested_at"])
        except (TypeError, ValueError, KeyError):
            raise PermissionError("No verified pending email-code request is recorded for this application.") from None
        if (row[0] != "submission_attempted" or row[1] != self.config.expected_page_url
                or notes.get("exact_role_form_verified") is not True or notes.get("submit_clicked") is not True
                or requested_at.tzinfo is None
                or not dt.timedelta(0) <= dt.datetime.now(dt.timezone.utc)-requested_at <= dt.timedelta(minutes=15)):
            raise PermissionError("No fresh approved pending email-code request matches this application.")
        if not complete_email_verification(self.connect(), page_url=self.config.expected_page_url,
                                           requested_at=requested_at, approved=True):
            raise PermissionError("The recorded email-code step is not visible; review the portal without resubmitting.")
        return self.inspect(job_id, stage="submission_attempted")

    def fill_credentials(self, job_id: str, *, ats_key: str, email_selector: str, password_selector: str,
                         confirm_selector: str | None = None, create: bool = False, approved: bool = False,
                         mailbox: str | None = None, vault: Any = None) -> dict[str, Any]:
        """Type the vault credentials for ``ats_key`` into the exact page; returns no secret."""
        from .credentials import CredentialError, fill_credentials

        if self.config.expected_page_url is None:
            raise PermissionError("the application page URL is required to fill credentials")
        gate_ok = self._authorize(job_id, "credential") if self.config.policy_run else False
        if not (approved or gate_ok):
            raise PermissionError("filling ATS credentials requires approval or a policy run")
        if mailbox is None:
            try:
                from jobhunter_integrations.gmail_auth import expected_account
                mailbox = expected_account()
            except Exception:
                raise CredentialError("The jobs mailbox is not configured.") from None
        return fill_credentials(self.connect(), page_url=self.config.expected_page_url, ats_key=ats_key,
                                email_selector=email_selector, password_selector=password_selector,
                                confirm_selector=confirm_selector, create=create, mailbox=mailbox, vault=vault)

    def verify_receipt(self, job_id: str, *, out_path: str | None = None) -> dict[str, Any]:
        """Check the confirmation page and write ``evidence/receipt.json`` (``jobhunter.receipt/1``)."""
        from .receipt import connect_receipt_page, verify_receipt, write_receipt

        title, company, _ = _expected_job(self.config.db_path, job_id)
        job = {"id": job_id, "title": title, "company": company,
               "location": _expected_location(self.config.db_path, job_id)}
        if self.client is None:
            if self.config.expected_page_url is None:
                raise PermissionError("the application page URL is required to verify a receipt")
            self.client = connect_receipt_page(self.config.cdp_host, self.config.cdp_port,
                                               self.config.expected_page_url)
        evidence = _job_output_dir(self.config.output_dir, job_id)
        receipt = verify_receipt(self.client, job, screenshot_path=evidence / "receipt.png",
                                 page_url=self.config.expected_page_url)
        write_receipt(receipt, out_path or evidence / "receipt.json")
        self.last_receipt = receipt
        return receipt


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
    if not inspection.loaded_application_page:
        lines += ["", "### Blockers", "- Application page is blank or still loading"]
    if inspection.missing_required:
        lines += ["", "### Missing required fields", *[f"- {name}" for name in inspection.missing_required[:20]]]
    if inspection.invalid_fields:
        lines += ["", "### Invalid fields", *[f"- {name}" for name in inspection.invalid_fields[:20]]]
    required = [i for i in inspection.inputs if i.get("required")]
    if required:
        lines += ["", "### Required fields seen", *[f"- {i.get('label') or i.get('name') or i.get('id') or i.get('tag')}" for i in required[:20]]]
    return "\n".join(lines)
