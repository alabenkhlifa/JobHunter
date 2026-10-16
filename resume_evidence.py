"""Structured, candidate-confirmed evidence and deterministic selection for one posting.

Every bullet a resume can show is an ``evidence_bank`` item whose exact
``public_text`` the candidate confirmed. ``facets`` describe that text without
changing it: which project it belongs to, what kind of work it shows, the
stack and domains it names. Selection here only picks and orders confirmed
items; it never composes wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from resume_inline import normalized_text, plain_text

KINDS = ("ownership", "architecture", "delivery", "infrastructure", "integration", "performance",
         "reliability", "security", "leadership", "quality", "ai_tooling", "migration", "product", "other")
# A project opens with scope: what was owned or designed, before features and tooling.
OPENING_TIERS = (("ownership", "architecture"), ("delivery", "infrastructure"))
KIND_BASE = {"ownership": 22, "architecture": 22, "delivery": 14, "infrastructure": 14, "integration": 14,
             "reliability": 14, "migration": 12, "performance": 12, "security": 12, "leadership": 12,
             "quality": 8, "product": 6, "ai_tooling": 5, "other": 0}
NEED_PATTERNS = {
    "leadership": r"\b(?:lead(?:ing|er|ership)?|mentor\w*|coach\w*|manag\w* (?:a |the |an? \w+ )?team|team lead|line manag\w*|grow(?:ing)? the team|supervis\w*)\b",
    "ownership": r"\b(?:own\w*|end[- ]to[- ]end|accountab\w*|responsib\w* for)\b",
    "architecture": r"\b(?:architect\w*|system design|solution design|api design|technical direction|technical vision|adrs?|rfcs?|design patterns)\b",
    "delivery": r"\b(?:deliver\w*|ship\w*|features?|develop\w*|implement\w*)\b",
    "infrastructure": r"\b(?:cloud|aws|azure|gcp|kubernetes|terraform|infrastructure|ci/cd|devops|deploy\w*|iac|pipelines?)\b",
    "integration": r"\b(?:integrat\w*|apis?|kafka|messag\w*|event[- ]driven|webhooks?|rest|grpc|mqtt|streaming)\b",
    "performance": r"\b(?:performance|scalab\w*|latency|throughput|optimi[sz]\w*|efficien\w*|high[- ]load)\b",
    "reliability": r"\b(?:reliab\w*|resilien\w*|availability|incident\w*|on[- ]call|zero[- ]downtime|correctness|consisten\w*|idempoten\w*|troubleshoot\w*)\b",
    "security": r"\b(?:security|secure|oauth2?|authenticat\w*|authori[sz]\w*|compliance|gdpr|privacy)\b",
    "quality": r"\b(?:test\w*|tdd|code reviews?|quality|sonar\w*|clean code)\b",
    "ai_tooling": r"\b(?:ai[- ]assisted|copilot|claude|llms?|agentic|gen(?:erative)? ?ai|ai[- ]native)\b",
    "migration": r"\b(?:migrat\w*|moderni[sz]\w*|legacy|upgrad\w*)\b",
    "product": r"\b(?:product|customers?|business|growth|stakeholders?)\b",
}
FAMILY_KINDS = {
    "architect": {"architecture": 20, "ownership": 15, "infrastructure": 10, "integration": 6},
    "lead": {"leadership": 20, "ownership": 10, "delivery": 6},
    "backend": {"ownership": 10, "delivery": 8, "reliability": 6},
    "platform": {"infrastructure": 20, "ownership": 10, "reliability": 6},
    "other": {},
}
DOMAIN_PATTERNS = {
    "industrial": r"\b(?:industrial|iot|mes|manufacturing|factory|factories|plants?|machines?|automation)\b",
    "automotive": r"\b(?:automotive|cars?|vehicles?|dealers?|mobility)\b",
    "payments": r"\b(?:payments?|payouts?|fintech|banking|banks?|transactions?|trading|checkout|billing)\b",
    "agriculture": r"\b(?:agricultur\w*|farm\w*|agri\w*)\b",
    "advertising": r"\b(?:advertis\w*|media|signage|screens?)\b",
    "insurance": r"\b(?:insurance|insurer|reinsurance)\b",
    "retail": r"\b(?:retail|e-?commerce|marketplace|basket)\b",
    "public_sector": r"\b(?:government|public sector|ngo|development cooperation)\b",
    "life_sciences": r"\b(?:life sciences|pharma\w*|health\w*|clinical)\b",
    "telecom": r"\b(?:telecom\w*|operators?)\b",
}
ROLE_FAMILIES = (
    ("architect", r"\barchitect(?:ure)?\b"),
    ("lead", r"\b(?:lead|leader|tech lead|technical lead|head|principal|staff)\b"),
    ("platform", r"\bplatform\b"),
    ("backend", r"\b(?:backend|back-end|back end|java|kotlin|developer|engineer)\b"),
)
STACK_ALIASES = {
    "node": ("node.js", "nodejs", "node js"), "kubernetes": ("k8s",), "postgresql": ("postgres",),
    "azure api management": ("apim",), "github actions": ("github actions ci/cd",),
    "aws": ("amazon web services",), "spring boot": ("springboot",), "typescript": ("ts",),
    "docker compose": ("docker-compose",), "event hubs": ("eventhub", "event hub"),
}
MONTHS = {name: index for index, name in enumerate(
    ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"), 1)}
MONTHS.update({name[:3]: index for name, index in list(MONTHS.items())})
MONTHS["sept"] = 9
FIGURE_PATTERN = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?[%+]?)")


@dataclass(frozen=True)
class EvidenceItem:
    id: str
    text: str
    experience_id: str
    company: str
    project: str
    project_dates: str
    role: str
    tech: str
    kind: str
    stack: tuple
    domains: tuple
    figures: tuple
    group: str
    recency: int
    wording: str


@dataclass
class PostingNeeds:
    family: str
    seniority: str
    stack: set
    kinds: dict
    domains: set
    leadership: bool


@dataclass
class Project:
    name: str
    company: str
    dates: str
    role: str
    tech: str
    bullets: list
    score: float


@dataclass
class Selection:
    needs: PostingNeeds
    projects: list
    scores: dict = field(default_factory=dict)


def _text(value):
    return " ".join(re.sub(r"[^a-z0-9+#./-]+", " ", str(value or "").lower()).split())


def _contains(text, term):
    return re.search(r"(?<![a-z0-9+#])" + re.escape(term) + r"(?![a-z0-9+#])", text) is not None


def role_family(title):
    text = str(title or "").lower()
    for family, pattern in ROLE_FAMILIES:
        if re.search(pattern, text):
            return family
    return "other"


def seniority(title):
    text = str(title or "").lower()
    if re.search(r"\b(?:architect|principal|staff|head|cto)\b", text):
        return "architect"
    if re.search(r"\b(?:lead|leader)\b", text):
        return "lead"
    if re.search(r"\bsenior\b", text):
        return "senior"
    return "unspecified"


def _month_value(text, *, last):
    text = str(text or "")
    if last and re.search(r"\b(?:present|current)\b", text, re.I):
        today = date.today()
        return today.year * 12 + today.month - 1
    numeric = re.findall(r"\b(0?[1-9]|1[0-2])/(\d{4})\b", text)
    if numeric:
        month, year = numeric[-1 if last else 0]
        return int(year) * 12 + int(month) - 1
    named = re.findall(r"\b([A-Za-z]+)\.?\s+(\d{4})\b", text)
    named = [(m, y) for m, y in named if m.lower() in MONTHS]
    if named:
        month, year = named[-1 if last else 0]
        return int(year) * 12 + MONTHS[month.lower()] - 1
    years = re.findall(r"\b((?:19|20)\d{2})\b", text)
    if years:
        return int(years[-1 if last else 0]) * 12 + (11 if last else 0)
    return 0


def _recency_points(end_month):
    if not end_month:
        return 0
    today = date.today()
    years = (today.year * 12 + today.month - 1 - end_month) / 12
    return 6 if years < 1 else 5 if years < 2 else 4 if years < 3 else 3 if years < 4 else 2 if years < 6 else 1


def figures(text):
    return tuple(dict.fromkeys(f for f in FIGURE_PATTERN.findall(plain_text(text)) if len(f) >= 2))


def _groups(records):
    parent = {}

    def find(value):
        while parent.setdefault(value, value) != value:
            value = parent[value]
        return value

    for record in records:
        for linked in record.get("source_evidence_ids") or []:
            parent[find(record["id"])] = find(linked)
    achievement = {}
    for record in records:
        if record.get("achievement_id"):
            achievement[find(record["id"])] = record["achievement_id"]
    return {record["id"]: achievement.get(find(record["id"]), find(record["id"])) for record in records}


def catalogue(profile):
    """Confirmed, public, resume-visible evidence with its facets; untagged items rank last."""
    experiences = {e.get("id"): e for e in profile.get("experience") or [] if isinstance(e, dict)}
    engagements = {}
    for group in profile.get("employment_groups") or []:
        if group.get("confirmation") != "candidate-reviewed":
            continue
        for engagement in group.get("engagements") or []:
            engagements[(group.get("company"), engagement.get("name"))] = engagement
    records = [e for e in profile.get("evidence_bank") or [] if isinstance(e, dict)
               and e.get("confirmation") == "candidate-confirmed" and e.get("confidentiality") == "public"
               and "resume" in (e.get("visibility") or [])]
    groups = _groups(records)
    items = []
    for record in records:
        experience = experiences.get(record.get("experience_id")) or {}
        company = str(experience.get("company") or "")
        facets = record.get("facets") or {}
        project = str(facets.get("project") or company)
        engagement = engagements.get((company, project)) or {}
        dates = str(engagement.get("dates") or experience.get("dates") or "")
        role = str(engagement.get("role") or facets.get("role") or "")
        tech = engagement.get("tech") or experience.get("tech") or ""
        if isinstance(tech, list):
            tech = ", ".join(str(t) for t in tech)
        items.append(EvidenceItem(
            id=record["id"], text=record["public_text"], experience_id=str(record.get("experience_id") or ""),
            company=company, project=project, project_dates=dates, role=role, tech=str(tech),
            kind=facets.get("kind") if facets.get("kind") in KINDS else "other",
            stack=tuple(dict.fromkeys(_text(s) for s in facets.get("stack") or [] if _text(s))),
            domains=tuple(dict.fromkeys(str(d).lower() for d in facets.get("domains") or [])),
            figures=figures(record["public_text"]), group=groups[record["id"]],
            recency=_month_value(dates, last=True), wording=str(record.get("wording") or "full")))
    return items


def posting_needs(job, items=()):
    """What the posting asks for, in the catalogue's own vocabulary."""
    title = str(job.get("title") or "")
    text = _text(" ".join(str(job.get(k) or "") for k in ("title", "description", "tech_required", "tech_nice_to_have")))
    family = role_family(title)
    vocabulary = {term for item in items for term in item.stack}
    stack = set()
    for term in vocabulary:
        if _contains(text, term) or any(_contains(text, alias) for alias in STACK_ALIASES.get(term, ())):
            stack.add(term)
    kinds = dict(FAMILY_KINDS.get(family, {}))
    for kind, pattern in NEED_PATTERNS.items():
        count = len(re.findall(pattern, text))
        if count:
            kinds[kind] = kinds.get(kind, 0) + min(3, count) * 8
    domains = {name for name, pattern in DOMAIN_PATTERNS.items() if re.search(pattern, text)}
    leadership = family in ("lead", "architect") or kinds.get("leadership", 0) >= 20
    return PostingNeeds(family=family, seniority=seniority(title), stack=stack, kinds=kinds, domains=domains,
                        leadership=leadership)


def score(item, needs, blueprint):
    value = KIND_BASE.get(item.kind, 0) + needs.kinds.get(item.kind, 0)
    if item.kind in (blueprint.get("lead_kinds") or []):
        value += 10
    value += min(48, 12 * len(set(item.stack) & needs.stack))
    value += min(20, 10 * len(set(item.domains) & needs.domains))
    if item.figures:
        value += 4
    return value + _recency_points(item.recency)


def _company_allowed(company, rule, items, needs):
    if rule == "never":
        return False
    if rule == "if_leadership":
        return needs.leadership
    if rule == "if_relevant":
        return any(set(item.stack) & needs.stack for item in items)
    return True


def _opening(candidates):
    for tier in OPENING_TIERS:
        tiered = [c for c in candidates if c.kind in tier]
        if tiered:
            return tiered[0]
    return candidates[0]


def _pick(candidates, scores, cap):
    """Scope first, then the strongest distinct work; one figure, one wording per fact."""
    if not candidates or cap <= 0:
        return []
    opening = _opening(candidates)
    chosen, used_kinds, used_figures, used_groups = [opening], {opening.kind}, set(opening.figures), {opening.group}
    remaining = [c for c in candidates if c is not opening]
    while remaining and len(chosen) < cap:
        def effective(candidate):
            return scores[candidate.id] * (0.6 if candidate.kind in used_kinds else 1.0)
        best = max(remaining, key=effective)
        remaining.remove(best)
        if best.group in used_groups or set(best.figures) & used_figures:
            continue
        chosen.append(best)
        used_kinds.add(best.kind)
        used_figures.update(best.figures)
        used_groups.add(best.group)
    return chosen


def select(profile, job, blueprint):
    """Deterministic selection of confirmed evidence for a posting under a role blueprint."""
    items = catalogue(profile)
    needs = posting_needs(job, items)
    scores = {item.id: score(item, needs, blueprint) for item in items}
    by_company = {}
    for item in items:
        by_company.setdefault(item.company, []).append(item)
    rules = blueprint.get("optional_companies") or {}
    allowed = {company for company, members in by_company.items()
               if _company_allowed(company, rules.get(company, "always"), members, needs)}
    by_project = {}
    for item in items:
        if item.company in allowed:
            by_project.setdefault((item.company, item.project), []).append(item)
    projects = []
    for (company, name), members in by_project.items():
        # Keep the strongest wording of each confirmed fact before ranking.
        best = {}
        for member in sorted(members, key=lambda m: (-scores[m.id], m.wording != "full", m.id)):
            best.setdefault(member.group, member)
        candidates = sorted(best.values(), key=lambda m: (-scores[m.id], m.id))
        ranked = sorted((scores[c.id] for c in candidates), reverse=True)
        value = ranked[0] + (0.25 * ranked[1] if len(ranked) > 1 else 0)
        sample = candidates[0]
        projects.append(Project(name=name, company=company, dates=sample.project_dates, role=sample.role,
                                tech=sample.tech, bullets=candidates, score=value))
    if not projects:
        return Selection(needs=needs, projects=[], scores=scores)
    limits = blueprint.get("projects") or {}
    pinned = list(limits.get("pinned") or [])
    strongest = max(p.score for p in projects)
    kept = [p for p in projects if p.name in pinned or p.score >= 0.3 * strongest]
    kept.sort(key=lambda p: (p.name not in pinned, pinned.index(p.name) if p.name in pinned else 0,
                             -p.score, -_month_value(p.dates, last=True), p.name))
    kept = kept[:int(limits.get("max") or len(kept))]
    caps = limits.get("bullets") or {}
    for index, project in enumerate(kept):
        cap = int(caps.get("lead" if index == 0 else "other") or 3)
        project.bullets = _pick(project.bullets, scores, cap)
    return Selection(needs=needs, projects=[p for p in kept if p.bullets], scores=scores)
