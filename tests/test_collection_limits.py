import sys
from unittest import mock

import pytest

import scraper


@pytest.mark.parametrize("value", ["0", "-1", "invalid"])
def test_invalid_page_limit_fails_before_database_or_network(monkeypatch, value):
    monkeypatch.setattr(sys, "argv", ["scraper.py", "--max-pages", value])
    with mock.patch.object(scraper, "load_dotenv"), \
         mock.patch.object(scraper, "init_db") as database, \
         mock.patch.object(scraper, "create_session") as network:
        with pytest.raises(SystemExit) as exc:
            scraper.main()
    assert exc.value.code == 2
    database.assert_not_called()
    network.assert_not_called()


@pytest.mark.parametrize("override,expected", [(None, 10), ("7", 7), ("12", 12)])
def test_cli_page_limit_applies_after_profile_loading(monkeypatch, override, expected):
    args = ["scraper.py", "--profile", "test", "--job-stats"]
    if override is not None:
        args += ["--max-pages", override]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(scraper, "CONFIG", dict(scraper.CONFIG))
    with mock.patch.object(scraper, "load_dotenv"), \
         mock.patch.object(scraper, "load_profile_config", return_value={"max_pages": 10}) as profile, \
         mock.patch.object(scraper, "init_db"), \
         mock.patch.object(scraper, "get_job_status_summary", return_value={}):
        scraper.main()
    profile.assert_called_once_with("test")
    assert scraper.CONFIG["max_pages"] == expected


@pytest.mark.parametrize("source", ["linkedin", "foundit"])
def test_search_sources_stop_after_seven_pages(monkeypatch, source):
    monkeypatch.setitem(scraper.CONFIG, "max_pages", 7)

    def response(session, url, *, params, **kwargs):
        result = mock.Mock(status_code=200)
        if source == "linkedin":
            result.text = (
                '<div class="base-search-card"><span class="sr-only">Backend Engineer</span>'
                f'<a class="base-card__full-link" href="https://example.test/jobs/role-{params["start"]}"></a></div>'
            )
        else:
            result.json.return_value = {"jobSearchResponse": {"data": [
                {"id": params["start"], "title": "Backend Engineer"},
            ]}}
        return result

    with mock.patch.object(scraper, "rate_limited_get", side_effect=response) as get:
        pages = list(getattr(scraper, "scrape_" + source)(mock.Mock(), "backend", "Dubai"))
    assert len(pages) == 7
    assert all(len(page) == 1 for page in pages)
    stride = 1 if source == "linkedin" else 15
    assert [call.kwargs["params"]["start"] for call in get.call_args_list] == list(range(0, 7 * stride, stride))


def linkedin_card(number, *, date_class="job-search-card__listdate", valid=True):
    title = '<span class="sr-only">Java Engineer</span>' if valid else ''
    return (f'<div class="base-search-card">{title}'
            f'<a class="base-card__full-link" href="https://example.test/jobs/role-{number}"></a>'
            '<span class="job-search-card__location">Dubai</span>'
            f'<time class="{date_class}" datetime="2026-09-12"></time></div>')


def test_linkedin_uses_actual_card_count_and_freshness_filter(monkeypatch):
    monkeypatch.setitem(scraper.CONFIG, "max_pages", 10)
    responses = [mock.Mock(status_code=200, text="".join(linkedin_card(i) for i in range(10))),
                 mock.Mock(status_code=200, text="".join(linkedin_card(i) for i in range(10, 20))),
                 mock.Mock(status_code=200, text="")]
    with mock.patch.object(scraper, "rate_limited_get", side_effect=responses) as get:
        pages = list(scraper.scrape_linkedin(mock.Mock(), "java", "Dubai"))
    assert [len(page) for page in pages] == [10, 10]
    assert [call.kwargs["params"]["start"] for call in get.call_args_list] == [0, 10, 20]
    assert all(call.kwargs["params"]["f_TPR"] == "r172800" for call in get.call_args_list)


def test_linkedin_counts_malformed_cards_and_honours_time_override(monkeypatch):
    monkeypatch.setitem(scraper.CONFIG, "linkedin_time_range", "r86400")
    with mock.patch.object(scraper, "rate_limited_get", side_effect=[
        mock.Mock(status_code=200, text=linkedin_card(1) + linkedin_card(2, valid=False)),
        mock.Mock(status_code=200, text=""),
    ]) as get:
        assert len(list(scraper.scrape_linkedin(mock.Mock(), "java", "Dubai"))[0]) == 1
    assert get.call_args.kwargs["params"]["start"] == 2
    assert get.call_args.kwargs["params"]["f_TPR"] == "r86400"


def test_linkedin_new_date_class_is_stored(monkeypatch):
    monkeypatch.setitem(scraper.CONFIG, "max_pages", 1)
    with mock.patch.object(scraper, "rate_limited_get", return_value=mock.Mock(
        status_code=200, text=linkedin_card(1, date_class="job-search-card__listdate--new"),
    )):
        job = next(scraper.scrape_linkedin(mock.Mock(), "java", "Dubai"))[0]
    assert job["date_posted"] == "2026-09-12"


@pytest.fixture
def collection(monkeypatch, tmp_path):
    from copy import deepcopy
    from collections import Counter
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "jobs.db")
    monkeypatch.setattr(scraper, "CONFIG", config)
    conn = scraper.init_db()
    fetch = mock.Mock(return_value="Required: 5 years of experience. Java Spring Boot microservices on AWS.")
    monkeypatch.setattr(scraper, "fetch_job_description", fetch)
    state = dict(conn=conn, session=mock.Mock(), seen_titles={}, skip_counts=Counter())
    yield state, fetch
    conn.close()


def collection_job(**overrides):
    job = dict(id="li-1", title="Java Architect", company="Acme", location="Dubai",
               url="https://example.test/1", source="LinkedIn", date_posted="")
    job.update(overrides)
    return job


@pytest.mark.parametrize("description,reason", [
    ("Required: 9 years of experience with Java.", "wants 9+ years, over the 7 cap"),
    ("Must be based in UAE. Java Spring Boot.", "requires local presence"),
])
def test_fetched_knockout_is_saved_and_not_fetched_next_run(collection, description, reason):
    state, fetch = collection
    fetch.return_value = description
    assert scraper.evaluate_job(collection_job(), **state) is None
    row = state["conn"].execute("SELECT score, score_breakdown FROM jobs").fetchone()
    assert row == (0, "knocked out: " + reason)
    state["seen_titles"] = scraper.load_recent_duplicate_keys(state["conn"], 7, 45)
    assert state["seen_titles"] == {}
    assert scraper.evaluate_job(collection_job(), **state) is None
    assert fetch.call_count == 1
    assert state["skip_counts"]["already_seen"] == 1


@pytest.mark.parametrize("location,allowed", [("Dubai, UAE", True), ("Sharjah, UAE", False)])
def test_gulftalent_uses_existing_collection_filters_and_persistence(collection, location, allowed):
    state, fetch = collection
    job = collection_job(id="gulftalent-12345", source="GulfTalent", location=location,
                         url="https://www.gulftalent.com/uae/jobs/java-architect-12345")
    result = scraper.evaluate_job(job, **state)
    assert (result is not None) is allowed
    saved = state["conn"].execute("SELECT id, source FROM jobs").fetchall()
    assert saved == ([("gulftalent-12345", "GulfTalent")] if allowed else [])
    assert fetch.call_count == int(allowed)


def test_gulftalent_missing_description_is_retried_after_access_recovers(collection):
    state, fetch = collection
    job = collection_job(id="gulftalent-12345", source="GulfTalent",
                         url="https://www.gulftalent.com/uae/jobs/java-architect-12345")
    description = fetch.return_value
    fetch.return_value = ""
    assert scraper.evaluate_job(job, **state) is None
    assert state["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert state["skip_counts"]["missing_description"] == 1
    fetch.return_value = description
    assert scraper.evaluate_job(job, **state) is not None
    assert state["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_default_buckets_use_regions_for_linkedin_and_countries_for_gulf_boards(monkeypatch):
    monkeypatch.setattr(scraper, "CONFIG", scraper.DEFAULT_CONFIG)
    buckets = scraper.build_collection_buckets(mock.Mock())
    assert set(buckets) == {"LinkedIn/" + region for region in scraper.CONFIG["regions"]} | {
        "Foundit/United Arab Emirates", "Foundit/Saudi Arabia",
        "GulfTalent/United Arab Emirates", "GulfTalent/Saudi Arabia"}
    assert len(buckets["Foundit/United Arab Emirates"]["generators"]) == len(scraper.CONFIG["keywords"])
    assert len(buckets["GulfTalent/United Arab Emirates"]["generators"]) == len(scraper.CONFIG["keywords"])


@pytest.mark.parametrize("city,country", [
    ("Dubai", "United Arab Emirates"), ("Abu Dhabi", "United Arab Emirates"),
    ("Jeddah", "Saudi Arabia"), ("Riyadh", "Saudi Arabia"),
])
def test_foundit_queries_country_and_preserves_null_location(monkeypatch, city, country):
    monkeypatch.setitem(scraper.CONFIG, "max_pages", 1)
    response = mock.Mock(status_code=200)
    response.json.return_value = {"jobSearchResponse": {"data": [{"id": 1, "title": "Engineer", "locations": None}]}}
    with mock.patch.object(scraper, "rate_limited_get", return_value=response) as get:
        row = next(scraper.scrape_foundit(mock.Mock(), "java", city))[0]
    assert get.call_args.kwargs["params"]["locations"] == country
    assert row["location"] == ""
    assert row["query_country"] == country


@pytest.mark.parametrize("location,country,city", [
    (None, "United Arab Emirates", "Dubai"), ("Remote", "United Arab Emirates", "Abu Dhabi"),
    ("United Arab Emirates", "United Arab Emirates", "Dubai"),
    ("Saudi Arabia, Mecca", "Saudi Arabia", "Jeddah"),
])
def test_foundit_resolves_city_from_description(collection, location, country, city):
    state, fetch = collection
    fetch.return_value += f" This role is in {city}."
    result = scraper.evaluate_job(collection_job(location=location, source="Foundit", query_country=country), **state)
    assert result["location"] == f"{city}, {country}"
    assert result["score"] > 0


@pytest.mark.parametrize("source,location,description", [
    ("Foundit", None, "Work in Sharjah with Java."),
    ("LinkedIn", "United Arab Emirates", "Work in Sharjah with Java."),
    ("LinkedIn", "Saudi Arabia", "Work in Mecca with Java."),
])
def test_country_without_chosen_city_is_saved_at_zero(collection, source, location, description):
    state, fetch = collection
    fetch.return_value = description
    assert scraper.evaluate_job(collection_job(source=source, location=location, query_country="United Arab Emirates"), **state) is None
    score, reason = state["conn"].execute("SELECT score, score_breakdown FROM jobs").fetchone()
    assert score == 0
    assert reason.startswith("knocked out: outside the configured markets")


@pytest.mark.parametrize("location,title,city,country", [
    ("United Arab Emirates", "Senior Backend Engineer – Java SpringBoot MicroServices", "Abu Dhabi", "United Arab Emirates"),
    ("uae", "Tech Lead", "Dubai", "United Arab Emirates"),
    ("Saudi Arabia", "Kotlin Developer", "Riyadh", "Saudi Arabia"),
])
def test_bare_country_target_title_is_resolved(collection, location, title, city, country):
    state, fetch = collection
    fetch.return_value += f" Work in {city}."
    result = scraper.evaluate_job(collection_job(location=location, title=title), **state)
    assert result["location"] == f"{city}, {country}"
    assert result["score"] > 0


def test_country_resolution_uses_first_city_and_only_chosen_cities(collection, monkeypatch):
    state, fetch = collection
    fetch.return_value += " Abu Dhabi office, with a Dubai team."
    assert scraper.resolve_description_city(fetch.return_value, "uae") == "Abu Dhabi, United Arab Emirates"
    monkeypatch.setitem(scraper.CONFIG, "allowed_locations", ["dubai"])
    assert scraper.resolve_description_city(fetch.return_value, "uae") == "Dubai, United Arab Emirates"
    assert scraper.resolve_description_city("Dubai office", "ksa") is None


@pytest.mark.parametrize("location,title,source,query", [
    ("United Arab Emirates", "Software Engineer", "LinkedIn", ""),
    ("Sharjah", "Java Architect", "LinkedIn", ""),
    ("Saudi Arabia", "Java Architect", "Foundit", "United Arab Emirates"),
])
def test_unrescuable_location_skips_without_fetch_or_save(collection, location, title, source, query):
    state, fetch = collection
    assert scraper.evaluate_job(collection_job(location=location, title=title, source=source, query_country=query), **state) is None
    fetch.assert_not_called()
    assert state["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert state["skip_counts"]["outside_location"] == 1


@pytest.mark.parametrize("title", ["Contractor Java Developer", "Java Engineer, banking sector", "Java Engineer Autoinjectors"])
def test_exclusions_match_words_not_substrings(collection, title):
    assert not scraper.is_excluded(collection_job(title=title))


@pytest.mark.parametrize("title", ["CTO", "SAP Developer", "D365 Architect", "Pre-sales Architect"])
def test_exclusions_still_match_complete_terms(collection, title):
    assert scraper.is_excluded(collection_job(title=title))


@pytest.mark.parametrize("location", ["Sankt Gallen", "St. Gallen"])
def test_swiss_city_spellings_are_allowed(collection, location):
    assert scraper.is_allowed_location(collection_job(location=location))


def test_generic_title_dedupe_fetches_different_descriptions_and_skips_twins(collection):
    state, fetch = collection
    first = collection_job(title="Architect", company="Virtusa")
    second = dict(first, id="li-2")
    third = dict(first, id="li-3")
    fetch.side_effect = ["Java Spring Boot. Required: 5 years of experience building payment microservices on AWS.",
                        "Node.js NestJS TypeScript. Required: 5 years of experience building identity microservices on AWS.",
                        " JAVA  SPRING BOOT. Required: 5 years of experience building payment microservices on AWS. "]
    assert scraper.evaluate_job(first, **state)["score"] >= 45
    state["seen_titles"] = scraper.load_recent_duplicate_keys(state["conn"], 7, 45)
    assert scraper.evaluate_job(second, **state)["score"] >= 45
    assert scraper.evaluate_job(third, **state) is None
    assert state["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 2
    hashes = next(iter(state["seen_titles"].values()))
    assert len(hashes) == 2
    assert fetch.call_count == 3


def test_long_title_duplicate_still_skips_before_fetch(collection):
    import job_scoring
    state, fetch = collection
    job = collection_job(title="Java Payment Software Architect")
    state["seen_titles"][job_scoring.duplicate_key(job)] = {scraper.description_hash("different body")}
    assert scraper.evaluate_job(job, **state) is None
    fetch.assert_not_called()
    assert state["skip_counts"]["repost"] == 1


def test_default_collection_does_not_stop_at_25_and_logs_skip_summary(collection, monkeypatch, caplog):
    import logging
    from datetime import datetime, timezone, timedelta
    state, fetch = collection
    monkeypatch.setattr(sys, "argv", ["scraper.py", "--collect-only"])
    monkeypatch.setattr(scraper, "load_dotenv", lambda: None)
    monkeypatch.setattr(scraper, "load_profile_config", lambda _: scraper.CONFIG)
    monkeypatch.setattr(scraper, "init_db", lambda: state["conn"])
    monkeypatch.setattr(scraper, "create_session", mock.Mock())
    monkeypatch.setattr(scraper, "setup_logging", lambda: None)
    pages = [[collection_job(id=f"li-{n}", company=f"Company {n}") for n in range(27)], [
        collection_job(id="excluded", title="CTO"),
        collection_job(id="outside", location="Sharjah"),
        collection_job(id="old", date_posted=(datetime.now(timezone.utc)-timedelta(days=10)).isoformat()),
        collection_job(id="li-0"),
    ]]
    monkeypatch.setattr(scraper, "SCRAPERS", [("LinkedIn", lambda *args: iter(pages))])
    monkeypatch.setitem(scraper.CONFIG, "keywords", ["java"])
    monkeypatch.setitem(scraper.CONFIG, "regions", {"Dubai": ["Dubai"]})
    assert scraper.CONFIG["min_matching_jobs"] == 0
    with caplog.at_level(logging.INFO, logger="scraper"):
        scraper.main()
    assert fetch.call_count == 27
    summary = [record.message for record in caplog.records if record.message.startswith("Pre-fetch skips:")]
    assert summary == ["Pre-fetch skips: excluded=1, outside_location=1, too_old=1, repost=0, already_seen=1"]


@pytest.mark.parametrize("title", [
    "Java Architect with DevOps", "Technical Architect (Java & DevOps)",
    "Lead Fullstack Software Engineer – Java & Angular", "Senior Fullstack Engineer (Java & Angular)",
    "Full Stack Developer (React / Next.js + Node.js + PostgreSQL + GCP)",
    "Senior Fullstack Developer (Node.js+React.js+API Banking)", "Développeur Java/Vue.Js",
    "Expert Backend Engineer", "Senior Expert - Software Engineer", "Staff Software Engineer",
    "Kotlin SRE Engineer", "Backend React Developer", "Full Stack Angular Engineer",
    "Java DevOps Engineer", "Spring Vue Developer",
])
def test_title_rescues_survive_the_collection_filters(collection, title):
    state, fetch = collection
    result = scraper.evaluate_job(collection_job(title=title), **state)
    fetch.assert_called_once()
    assert result["score"] > 0


def test_duplicate_hash_uses_first_400_normalized_characters():
    prefix = "a" * 400
    assert scraper.description_hash(prefix + "one") == scraper.description_hash(prefix + "two")
    assert scraper.description_hash("JAVA   SPRING\nBOOT") == scraper.description_hash("java spring boot")
    assert scraper.description_hash("java") != scraper.description_hash("node.js")


@pytest.mark.parametrize("requirement", ["Fluent French required", "Native Arabic required", "Fluent English required"])
def test_spoken_language_requirement_is_not_persisted_as_knockout(collection, requirement):
    state, fetch = collection
    fetch.return_value += " " + requirement
    result = scraper.evaluate_job(collection_job(), **state)
    assert result["score"] > 0
    assert state["conn"].execute("SELECT score FROM jobs").fetchone()[0] > 0
