"""The Spain-board plumbing shared by every board: registry, columns, links, buckets."""

from copy import deepcopy
from datetime import datetime, timezone
import logging
from unittest import mock

import pytest

import jobhunter_sources
from jobhunter_sources import base
import scraper


@pytest.fixture
def database(monkeypatch, tmp_path):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "jobs.db")
    monkeypatch.setattr(scraper, "CONFIG", config)
    conn = scraper.init_db()
    yield conn
    conn.close()


def job(**over):
    row = dict(id="sj-abc123", title="Backend Architect", company="Acme", location="Madrid, Spain", score=70,
               url="https://www.spainjobs.io/companies/acme/backend-architect--abc1234567", source="SpainJobs.io",
               date_posted="2026-09-30T00:00:00+00:00", description="Java Spring Boot microservices on AWS.")
    row.update(over)
    return row


def test_registry_lists_the_two_spain_boards_in_collection_order():
    names = [board.name for board in jobhunter_sources.boards()]
    assert names == ["Spain Dev Jobs", "SpainJobs.io"]
    assert [board.high_priority for board in jobhunter_sources.boards()] == [True, True]
    keys = [board.key for board in jobhunter_sources.boards()]
    assert keys == ["sdj", "sj"]
    assert all(board.prefix == board.key + "-" for board in jobhunter_sources.boards())


def test_boards_only_search_spain():
    assert all(board.countries == frozenset({"es"}) for board in jobhunter_sources.boards())


def test_registry_lookups_by_name_key_host_and_job_id():
    board = jobhunter_sources.by_name("SpainJobs.io")
    assert board is jobhunter_sources.by_name("spainjobs.io") is jobhunter_sources.by_key(board.key)
    assert jobhunter_sources.by_host(next(iter(board.hosts))) is board
    assert jobhunter_sources.by_job_id(board.prefix + "x") is board
    assert jobhunter_sources.by_name("LinkedIn") is None
    assert jobhunter_sources.by_name("") is None
    assert jobhunter_sources.by_host("www.linkedin.com") is None
    assert jobhunter_sources.by_job_id("li-123") is None


def test_a_board_that_fails_to_import_is_skipped_not_fatal(monkeypatch, caplog):
    monkeypatch.setattr(jobhunter_sources, "BOARD_MODULES", ("jobhunter_sources.spain_devjobs", "jobhunter_sources.no_such_board"))
    monkeypatch.setattr(jobhunter_sources, "_modules", {})
    with caplog.at_level(logging.ERROR, logger="scraper"):
        boards = jobhunter_sources.boards()
    assert [board.name for board in boards] == ["Spain Dev Jobs"]
    assert any("no_such_board" in record.message for record in caplog.records)


def test_scrapers_put_the_spain_boards_before_linkedin_and_foundit_last():
    assert [name for name, _ in scraper.SCRAPERS] == ["Spain Dev Jobs", "SpainJobs.io", "LinkedIn", "Foundit"]


def test_buckets_use_one_generator_per_city_for_keyword_less_boards(monkeypatch):
    monkeypatch.setattr(scraper, "CONFIG", scraper.DEFAULT_CONFIG)
    buckets = scraper.build_collection_buckets(mock.Mock())
    keywords = len(scraper.CONFIG["keywords"])
    for board in jobhunter_sources.boards():
        for city in ("Valencia", "Madrid", "Barcelona"):
            generators = buckets[f"{board.name}/{city}"]["generators"]
            assert len(generators) == (keywords if board.keyword_search else 1), board.name
    assert len(buckets["LinkedIn/Madrid"]["generators"]) == keywords
    assert not any(name.startswith("Foundit/") for name in buckets)


def test_boards_get_no_bucket_for_non_spanish_regions(monkeypatch):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["regions"] = {"Dubai": ["Dubai"], "Valencia": ["Valencia, Spain"]}
    monkeypatch.setattr(scraper, "CONFIG", config)
    buckets = scraper.build_collection_buckets(mock.Mock())
    assert "LinkedIn/Dubai" in buckets and "LinkedIn/Valencia" in buckets
    assert "Spain Dev Jobs/Valencia" in buckets and "Spain Dev Jobs/Dubai" not in buckets


def test_disabled_sources_skip_their_buckets(monkeypatch, caplog):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["disabled_sources"] = ["spainjobs.io"]
    monkeypatch.setattr(scraper, "CONFIG", config)
    with caplog.at_level(logging.INFO, logger="scraper"):
        buckets = scraper.build_collection_buckets(mock.Mock())
    assert not any(name.startswith("SpainJobs.io/") for name in buckets)
    assert {name for name in buckets if name.startswith("Spain Dev Jobs/")} == {
        "Spain Dev Jobs/Valencia", "Spain Dev Jobs/Madrid", "Spain Dev Jobs/Barcelona"}
    assert [record.message for record in caplog.records if "disabled by config" in record.message] == [
        "SpainJobs.io: disabled by config"]


def test_board_scraper_wrapper_passes_the_rate_limited_get_and_config(monkeypatch):
    seen = {}

    def fake_scrape(session, keyword, location, *, get, config):
        seen.update(keyword=keyword, location=location, config=config)
        get("https://example.test/listing", params={"page": 1})
        yield [job()]

    board = jobhunter_sources.by_name("Spain Dev Jobs")
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: mock.Mock(scrape=fake_scrape))
    with mock.patch.object(scraper, "rate_limited_get", return_value=mock.Mock(status_code=200)) as get:
        pages = list(scraper._board_scraper(board)(mock.Mock(), None, "Valencia, Spain"))
    assert pages == [[job()]]
    assert seen == dict(keyword=None, location="Valencia, Spain", config=scraper.CONFIG)
    assert get.call_args.kwargs == {"params": {"page": 1}}


def test_fetch_job_description_dispatches_to_the_board_module(monkeypatch):
    def fake_fetch(session, row, *, get):
        row["apply_url"] = "https://jobs.example.test/apply"
        row["language_requirement"] = "board: no Spanish required"
        return "Full text"

    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: mock.Mock(fetch_description=fake_fetch))
    row = job()
    with mock.patch.object(scraper, "rate_limited_get") as get:
        assert scraper.fetch_job_description(mock.Mock(), row) == "Full text"
    get.assert_not_called()
    assert row["apply_url"] == "https://jobs.example.test/apply"


def test_save_job_stores_apply_url_and_language_requirement(database):
    scraper.save_job(database, job(apply_url=" https://jobs.example.test/apply ", language_requirement="board: no Spanish required"))
    scraper.save_job(database, job(id="li-1", source="LinkedIn"))
    rows = dict(database.execute("SELECT id, apply_url || '|' || language_requirement FROM jobs").fetchall())
    assert rows == {"sj-abc123": "https://jobs.example.test/apply|board: no Spanish required", "li-1": "|"}
    stored = scraper.get_job_by_id(database, "sj-abc123")
    assert stored["apply_url"] == "https://jobs.example.test/apply"


def test_source_columns_are_added_to_an_older_database(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT)")
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["db_path"] = str(path)
    monkeypatch.setattr(scraper, "CONFIG", config)
    conn = scraper.init_db()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    conn.close()
    assert {"apply_url", "language_requirement"} <= columns


@pytest.mark.parametrize("apply_url,expected", [
    ("https://jobs.example.test/apply", "https://jobs.example.test/apply"),
    ("https://www.spainjobs.io/companies/acme/backend-architect--abc1234567", ""),  # same as the listing
    ("", ""), (None, ""), ("javascript:alert(1)", ""), ("not a url", ""),
])
def test_official_apply_url_only_returns_a_distinct_http_link(apply_url, expected):
    assert scraper.official_apply_url(job(apply_url=apply_url)) == expected


def test_card_and_digest_show_the_employer_posting_and_the_board_note():
    row = job(apply_url="https://jobs.example.test/apply", language_requirement="board: no Spanish required",
              market="madrid", ai_sponsorship="no_info")
    card = scraper.format_job_message(row)
    assert "Employer posting: https://jobs.example.test/apply" in card
    assert "\U0001f5e3 board: no Spanish required" in card
    assert card.index(row["url"]) < card.index("Employer posting")
    digest = scraper.format_digest_message([row], 0, [], today=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert '<a href="https://jobs.example.test/apply">employer posting</a>' in digest
    plain = scraper.format_digest_message([job(market="madrid", ai_sponsorship="no_info")], 0, [],
                                          today=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert "employer posting" not in plain


def test_application_record_prefers_the_employer_posting(monkeypatch):
    import jobhunter_interest_flow
    assert scraper.official_apply_url(job(apply_url="https://jobs.example.test/apply")) == "https://jobs.example.test/apply"
    # The interest flow reads the same helper when it records package_generated.
    source = open(jobhunter_interest_flow.__file__).read()
    assert 'application_url=scraper.official_apply_url(job) or job.get("url")' in source


# ── evaluate_job with board jobs ─────────────────────────────────────────────

@pytest.fixture
def collection(monkeypatch, tmp_path):
    from collections import Counter
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "jobs.db")
    monkeypatch.setattr(scraper, "CONFIG", config)
    conn = scraper.init_db()
    yield dict(conn=conn, session=mock.Mock(), seen_titles={}, skip_counts=Counter())
    conn.close()


def board_module(text, **enrich):
    def fetch(session, row, *, get):
        row.update(enrich)
        return text
    return mock.Mock(fetch_description=fetch)


def test_an_unreadable_board_page_is_not_saved_so_it_is_retried(collection, monkeypatch, caplog):
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: board_module(""))
    with caplog.at_level(logging.WARNING, logger="scraper"):
        assert scraper.evaluate_job(job(), **collection) is None
    assert collection["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert any("left unsaved for a retry" in record.message for record in caplog.records)
    # LinkedIn keeps its old behaviour: an empty page is scored and saved.
    with mock.patch.object(scraper, "fetch_job_description", return_value=""):
        scraper.evaluate_job(job(id="li-9", source="LinkedIn", url="https://www.linkedin.com/jobs/view/9"), **collection)
    assert collection["conn"].execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_board_fields_survive_evaluation(collection, monkeypatch):
    text = "Required: 5 years of experience. Java Spring Boot microservices on AWS."
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: board_module(
        text, apply_url="https://jobs.example.test/apply", language_requirement="board: no Spanish required",
        salary_text="€80,000 - €90,000 a year"))
    result = scraper.evaluate_job(job(location="Madrid, Spain (hybrid)"), **collection)
    assert result["score"] > 0
    row = collection["conn"].execute(
        "SELECT apply_url, language_requirement, salary, work_model FROM jobs WHERE id = 'sj-abc123'").fetchone()
    assert row == ("https://jobs.example.test/apply", "board: no Spanish required", "€80,000 - €90,000 a year", "hybrid")


def test_body_salary_and_body_work_model_win_over_board_hints(collection, monkeypatch):
    text = "Required: 5 years. Salary: AED 30,000 per month. Fully remote role. Java Spring Boot on AWS."
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: board_module(text, salary_text="€1 a year"))
    result = scraper.evaluate_job(job(location="Madrid, Spain (hybrid)"), **collection)
    assert result["salary"] == scraper.extract_salary(text, "Madrid, Spain (hybrid)") != ""
    assert result["work_model"] == "remote"


def test_a_board_note_knocks_out_a_translated_spanish_ad(collection, monkeypatch):
    text = "Required: 5 years of experience. Java Spring Boot microservices on AWS."
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: board_module(
        text, language_requirement="board: ad written in Spanish, Spanish requirement not stated"))
    result = scraper.evaluate_job(job(), **collection)
    assert result["score"] == 0  # a rubric knockout is stored at zero, like every other
    reason = collection["conn"].execute("SELECT score, score_breakdown FROM jobs WHERE id = 'sj-abc123'").fetchone()
    assert reason == (0, "knocked out: language barrier: Spanish (board: ad written in Spanish)")


def test_a_crashing_board_fetch_yields_an_empty_description_not_an_exception(monkeypatch, caplog):
    def explode(session, row, *, get):
        raise KeyError("unexpected markup")
    monkeypatch.setattr(jobhunter_sources, "module_for", lambda b: mock.Mock(fetch_description=explode))
    with caplog.at_level(logging.ERROR, logger="scraper"):
        assert scraper.fetch_job_description(mock.Mock(), job()) == ""
    assert any("description fetch failed" in record.message for record in caplog.records)


def test_main_loop_survives_an_evaluation_error(collection, monkeypatch, caplog):
    import sys
    monkeypatch.setattr(sys, "argv", ["scraper.py", "--collect-only"])
    monkeypatch.setattr(scraper, "load_dotenv", lambda: None)
    monkeypatch.setattr(scraper, "load_profile_config", lambda _: scraper.CONFIG)
    monkeypatch.setattr(scraper, "init_db", lambda: collection["conn"])
    monkeypatch.setattr(scraper, "create_session", mock.Mock())
    monkeypatch.setattr(scraper, "setup_logging", lambda: None)
    pages = [[job(id="sj-bad"), job(id="sj-good", company="Other")]]
    monkeypatch.setattr(scraper, "SCRAPERS", [("SpainJobs.io", lambda *args: iter(pages))])
    monkeypatch.setitem(scraper.CONFIG, "regions", {"Madrid": ["Madrid, Spain"]})
    calls = []

    def evaluate(row, **kwargs):
        calls.append(row["id"])
        if row["id"] == "sj-bad":
            raise ValueError("boom")
        return None
    monkeypatch.setattr(scraper, "evaluate_job", evaluate)
    with caplog.at_level(logging.ERROR, logger="scraper"):
        scraper.main()
    assert calls == ["sj-bad", "sj-good"]
    assert any("evaluation failed" in record.message for record in caplog.records)


# ── base helpers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("location,city", [
    ("Valencia, Spain", "Valencia"), ("València, Spain", "Valencia"), ("Madrid, Spain", "Madrid"),
    ("Barcelona, Spain", "Barcelona"), ("Seville, Spain", None), ("", None),
])
def test_city_of_reads_the_scraper_region_strings(location, city):
    assert base.city_of(location) == city


@pytest.mark.parametrize("card,queried,expected", [
    ("Valencia, Spain", "Valencia, Spain", "Valencia, Spain"),
    ("Remote", "Valencia, Spain", "Valencia, Spain (remote)"),
    ("Barcelona · Hybrid", "Barcelona, Spain", "Barcelona, Spain (hybrid)"),
    ("Madrid, Comunidad de Madrid", "Madrid, Spain", "Madrid, Spain"),
    ("", "Madrid, Spain", "Madrid, Spain"),
    ("Remote - Spain", "Madrid, Spain", "Madrid, Spain (remote)"),
    ("Hybrid · Madrid", "Valencia, Spain", "Madrid, Spain (hybrid)"),
    ("Sevilla", "Madrid, Spain", "Sevilla"),
    ("Lisbon, Portugal", "Madrid, Spain", "Lisbon, Portugal"),
])
def test_spanish_location_normalises_to_a_chosen_city_or_keeps_foreign_places(card, queried, expected):
    assert base.spanish_location(card, queried) == expected
    import job_scoring
    allowed = job_scoring.location_allowed(expected, job_scoring.DEFAULT_MARKETS)
    assert allowed is (expected.endswith(("Spain", "(remote)", "(hybrid)")))


@pytest.mark.parametrize("title,keep", [
    ("Senior Backend Engineer", True), ("Arquitecto de Software", True), ("Tech Lead", True),
    ("Desarrollador Java", True), ("Marketing Manager", False), ("Chef de Partie", False),
])
def test_title_prefilter_for_city_listings(title, keep):
    assert base.title_matches_search(title) is keep
    assert base.title_matches_search(title, {"city_listing_title_terms": ("chef",)}) is (title == "Chef de Partie")


def test_title_prefilter_uses_an_invited_profiles_own_roles():
    config = {"matching": {"preset": "generic"}, "keywords": ["Data Analyst", "product designer"]}
    assert base.title_matches_search("Senior Data Analyst", config)
    assert base.title_matches_search("Product Designer", config)
    assert not base.title_matches_search("Backend Engineer", config)
    assert not base.title_matches_search("Backend Engineer", {"matching": {"preset": "generic"}, "keywords": []})


NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize("value,expected", [
    ("3 days ago", "2026-09-28T12:00:00+00:00"), ("today", "2026-10-01T12:00:00+00:00"),
    ("yesterday", "2026-09-30T12:00:00+00:00"), ("hace 2 días", "2026-09-29T12:00:00+00:00"),
    ("2 weeks ago", "2026-09-17T12:00:00+00:00"), ("an hour ago", "2026-10-01T12:00:00+00:00"),
    ("2026-09-29", "2026-09-29T00:00:00+00:00"), ("29/09/2026", "2026-09-29T00:00:00+00:00"),
    ("2026-09-29T10:00:00Z", "2026-09-29T10:00:00+00:00"), (1759312800000, "2025-10-01T10:00:00+00:00"),
    ("garbage", ""), (None, ""), ("", ""),
])
def test_iso_date_reads_what_boards_print(value, expected):
    assert base.iso_date(value, now=NOW) == expected


def test_clean_text_keeps_block_breaks_and_drops_scripts():
    text = base.clean_text("<div><p>Hello <b>world</b></p><ul><li>one</li><li>two</li></ul><script>x()</script><br>end</div>")
    assert text == "Hello world\n\none\n\ntwo\n\nend"
    assert base.clean_text(None) == ""


def test_stable_id_prefixes_and_sanitises():
    board = jobhunter_sources.by_name("Spain Dev Jobs")
    assert base.stable_id(board, "9ubi3qv") == board.prefix + "9ubi3qv"
    assert base.stable_id(board, "a b/c") == board.prefix + "a-b-c"
    assert base.stable_id(board, "") == ""
