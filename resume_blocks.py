"""Candidate-approved fixed resume flavors, projects and page fitting."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Callable


class ResumeBlocksError(ValueError):
    """The fixed resume catalog or a requested selection is invalid."""


RENDERER_BASE_FIELDS = {
    "name", "email", "phone", "website", "linkedin", "location", "skills", "certifications",
    "certification_links", "education", "additional", "volunteering", "layout",
}
LAYOUT_FIELDS = {"volunteering_section_title", "teaching_certification_spacing", "compact_spacing", "education_dates_inline"}
EDUCATION_FIELDS = {"degree", "school", "location", "dates", "display_degree", "display_school"}
VOLUNTEERING_FIELDS = {"id", "title", "organization", "dates", "description", "url", "align", "vertical_inset"}


def _required_text(value: Any, path: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ResumeBlocksError(f"{path} must be non-empty text")


def _unique_ids(items: list, path: str) -> set[str]:
    result: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ResumeBlocksError(f"{path}[{index}] must be an object")
        identifier = item.get("id")
        _required_text(identifier, f"{path}[{index}].id")
        if identifier in result:
            raise ResumeBlocksError(f"{path}[{index}].id is duplicated")
        result.add(identifier)
    return result


def _confirmed(item: dict, path: str) -> None:
    if item.get("confirmation") not in ("candidate-confirmed", "draft", "unconfirmed"):
        raise ResumeBlocksError(f"{path}.confirmation is invalid")


def _text_list(value: Any, path: str, *, nonempty: bool = False) -> None:
    if not isinstance(value, list) or (nonempty and not value) or any(not isinstance(v, str) or not v.strip() for v in value):
        raise ResumeBlocksError(f"{path} must be a list of non-empty strings")


def _validate_base(base: dict) -> None:
    for field in ("name", "email", "phone", "website", "linkedin", "location"):
        if field in base and not isinstance(base[field], str):
            raise ResumeBlocksError(f"resume_blocks.base.{field} must be text")
    if "skills" in base:
        skills = base["skills"]
        if not isinstance(skills, dict):
            raise ResumeBlocksError("resume_blocks.base.skills must be an object")
        for category, values in skills.items():
            _required_text(category, "resume_blocks.base.skills category")
            _text_list(values, "resume_blocks.base.skills category")
    for field in ("certifications",):
        if field in base:
            _text_list(base[field], f"resume_blocks.base.{field}")
    if "certification_links" in base and (not isinstance(base["certification_links"], dict)
                                             or any(not isinstance(k, str) or not isinstance(v, str)
                                                    for k, v in base["certification_links"].items())):
        raise ResumeBlocksError("resume_blocks.base.certification_links must map names to links")
    if "education" in base:
        education = base["education"]
        if not isinstance(education, list) or any(not isinstance(e, dict) or set(e) - EDUCATION_FIELDS
                                                   or any(not isinstance(v, str) for v in e.values()) for e in education):
            raise ResumeBlocksError("resume_blocks.base.education has invalid renderer fields")
    if "volunteering" in base:
        volunteering = base["volunteering"]
        if not isinstance(volunteering, list) or any(not isinstance(e, dict) or set(e) - VOLUNTEERING_FIELDS
                                                       or any(not isinstance(v, (str, int, float)) for v in e.values())
                                                       for e in volunteering):
            raise ResumeBlocksError("resume_blocks.base.volunteering has invalid renderer fields")
    if "additional" in base:
        additional = base["additional"]
        languages = additional.get("languages") if isinstance(additional, dict) else None
        language_list_ok = isinstance(languages, list) and bool(languages) and all(
            (isinstance(item, str) and bool(item.strip())) or
            (isinstance(item, dict) and set(item) <= {"name", "level"}
             and isinstance(item.get("name"), str) and bool(item["name"].strip())
             and ("level" not in item or isinstance(item["level"], str))) for item in languages)
        if (not isinstance(additional, dict) or set(additional) - {"languages"}
                or not (isinstance(languages, str) and languages.strip() or language_list_ok)):
            raise ResumeBlocksError("resume_blocks.base.additional has invalid renderer fields")
    if "layout" in base:
        layout = base["layout"]
        if not isinstance(layout, dict) or set(layout) - LAYOUT_FIELDS:
            raise ResumeBlocksError("resume_blocks.base.layout has invalid renderer fields")
        if any(not isinstance(layout[key], bool) for key in
               ("teaching_certification_spacing", "compact_spacing", "education_dates_inline") if key in layout):
            raise ResumeBlocksError("resume_blocks.base.layout flags must be booleans")
        if "volunteering_section_title" in layout and not isinstance(layout["volunteering_section_title"], str):
            raise ResumeBlocksError("resume_blocks.base.layout.volunteering_section_title must be text")


def _known_id_list(value: Any, known: set[str], path: str, *, nonempty: bool) -> None:
    if (not isinstance(value, list) or (nonempty and not value)
            or any(not isinstance(identifier, str) or identifier not in known for identifier in value)
            or len(value) != len(set(value))):
        raise ResumeBlocksError(f"{path} must name unique known projects")


def _validate_catalog(profile: dict) -> dict:
    catalog = profile.get("resume_blocks")
    if not isinstance(catalog, dict):
        raise ResumeBlocksError("resume_blocks must be an object")
    if set(catalog) - {"version", "enabled", "base", "flavors", "projects", "variants", "metadata"}:
        raise ResumeBlocksError("resume_blocks has invalid catalog fields")
    if catalog.get("version") != 1 or catalog.get("enabled") is not True:
        raise ResumeBlocksError("resume_blocks must have version 1 and be enabled")
    metadata = catalog.get("metadata")
    if metadata is not None and (not isinstance(metadata, dict) or set(metadata) - {"source_pdf_sha256"}
                                or ("source_pdf_sha256" in metadata and not isinstance(metadata["source_pdf_sha256"], str))):
        raise ResumeBlocksError("resume_blocks.metadata has invalid fields")
    base = catalog.get("base")
    if (not isinstance(base, dict) or {"experience", "headline", "summary"} & set(base)
            or set(base) - RENDERER_BASE_FIELDS):
        raise ResumeBlocksError("resume_blocks.base must be public renderer fields without experience, headline or summary")
    _validate_base(base)

    flavors = catalog.get("flavors")
    projects = catalog.get("projects")
    variants = catalog.get("variants")
    if not isinstance(flavors, list) or not flavors:
        raise ResumeBlocksError("resume_blocks.flavors must be a non-empty list")
    if not isinstance(projects, list) or not projects:
        raise ResumeBlocksError("resume_blocks.projects must be a non-empty list")
    if not isinstance(variants, list) or not variants:
        raise ResumeBlocksError("resume_blocks.variants must be a non-empty list")
    flavor_ids = _unique_ids(flavors, "resume_blocks.flavors")
    project_ids = _unique_ids(projects, "resume_blocks.projects")
    variant_ids = _unique_ids(variants, "resume_blocks.variants")

    for index, flavor in enumerate(flavors):
        path = f"resume_blocks.flavors[{index}]"
        _confirmed(flavor, path)
        if set(flavor) - {"id", "label", "confirmation", "headline", "summary"}:
            raise ResumeBlocksError(f"{path} has invalid fields")
        _required_text(flavor.get("label"), f"{path}.label")
        if flavor["confirmation"] == "candidate-confirmed":
            _required_text(flavor.get("headline"), f"{path}.headline")
            _required_text(flavor.get("summary"), f"{path}.summary")
        elif any(key in flavor and not isinstance(flavor[key], str) for key in ("headline", "summary")):
            raise ResumeBlocksError(f"{path} draft wording must be text")

    employer_fields = {"title", "dates", "location", "company_url"}
    entry_fields = {"name", "dates", "role", "tech", "bullets"}
    for index, project in enumerate(projects):
        path = f"resume_blocks.projects[{index}]"
        _confirmed(project, path)
        if set(project) - {"id", "label", "confirmation", "company", "role", "entry"}:
            raise ResumeBlocksError(f"{path} has invalid fields")
        _required_text(project.get("label"), f"{path}.label")
        _required_text(project.get("company"), f"{path}.company")
        role = project.get("role")
        role_fields = employer_fields | {"subtitle", "tech", "bullets"}
        if not isinstance(role, dict) or set(role) - role_fields:
            raise ResumeBlocksError(f"{path}.role has invalid employer fields")
        _required_text(role.get("title"), f"{path}.role.title")
        _required_text(role.get("dates"), f"{path}.role.dates")
        if any(not isinstance(value, str) for key, value in role.items() if key != "bullets"):
            raise ResumeBlocksError(f"{path}.role fields must be text")
        if "bullets" in role and (not isinstance(role["bullets"], list)
                                   or any(not isinstance(bullet, str) or not bullet.strip() for bullet in role["bullets"])):
            raise ResumeBlocksError(f"{path}.role.bullets must be non-empty text")
        entry = project.get("entry")
        if entry is None:
            bullets = role.get("bullets")
            if not isinstance(bullets, list) or not bullets:
                raise ResumeBlocksError(f"{path}.role.bullets must be a non-empty list for standalone projects")
        else:
            if not isinstance(entry, dict) or set(entry) - entry_fields:
                raise ResumeBlocksError(f"{path}.entry has invalid fields")
            _required_text(entry.get("name"), f"{path}.entry.name")
            _required_text(entry.get("dates"), f"{path}.entry.dates")
            if any(not isinstance(entry.get(field, ""), str) for field in ("role", "tech")):
                raise ResumeBlocksError(f"{path}.entry role and tech must be text")
            bullets = entry.get("bullets")
            if not isinstance(bullets, list) or not bullets:
                raise ResumeBlocksError(f"{path}.entry.bullets must be a non-empty list")
        if any(not isinstance(bullet, str) or not bullet.strip() for bullet in bullets):
            raise ResumeBlocksError(f"{path} bullets must be non-empty text")
        for field in ("location", "company_url"):
            if field in role and not isinstance(role[field], str):
                raise ResumeBlocksError(f"{path}.role.{field} must be text")

    for index, variant in enumerate(variants):
        path = f"resume_blocks.variants[{index}]"
        _confirmed(variant, path)
        if set(variant) - {"id", "label", "flavor_id", "confirmation", "max_pages", "project_ids", "default_project_ids"}:
            raise ResumeBlocksError(f"{path} has invalid fields")
        _required_text(variant.get("label"), f"{path}.label")
        if variant.get("flavor_id") not in flavor_ids:
            raise ResumeBlocksError(f"{path}.flavor_id is unknown")
        if variant.get("max_pages") not in (1, 2) or isinstance(variant.get("max_pages"), bool):
            raise ResumeBlocksError(f"{path}.max_pages must be 1 or 2")
        allowed = variant.get("project_ids")
        defaults = variant.get("default_project_ids")
        _known_id_list(allowed, project_ids, f"{path}.project_ids", nonempty=True)
        _known_id_list(defaults, set(allowed), f"{path}.default_project_ids", nonempty=True)
        if not isinstance(defaults, list):
            raise ResumeBlocksError(f"{path}.default_project_ids must name allowed projects")
    return catalog


def enabled(profile: dict) -> bool:
    opted_in = isinstance(profile, dict) and isinstance(profile.get("resume_blocks"), dict) \
        and profile["resume_blocks"].get("enabled") is True
    if not opted_in:
        return False
    catalog(profile)  # An opted-in but malformed catalog is an error, never a legacy fallback.
    return True


def catalog(profile: dict) -> dict:
    """Validate and return the fixed-block catalog."""
    return _validate_catalog(profile)


def build(profile: dict, selection: dict) -> dict:
    """Build a renderer payload from the exact candidate-confirmed blocks."""
    data = catalog(profile)
    if not isinstance(selection, dict):
        raise ResumeBlocksError("A flavor, variant and project selection is required")
    flavors = {item["id"]: item for item in data["flavors"] if item["confirmation"] == "candidate-confirmed"}
    variants = {item["id"]: item for item in data["variants"] if item["confirmation"] == "candidate-confirmed"}
    projects = {item["id"]: item for item in data["projects"] if item["confirmation"] == "candidate-confirmed"}
    flavor_id, variant_id, project_ids = (selection.get("flavor_id"), selection.get("variant_id"), selection.get("project_ids"))
    if flavor_id not in flavors:
        raise ResumeBlocksError("Choose a candidate-confirmed resume flavor")
    if variant_id not in variants:
        raise ResumeBlocksError("Choose a candidate-confirmed project variant")
    variant = variants[variant_id]
    if variant["flavor_id"] != flavor_id:
        raise ResumeBlocksError("The selected project variant does not belong to the chosen flavor")
    if not isinstance(project_ids, list) or not project_ids:
        raise ResumeBlocksError("Choose at least one project")
    if any(not isinstance(value, str) for value in project_ids) or len(project_ids) != len(set(project_ids)):
        raise ResumeBlocksError("Project choices must be unique IDs")
    allowed = set(variant["project_ids"])
    if set(project_ids) - allowed:
        raise ResumeBlocksError("A selected project is not allowed by this variant")
    if set(project_ids) - projects.keys():
        raise ResumeBlocksError("A selected project is not candidate-confirmed")

    payload = copy.deepcopy(data["base"])
    flavor = flavors[flavor_id]
    payload["headline"], payload["summary"] = flavor["headline"], flavor["summary"]
    by_company: dict[str, dict] = {}
    for project_id in project_ids:
        project = projects[project_id]
        role_data = project["role"]
        company = project["company"]
        role = by_company.get(company)
        if role is None:
            role = {key: copy.deepcopy(value) for key, value in role_data.items() if key != "bullets"}
            role.update({"company": company, "bullets": [], "engagements": []})
            by_company[company] = role
        elif any(role.get(key) != value for key, value in role_data.items() if key != "bullets"):
            raise ResumeBlocksError(f"Selected projects for {company} have conflicting employer details")
        entry = project.get("entry")
        if entry is None:
            role["bullets"].extend(copy.deepcopy(role_data["bullets"]))
        else:
            engagement = copy.deepcopy(entry)
            role["engagements"].append(engagement)
            role["bullets"].extend(copy.deepcopy(engagement["bullets"]))
    payload["experience"] = list(by_company.values())
    return payload


def content_digest(profile: dict, selection: dict) -> str:
    data = catalog(profile)
    selected = build(profile, selection)
    return hashlib.sha256(json.dumps({"catalog": data, "selection": selection, "payload": selected},
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _metrics(result: Any) -> tuple[int, list[float]]:
    if isinstance(result, dict):
        pages = int(result.get("pages") or 0)
        fills = [float(value) for value in result.get("main_fills") or []]
    else:
        pages, fills = int(result), []
    if pages < 1:
        raise ResumeBlocksError("The resume renderer returned an invalid page count")
    return pages, fills


def fit(payload: dict, render: Callable[[dict], Any], max_pages: int) -> tuple[dict, dict]:
    """Fit with spacing only; preserve all approved content and reject overflow."""
    if max_pages not in (1, 2) or isinstance(max_pages, bool):
        raise ResumeBlocksError("Maximum pages must be 1 or 2")
    result = copy.deepcopy(payload)
    pages, fills = _metrics(render(result))
    sparse = pages == 2 and bool(fills) and fills[-1] < 0.5
    compact = False
    if pages > max_pages or sparse:
        result.setdefault("layout", {})["compact_spacing"] = True
        pages, fills = _metrics(render(result))
        compact = True
    if pages > max_pages:
        raise ResumeBlocksError(
            f"The approved resume content uses {pages} pages, above the selected {max_pages}-page limit. "
            "Choose a different approved project variant or confirm a shorter fixed block."
        )
    sparse = pages == 2 and bool(fills) and fills[-1] < 0.5
    return result, {"target_pages": max_pages, "pages": pages, "main_fills": fills,
                    "compact": compact, "page_two_sparse": sparse,
                    "action_warning": "Page two is under half full; review the rendered resume." if sparse else ""}
