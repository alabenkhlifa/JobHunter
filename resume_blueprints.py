"""Role-family resume blueprints: what a resume for a family looks like, confirmed once.

A blueprint holds confirmed presentation choices (headline wording, fallback
summary, skills, certification order, page target, which projects lead, how
many bullets each gets, which side roles appear). The evidence itself comes
from ``resume_evidence``; a blueprint never adds wording of its own.
"""

from __future__ import annotations

import copy
import re

import resume_evidence as evidence
import resume_quality

FAMILIES = ("backend", "lead", "architect", "platform", "other")
HEADLINE_KEYS = ("default", "senior", "lead", "architect")
RULES = ("always", "never", "if_leadership", "if_relevant")
IDENTITY_FIELDS = ("name", "email", "phone", "website", "linkedin", "location")
EDUCATION_FIELDS = ("degree", "school", "location", "dates", "display_degree", "display_school")
EXPERIENCE_FIELDS = ("title", "company", "company_url", "subtitle", "location", "dates")


class BlueprintError(ValueError):
    """A blueprint that cannot safely drive a resume; the message names the field, never candidate text."""


def _string_list(value, path):
    if not isinstance(value, list) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise BlueprintError(f"{path} must be a list of non-empty strings")


def validate(profile):
    """Return the candidate-confirmed blueprints after structural validation of all of them."""
    items = profile.get("resume_blueprints")
    if items is None:
        return []
    if not isinstance(items, list):
        raise BlueprintError("resume_blueprints must be a list")
    seen, confirmed = set(), []
    for index, item in enumerate(items):
        path = f"resume_blueprints[{index}]"
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].strip():
            raise BlueprintError(f"{path}.id is required")
        if item["id"] in seen:
            raise BlueprintError(f"{path}.id is a duplicate")
        seen.add(item["id"])
        if item.get("confirmation") not in ("candidate-confirmed", "draft", "unconfirmed"):
            raise BlueprintError(f"{path}.confirmation is invalid")
        _string_list(item.get("families"), f"{path}.families")
        if not item["families"] or set(item["families"]) - set(FAMILIES):
            raise BlueprintError(f"{path}.families must name known role families")
        headline = item.get("headline")
        if (not isinstance(headline, dict) or not isinstance(headline.get("default"), str) or not headline["default"].strip()
                or set(headline) - set(HEADLINE_KEYS) or any(not isinstance(v, str) or not v.strip() for v in headline.values())):
            raise BlueprintError(f"{path}.headline needs confirmed wording under default")
        summary = item.get("summary")
        if isinstance(summary, dict):
            if (not isinstance(summary.get("default"), str) or not summary["default"].strip()
                    or set(summary) - set(HEADLINE_KEYS) or any(not isinstance(v, str) or not v.strip() for v in summary.values())):
                raise BlueprintError(f"{path}.summary needs confirmed fallback wording under default")
        elif not isinstance(summary, str) or not summary.strip():
            raise BlueprintError(f"{path}.summary needs confirmed fallback wording")
        if "summary_stack" in item and (not isinstance(item["summary_stack"], str) or not item["summary_stack"].strip()):
            raise BlueprintError(f"{path}.summary_stack must be non-empty wording")
        for field in ("summary_focus", "lead_kinds"):
            _string_list(item.get(field, []), f"{path}.{field}")
            if set(item.get(field, [])) - set(evidence.KINDS):
                raise BlueprintError(f"{path}.{field} names an unknown evidence kind")
        if item.get("pages") not in (1, 2) or isinstance(item.get("pages"), bool):
            raise BlueprintError(f"{path}.pages must be 1 or 2")
        projects = item.get("projects") or {}
        bullets = projects.get("bullets") or {}
        if (not isinstance(projects, dict) or type(projects.get("max", 1)) is not int or projects.get("max", 1) < 1
                or not isinstance(bullets, dict) or any(type(bullets.get(k, 1)) is not int or bullets.get(k, 1) < 1
                                                        for k in ("lead", "other"))):
            raise BlueprintError(f"{path}.projects limits must be positive integers")
        _string_list(projects.get("pinned", []), f"{path}.projects.pinned")
        rules = item.get("optional_companies") or {}
        if not isinstance(rules, dict) or any(not isinstance(k, str) or v not in RULES for k, v in rules.items()):
            raise BlueprintError(f"{path}.optional_companies rules must be one of {', '.join(RULES)}")
        skills = item.get("skills") or {}
        if not isinstance(skills, dict):
            raise BlueprintError(f"{path}.skills must be an object")
        for category, values in skills.items():
            if not isinstance(category, str) or not category.strip():
                raise BlueprintError(f"{path}.skills has an invalid category")
            _string_list(values, f"{path}.skills category")
        _string_list(item.get("certifications", []), f"{path}.certifications")
        for flag in ("show_project_roles", "default"):
            if flag in item and type(item[flag]) is not bool:
                raise BlueprintError(f"{path}.{flag} must be a boolean")
        if item["confirmation"] == "candidate-confirmed":
            confirmed.append(item)
    return confirmed


def enabled(profile):
    try:
        return bool(validate(profile))
    except BlueprintError:
        return False


def choose(profile, job):
    """The confirmed blueprint for the posting's role family, else the default one, else None."""
    confirmed = validate(profile)
    family = evidence.role_family(job.get("title"))
    for item in confirmed:
        if family in item["families"]:
            return item
    return next((item for item in confirmed if item.get("default") is True), None)


def headline_for(blueprint, needs):
    headline = blueprint["headline"]
    return headline.get(needs.seniority) or headline["default"]


def summary_for(blueprint, needs):
    """The confirmed fallback summary for the headline the posting gets."""
    summary = blueprint["summary"]
    if isinstance(summary, str):
        return summary
    return summary.get(needs.seniority) or summary["default"]


def career_opening(blueprint, needs, years):
    """The summary's first sentence: the headline's title, the career years and the core stack.

    ``summary_stack`` is the candidate's wording for the stack; without it the
    headline's own stack is used.
    """
    title, _, stack = headline_for(blueprint, needs).partition("|")
    words = title.split()
    title = " ".join(words[:1] + [w if w.isupper() else w.lower() for w in words[1:]])
    stack = blueprint.get("summary_stack") or re.sub(r"\s*&\s*", " and ", stack.strip())
    return f"{title} with {years}+ years in {stack}" if stack else f"{title} with {years}+ years of experience"


def focused_keywords(tech, bullets, job_text):
    """Keep only the project keywords the selected bullets or the posting actually name."""
    terms = [t.strip() for t in re.split(r"\s*[·|,]\s*", str(tech or "")) if t.strip()]
    if not terms:
        return ""
    corpus = evidence._text(" ".join(bullets))
    focused = [t for t in terms if evidence._contains(corpus, evidence._text(t)) or evidence._contains(job_text, evidence._text(t))]
    return " · ".join(focused or terms)


def _end_month(value):
    return evidence._month_value(value, last=True)


def build(profile, job, blueprint, selection):
    """A renderer payload from confirmed wording only: identity, blueprint choices and selected evidence."""
    languages = (profile.get("additional") or {}).get("languages")
    if not isinstance(languages, str) or not languages.strip():
        raise BlueprintError("Confirmed languages are required before a resume can be built")
    job_text = evidence._text(" ".join(str(job.get(k) or "") for k in ("title", "description", "tech_required")))
    payload = {k: copy.deepcopy(profile[k]) for k in IDENTITY_FIELDS if k in profile}
    payload["headline"] = headline_for(blueprint, selection.needs)
    payload["summary"] = summary_for(blueprint, selection.needs)
    payload["skills"] = copy.deepcopy(blueprint.get("skills") or {})
    known = profile.get("certifications") or []
    payload["certifications"] = [c for c in blueprint.get("certifications") or [] if c in known]
    links = profile.get("certification_links") or {}
    payload["certification_links"] = {c: links[c] for c in payload["certifications"] if c in links}
    reviewed_sidebar = bool((profile.get("resume_quality") or {}).get("reviewed_sidebar"))
    education = [{k: v for k, v in item.items() if k in EDUCATION_FIELDS}
                 for item in profile.get("education") or [] if isinstance(item, dict)]
    if reviewed_sidebar:
        # The reviewed sidebar lists degrees in their display wording; teaching and courses
        # that older snapshots kept under Education are shown under Teaching & Training.
        reviewed = [e for e in education if e.get("display_degree") or e.get("display_school")]
        education = reviewed or education
    payload["education"] = education
    payload["additional"] = {"languages": languages}
    payload["volunteering"] = resume_quality.public_volunteering(profile)

    groups = {g.get("company"): g for g in profile.get("employment_groups") or []
              if g.get("confirmation") == "candidate-reviewed"}
    standalone = {e.get("company"): e for e in profile.get("experience") or [] if isinstance(e, dict)}
    by_company = {}
    for project in selection.projects:
        by_company.setdefault(project.company, []).append(project)

    def company_key(company):
        source = groups.get(company) or standalone.get(company) or {}
        return (company not in groups, -_end_month(source.get("dates")),
                -evidence._month_value(source.get("dates"), last=False), company)

    experience = []
    for company in sorted(by_company, key=company_key):
        projects = by_company[company]
        if company in groups:
            group = groups[company]
            engagements = []
            for project in projects:
                texts = [b.text for b in project.bullets]
                entry = {"name": project.name, "dates": project.dates, "bullets": texts,
                         "tech": focused_keywords(project.tech, texts, job_text)}
                if blueprint.get("show_project_roles") and project.role:
                    entry["role"] = project.role
                engagements.append(entry)
            role = {"title": str(group.get("title") or ""), "company": company, "dates": str(group.get("dates") or ""),
                    "location": str(group.get("location") or ""), "engagements": engagements,
                    "bullets": [b for e in engagements for b in e["bullets"]]}
            if group.get("progression"):
                role["progression"] = str(group["progression"])
        else:
            source = standalone.get(company) or {}
            texts = [b.text for project in projects for b in project.bullets]
            role = {k: source[k] for k in EXPERIENCE_FIELDS if k in source}
            role.update({"company": company, "bullets": texts,
                         "tech": focused_keywords(source.get("tech"), texts, job_text)})
        experience.append(role)
    payload["experience"] = experience
    result = resume_quality.presentation(payload, profile, job)
    if reviewed_sidebar and result.get("volunteering"):
        # The reviewed sidebar shows teaching as a title and date; the last record for the
        # same organization and dates is the candidate's current wording.
        latest = {}
        for entry in result["volunteering"]:
            latest[(entry.get("organization"), entry.get("dates"))] = {**entry, "description": ""}
        result["volunteering"] = list(latest.values())
    return result
