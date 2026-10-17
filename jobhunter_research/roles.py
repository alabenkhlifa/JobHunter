"""Map a vacancy title to the fixed role families and levels used by research.

Families: senior_software_engineer, lead_software_engineer, software_architect,
backend_engineer, engineering_manager. Levels: mid, senior, lead, staff.
Patterns cover English and Spanish titles. ``backend_engineer`` is the family
for individual-contributor engineering titles without a seniority marker.
"""

from __future__ import annotations

import re
from typing import Any

ROLE_FAMILIES = (
    "senior_software_engineer",
    "lead_software_engineer",
    "software_architect",
    "backend_engineer",
    "engineering_manager",
)
LEVELS = ("mid", "senior", "lead", "staff")

_FLAGS = re.IGNORECASE
_MANAGER = re.compile(
    r"\b(?:engineering|software|development|dev|technology)\s+manager\b"
    r"|\bhead\s+of\s+(?:engineering|software|development|technology|tech)\b"
    r"|\b(?:director|vp|vice\s+president)\s+(?:of\s+)?(?:engineering|software|technology)\b"
    r"|\b(?:responsable|jefe|jefa|gerente|director[a]?)\s+de\s+(?:ingenier[ií]a|desarrollo|software|tecnolog[ií]a)\b",
    _FLAGS,
)
_ARCHITECT = re.compile(r"\barchitect\b|\barquitect[oa]\b", _FLAGS)
_STAFF = re.compile(r"\b(?:staff|principal)\b", _FLAGS)
_LEAD = re.compile(
    r"\blead(?:er)?\b|\btech(?:nical)?\s*lead(?:er)?\b|\bteam\s+lead(?:er)?\b"
    r"|\bl[ií]der(?:es)?\b|\bjef[ea]s?\s+de\s+equipo\b",
    _FLAGS,
)
_SENIOR = re.compile(r"\bs(?:e|é)nior\b|\bsr\.?(?=\s|$)|\bexpert[oa]?\b", _FLAGS)
_JUNIOR = re.compile(r"\bjunior\b|\bjr\.?(?=\s|$)|\bgraduate\b|\bintern\b|\bbecari[oa]\b|\btrainee\b", _FLAGS)
_BACKEND = re.compile(r"\bback[\s-]?end\b", _FLAGS)
_ENGINEERING = re.compile(
    r"\b(?:engineer|developer|programmer|swe|sde|ingenier[oa]|desarrollador[a]?|programador[a]?|dev)\b", _FLAGS
)


def role_level(title: str, min_experience: Any = None) -> str:
    """mid, senior, lead or staff from the title; experience lifts unmarked titles."""
    value = " ".join(str(title or "").split())
    if _STAFF.search(value):
        return "staff"
    if _LEAD.search(value) or _MANAGER.search(value):
        return "lead"
    if _SENIOR.search(value):
        return "senior"
    if _JUNIOR.search(value):
        return "mid"
    try:
        years = int(min_experience)
    except (TypeError, ValueError):
        years = -1
    return "senior" if years >= 5 else "mid"


def role_family(title: str, min_experience: Any = None) -> str | None:
    """The research role family, or None for non-engineering titles."""
    value = " ".join(str(title or "").split())
    if not value:
        return None
    if _MANAGER.search(value):
        return "engineering_manager"
    if _ARCHITECT.search(value):
        return "software_architect"
    if not (_ENGINEERING.search(value) or _LEAD.search(value) or _BACKEND.search(value)):
        return None
    level = role_level(value, min_experience)
    if level in {"lead", "staff"}:
        return "lead_software_engineer"
    if level == "senior":
        return "senior_software_engineer"
    return "backend_engineer"


def _base_role(title: str) -> str:
    import jobhunter_interest_flow as flow

    base = flow.salary_role_title(title)
    if base in {"Software Engineer", "Backend Engineer"}:
        return base
    return "Backend Engineer" if _BACKEND.search(str(title or "")) else "Software Engineer"


def search_role(title: str, family: str | None = None, level: str | None = None) -> str:
    """The role phrase salary sites use, e.g. 'Senior Software Engineer'."""
    family = family or role_family(title)
    level = level or role_level(title)
    if family == "engineering_manager":
        return "Engineering Manager"
    if family == "software_architect":
        import jobhunter_interest_flow as flow

        base = flow.salary_role_title(title)
        return base if base.endswith("Architect") else "Software Architect"
    base = _base_role(title)
    if family == "lead_software_engineer":
        prefix = "Staff" if level == "staff" and re.search(r"\bstaff\b", str(title), _FLAGS) else (
            "Principal" if level == "staff" else "Lead")
        return f"{prefix} {base}"
    if family == "senior_software_engineer" or level == "senior":
        return f"Senior {base}"
    if family is None:
        import jobhunter_interest_flow as flow

        return flow.salary_role_title(title) or "Software Engineer"
    return base


def base_role(phrase: str) -> str:
    """Drop the level word: 'Senior Software Engineer' -> 'Software Engineer'."""
    return re.sub(r"^(?:Senior|Lead|Staff|Principal)\s+", "", str(phrase or "")).strip()


def levels_fyi_path(family: str | None, level: str) -> str | None:
    """Path under levels.fyi/t/ for a family+level, Spain-wide (verified slugs only)."""
    if family == "engineering_manager":
        return "software-engineering-manager/locations/spain"
    if family == "software_architect":
        return "solution-architect/locations/spain"
    if family in {"senior_software_engineer", "lead_software_engineer"} or (
        family == "backend_engineer" and level != "mid"
    ):
        return "software-engineer/levels/senior/locations/spain"
    if family == "backend_engineer":
        return "software-engineer/locations/spain"
    return None
