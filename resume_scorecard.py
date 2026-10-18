"""The resume rubric in code: dimensions, flags and a score instead of a refusal.

Only truth and the PDF audit remain hard stops elsewhere. Everything here is a
measured judgement the candidate (or the autopilot policy) can act on.
"""

from __future__ import annotations

import re
from collections import Counter

import resume_evidence as evidence
from resume_inline import normalized_text, plain_text

THRESHOLD = 60
DIMENSIONS = ("relevance", "bullet_strength", "repetition", "role_family_fit", "summary_quality", "page_use")
OWNERSHIP_VERBS = re.compile(
    r"\b(?:owned|owner|architected|designed|led|lead|built|delivered|migrated|moved|set up|secured|prevented|cut|"
    r"sped up|reduced|grew|fixed|trained|mentored|onboarded|shipped|developed|implemented|integrated|reverse-engineered|"
    r"hardened|established|coordinated|managed|operated|deployed|maintained)\b", re.I)
OUTCOME_MARKERS = re.compile(
    r"\b(?:zero[- ]downtime|no incidents|without|cut|reduced|faster|eliminat\w*|prevent\w*|cannot|kept working|"
    r"went live|accurate|estimated|serving|covering|resolv\w*)\b|\d", re.I)
LEVELS = {"unspecified": 0, "senior": 1, "lead": 2, "architect": 3}


def _bullets(payload):
    for role in payload.get("experience") or []:
        for entry in role.get("engagements") or [role]:
            yield from entry.get("bullets") or []


def _band(value, bands):
    """bands: ((threshold, score), ...) descending; returns the first score whose threshold is met."""
    for threshold, score in bands:
        if value >= threshold:
            return score
    return 1


def _relevance(payload, needs, kinds_by_text, catalogue):
    flags = []
    vocabulary = {term for item in catalogue for term in item.stack}
    corpus = evidence._text(" ".join([payload.get("headline", ""), *_bullets(payload),
                                       *(s for v in (payload.get("skills") or {}).values() for s in (v if isinstance(v, list) else [v])),
                                       *(str(e.get("tech") or "") for r in payload.get("experience") or [] for e in (r.get("engagements") or [r]))]))
    present = {term for term in vocabulary if evidence._contains(corpus, term)}
    stack_score = 3
    if needs.stack:
        ratio = len(needs.stack & present) / len(needs.stack)
        stack_score = _band(ratio, ((0.8, 5), (0.6, 4), (0.45, 3), (0.3, 2)))
        missing = sorted(needs.stack - present)
        if missing:
            flags.append("posting stack not shown: " + ", ".join(missing))
    top = [k for k, _ in sorted(needs.kinds.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
    selected = {kinds_by_text.get(normalized_text(b), "other") for b in _bullets(payload)}
    kind_score = 3
    if top:
        covered = sum(1 for k in top if k in selected)
        kind_score = {3: 5, 2: 4, 1: 3, 0: 1}[min(3, covered) if len(top) == 3 else round(covered * 3 / len(top))]
        uncovered = [k for k in top if k not in selected]
        if uncovered:
            flags.append("posting needs without evidence: " + ", ".join(uncovered))
    return round((stack_score + kind_score) / 2), flags


def _bullet_strength(payload, catalogue):
    vocabulary = {term for item in catalogue for term in item.stack}
    weak, total = [], 0
    for bullet in _bullets(payload):
        total += 1
        text = plain_text(bullet)
        corpus = evidence._text(text)
        has_verb = bool(OWNERSHIP_VERBS.search(text))
        has_outcome = bool(OUTCOME_MARKERS.search(text))
        has_stack = any(evidence._contains(corpus, term) for term in vocabulary)
        if not (has_verb and (has_outcome or has_stack)):
            weak.append(text)
    if not total:
        return 1, ["no bullets"]
    score = _band((total - len(weak)) / total, ((0.8, 5), (0.6, 4), (0.4, 3), (0.2, 2)))
    return score, [f"weak bullet: {w[:70]}" for w in weak[:3]]


def _repetition(payload):
    flags = []
    bullets = list(_bullets(payload))
    counts = Counter(f for b in bullets for f in set(evidence.figures(b)))
    repeated = sorted(f for f, n in counts.items() if n > 1)
    for figure in repeated:
        flags.append(f"repeated figure {figure} in bullets")
    exact = [b for b, n in Counter(normalized_text(b) for b in bullets).items() if n > 1]
    flags.extend(f"duplicate bullet: {b[:60]}" for b in exact)
    summary_figures = set(evidence.figures(payload.get("summary") or ""))
    shared = summary_figures & set(counts)
    extra = max(0, len(shared) - 4)
    if extra:
        flags.append("summary repeats bullet figures: " + ", ".join(sorted(shared)))
    score = 5 - 2 * len(repeated) - 2 * len(exact) - extra
    return max(1, score), flags


def _family_fit(payload, needs, blueprint):
    if needs.family not in (blueprint.get("families") or []):
        if needs.family == "other":
            return 3, ["role family: posting unclassified, blueprint " + blueprint["id"]]
        return 1, [f"role family: posting {needs.family}, blueprint {blueprint['id']}"]
    posting = LEVELS.get(needs.seniority, 0)
    headline = LEVELS.get(evidence.seniority(payload.get("headline")), 0)
    if posting and headline and headline > posting + 1:
        return 3, ["headline a level above the posting"]
    if posting and headline and headline != posting:
        return 4, []
    return 5, []


def _summary_quality(payload, needs, summary):
    flags = []
    text = evidence._text(payload.get("summary") or "")
    mentioned = sum(1 for term in needs.stack if evidence._contains(text, term))
    score = 5 if mentioned >= 2 else 4 if mentioned == 1 else 3
    if summary.get("source") == "fallback":
        score = min(score, 3)
        flags.append("summary: fallback wording used" + (f" ({summary.get('reason')})" if summary.get("reason") else ""))
    if summary.get("review_verdict") == "blocked":
        if summary.get("source") != "fallback":
            score = max(1, score - 1)
        flags.append("summary review blocked: " + "; ".join(str(f)[:120] for f in summary.get("flags") or [])[:300])
    return score, flags


def _page_use(layout, blueprint):
    target = int(blueprint.get("pages") or 1)
    pages, fills = int(layout.get("pages") or 1), list(layout.get("main_fills") or [])
    if pages > target:
        return 1, [f"overflow: {pages} pages for a {target}-page blueprint"]
    last = fills[-1] if fills else 0
    if pages == 1:
        score = _band(last, ((0.85, 5), (0.7, 4), (0.55, 3)))
        return score, ([] if score >= 4 else [f"page one only {last:.0%} filled"])
    score = _band(last, ((0.5, 5), (0.35, 4), (0.2, 3)))
    return score, ([] if score >= 4 else [f"page two under-used ({last:.0%})"])


def score_package(payload, profile, needs, blueprint, *, layout, summary, audit):
    """Dimensions 1-5, a 0-100 score, flags, and whether a human should look before it is used."""
    catalogue = evidence.catalogue(profile)
    kinds_by_text = {normalized_text(item.text): item.kind for item in catalogue}
    dimensions, flags, critical = {}, [], []
    dimensions["relevance"], extra = _relevance(payload, needs, kinds_by_text, catalogue)
    flags.extend(extra)
    dimensions["bullet_strength"], extra = _bullet_strength(payload, catalogue)
    flags.extend(extra)
    dimensions["repetition"], extra = _repetition(payload)
    flags.extend(extra)
    dimensions["role_family_fit"], extra = _family_fit(payload, needs, blueprint)
    flags.extend(extra)
    if dimensions["role_family_fit"] == 1:
        critical.extend(extra)
    dimensions["summary_quality"], extra = _summary_quality(payload, needs, summary)
    flags.extend(extra)
    dimensions["page_use"], extra = _page_use(layout, blueprint)
    flags.extend(extra)
    if dimensions["page_use"] == 1:
        critical.extend(extra)
    if not audit.get("pdf_text_verified"):
        critical.append("PDF audit failed")
        flags.append("PDF audit failed")
    for project in layout.get("dropped_projects") or []:
        flags.append(f"dropped project to fit: {project}")
    score = round(sum(dimensions.values()) / len(DIMENSIONS) * 20)
    return {"score": score, "dimensions": dimensions, "flags": flags, "critical": critical,
            "needs_review": score < THRESHOLD or bool(critical), "threshold": THRESHOLD}
