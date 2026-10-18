"""Small, explicit candidate job requests; never model-generated actions."""
from __future__ import annotations

from dataclasses import dataclass
import re


JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
OPERATIONS = ("details", "interested", "prepare")
MAX_DIGEST_JOBS = 12
MAX_REFERENCE_CHARS = 200


@dataclass(frozen=True)
class JobIntent:
    operation: str
    reference: str = ""


def parse_job_intent(text: str) -> JobIntent | None:
    """Require a complete directive, leaving preference/interview replies alone."""
    if not isinstance(text, str) or len(text) > 300 or "\n" in text or text.startswith("/"):
        return None
    request = text.strip().rstrip(".?!").strip()
    request = re.sub(r"^(?:please\s+|can you\s+|could you\s+)", "", request, flags=re.I)
    if re.fullmatch(r"(?:show|list)(?:\s+me)?(?:\s+my)?\s+(?:collected|held|queued)\s+(?:jobs|job matches)", request, re.I):
        return JobIntent("jobs", "collected")
    if re.fullmatch(r"(?:show|list)(?:\s+me)?(?:\s+my)?(?:\s+(?:latest|recent))?\s+(?:jobs|job matches)", request, re.I):
        return JobIntent("jobs")
    patterns = (
        ("details", r"(?:show\s+(?:me\s+)?details(?:\s+(?:for|of|on))?\s+|show\s+(?:me\s+)?(?:the\s+)?(?:job|role|posting)\s+|details\s+(?:(?:for|of|on)\s+)?)"),
        ("interested", r"(?:mark\s+(?:me\s+)?interested(?:\s+in)?\s+|interested(?:\s+in)?\s+)"),
        ("prepare", r"prepare\s+(?:(?:my|a|the)\s+)?(?:(?:resume|application package)(?:\s+for)?\s+)?"),
    )
    for operation, prefix in patterns:
        match = re.fullmatch(prefix + r"(.+)", request, re.I)
        if not match:
            continue
        reference = re.sub(r"^(?:the\s+)?(?:job|role|posting)\s+", "", match[1].strip(), flags=re.I).strip()
        if not reference or len(reference) > MAX_REFERENCE_CHARS:
            return None
        # These are profile preferences or an unspecified document request.
        if re.search(r"\b(?:roles|jobs|skills|preferences|profile|experience|wording)\b", reference, re.I):
            return None
        if operation == "prepare" and reference.casefold() in {"resume", "my resume", "the resume", "application package"}:
            return None
        return JobIntent(operation, reference)
    return None


def bare_show_id(text: str) -> str | None:
    """A bare 'show <id>' needs an exact own-database match before routing."""
    if not isinstance(text, str):
        return None
    match = re.fullmatch(r'(?:please\s+)?show\s+([A-Za-z0-9][A-Za-z0-9._-]{0,127})[?!]?', text.strip(), re.I)
    return match[1] if match else None


def parse_job_callback(data: str) -> JobIntent:
    if not isinstance(data, str) or len(data.encode("utf-8")) > 64:
        raise ValueError("This job button is invalid. Ask to show your jobs again.")
    match = re.fullmatch(r"jh:job:(details|interested|prepare):([A-Za-z0-9][A-Za-z0-9._-]{0,127})", data)
    if not match:
        raise ValueError("This job button is invalid. Ask to show your jobs again.")
    return JobIntent(match[1], match[2])


def matching_jobs(reference: str, jobs: list[dict]) -> list[dict]:
    """Exact IDs win; title/company references must identify a unique own job."""
    exact = [job for job in jobs if job["id"] == reference]
    if exact:
        return exact
    needle = " ".join(reference.casefold().split())
    if len(needle) < 3 or needle in {"this", "that", "it", "first", "last", "one", "this role", "that role"}:
        return []
    values = [(job, " ".join(str(job.get("title") or "").casefold().split()),
               " ".join(str(job.get("company") or "").casefold().split())) for job in jobs]
    exact = [job for job, title, company in values if needle in {title, company, f"{title} at {company}", f"{title} — {company}"}]
    return exact or [job for job, title, company in values if needle in title or needle in company]
