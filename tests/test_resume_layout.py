"""Page planning: trim the weakest confirmed bullets until the resume fits its blueprint pages."""

import copy

import resume_layout as layout


def payload_with(projects, standalone=()):
    """A renderer payload whose bullets are identifiable strings; scores map text -> score."""
    group = {"title": "Lead", "company": "Group Co", "dates": "2020 - Present", "engagements": [
        {"name": name, "dates": "2020 - 2024", "bullets": list(bullets), "tech": ""} for name, bullets in projects]}
    group["bullets"] = [b for _, bullets in projects for b in bullets]
    roles = [group] + [{"title": "Role", "company": company, "dates": "2019 - 2020", "bullets": list(bullets)}
                       for company, bullets in standalone]
    return {"name": "C", "summary": "S", "experience": roles, "certifications": ["Cert A", "Cert B"], "skills": {}}


def fake_render(capacity, page_height=10):
    """Pages from bullet count: ``capacity`` bullets per page; fills from the remainder."""
    calls = []

    def render(payload):
        bullets = [b for r in payload["experience"] for e in (r.get("engagements") or [r]) for b in e["bullets"]]
        compact = (payload.get("layout") or {}).get("compact_spacing") and len(bullets) >= 4
        count = len(bullets) - (1 if compact else 0)
        pages = max(1, -(-count // capacity))
        fills = [1.0] * (pages - 1) + [round((count - capacity * (pages - 1)) / capacity, 2)]
        calls.append(count)
        return {"pages": pages, "main_fills": fills, "main_fill": fills[0] if pages == 1 else None}
    render.calls = calls
    return render


SCORES = {"own": 90, "arch": 80, "mig": 60, "video": 40, "mentor": 30, "tdd": 20, "weak": 10, "old": 25, "cto": 50}


def test_fits_one_page_by_dropping_the_weakest_non_opening_bullets_first():
    payload = payload_with([("Cars", ["own", "mig", "video", "mentor", "tdd"]), ("Farm", ["arch", "weak"])],
                           standalone=[("Old Co", ["old"])])
    fitted, report = layout.fit_pages(payload, fake_render(capacity=5), SCORES, pages=1, openings={"own", "arch", "old"})
    remaining = [b for r in fitted["experience"] for e in (r.get("engagements") or [r]) for b in e["bullets"]]
    assert report["pages"] == 1 and len(remaining) == 6 and report["compact"] is True
    assert {"own", "arch", "old"} <= set(remaining)          # openings survive while their project stays
    assert report["dropped"] == ["weak", "tdd"]                # weakest first
    assert {"mig", "video", "mentor"} <= set(remaining)


def test_drops_a_whole_weak_project_before_touching_stronger_openings():
    payload = payload_with([("Cars", ["own", "mig"]), ("Farm", ["arch"]), ("Tiny", ["weak"])])
    fitted, report = layout.fit_pages(payload, fake_render(capacity=2), SCORES, pages=1, openings={"own", "arch", "weak"})
    names = [e["name"] for e in fitted["experience"][0]["engagements"]]
    assert names == ["Cars", "Farm"] and report["dropped_projects"] == ["Tiny"]
    assert report["dropped"] == ["mig"]                        # then the weakest bullet of a kept project
    assert fitted["experience"][0]["bullets"] == ["own", "arch"]


def test_two_page_target_keeps_two_pages_only_when_page_two_is_used():
    bullets = [("Cars", ["own", "mig", "video", "mentor", "tdd"]), ("Farm", ["arch", "weak"]), ("Cto", ["cto"])]
    fitted, report = layout.fit_pages(payload_with(bullets), fake_render(capacity=5), SCORES, pages=2,
                                      openings={"own", "arch", "cto"})
    assert report["pages"] == 2 and report["dropped"] == [] and report["main_fills"][1] >= 0.5
    thin = [("Cars", ["own", "mig", "video", "mentor", "tdd"]), ("Farm", ["arch"])]
    fitted, report = layout.fit_pages(payload_with(thin), fake_render(capacity=5), SCORES, pages=2,
                                      openings={"own", "arch"})
    assert report["pages"] == 1 and report["dropped"] == [] and report["note"] == "page two under-used"
    assert fitted["layout"]["compact_spacing"] is True


def test_compact_spacing_is_tried_before_dropping_content():
    payload = payload_with([("Cars", ["own", "mig", "video", "mentor", "tdd", "weak"])])
    fitted, report = layout.fit_pages(payload, fake_render(capacity=5), SCORES, pages=1, openings={"own"})
    assert report["dropped"] == [] and fitted["layout"]["compact_spacing"] is True and report["pages"] == 1


def test_irrelevant_omittable_certifications_go_before_bullets():
    payload = payload_with([("Cars", ["own", "mig", "video", "mentor", "tdd", "weak", "old"])])
    calls = []

    def render(data):
        calls.append(len(data["certifications"]))
        bullets = [b for e in data["experience"][0]["engagements"] for b in e["bullets"]]
        count = len(bullets) + len(data["certifications"]) - (1 if (data.get("layout") or {}).get("compact_spacing") else 0)
        pages = 1 if count <= 7 else 2
        return {"pages": pages, "main_fills": [1.0] if pages == 1 else [1.0, 0.1], "main_fill": 1.0}

    fitted, report = layout.fit_pages(payload, render, SCORES, pages=1, openings={"own"}, omittable_certifications=["Cert B"])
    assert fitted["certifications"] == ["Cert A"] and report["omitted_certifications"] == ["Cert B"]
    assert report["dropped"] == [] and report["pages"] == 1


def test_reports_overflow_when_nothing_more_can_be_dropped():
    payload = payload_with([("Cars", ["own"]), ("Farm", ["arch"])])
    fitted, report = layout.fit_pages(payload, fake_render(capacity=1), SCORES, pages=1, openings={"own", "arch"})
    assert report["pages"] == 1 and report["dropped_projects"] == ["Farm"]
    original = copy.deepcopy(payload)
    assert payload == original  # the input payload is never mutated
