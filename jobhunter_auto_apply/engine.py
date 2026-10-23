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
import html
import json
import os
import re
import sqlite3
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

import scraper

from .cdp import BrowserDialogError, BrowserTimeoutError, CDPClient, CDPError, connect_first_page
from .email_verification import complete_email_verification
from . import prefill, successfactors_ui5


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
    profile_path: str = "data/master-profile.json"
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
    frame_path: list[str] = field(default_factory=list)
    prefill_issues: list[str] = field(default_factory=list)

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
            and not self.prefill_issues
        )

    @property
    def actionable_application_form(self) -> bool:
        if not self.loaded_application_page:
            return False
        context = getattr(self, "application_context", {})
        if context.get("kind") in {"linkedin_easy_apply", "successfactors_ui5"}:
            return bool(context.get("actionable"))
        if context.get("kind") == "successfactors":
            # Registration and application share one form. Localized labels
            # and text-typed email fields must not turn it into a login shell.
            owned = {item.get("id") for item in self.inputs
                     if item.get("form") == context.get("form") and not item.get("disabled")}
            if {"fbclc_userName", "fbclc_fName", "fbclc_lName"} <= owned:
                return any(button.get("form") == context.get("form")
                           and button.get("id") == "fbqa_apply" and button.get("type") == "submit"
                           for button in self.buttons)
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
        # Clarivate's later steps have questionnaire/consent controls, followed
        # by an input-free review. Keep this adapter bound to exact step names,
        # one rendered form and its controls; identity is checked separately.
        parsed = urlsplit(self.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        clarivate = False
        if parsed.hostname == 'careers.clarivate.com' and parsed.path == '/apply':
            steps = {('2', 'workAndEducation'): 'experience',
                     ('3', 'jobSpecificQuestions'): 'questions',
                     ('4', 'voluntaryInformation'): 'consent',
                     ('5', 'applicationReview'): 'review'}
            pair = (query.get('step', [''])[0], query.get('stepname', [''])[0])
            kind = steps.get(pair) if len(query.get('step', [])) == len(query.get('stepname', [])) == 1 else None
            for form in getattr(self, 'forms', []):
                controls = [b for b in self.buttons if b.get('form') == form.get('id') and not b.get('disabled')]
                previous = any(b.get('id') == 'previous' and b.get('text') == 'Previous Step' for b in controls)
                next_step = any(b.get('id') == 'next' and b.get('text') == 'Next Step' for b in controls)
                owned = [x for x in self.inputs if x.get('form') == form.get('id') and not x.get('disabled')]
                if kind == 'experience':
                    clarivate |= previous and next_step and all(
                        any(x.get('required') and str(x.get('id', '')).startswith(prefix) for x in owned)
                        for prefix in ('experienceData[', 'educationData['))
                elif kind == 'questions':
                    clarivate |= previous and next_step and any(
                        x.get('required') and str(x.get('id', '')).startswith('jsqData.') for x in owned)
                elif kind == 'consent':
                    clarivate |= previous and next_step and any(
                        x.get('id') == 'agreementCheck' and x.get('type') == 'checkbox' and x.get('required') for x in owned)
                elif kind == 'review':
                    clarivate |= previous and not owned and all(
                        text in str(form.get('text', '')) for text in ('My Information', 'My Experience', 'Work Experience', 'Education')) and any(
                            b.get('text') == 'Submit' and b.get('type') == 'submit' for b in controls)
        return bool(editable or resume_first or final_review or clarivate)

    def verifies_application_for(self, job_title: str, job_company: str | None = None,
                                 job_location: str | None = None, *, job_id: str | None = None) -> bool:
        """A source Apply link and an ATS shell cannot verify this application."""
        context = getattr(self, "application_context", {})
        parsed = urlsplit(self.url)
        if parsed.hostname == "careers.clarivate.com":
            # This employer-owned form has a generic title and no separate brand
            # heading. Bind its visible role/requisition to the exact Phenom URL;
            # never strip arbitrary requisitions in the generic heading matcher.
            sequences = parse_qs(parsed.query, keep_blank_values=True).get("jobSeqNo", [])
            sequence = re.fullmatch(r"CLACLAGB(JREQ[0-9]+)EXTERNALENGB", sequences[0]) if len(sequences) == 1 else None
            headings = [match for heading in self.headings
                        if (match := re.fullmatch(r"(.+)\s+\((JREQ[0-9]+)\)", heading.strip()))]
            return bool(
                parsed.scheme == "https" and parsed.path == "/apply"
                and parsed.netloc.casefold() in {"careers.clarivate.com", "careers.clarivate.com:443"}
                and _title_tokens(job_company or "") == ("clarivate",)
                and self.actionable_application_form and sequence and len(headings) == 1
                and headings[0][2] == sequence[1]
                and _title_tokens(headings[0][1]) == _title_tokens(job_title) != ()
            )
        if parsed.hostname in {"www.linkedin.com", "linkedin.com"}:
            match = re.fullmatch(r"/jobs/view/(\d+)/?", parsed.path)
            return bool(
                parsed.scheme == "https" and match and job_id == f"li-{match[1]}"
                and context.get("kind") == "linkedin_easy_apply"
                and context.get("job_id") == match[1] and self.actionable_application_form
                and _heading_matches_job(context.get("role", ""), job_title, job_company, job_location)
                and _title_tokens(context.get("company", "")) == _title_tokens(job_company or "")
                and _title_tokens(context.get("dialog_company", "")) == _title_tokens(job_company or "")
            )
        if context.get("kind") in {"successfactors", "successfactors_ui5"}:
            return bool(
                _successfactors_identity(self.url, context, job_company)
                and self.actionable_application_form
                and _heading_matches_job(context.get("role", ""), job_title, job_company, job_location)
            )
        return bool(
            self.actionable_application_form
            and any(_heading_matches_job(heading, job_title, job_company, job_location) for heading in self.headings)
            and _page_identifies_company(self.url, self.title, self.headings, job_company)
        )


def _title_tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()))


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
# Explicit employer tenants cover group-branded ATS pages whose headings omit
# the listed subsidiary. Never infer an employer from a partial hostname.
_ATS_EMPLOYER_TENANTS = {
    "careers-en-axa.icims.com": {("axa",), ("axa", "group", "operations")},
}

# Explicit host/tenant/brand bindings: shared ATS hosts do not identify an
# employer. The DOM adapter and Python verifier use this same registry.
_SUCCESSFACTORS_TENANTS = (
    {"host": "career2.successfactors.eu", "tenant": "SwissRe", "company": "Swiss Re",
     "brands": ["careers.swissre.com"]},
    {"host": "career2.successfactors.eu", "tenant": "SF2013", "company": "Zurich Insurance",
     "brands": ["www.careers.zurich.com", "careers.zurich.com", "www.zurich.com"]},
    {"host": "career55.sapsf.eu", "tenant": "denodosl", "company": "Denodo",
     "brands": ["www.denodo.com", "jobs.denodo.com"]},
    {"host": "career5.successfactors.eu", "tenant": "gfttechnol", "company": "GFT Technologies",
     "brands": ["www.gft.com", "jobs.gft.com"]},
)


def _successfactors_identity(url: str, context: dict, company: str | None) -> bool:
    parsed = urlsplit(url)
    binding = next((item for item in _SUCCESSFACTORS_TENANTS
                    if item["host"] == parsed.hostname and item["tenant"] == context.get("tenant")), None)
    if not binding or parsed.scheme != "https" or parsed.netloc.casefold() not in {
            binding["host"], binding["host"] + ":443"} or parsed.path not in {"/careers", "/career", "/portalcareer"}:
        return False
    query = parse_qs(parsed.query, keep_blank_values=True)
    if "company" in query and query["company"] != [binding["tenant"]]:
        return False
    requisition = str(context.get("requisition", ""))
    return bool(
        context.get("form") == "careerform"
        and _title_tokens(company or "") == _title_tokens(binding["company"])
        and context.get("brand_host") in binding["brands"]
        and re.fullmatch(r"\d+", requisition)
        and all(query[key] == [requisition] for key in ("career_job_req_id", "jobReqId") if key in query)
    )


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
    """Require rendered employer evidence or a recognized employer ATS tenant."""
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
    if parsed.scheme == "https" and expected in _ATS_EMPLOYER_TENANTS.get(parsed.hostname, set()):
        return True
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
    def role_tokens(words):
        # Move only a trailing seniority qualifier. Preserve every role word,
        # its order and its level; never use bag-of-words or fuzzy similarity.
        if len(words) >= 3 and words[-1] in {"senior", "junior", "lead", "staff", "principal"}:
            return (words[-1], *words[:-1])
        return words

    if role_tokens(tokens) == role_tokens(title):
        return True
    company = {word for word in _title_tokens(job_company or "") if word not in _COMPANY_FILLER}
    tags = _SUFFIX_TAGS | set(_title_tokens(job_location or ""))
    if role_tokens(tokens[:len(title)]) == role_tokens(title):
        suffix = {word for word in tokens[len(title):] if word not in _SUFFIX_FILLER}
        return bool(suffix) and suffix <= (company | tags)
    # The stored title may carry the tag (" - Madrid") that the rendered heading omits.
    if len(tokens) >= 2 and role_tokens(title[:len(tokens)]) == role_tokens(tokens):
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
    # A reconnect/inspection must not erase an uncertain submit, including in
    # manual mode. Otherwise the next process could repeat the same click.
    latest = _latest_application(config.db_path, job_id)
    if latest and stage not in _post_submission_stages() and (
            latest[1] or (latest[0] or "") in _post_submission_stages()):
        stage = latest[0] or stage
        with sqlite3.connect(config.db_path) as previous:
            row = previous.execute('SELECT notes FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1', (job_id,)).fetchone()
        if row and row[0] and kwargs.get('notes'):
            try:
                notes = json.loads(row[0])
                if not isinstance(notes, dict): notes = {'previous_notes': row[0]}
            except (TypeError, ValueError):
                notes = {'previous_notes': row[0]}
            notes['latest_diagnostic'] = kwargs['notes']
            kwargs['notes'] = json.dumps(notes)
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
  const ui5 = __JOBHUNTER_UI5__;
  if (ui5) return ui5;
  const inspectDocument = (document, window, location, framePath) => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' && rect.width > 0 && rect.height > 0;
  };
  // LinkedIn renders its application in a native dialog, with the file picker
  // attached to body. Bind that dialog to the exact job URL and page title;
  // never combine fields from background jobs, settings, or a second modal.
  const linkedInJob = location.protocol === 'https:' &&
    ['www.linkedin.com', 'linkedin.com'].includes(location.hostname) &&
    location.pathname.match(/^\/jobs\/view\/(\d+)\/?$/);
  const dialogs = linkedInJob ? [...document.querySelectorAll('dialog[open]')].filter(visible) : [];
  const dialog = dialogs.length === 1 && dialogs[0].querySelector(
    '[data-sdui-screen="com.linkedin.sdui.flagshipnav.jobs.easyapply.EasyApply"]') ? dialogs[0] : null;
  const dialogHeading = dialog?.querySelector('header h2')?.innerText.trim() || '';
  const decodeIdentity = text => {
    const element = document.createElement('textarea');
    element.innerHTML = text;
    return element.value.normalize('NFKC').replace(/\s+/g, ' ').trim();
  };
  const dialogCompany = decodeIdentity(dialogHeading.slice(9));
  const titleIdentity = decodeIdentity(document.title);
  const employerSuffix = ' | ' + dialogCompany + ' | LinkedIn';
  // Anchor the split to the visible dialog employer: both role and employer
  // may themselves contain pipes.
  const linkedin = dialog && dialogHeading.startsWith('Apply to ') && dialogCompany &&
    titleIdentity.endsWith(employerSuffix) && titleIdentity.length > employerSuffix.length;
  const scope = linkedin ? dialog : document;
  const scopeText = (linkedin ? dialog : document.body)?.innerText || '';
  const uploadButton = linkedin && [...dialog.querySelectorAll('button')].find(el =>
    visible(el) && !el.disabled && el.innerText.trim() === 'Upload resume');
  const resumeStep = !!uploadButton && /select or upload a resume/i.test(scopeText);
  const externalFiles = resumeStep ? [...document.querySelectorAll('body > input[type=file]')].filter(el =>
    !el.disabled && el.accept.split(',').includes('application/pdf')) : [];
  const ownedFile = externalFiles.length === 1 ? externalFiles[0] : null;
  let applicationContext = {};
  if (linkedin) {
    const controls = [...dialog.querySelectorAll('button')].filter(el => visible(el) && !el.disabled);
    const continuation = controls.some(el => /^(next|review|submit application)$/i.test(el.innerText.trim()));
    const fields = [...dialog.querySelectorAll('input,select,textarea')].filter(el =>
      visible(el) && !el.disabled && !['hidden','button','submit','password'].includes(el.type));
    const review = /review (?:your )?application/i.test(scopeText) &&
      controls.some(el => /^submit application$/i.test(el.innerText.trim()));
    applicationContext = {kind: 'linkedin_easy_apply', job_id: linkedInJob[1],
      role: titleIdentity.slice(0, -employerSuffix.length), company: dialogCompany, dialog_company: dialogCompany,
      actionable: continuation && (resumeStep || fields.length > 0 || review)};
  } else if (location.protocol === 'https:' && !location.username && !location.password &&
             ['', '443'].includes(location.port) && ['/careers', '/career', '/portalcareer'].includes(location.pathname)) {
    const unique = id => {
      const matches = document.querySelectorAll('[id="' + id + '"]');
      return matches.length === 1 ? matches[0] : null;
    };
    const tenant = unique('career_company');
    const form = unique('careerform');
    // SuccessFactors repeats this hidden field in the form header and table.
    // Accept matching copies owned by this form, never conflicting identities.
    const requisitions = [...document.querySelectorAll('[id="career_job_req_id"]')];
    const req = requisitions[0];
    const requisitionsMatch = requisitions.length && requisitions.every(el =>
      el.type === 'hidden' && el.form === form && el.value === req.value);
    const binding = __JOBHUNTER_SUCCESSFACTORS__.find(item =>
      item.host === location.hostname && item.tenant === tenant?.value);
    const query = new URLSearchParams(location.search), companies = query.getAll('company');
    const queryMatches = (!companies.length || (companies.length === 1 && companies[0] === tenant?.value)) &&
      ['career_job_req_id', 'jobReqId'].every(key => !query.has(key) ||
        (query.getAll(key).length === 1 && query.get(key) === req?.value));
    const jobReqs = [...document.querySelectorAll('[id="jobReqId"]')];
    const jobReqMatches = !jobReqs.length || (jobReqs.length === 1 && jobReqs[0].type === 'hidden' &&
      jobReqs[0].form === form && jobReqs[0].value === req?.value);
    const headings = form ? [...form.querySelectorAll('h1,h2,h3,[role=heading]')].filter(visible)
      .map(el => el.innerText.trim().replace(/^(?:Career Opportunities|Puestos vacantes):\s*/i, '')
        .match(/^(.+)\s+\((\d+)\)$/)).filter(Boolean) : [];
    const brand = binding && [...document.querySelectorAll('a[href]')].find(el => {
      const url = new URL(el.href);
      return visible(el) && url.protocol === 'https:' && !url.username && !url.password &&
        ['', '443'].includes(url.port) && binding.brands.includes(url.hostname);
    });
    if (binding && queryMatches && requisitionsMatch && jobReqMatches && form?.tagName === 'FORM' && visible(form) &&
        tenant?.type === 'hidden' && tenant.form === form && req?.type === 'hidden' && req.form === form &&
        /^\d+$/.test(req.value) && headings.length === 1 && headings[0][2] === req.value && brand) {
      applicationContext = {kind: 'successfactors', tenant: tenant.value, requisition: req.value,
        role: headings[0][1], brand_host: new URL(brand.href).hostname, form: form.id};
    }
  }
  const labelFor = (el) => {
    const labels = el.labels ? [...el.labels].map(l => l.innerText.trim()).filter(Boolean) : [];
    if (labels.length) return labels.join(' ');
    const aria = el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name || el.id || '';
    return aria.trim();
  };
  const widgetFor = (el) => {
    if (el.tagName !== 'SELECT' || !el.id) return null;
    const widget = document.getElementById(el.id + '_icimsDropdown');
    return widget && widget.getAttribute('role') === 'combobox' &&
      widget.closest('form') === el.form && visible(widget) ? widget : null;
  };
  const uploadedFileFor = (el) => {
    if (el.type !== 'file' || !el.id.endsWith('_File')) return '';
    const base = el.id.slice(0, -5), stored = document.getElementById(base);
    const name = document.getElementById(base + '_FileName');
    const current = document.getElementById(base + '_CurrentFile');
    const label = document.getElementById(base + '_FileNameLabel');
    return stored?.type === 'hidden' && stored.form === el.form && stored.value &&
      name?.type === 'hidden' && name.form === el.form && name.value &&
      current && current.closest('form') === el.form && visible(current) && label && current.contains(label) &&
      label.textContent.trim() === name.value ? name.value : '';
  };
  const unavailableChild = (el) => {
    const parentName = el.getAttribute('data-ddd-parent-link');
    if (el.tagName !== 'SELECT' || !parentName || el.options.length !== 1) return false;
    const name = el.name.replace(/^-?\d+_/, ''), prefix = el.id.slice(0, -name.length);
    const parent = document.getElementById(prefix + parentName);
    return parent?.form === el.form && parent.value && !['-1', '-999'].includes(parent.value) &&
      parent.getAttribute('data-ddd-child-link') === name &&
      parent.getAttribute('data-ddd-no-children') === el.selectedOptions[0]?.text.trim();
  };
  const presented = (el) => el === ownedFile || visible(el) || (
    el.type === 'file' && !el.matches(':disabled') && [...(el.labels || [])].some(visible)
  ) || (
    !el.matches(':disabled') && !!widgetFor(el)
  ) || !!uploadedFileFor(el);
  const formFor = (el) => {
    if (linkedin && (dialog.contains(el) || el === ownedFile)) return 'linkedin-easy-apply';
    const form = el.form || el.closest('form');
    return form ? (form.id || `form-${[...document.forms].indexOf(form)}`) : '';
  };
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
    if (uploadedFileFor(el)) return true;
    if (el.type === 'radio') {
      return [...document.querySelectorAll('input[type=radio]')].some(other => other.name === el.name && other.checked);
    }
    if (el.type === 'checkbox') return el.checked;
    if (widgetFor(el) && ['', '-1', '-999'].includes(el.value)) return false;
    return !!(el.value || '').trim();
  };
  return {
    url: location.href,
    application_context: applicationContext,
    frame_path: framePath,
    title: document.title,
    text: scopeText.slice(0, 12000),
    headings: [...scope.querySelectorAll('h1, h2, h3, [role=heading]')]
      .filter(el => visible(el) && !el.closest('aside, nav, [class*="recommend" i], [class*="similar" i], [class*="related" i]'))
      .slice(0, 20)
      .map(el => (el.innerText || '').trim().slice(0, 200)).filter(Boolean),
    forms: linkedin ? [{id: 'linkedin-easy-apply', text: scopeText.slice(0, 4000)}] : [...document.forms].filter(visible).slice(0, 20).map(el => ({
      id: el.id || `form-${[...document.forms].indexOf(el)}`,
      text: (el.innerText || '').trim().slice(0, 4000)
    })),
    inputs: [...scope.querySelectorAll('input, select, textarea'), ...(ownedFile ? [ownedFile] : [])].filter(presented).slice(0, 80).map((el) => ({
      tag: el.tagName.toLowerCase(),
      type: el.type || '',
      id: el.id || '',
      name: el.name || '',
      label: el === ownedFile ? 'Resume' : labelFor(el),
      form: formFor(el),
      question: questionFor(el),
      autocomplete: el.getAttribute('autocomplete') || '',
      value: (el.type === 'radio' || el.type === 'checkbox') ? (el.value || '') : '',
      disabled: !!el.disabled || el.matches(':disabled'),
      required: !unavailableChild(el) && (!!el.required || el.getAttribute('aria-required') === 'true' ||
        (applicationContext.kind === 'successfactors' && el.form?.id === applicationContext.form && /\*/.test(labelFor(el))) ||
        (!!widgetFor(el) && (widgetFor(el).getAttribute('aria-required') === 'true' || el.getAttribute('i_required') === 'true'))),
      widget_id: widgetFor(el)?.id || '',
      uploaded_file_name: uploadedFileFor(el),
      value_present: valuePresent(el),
      invalid: el.getAttribute('aria-invalid') === 'true' || !el.checkValidity(),
      options: el.tagName === 'SELECT' ? [...el.options].slice(0, 25).map(o => o.text.trim()).filter(Boolean) : []
    })),
    buttons: [...scope.querySelectorAll('button, input[type=submit], input[type=button], [role=button]')].filter(visible).slice(0, 80).map((el) => ({
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
      return /recaptcha|hcaptcha|captcha-delivery\.com/i.test(src) && style.display !== 'none' && style.visibility !== 'hidden'
        && rect.width >= 120 && rect.height >= 100;
    })
  };
  };
  const root = inspectDocument(document, window, location, []);
  const candidates = [];
  const selectorFor = (el) => {
    if (el.id && el.ownerDocument.querySelectorAll('#' + CSS.escape(el.id)).length === 1)
      return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node; node = node.parentElement) {
      const index = node.parentElement ? [...node.parentElement.children].indexOf(node) + 1 : 1;
      parts.unshift(node.tagName.toLowerCase() + ':nth-child(' + index + ')');
    }
    return parts.join(' > ');
  };
  const walk = (win, path) => {
    if (path.length >= 5 || candidates.length >= 20) return;
    for (const frame of win.document.querySelectorAll('iframe')) {
      const style = win.getComputedStyle(frame), rect = frame.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden' || rect.width <= 0 || rect.height <= 0) continue;
      try {
        const child = frame.contentWindow;
        if (!frame.contentDocument || child.location.origin !== location.origin) continue;
        const next = [...path, selectorFor(frame)];
        candidates.push(inspectDocument(child.document, child, location, next));
        walk(child, next);
      } catch (_) { /* Cross-origin frames remain outside this DOM scope. */ }
      if (candidates.length >= 20) break;
    }
  };
  walk(window, []);
  return {...root, frame_candidates: candidates, prefill_snapshot: __JOBHUNTER_PREFILL__};
})()
"""
    expression = expression.replace('__JOBHUNTER_UI5__', successfactors_ui5.SNAPSHOT_JS)
    expression = expression.replace('__JOBHUNTER_PREFILL__', prefill.SNAPSHOT_JS)
    expression = expression.replace('__JOBHUNTER_SUCCESSFACTORS__', json.dumps(_SUCCESSFACTORS_TENANTS))
    client.frame_path = ()
    data = client.evaluate(expression)
    if not isinstance(data, dict):
        raise CDPError("application page inspection returned no document")
    inspection = _inspection_from_data(data, blocklist_terms)
    candidates = [_inspection_from_data(item, blocklist_terms)
                  for item in data.get("frame_candidates", []) if isinstance(item, dict)]
    actionable = [item for item in candidates if item.actionable_application_form]
    # Never mix a heading from one document with a form in another, and never
    # guess between multiple embedded application forms.
    if not inspection.actionable_application_form and len(actionable) == 1:
        selected = actionable[0]
        challenges = ["human verification"] if any(
            "human verification" in item.blockers for item in candidates) else []
        selected.blockers = list(dict.fromkeys(inspection.blockers + selected.blockers + challenges))
        client.frame_path = tuple(selected.frame_path)
        return selected
    if not inspection.actionable_application_form and len(actionable) > 1:
        inspection.blockers.append("multiple embedded application forms")
    return inspection


def _inspection_from_data(data: dict[str, Any], blocklist_terms: tuple[str, ...]) -> PageInspection:
    text = data.get("text") or ""
    # The standard passive badge does not ask the user to solve a challenge.
    # Visible challenge frames are checked separately, including inside forms.
    lower = re.sub(r"(?im)^\s*protected by hcaptcha privacy\s*\|\s*terms\s*$", "", text).lower()
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
        frame_path=data.get("frame_path") or [],
    )
    # Full text for per-field classification; deliberately not a dataclass field
    # so asdict()/JSON output of existing callers is unchanged.
    inspection.page_text = text
    # Form-local text verifies an input-free final step without expanding the
    # CLI's public inspection schema or exposing duplicate applicant details.
    inspection.forms = data.get("forms") or []
    inspection.application_context = data.get("application_context") or {}
    inspection.prefill_snapshot = data.get('prefill_snapshot')
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

    def _prefill_audit(self, job_id: str, inspection: PageInspection) -> dict:
        snapshot = getattr(inspection, 'prefill_snapshot', None)
        if not snapshot:
            return {'supported': False, 'issues': [], 'operations': [], 'manual_review': []}
        try:
            sources = prefill.load_sources(self.config.db_path, job_id, self.config.profile_path)
            result = prefill.audit(snapshot, *sources[:3])
            result['source_sha256'] = sources[3]
        except (OSError, ValueError, sqlite3.Error, TypeError, KeyError):
            result = {'supported': True, 'issues': ['approved_prefill_sources_unavailable'],
                      'operations': [], 'manual_review': ['approved_prefill_sources_unavailable']}
        inspection.prefill_issues = result['issues']
        return result

    def _browser_issue(self, job_id: str, exc: CDPError) -> None:
        path = None
        runtime = Path(os.getenv('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}'))
        display = os.getenv('WAYLAND_DISPLAY', 'wayland-0')
        if shutil.which('grim') and (runtime/display).is_socket():
            destination = _job_output_dir(self.config.output_dir, job_id)/'browser-dialog.png'
            try:
                subprocess.run(['grim', str(destination)], check=True, capture_output=True, timeout=5,
                    env={**os.environ, 'XDG_RUNTIME_DIR': str(runtime), 'WAYLAND_DISPLAY': display})
                path = str(destination)
            except (OSError, subprocess.SubprocessError):
                pass
        latest = _latest_application(self.config.db_path, job_id)
        stage = (latest[0] if latest and (latest[1] or latest[0] in _post_submission_stages())
                 else 'blocked_browser_dialog')
        _record(self.config, job_id, stage, application_url=self.config.expected_page_url,
                evidence_path=path, notes=(None if latest and stage in _post_submission_stages() else
                    'Inspect the desktop dialog and previous action outcome; do not retry blindly.'),
                error=str(exc))

    def _submit_evaluate(self, job_id: str, client: CDPClient, expression: str) -> Any:
        try:
            return client.evaluate(expression)
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            # The browser may have clicked before it stopped responding. Keep
            # a durable uncertainty marker so reconnecting cannot resubmit.
            _record(self.config, job_id, 'submission_attempted',
                    application_url=self.config.expected_page_url,
                    notes=json.dumps({'submit_outcome_unknown': True, 'retry_forbidden': True,
                                      'exact_role_form_verified': True}), error=str(exc))
            self._browser_issue(job_id, exc)
            raise

    def _verify_before_mutation(self, job_id: str, client: CDPClient, *, check_prefill: bool = True) -> PageInspection:
        """Check the current rendered form before sending a document or clicking submit."""
        try:
            inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
            title, company, _ = _expected_job(self.config.db_path, job_id)
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            self._browser_issue(job_id, exc)
            raise
        except (CDPError, OSError):
            _record_unverified(self.config, job_id)
            raise
        if not inspection.verifies_application_for(title, company, _expected_location(self.config.db_path, job_id), job_id=job_id):
            _record_unverified(self.config, job_id, url=inspection.url)
            raise CDPError("exact role and employer application form were not verified; no browser action was taken")
        if inspection.blockers:
            raise CDPError('A site verification blocker is visible; no browser action was taken.')
        if self.config.policy_run and getattr(inspection, 'application_context', {}).get('kind') == 'successfactors_ui5':
            raise GateRefused('SAP UI5 applications require the owner-driven manual flow.')
        if check_prefill and self._prefill_audit(job_id, inspection)['issues']:
            _record(self.config, job_id, 'blocked_prefill_review', application_url=inspection.url,
                    notes=json.dumps({'prefill_issues': inspection.prefill_issues}), error='cv_parser_output_requires_review')
            raise CDPError('CV parser output differs from confirmed facts; reconcile the form before continuing.')
        return inspection

    def inspect(self, job_id: str, *, stage: str = "draft_inspected") -> PageInspection:
        if self.config.policy_run and stage == "draft_inspected" and self.config.run_id is None:
            self._authorize(job_id, "inspect")
        try:
            client = self.connect()
            inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            self._browser_issue(job_id, exc)
            raise
        except (CDPError, OSError):
            _record_unverified(self.config, job_id)
            raise
        if not inspection.loaded_application_page:
            _record_unverified(self.config, job_id)
            raise CDPError("application page is blank or still loading; no draft state was recorded")
        if stage != "submission_attempted":
            title, company, _ = _expected_job(self.config.db_path, job_id)
            location = _expected_location(self.config.db_path, job_id)
            if not inspection.verifies_application_for(title, company, location, job_id=job_id):
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
        self._prefill_audit(job_id, inspection)
        if self.config.policy_run and inspection.prefill_issues:
            _record(self.config, job_id, 'blocked_prefill_review', application_url=inspection.url,
                    notes=json.dumps({'prefill_issues': inspection.prefill_issues}), error='cv_parser_output_requires_review')
            raise CDPError('CV parser output requires owner review; no policy action was taken.')
        if self.config.policy_run:
            return self._record_policy_inspection(job_id, inspection, stage, evidence_path)
        status = "blocked_unknown_questions" if inspection.sensitive_questions else stage
        if inspection.prefill_issues:
            status = 'blocked_prefill_review'
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
                    "prefill_issues": inspection.prefill_issues,
                },
                ensure_ascii=False,
            ),
            error="; ".join(inspection.blockers or inspection.sensitive_questions) or None,
        )
        return inspection

    def reconcile_prefill(self, job_id: str, *, approved: bool = False) -> dict:
        """Audit first; repair only unambiguous facts with explicit manual approval."""
        if not self.config.expected_page_url or self.config.policy_run:
            raise PermissionError('Prefill reconciliation requires the exact URL and owner-driven manual flow.')
        self._refuse_resubmit(job_id)
        client = self.connect()
        before = None
        source_sha = None
        changes = []
        for _ in range(80):
            inspection = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
            title, company, _ = _expected_job(self.config.db_path, job_id)
            if (inspection.url != self.config.expected_page_url or inspection.blockers
                    or not inspection.verifies_application_for(title, company, job_id=job_id)):
                raise CDPError('Exact unobstructed application form is required for reconciliation.')
            result = self._prefill_audit(job_id, inspection)
            if not result['supported']:
                raise CDPError('No supported CV parser audit adapter exists for this form.')
            if source_sha is not None and source_sha != result.get('source_sha256'):
                raise CDPError('Approved prefill sources changed during reconciliation.')
            source_sha = result.get('source_sha256')
            if before is None:
                before = getattr(inspection, 'prefill_snapshot', None)
                if approved and result['issues'] and not result['manual_review']:
                    folder = _job_output_dir(self.config.output_dir, job_id)
                    backup = folder/f'prefill-before-{time.time_ns()}.json'
                    backup.write_text(json.dumps(before, ensure_ascii=False))
                    backup.chmod(0o600)
            if not approved or not result['issues'] or result['manual_review']:
                result['changes'] = changes
                if approved and not result['issues']:
                    self.inspect(job_id)
                return result
            # Remove surplus rows first, then re-read after every operation.
            operations = sorted(result['operations'], key=lambda op: op['kind'] != 'remove')
            op = operations[0]
            if op['kind'] == 'remove':
                client.click(op['selector'])
            elif op['kind'] == 'select':
                client.select_option(op['selector'], op['value'])
            else:
                client.fill(op['selector'], op['value'])
            # Wait for the controlled form to commit the change, without
            # repeating a click or typing operation that may already have run.
            deadline = time.monotonic()+3
            signature = json.dumps(getattr(inspection, 'prefill_snapshot', None), sort_keys=True)
            while time.monotonic() < deadline:
                time.sleep(.1)
                snapshot = client.evaluate(prefill.SNAPSHOT_JS)
                if json.dumps(snapshot, sort_keys=True) != signature:
                    break
            else:
                raise CDPError('Prefill edit did not settle; inspect the form before retrying.')
            changes.append({'kind': op['kind'], 'selector': op['selector']})
        raise CDPError('Prefill repair limit reached; review the form.')

    def advance(self, job_id: str, selector: str, *, approved: bool = False) -> PageInspection:
        if not approved or not self.config.expected_page_url or self.config.policy_run:
            raise PermissionError('Advancing requires owner-approved preparation and the exact page URL.')
        self._refuse_resubmit(job_id)
        client = self.connect()
        inspection = self._verify_before_mutation(job_id, client)
        if inspection.url != self.config.expected_page_url or inspection.missing_required or inspection.invalid_fields:
            raise CDPError('Required or invalid fields remain; no Next action was taken.')
        ui5 = getattr(inspection, 'application_context', {}).get('kind') == 'successfactors_ui5'
        if ui5:
            controls = [b for b in inspection.buttons if b.get('selector') == selector and not b.get('disabled')]
            button = controls[0]['text'] if len(controls) == 1 else None
        else:
            button = client.evaluate(f"""(() => {{const e=document.querySelector({json.dumps(selector)});
          return e&&e.tagName==='BUTTON'&&!e.disabled ? e.innerText.trim() : null;}})()""")
        if button not in {'Next', 'Next Step', 'Continue', 'Review'}:
            raise PermissionError('Only a continuation control is allowed; final submission is separately approved.')
        try:
            client.click(selector)
            deadline = time.monotonic()+client.timeout
            title, company, _ = _expected_job(self.config.db_path, job_id)
            while time.monotonic() < deadline:
                time.sleep(.15)
                current = inspect_page(client, blocklist_terms=self.config.blocklist_terms)
                if (current.url != self.config.expected_page_url or
                        (ui5 and getattr(current, 'application_context', {}).get('step') !=
                         getattr(inspection, 'application_context', {}).get('step'))) and (
                        current.verifies_application_for(title, company, job_id=job_id)):
                    return self.inspect(job_id)
                if (current.url == self.config.expected_page_url and
                        (getattr(current, 'prefill_snapshot', None) or {}).get('errors')):
                    raise CDPError('The ATS reported validation errors; inspect the form before continuing.')
            raise CDPError('Next outcome is unverified; inspect the page without repeating the click.')
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            self._browser_issue(job_id, exc)
            raise

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
        inspection = self._verify_before_mutation(job_id, client, check_prefill=False)
        if getattr(inspection, "application_context", {}).get("kind") == "successfactors_ui5":
            owned = [item for item in inspection.inputs if item.get("type") == "file"
                     and item.get("selector") == selector and not item.get("disabled")]
            if len(owned) != 1:
                raise CDPError("The SAP UI5 upload must use the inspected form's file control.")
        if getattr(inspection, "application_context", {}).get("kind") == "linkedin_easy_apply":
            owned_files = [item for item in inspection.inputs if item.get("type") == "file"]
            if len(owned_files) != 1 or not client.evaluate(f"""(() => {{
              const files = [...document.querySelectorAll('input[type=file]')];
              return files.length === 1 && files[0] === document.querySelector({json.dumps(selector)});
            }})()"""):
                raise CDPError("the LinkedIn resume upload control is ambiguous or unavailable")
        if client.evaluate("location.href") != self.config.expected_page_url:
            raise PermissionError("The approved application page changed before upload.")
        try:
            client.upload_file(selector, upload_path)
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            self._browser_issue(job_id, exc)
            raise
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
        self._refuse_resubmit(job_id)
        client = self.connect()
        if client.evaluate("location.href") != self.config.expected_page_url:
            raise PermissionError("The approved application page changed before submission.")
        inspection = self._verify_before_mutation(job_id, client)
        if getattr(inspection, 'application_context', {}).get('kind') == 'successfactors_ui5':
            return self._submit_ui5(job_id, client, inspection, selector)
        requested_at = dt.datetime.now(dt.timezone.utc)
        result = self._submit_evaluate(job_id, client,
            f"""
(() => {{
  if ({json.dumps(self.config.expected_page_url)} !== null && location.href !== {json.dumps(self.config.expected_page_url)}) return {{ok:false, reason:'page changed'}};
  if (!/^https?:$/.test(location.protocol) || !document.body) return {{ok:false, reason:'application page is not loaded'}};
  const visible = el => {{const r=el.getBoundingClientRect(), s=getComputedStyle(el);
    return !el.disabled && r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';}};
  const required = [...document.querySelectorAll('input[required],select[required],textarea[required],input[aria-required=true],select[aria-required=true],textarea[aria-required=true]')].filter(visible);
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
    return /recaptcha|hcaptcha|captcha-delivery\\.com/i.test(src) && style.display !== 'none' && style.visibility !== 'hidden'
      && rect.width >= 120 && rect.height >= 100;
  }});
  if (challenge) return {{ok:false, reason:'human verification is open'}};
  const el = document.querySelector({json.dumps(selector)});
  if (!el || el.disabled) return {{ok:false, reason:'selector unavailable'}};
  el.scrollIntoView({{block:'center',behavior:'instant'}});
  const rect = el.getBoundingClientRect(), top = document.elementFromPoint(rect.x+rect.width/2,rect.y+rect.height/2);
  if (!visible(el) || (top !== el && !el.contains(top))) return {{ok:false, reason:'submit control is obscured'}};
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

    def _submit_ui5(self, job_id: str, client: CDPClient, inspection: PageInspection,
                    selector: str) -> PageInspection:
        """Submit only the verified Review step; persist uncertainty before the click."""
        context = inspection.application_context
        controls = [b for b in inspection.buttons if b.get('selector') == selector
                    and b.get('text') in {'Submit', 'Submit Application', 'Apply'} and not b.get('disabled')]
        if (context.get('step') != 'Review' or len(controls) != 1 or inspection.missing_required
                or inspection.invalid_fields or inspection.prefill_issues):
            raise CDPError('Only a complete SAP UI5 Review step can be submitted.')
        if client.evaluate('location.href') != self.config.expected_page_url:
            raise PermissionError('The approved application page changed before submission.')
        _record(self.config, job_id, 'submission_attempted', application_url=self.config.expected_page_url,
                notes=json.dumps({'exact_role_form_verified': True, 'retry_forbidden': True,
                                  'receipt_review_required': True}))
        try:
            client.click(selector)
        except (BrowserDialogError, BrowserTimeoutError) as exc:
            self._browser_issue(job_id, exc)
            raise
        time.sleep(2)
        return self.inspect(job_id, stage='submission_attempted')

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
