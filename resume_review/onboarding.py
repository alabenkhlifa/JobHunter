"""Copy existing public wording into a single draft resume for candidate review."""

import copy

from resume_blocks import EDUCATION_FIELDS, RENDERER_BASE_FIELDS, catalog as validate_catalog


def single_resume_catalog(profile):
    base = {key: copy.deepcopy(value) for key, value in profile.items() if key in RENDERER_BASE_FIELDS}
    for field in ("name", "email", "phone", "website", "linkedin", "location"):
        base.setdefault(field, "")
    for field in ("education", "certifications", "volunteering"):
        base.setdefault(field, [])
    base.setdefault("skills", {})
    # The fixed renderer only accepts languages in additional; other profile data
    # remains in the untouched master profile.
    if "additional" in base:
        additional = base.pop("additional")
        if "languages" in additional:
            base["additional"] = {"languages": additional["languages"]}
        if not base.get("website") and isinstance(additional.get("github"), str):
            base["website"] = additional["github"]
    if "education" in base:
        base["education"] = [{k: v for k, v in item.items() if k in EDUCATION_FIELDS}
                             for item in base["education"]]
    projects = []
    for index, role in enumerate(profile.get("experience", [])):
        details = {key: copy.deepcopy(role[key]) for key in
                   ("title", "dates", "location", "company_url", "subtitle", "tech") if key in role}
        if isinstance(details.get("tech"), list):
            details["tech"] = ", ".join(details["tech"])
        engagements = role.get("engagements") or [None]
        for unit, entry in enumerate(engagements):
            project = {"id": f"experience-{index + 1}-{unit + 1}", "confirmation": "draft",
                       "label": entry["name"] if entry else role["company"],
                       "company": role["company"], "role": copy.deepcopy(details), "entry": None}
            if entry:
                project["entry"] = {key: copy.deepcopy(entry[key]) for key in
                                    ("name", "dates", "role", "tech", "bullets") if key in entry}
            else:
                project["role"]["bullets"] = copy.deepcopy(role.get("bullets", []))
            projects.append(project)
    ids = [project["id"] for project in projects]
    catalog = {"version": 1, "enabled": True, "base": base,
               "flavors": [{"id": "resume", "label": "My resume", "confirmation": "draft",
                            "headline": profile.get("headline", ""), "summary": profile.get("summary", "")}],
               "projects": projects,
               "variants": [{"id": "resume", "label": "My resume", "flavor_id": "resume",
                             "confirmation": "draft", "max_pages": 2,
                             "project_ids": ids, "default_project_ids": ids[:]}]}
    validate_catalog({"resume_blocks": catalog})
    return catalog
