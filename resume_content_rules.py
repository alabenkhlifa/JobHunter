"""Content checks shared by owner previews and application packages."""

import copy
import re

from resume_inline import normalized_text


# These are job responsibilities, not technologies to import into a resume.
TOPICS = {
    "payments": r"\b(?:payments?|checkout|billing|transaction processing)\b",
    "concurrency": r"\b(?:concurren\w*|overselling|stock reservations?|race conditions?)\b",
    "authentication": r"\b(?:oauth2?|authentication|authorization|access control)\b",
    "observability": r"\b(?:observability|monitoring|telemetry)\b",
}


def _topics(text):
    text = normalized_text(text).casefold()
    return {name for name, pattern in TOPICS.items() if re.search(pattern, text)}


def variant_evidence_gaps(profile, resume, job):
    """Report relevant approved facts outside a fixed snapshot; never import them."""
    from resume_quality import _unique_bullets, _usable

    requested = _topics(str(job.get("title") or "") + " " + str(job.get("description") or ""))
    selected = {normalized_text(b) for b in _unique_bullets(resume)}
    covered = _topics(" ".join(selected))
    missing = requested - covered
    if not missing:
        return []
    included_companies = {r.get("company") for r in resume.get("experience") or []}
    included_projects = {e.get("name") for r in resume.get("experience") or []
                         for e in r.get("engagements") or []}
    included_projects.update(r.get("subtitle") for r in resume.get("experience") or [] if r.get("subtitle"))
    permitted_expansion = (profile.get("resume_quality") or {}).get("fill_same_employer_projects") is True
    projects = [(group.get("company"), project)
                for group in profile.get("employment_groups") or []
                if group.get("confirmation") == "candidate-reviewed"
                and group.get("company") in included_companies
                for project in group.get("engagements") or []
                if permitted_expansion or project.get("name") in included_projects]
    gaps = []
    for record in _usable(profile):
        text = record["public_text"]
        topics = _topics(text) & missing
        if not topics or normalized_text(text) in selected:
            continue
        matches = [(company, project) for company, project in projects
                   if normalized_text(text) in {normalized_text(b) for b in project.get("bullets") or []}]
        if len(matches) == 1:
            company, project = matches[0]
            gaps.append({"evidence_id": record["id"], "company": company,
                         "project": project["name"], "topics": sorted(topics), "public_text": text})
    return gaps


def propose_evidence_updates(resume, gaps, *, profile=None, job=None):
    """Change only an unapproved review payload, preserving its approved source."""
    result = copy.deepcopy(resume)
    for gap in gaps:
        project = next((e for r in result.get("experience") or [] if r.get("company") == gap["company"]
                        for e in r.get("engagements") or [] if e.get("name") == gap["project"]), None)
        if project is None:
            # New projects need their approved context/opening and the measured
            # expansion path; a single isolated fact is not a safe substitute.
            continue
        project["bullets"].append(gap["public_text"])
    if profile is not None and job is not None:
        from resume_quality import _unique_bullets, project_opening_bullets
        from resume_bullet_ranking import bullet_match
        from jobhunter_interest_flow import _job_relevance_text

        budget = len(list(_unique_bullets(resume)))
        protected = {normalized_text(g["public_text"]) for g in gaps}
        candidates = []
        for role in result.get("experience") or []:
            for project in role.get("engagements") or []:
                bullets = project.get("bullets") or []
                opening = project_opening_bullets(profile, role.get("company"), project.get("name"), bullets)
                # Preserve broad context and its principal stack. A third
                # opening point such as training can yield to a new role's
                # directly relevant achievement in an unapproved proposal.
                protected.update(normalized_text(b) for b in opening[:2])
                for bullet in bullets:
                    match = bullet_match(bullet, _job_relevance_text(job), str(job.get("title") or ""), quality=True)
                    candidates.append((match.score, -len(bullet), project, bullet))
        for _, _, project, bullet in sorted(candidates, key=lambda c: c[:2]):
            if len(list(_unique_bullets(result))) <= budget:
                break
            if normalized_text(bullet) not in protected and len(project["bullets"]) > 1:
                project["bullets"].remove(bullet)
    return result


def evidence_updates_for_issues(profile, resume, issues):
    """Resolve a critic's evidence IDs to exact public facts in selected projects."""
    from resume_quality import _unique_bullets, _usable

    selected = {normalized_text(b) for b in _unique_bullets(resume)}
    projects = {(r.get("company"), p.get("name")) for r in resume.get("experience") or []
                for p in r.get("engagements") or []}
    reviewed = [(g.get("company"), p) for g in profile.get("employment_groups") or []
                if g.get("confirmation") == "candidate-reviewed"
                for p in g.get("engagements") or [] if (g.get("company"), p.get("name")) in projects]
    issues = " ".join(issues)
    updates = []
    for record in _usable(profile):
        if normalized_text(record["public_text"]) in selected:
            continue
        if not re.search(r"(?<![\w.:-])" + re.escape(record["id"]) + r"(?:(?![\w.:-])|(?=[.:](?:\s|$)))", issues):
            continue
        matches = [(company, p) for company, p in reviewed
                   if normalized_text(record["public_text"]) in {normalized_text(b) for b in p.get("bullets") or []}]
        if len(matches) == 1:
            company, project = matches[0]
            updates.append({"evidence_id": record["id"], "company": company, "project": project["name"],
                            "public_text": record["public_text"], "reason": "Independent job-specific content review"})
    return updates


def selection_review_context(profile, resume):
    """Let the independent critic compare the selection with permitted public facts."""
    from resume_quality import _unique_bullets, _usable

    selected = {normalized_text(b) for b in _unique_bullets(resume)}
    companies = {r.get("company") for r in resume.get("experience") or []}
    selected_projects = {p.get("name") for r in resume.get("experience") or []
                         for p in r.get("engagements") or []}
    selected_projects.update(r.get("subtitle") for r in resume.get("experience") or [] if r.get("subtitle"))
    projects = [p for group in profile.get("employment_groups") or []
                if group.get("confirmation") == "candidate-reviewed" and group.get("company") in companies
                for p in group.get("engagements") or [] if p.get("name") in selected_projects]
    allowed = {normalized_text(b) for p in projects for b in p.get("bullets") or []}
    return {
        "selected_projects": [{"name": p.get("name"), "bullets": p.get("bullets") or []}
                              for r in resume.get("experience") or [] for p in r.get("engagements") or []],
        "unselected_public_evidence": [{"id": e["id"], "text": e["public_text"]}
                                       for e in _usable(profile)
                                       if normalized_text(e["public_text"]) in allowed - selected
                                       and e.get("wording") != "short"],
    }


def audit_resume_content(resume, profile, job):
    from resume_quality import ResumeQualityError, project_opening_bullets

    checked = 0
    for role in resume.get("experience") or []:
        if (profile.get("resume_quality") or {}).get("group_bullets_by_client") and role.get("highlights"):
            raise ResumeQualityError("Customer work must remain beneath its project heading")
        for project in role.get("engagements") or []:
            bullets = project.get("bullets") or []
            opening = project_opening_bullets(profile, role.get("company"), project.get("name"), bullets)
            present = {normalized_text(b): b for b in bullets}
            expected = [present[normalized_text(b)] for b in opening if normalized_text(b) in present]
            if expected and bullets[:len(expected)] != expected:
                raise ResumeQualityError("Project scope must precede supplementary feature details")
            checked += 1
    return {"project_openings_verified": True, "customer_attribution_verified": True,
            "projects_checked": checked, "evidence_gaps": variant_evidence_gaps(profile, resume, job)}
