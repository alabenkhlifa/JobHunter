"""Which stored offers the autopilot may apply to, in what order, and why.

Reads only. Cheap filters (status, history, country, city, role, company)
run before the expensive ones (current hard filters, effective score), and a
job that clears everything but the reviewer gate is classified for review
instead of being applied to.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

import job_scoring
import jobhunter_queue
import scraper

from . import policy as policy_module
from . import store

# The current rubric writes "stack 0.98x35, ..." or "knocked out: ..."; older
# rows hold a legacy scale whose stored score is not comparable to min_score.
CURRENT_BREAKDOWN = re.compile(r"^(stack \d|knocked out:)")
HOLD_DAYS = 2
STACK_BOOST = 5

# Stages at or after the submit click, or his own later decisions: never again.
POST_SUBMISSION_STAGES = store.POST_SUBMISSION_STAGES
# A job the autopilot itself moved to interested (it packages before applying)
# stays its own while the latest log row says it never finished.
OWNED_INTERESTED_STATUSES = ("selected", "packaged", "applying", "rehearsed")


def _rx(pattern):
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


_B, _E = r"(?<![\w])", r"(?![\w])"
_SENIOR = _rx(_B + r"(?:senior|s[eé]nior|sr\.?)" + r"(?![\w])")
_SEMI_SENIOR = _rx(_B + r"(?:semi[\s-]?s[eé]nior|ssr)" + _E)
_NOUN = _rx(_B + r"(?:engineer|developer|programmer|swe|dev|ingenier[oa]s?(?:/[oa])?|"
            r"desarrollador(?:es|a|/a)?|programador(?:es|a|/a)?)" + _E)
_LEAD = _rx(_B + r"(?:tech(?:nical)?[\s-]?lead(?:er)?|team[\s-]?lead(?:er)?|lead(?:er)?|"
            r"l[ií]der(?:\s+t[eé]cnic[oa])?|jefe\s+de\s+equipo|responsable\s+t[eé]cnic[oa])" + _E)
_LEAD_CONTEXT = _rx(_B + r"(?:software|backend|back[\s-]end|tech|technical|t[eé]cnic[oa]|java|kotlin|"
                    r"development|desarrollo|engineering|ingenier[ií]a)" + _E)
_ARCHITECT = _rx(_B + r"(?:architect|arquitect[oa](?:/a)?)" + _E)
_BACKEND = _rx(_B + r"(?:back[\s-]?end)" + _E)
_LANGUAGE_DEV = _rx(_B + r"(?:java|kotlin|spring(?:\s+boot)?|node(?:\.js)?|python|golang|go)\s+"
                    r"(?:engineer|developer|desarrollador(?:a|/a)?|programador(?:a|/a)?)" + _E
                    + r"|" + _B + r"(?:desarrollador(?:a|/a)?|programador(?:a|/a)?|ingenier[oa](?:/a)?)"
                    r"\s+(?:de\s+)?(?:java|kotlin|back[\s-]?end)" + _E)
_MANAGER = _rx(_B + r"(?:engineering\s+manager|software\s+(?:development\s+|engineering\s+)?manager|"
               r"development\s+manager|manager,?\s+(?:software\s+)?engineering|"
               r"gerente\s+de\s+(?:ingenier[ií]a|desarrollo)|responsable\s+de\s+(?:ingenier[ií]a|desarrollo))" + _E)
_EXCLUDED_TITLE = _rx(_B + r"(?:principal|staff|director[a]?|head\s+of|vp|vice\s+president|chief|cto|"
                      r"intern|internship|junior|jr\.?|trainee|becari[oa]|pr[aá]cticas|graduate)" + r"(?![\w])")


def _senior(title):
    return bool(_SENIOR.search(title)) and not _SEMI_SENIOR.search(title)


ROLE_MATCHERS = {
    "senior_software_engineer": lambda t: _senior(t) and bool(_NOUN.search(t)),
    "lead_software_engineer": lambda t: bool(_LEAD.search(t)) and bool(_NOUN.search(t) or _LEAD_CONTEXT.search(t)),
    "software_architect": lambda t: bool(_ARCHITECT.search(t)),
    "backend_engineer": lambda t: bool(_BACKEND.search(t) and _NOUN.search(t)) or bool(_LANGUAGE_DEV.search(t)),
    "engineering_manager": lambda t: bool(_MANAGER.search(t)),
}
ROLE_LABELS = {
    "senior_software_engineer": "Senior Software Engineer",
    "lead_software_engineer": "Lead Software Engineer",
    "software_architect": "Software Architect",
    "backend_engineer": "Backend Engineer",
    "engineering_manager": "Engineering Manager",
}


def role_families(title):
    """Every family the raw title names; principal/staff/director/junior names none."""
    title = " ".join(str(title or "").split())
    if not title or _EXCLUDED_TITLE.search(title):
        return []
    return [family for family, matches in ROLE_MATCHERS.items() if matches(title)]


def _parse(value):
    try:
        when = datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def job_date(job):
    """The date prepare_review_candidate measures freshness from, so passing it bypasses only freshness."""
    for field in ("date_posted", "date_scraped"):
        when = _parse(job.get(field))
        if when is not None:
            return when
    return None


def effective_score(job, *, now=None):
    """(score, basis): the stored score when it is on the current scale, else a rescore in memory."""
    breakdown = str(job.get("score_breakdown") or "")
    stored = job.get("score")
    if CURRENT_BREAKDOWN.match(breakdown) and type(stored) is int:
        return stored, "stored"
    when = _parse(job.get("date_scraped")) or _parse(job.get("date_posted")) or now or datetime.now(timezone.utc)
    score, _ = scraper.score_job(dict(job), now=when)
    return int(score), "rescored"


def _stack_terms(job):
    return " ".join(str(job.get(k) or "") for k in ("tech_required", "title")).lower()


def preferred_stack_hits(job, stack):
    text = _stack_terms(job)
    return [term for term in stack if re.search(_B + re.escape(term.lower()) + _E, text)]


def _company_matches(company, names):
    company = str(company or "").casefold()
    return any(name.casefold() in company for name in names if name.strip())


def _application_rows(conn):
    if not store.table_exists(conn, "applications"):
        return {}
    rows = {}
    for job_id, stage, submitted_at in conn.execute(
            "SELECT job_id, stage, submitted_at FROM applications ORDER BY id"):
        rows.setdefault(job_id, []).append((str(stage or ""), submitted_at))
    return rows


def application_closed(rows):
    """True when any application row reached submission or a blocked stage."""
    return any(stage in POST_SUBMISSION_STAGES or stage.startswith("blocked_") or submitted_at
               for stage, submitted_at in rows)


_LEGAL_SUFFIXES = re.compile(r"\b(?:s\.?l\.?u?|s\.?a\.?u?|sl|sa|ltd|limited|inc|llc|gmbh|plc|espa[nñ]a|iberia|spain)\b\.?", re.I)


def role_key(job):
    """Same employer and same role, whatever board the copy came from."""
    company = _LEGAL_SUFFIXES.sub(" ", str(job.get("company") or "").lower())
    company = " ".join(re.findall(r"[a-z0-9]+", company))
    # Keep a parenthetical ("(Payments)"): it usually names a distinct team or
    # product; drop a trailing " - Madrid" / " | Hybrid" location or mode tag.
    title = str(job.get("title") or "")
    title = job_scoring.normalise_title(re.split(r"\s+[-|\u2013]\s+", title)[0])
    return company, title


def _applied_role_keys(conn):
    """Role keys of jobs he already applied to (any copy), so no second copy is sent."""
    if not store.table_exists(conn, "applications"):
        return set()
    keys = set()
    for row in conn.execute(
            "SELECT j.company, j.title, a.stage, a.submitted_at FROM applications a "
            "JOIN jobs j ON j.id = a.job_id"):
        if application_closed([(str(row[2] or ""), row[3])]):
            keys.add(role_key({"company": row[0], "title": row[1]}))
    return keys


def _owned_interested(conn):
    """Interested jobs the autopilot moved there itself and has not finished."""
    if not store.table_exists(conn, "auto_apply_log"):
        return set()
    latest = {}
    for job_id, status in conn.execute("SELECT job_id, status FROM auto_apply_log ORDER BY run_id, id"):
        latest[job_id] = status
    owned = {job_id for job_id, status in latest.items() if status in OWNED_INTERESTED_STATUSES}
    if owned and store.table_exists(conn, "job_feedback"):
        manual = {row[0] for row in conn.execute(
            "SELECT DISTINCT job_id FROM job_feedback WHERE action IN ('interested', 'skip', 'paused')")}
        owned -= manual
    return owned


def _verdict(job):
    return {"verdict": job.get("ai_verdict") or "", "reason": job.get("ai_verdict_reason") or "",
            "sponsorship": job.get("ai_sponsorship") or "", "rank": job.get("ai_rank"),
            "rubric": job.get("ai_rubric") or "", "reviewed_at": job.get("ai_reviewed_at") or ""}


def review_need(job, *, now, rubric):
    """'unreviewed' | 'hold_expired' | 'stale_rubric' | None (the verdict stands)."""
    verdict = job.get("ai_verdict") or ""
    if not verdict:
        return "unreviewed"
    if verdict in ("send", "hold") and (job.get("ai_rubric") or "") != rubric:
        return "stale_rubric"
    if verdict == "hold":
        probe = {k: job.get(k) for k in ("ai_verdict", "ai_reviewed_at", "ai_rubric")}
        jobhunter_queue._expire_hold(probe, now=now, rubric=rubric, hold_days=HOLD_DAYS)
        if probe.get("previous_verdict") == "hold":
            return "hold_expired"
    return None


def _location_label(job, policy):
    location = " ".join(str(job.get("location") or "").split())
    return location or ", ".join(policy_module.COUNTRY_LABELS.get(c, c) for c in policy["locations"]["countries"])


def select(conn, policy, *, now=None, run_id=None, exclude_ids=(), rubric=None):
    """Candidates, review needs and per-filter counts. Never writes.

    ``exclude_ids`` are jobs already in today's log; the caller decides which
    of today's rows are carried forward instead.
    """
    now = now or datetime.now(timezone.utc)
    rubric = scraper.CONFIG.get("review_rubric", "") if rubric is None else rubric
    markets = scraper.CONFIG.get("markets")
    statuses = [s for s in policy["include_statuses"] if s not in policy_module.NEVER_STATUSES]
    owned = _owned_interested(conn)
    applications = _application_rows(conn)
    applied_roles = _applied_role_keys(conn)
    excluded = set(exclude_ids)
    countries = set(policy["locations"]["countries"])
    cities = {c.lower() for c in policy["locations"]["cities"]}
    roles = list(policy["roles"])
    allow, deny = policy["companies"]["allow"], policy["companies"]["deny"]
    filtered = {}

    def drop(reason):
        filtered[reason] = filtered.get(reason, 0) + 1

    placeholders = ",".join("?" * len(statuses))
    cursor = conn.execute(f"SELECT * FROM jobs WHERE status IN ({placeholders}) OR status = 'interested'",
                          statuses)
    names = [c[0] for c in cursor.description]
    selected, review_pool, needs_review = [], [], {}
    for raw in cursor.fetchall():
        job = dict(zip(names, raw))
        job_id = job["id"]
        if job.get("status") == "interested" and job_id not in owned:
            continue
        if application_closed(applications.get(job_id, ())):
            drop("already_applied_or_blocked")
            continue
        if job_id in excluded:
            drop("already_in_todays_run")
            continue
        if role_key(job) in applied_roles:
            drop("duplicate_of_applied")
            continue
        location = str(job.get("location") or "")
        if job_scoring.market_country(location) not in countries:
            drop("country")
            continue
        if cities and job_scoring.market_region(location, markets=markets) not in cities:
            drop("city")
            continue
        families = [family for family in role_families(job.get("title")) if family in roles]
        if not families:
            drop("role")
            continue
        if deny and _company_matches(job.get("company"), deny):
            drop("company_denied")
            continue
        if allow and not _company_matches(job.get("company"), allow):
            drop("company_not_allowed")
            continue
        dated = job_date(job)
        candidate, reason = scraper.prepare_review_candidate(job, now=dated) if dated else (job, "no usable date")
        if reason:
            drop("hard_filter")
            continue
        score, basis = effective_score(candidate, now=now)
        if score < policy["min_score"]:
            drop("score")
            continue
        hits = preferred_stack_hits(candidate, policy["preferred_stack"])
        candidate.update(
            effective_score=score, score_basis=basis, role=families[0],
            rank_score=score + (STACK_BOOST if hits else 0), stack_hits=hits,
        )
        need = review_need(candidate, now=now, rubric=rubric)
        if need:
            needs_review[job_id] = need
            review_pool.append(candidate)
            continue
        if candidate.get("ai_verdict") != "send":
            drop("reviewer_" + (candidate.get("ai_verdict") or "none"))
            continue
        if candidate.get("ai_sponsorship") == "excluded":
            drop("sponsorship_excluded")
            continue
        candidate["verdict"] = _verdict(candidate)
        candidate["criteria"] = criteria_for(candidate, policy)
        selected.append(candidate)

    # The employer's own posting (apply_url) is the copy worth applying through.
    order = lambda job: (-job["rank_score"], -job["effective_score"],  # noqa: E731
                         0 if job.get("apply_url") else 1, str(job["id"]))
    selected.sort(key=order)
    review_pool.sort(key=order)
    selected = _one_per_role(selected, drop)
    review_pool = _one_per_role(review_pool, drop, needs_review=needs_review)
    return {"selected": selected, "review_pool": review_pool, "needs_review": needs_review,
            "filtered": filtered, "rubric": rubric}


def _one_per_role(jobs, drop, *, needs_review=None):
    """Keep the best-ranked copy of a role; the same job sits on several boards."""
    seen, kept = set(), []
    for job in jobs:
        key = role_key(job)
        if key in seen:
            drop("duplicate_role")
            if needs_review is not None:
                needs_review.pop(job["id"], None)
            continue
        seen.add(key)
        kept.append(job)
    return kept


def criteria_for(job, policy):
    criteria = [
        f"score {job['effective_score']} ≥ {policy['min_score']}"
        + (" (rescored)" if job.get("score_basis") == "rescored" else ""),
        "reviewer: send",
        f"role: {ROLE_LABELS.get(job.get('role'), job.get('role'))}",
        f"location: {_location_label(job, policy)}",
    ]
    if job.get("stack_hits"):
        criteria.append("stack: " + ", ".join(job["stack_hits"]))
    return criteria
