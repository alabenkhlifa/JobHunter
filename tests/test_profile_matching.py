import copy
import json
from datetime import datetime, timezone

import pytest

import job_scoring
import jobhunter_matching as matching
import scraper


NOW = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)


def market(name="France", authorization="authorized", relocation=False):
    return {"name": name, "locations": [name], "work_authorization": authorization,
            "relocation_required": relocation}


def config(**overrides):
    data = {"matching": {"preset": "generic", "preferred_roles": ["frontend developer"],
                         "preferred_technologies": ["react", "typescript"]},
            "markets": [market()]}
    data.update(overrides)
    return matching.validate_config(data)


def job(**overrides):
    item = dict(id="one", title="Junior Frontend Developer", company="Example",
                location="Paris, France", description="Build React and TypeScript applications.",
                min_experience=1, tech_required="react, typescript", tech_nice_to_have="",
                date_posted=NOW.isoformat(), date_scraped=NOW.isoformat(), score=1)
    item.update(overrides)
    return item


def evaluate(posting, settings):
    return job_scoring.evaluate(posting, allowed_locations=settings["allowed_locations"],
                                matching=settings["matching"], markets=settings["markets"], now=NOW)


def test_generic_frontend_and_non_software_professions_have_no_owner_knockouts():
    frontend = evaluate(job(), config())
    assert frontend["passed"] and frontend["total"] >= 80
    architect = config(matching={"preset": "generic", "preferred_roles": ["architect"],
                                  "preferred_technologies": ["revit"]})
    result = evaluate(job(title="Senior Architect", description="Design luxury villas in Revit.",
                          tech_required="revit", min_experience=12), architect)
    assert result["passed"] and result["total"] >= 80
    assert job_scoring.knockout(job(), allowed_locations=("france",))


def test_the_same_no_sponsorship_job_has_candidate_specific_eligibility():
    posting = job(description="Build React applications. No visa sponsorship.")
    assert evaluate(posting, config())["passed"]
    needs_visa = config(markets=[market(authorization="sponsorship_required")])
    assert "excludes" in evaluate(posting, needs_visa)["reason"]


@pytest.mark.parametrize("authorization, sponsorship, expected", [
    ("authorized", "excluded", True), ("authorized", "doubtful", True),
    ("sponsorship_required", "offered", True), ("sponsorship_required", "implied", False),
    ("sponsorship_required", "excluded", False), ("unknown", "offered", False),
])
def test_final_review_holds_uncertain_authorization(authorization, sponsorship, expected):
    posting = job(market="france", ai_verdict="send", ai_sponsorship=sponsorship, ai_rank=1)
    selected = job_scoring.select_sendable([posting], markets=[market(authorization=authorization)])
    assert bool(selected) is expected


def test_relocation_and_work_permission_are_independent():
    posting = job(description="No relocation. Local candidates only. No visa sponsorship.")
    assert evaluate(posting, config())["passed"]
    result = evaluate(posting, config(markets=[market(relocation=True)]))
    assert "relocation" in result["reason"]
    # Already-local does not establish work permission.
    result = evaluate(posting, config(markets=[market(authorization="sponsorship_required")]))
    assert "sponsorship" in result["reason"]


def test_mixed_destination_policy_and_caps():
    policies = [market(), market("Germany", "sponsorship_required")]
    reviewed = [job(id="fr", market="france", ai_rank=1, ai_verdict="send", ai_sponsorship="excluded"),
                job(id="de", location="Germany", market="germany", ai_rank=2, ai_verdict="send", ai_sponsorship="implied"),
                job(id="de2", location="Germany", market="germany", ai_rank=3, ai_verdict="send", ai_sponsorship="offered")]
    assert [j["id"] for j in job_scoring.select_sendable(reviewed, markets=policies)] == ["fr", "de2"]
    assert [j["id"] for j in job_scoring.select_sendable(reviewed, markets=policies, cap=1)] == ["fr"]


def test_unknown_and_ambiguous_locations_do_not_borrow_another_destination_policy():
    policies = [market(), market("Germany", "sponsorship_required")]
    assert matching.resolve_market("France or Germany", policies) is None
    assert matching.resolve_market("Australia", [market("US")]) is None
    candidate = job(location="Other country", market="france", ai_verdict="send", ai_sponsorship="offered", ai_rank=1)
    assert job_scoring.select_sendable([candidate], markets=policies) == []


def test_generic_draft_is_inert_and_no_owner_searches_are_inherited():
    settings = matching.validate_config({"matching": {"preset": "generic"}})
    assert settings["keywords"] == settings["tech_terms"] == settings["exclude_terms"] == []
    assert settings["regions"] == {} and settings["local_presence_phrases"] == []
    assert not evaluate(job(), settings)["passed"]


def test_configured_exclusions_and_weights_are_applied():
    settings = config(matching={"preset": "generic", "preferred_roles": ["frontend developer"],
                                "excluded_roles": ["manager"], "excluded_technologies": ["java"],
                                "seniority": {"max_years": 5, "excluded_titles": ["principal"]},
                                "weights": {"stack": 0, "role": 100, "seniority": 0, "employer": 0, "freshness": 0}})
    assert evaluate(job(), settings)["total"] == 100
    for candidate in (job(title="Frontend Manager"), job(title="Principal Frontend Developer"),
                      job(description="Java required"), job(min_experience=8)):
        assert not evaluate(candidate, settings)["passed"]
    # Whole words: Java must not reject a JavaScript developer.
    assert evaluate(job(description="JavaScript required"), settings)["passed"]


@pytest.mark.parametrize("change", [
    {"matching": {"preset": "other"}}, {"matching": {"preferred_roles": "frontend"}},
    {"matching": {"preferred_technologies": [""]}}, {"matching": {"seniority": {"min_years": 5, "max_years": 2}}},
    {"matching": {"weights": {"stack": float("nan")}}}, {"delivery": {"cap": -1}},
    {"markets": [{"name": "France", "locations": ["France"], "work_authorization": "citizen"}]},
    {"markets": [{"name": "France", "locations": ["France"], "relocation_required": "false"}]},
])
def test_invalid_configuration_fails_before_collection(change):
    with pytest.raises(ValueError):
        config(**change)


def test_named_profile_loading_is_anchored_and_has_no_owner_defaults(tmp_path, monkeypatch):
    root = tmp_path / "private"
    profile = root / "tg_123"
    profile.mkdir(parents=True)
    (profile / "config.json").write_text(json.dumps(config()), encoding="utf-8")
    monkeypatch.setenv("JOBHUNTER_DATA_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    loaded = scraper.load_profile_config("tg_123")
    assert loaded["db_path"] == str(profile / "jobs.db")
    assert loaded["keywords"] == ["frontend developer"]
    assert loaded["max_experience"] == 30
    assert loaded["exclude_terms"] == []
    assert loaded["tech_terms"] == ["react", "typescript"]


@pytest.mark.parametrize("profile", ["../owner", "/tmp/owner", ".", "a/b", "a\\b", "", "a" * 65])
def test_profile_ids_cannot_traverse_paths(profile):
    with pytest.raises(ValueError):
        scraper.profile_directory(profile)


def test_profile_directory_rejects_symlink_escape_and_relative_root(tmp_path, monkeypatch):
    root, elsewhere = tmp_path / "root", tmp_path / "elsewhere"
    root.mkdir()
    elsewhere.mkdir()
    (root / "outsider").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setenv("JOBHUNTER_DATA_ROOT", str(root))
    with pytest.raises(ValueError):
        scraper.profile_directory("outsider")
    own = root / "own"
    own.mkdir()
    (root / "sibling").symlink_to(own, target_is_directory=True)
    with pytest.raises(ValueError):
        scraper.profile_directory("sibling")
    (own / "jobs.db").symlink_to(elsewhere / "private.db")
    with pytest.raises(ValueError):
        scraper.profile_directory("own")
    monkeypatch.setenv("JOBHUNTER_DATA_ROOT", "relative/path")
    with pytest.raises(ValueError):
        scraper.profile_directory("safe")


def test_generic_feedback_does_not_apply_owner_backend_preferences(monkeypatch):
    settings = config()
    monkeypatch.setattr(scraper, "CONFIG", settings)
    summary = {"by_reason": {"wrong stack": 10, "too junior": 5, "backend": 10}, "by_action": {"interested": 10}}
    assert scraper.apply_feedback_learning(job(), summary)["feedback_adjustment"] == 0
    assert scraper.apply_feedback_learning(job(title="Backend Architect"), summary)["feedback_adjustment"] == 0


def test_review_backlog_recomputes_scores_after_preferences_change(tmp_path, monkeypatch):
    settings = config()
    settings["db_path"] = str(tmp_path / "jobs.db")
    monkeypatch.setattr(scraper, "CONFIG", settings)
    conn = scraper.init_db()
    candidate = job(source="example", url="https://example.com/job", salary="", work_model="",
                    score_breakdown="old preferences", tech_required="", tech_nice_to_have="")
    scraper.save_job(conn, candidate)
    try:
        reviewed = scraper.get_review_candidates(conn, now=NOW)
        assert len(reviewed) == 1 and reviewed[0]["score"] >= 80
        assert reviewed[0]["tech_required"] == "react, typescript"
        assert conn.execute("SELECT score FROM jobs WHERE id = 'one'").fetchone()[0] == 1
    finally:
        conn.close()


def test_custom_digest_uses_destination_names_and_authorization():
    message = scraper.format_digest_message(
        [job(market="france", score=90, ai_sponsorship="excluded", url="https://example.com/job")],
        0, [], today=NOW, markets=[market()],
    )
    assert "FRANCE" in message and "Work authorized" in message
    assert "DUBAI" not in message and "No sponsorship" not in message


def test_generic_dedup_keeps_different_levels_and_destinations_separate():
    settings = config(markets=[market(), market("Germany")])
    kwargs = {"matching": settings["matching"], "markets": settings["markets"]}
    key = job_scoring.duplicate_key(job(), **kwargs)
    assert key != job_scoring.duplicate_key(job(title="Senior Frontend Developer"), **kwargs)
    assert key != job_scoring.duplicate_key(job(location="Germany"), **kwargs)


def test_normalization_does_not_mutate_callers_configuration():
    settings = {"matching": {"preset": "generic"}, "markets": [market()]}
    original = copy.deepcopy(settings)
    matching.validate_config(settings)
    assert settings == original


def test_generic_cli_cannot_send_using_owner_environment(monkeypatch):
    import argparse

    monkeypatch.setattr(scraper, "CONFIG", dict(scraper.CONFIG))
    monkeypatch.setattr(scraper, "load_profile_config", lambda _: config())
    monkeypatch.setattr(scraper, "parse_args", lambda: argparse.Namespace(
        profile="tg_123", max_pages=None, send_doc=None, send_msg="message"))
    with pytest.raises(ValueError, match="scoped JobHunter delivery"):
        scraper.main()


def test_backlog_rescore_uses_generic_weights_and_destination_authorization(monkeypatch):
    from tools import rescore_pending_backlog

    monkeypatch.setattr(scraper, "CONFIG", config())
    score, breakdown = rescore_pending_backlog.rescore(job(description="No visa sponsorship."), NOW)
    assert score >= 80 and "band excellent" in breakdown
    monkeypatch.setattr(scraper, "CONFIG", config(markets=[market(authorization="sponsorship_required")]))
    score, breakdown = rescore_pending_backlog.rescore(job(description="No visa sponsorship."), NOW)
    assert score == 0 and "sponsorship" in breakdown[0]


def test_salary_target_is_preserved_per_destination_without_conversion():
    target = {"amount": 60000, "currency": "EUR", "period": "year"}
    policy = market()
    policy["salary_target"] = target
    settings = config(markets=[policy, market("Tunisia")])
    assert settings["markets"][0]["salary_target"] == target
    assert matching.salary_target_for_job(job(), settings["markets"]) == target
    assert matching.salary_target_for_job(job(location="Tunisia"), settings["markets"]) is None


@pytest.mark.parametrize("target", [
    {"amount": 0, "currency": "EUR", "period": "year"},
    {"amount": float("inf"), "currency": "EUR", "period": "year"},
    {"amount": 10 ** 1000, "currency": "EUR", "period": "year"},
    {"amount": True, "currency": "EUR", "period": "year"},
    {"amount": 60000, "currency": "eur", "period": "year"},
    {"amount": 60000, "currency": "$", "period": "year"},
    {"amount": 60000, "currency": "EUR", "period": "week"},
    {"amount": 60000, "currency": "EUR"},
])
def test_invalid_salary_expectations_are_rejected(target):
    policy = market()
    policy["salary_target"] = target
    with pytest.raises(ValueError):
        config(markets=[policy])


@pytest.mark.parametrize("salary, expected", [
    ("EUR 60,000 per year", "met"), ("EUR 50,000 - 55,000 per year", "below"),
    ("EUR 50,000 - 65,000 per year", "unknown"), ("USD 70,000 per year", "unknown"),
    ("EUR 5,000 per month", "unknown"), ("EUR 70,000", "unknown"),
    ("€70,000 per year", "unknown"), ("", "unknown"),
])
def test_salary_comparison_requires_explicit_same_currency_period_and_source(salary, expected):
    policy = market()
    policy["salary_target"] = {"amount": 60000, "currency": "EUR", "period": "year"}
    candidate = job(salary=salary, description="Advertised base salary: " + salary)
    assert matching.salary_comparison(candidate, [policy]) == expected
    # Targets are ranking/research guidance, never an implicit hard filter.
    assert matching.eligibility_reason(candidate, [policy]) is None
    candidate["description"] = "Salary is negotiable."
    assert matching.salary_comparison(candidate, [policy]) == "unknown"


def test_salary_guidance_does_not_assume_a_gross_or_net_basis():
    policy = market()
    policy["salary_target"] = {"amount": 60000, "currency": "EUR", "period": "year"}
    candidate = job(salary="EUR 70,000 per year", description="Gross salary: EUR 70,000 per year")
    assert matching.salary_comparison(candidate, [policy]) == "unknown"
    assert matching.eligibility_reason(candidate, [policy]) is None
