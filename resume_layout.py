"""Page planning: make the resume fit its blueprint's page target by trimming the weakest evidence.

Order of attempts: tighter spacing, then listed irrelevant certifications, then
the lowest-scored non-opening bullets, then whole weak projects. A two-page
target falls back to one page when page two would stay mostly empty. Wording
is never changed; the input payload is never mutated.
"""

from __future__ import annotations

import copy

MIN_PAGE_TWO_FILL = 0.5


def _entries(payload):
    """(role, entry) pairs whose ``bullets`` lists hold the resume's evidence."""
    for role in payload.get("experience") or []:
        for entry in role.get("engagements") or [role]:
            yield role, entry


def _bullets(payload):
    return [b for _, entry in _entries(payload) for b in entry.get("bullets") or []]


def _measure(render, payload):
    result = render(payload)
    pages = int(result["pages"]) if isinstance(result, dict) else int(result)
    fills = list(result.get("main_fills") or []) if isinstance(result, dict) else []
    if not fills and isinstance(result, dict) and result.get("main_fill") is not None:
        fills = [float(result["main_fill"])]
    return pages, fills


def _sync_role_bullets(role):
    if role.get("engagements"):
        role["bullets"] = [b for e in role["engagements"] for b in e.get("bullets") or []]


def _drop_weakest(payload, scores, openings, report):
    """Remove the single weakest droppable thing; return False when nothing can go."""
    candidates = []
    roles = payload.get("experience") or []
    project_count = sum(len(role.get("engagements") or [role]) for role in roles)
    for role in roles:
        for entry in role.get("engagements") or [role]:
            bullets = entry.get("bullets") or []
            droppable = [b for b in bullets if b not in openings]
            if droppable and len(bullets) > 1:
                weakest = min(droppable, key=lambda b: (scores.get(b, 0), b))
                candidates.append((scores.get(weakest, 0), 0, weakest, role, entry, None))
            if project_count > 1:
                strongest = max((scores.get(b, 0) for b in bullets), default=0)
                candidates.append((strongest, 1, None, role, entry, entry.get("name") or role.get("company")))
    if not candidates:
        return False
    score, _, bullet, role, entry, project = min(candidates, key=lambda c: (c[0], c[1]))
    if bullet is not None:
        entry["bullets"] = [b for b in entry["bullets"] if b != bullet]
        report["dropped"].append(bullet)
    else:
        if role.get("engagements"):
            role["engagements"] = [e for e in role["engagements"] if e is not entry]
            if not role["engagements"]:
                payload["experience"] = [r for r in payload["experience"] if r is not role]
        else:
            payload["experience"] = [r for r in payload["experience"] if r is not role]
        report["dropped_projects"].append(project)
    _sync_role_bullets(role)
    return True


def _shrink(payload, render, scores, openings, target, report, omittable):
    pages, fills = _measure(render, payload)
    if pages > target:
        payload.setdefault("layout", {})["compact_spacing"] = True
        report["compact"] = True
        pages, fills = _measure(render, payload)
    for cert in omittable:
        if pages <= target:
            break
        if cert in (payload.get("certifications") or []):
            payload["certifications"].remove(cert)
            (payload.get("certification_links") or {}).pop(cert, None)
            report["omitted_certifications"].append(cert)
            pages, fills = _measure(render, payload)
    while pages > target:
        if not _drop_weakest(payload, scores, openings, report):
            report["note"] = f"overflow: {pages} pages for a {target}-page target"
            break
        pages, fills = _measure(render, payload)
    return pages, fills


def fit_pages(payload, render, scores, *, pages, openings=(), omittable_certifications=(), min_page_two_fill=MIN_PAGE_TWO_FILL):
    """Return (fitted payload, report). ``scores`` and ``openings`` are keyed by bullet text."""
    work = copy.deepcopy(payload)
    openings = set(openings)
    report = {"target_pages": pages, "dropped": [], "dropped_projects": [], "omitted_certifications": [],
              "compact": False, "note": ""}
    measured, fills = _shrink(work, render, scores, openings, pages, report, list(omittable_certifications))
    if pages == 2 and measured == 2 and fills and fills[-1] < min_page_two_fill:
        trial, trial_report = copy.deepcopy(work), copy.deepcopy(report)
        trial_pages, trial_fills = _shrink(trial, render, scores, openings, 1, trial_report, list(omittable_certifications))
        if trial_pages == 1:
            work, report, measured, fills = trial, trial_report, trial_pages, trial_fills
            report["note"] = "page two under-used"
    if report["compact"] and measured <= pages:
        trial = copy.deepcopy(work)
        trial["layout"].pop("compact_spacing", None)
        if not trial["layout"]:
            trial.pop("layout")
        trial_pages, trial_fills = _measure(render, trial)
        if trial_pages == measured and (trial_pages < 2 or not trial_fills or trial_fills[-1] >= min_page_two_fill):
            work, fills, report["compact"] = trial, trial_fills, False
    report["pages"], report["main_fills"] = measured, fills
    return work, report
