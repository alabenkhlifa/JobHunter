"""SpainJobs.io board: synthetic API rows and a trimmed detail page, no network."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pytest
import requests

import job_scoring
import jobhunter_availability as availability
import jobhunter_sources
import scraper
from jobhunter_sources import spainjobs_io as sj

DETAIL = (Path(__file__).parent / "fixtures" / "spainjobs_io" / "detail.html").read_text(encoding="utf-8")
DETAIL_URL = "https://www.spainjobs.io/companies/northwind-labs/senior-backend-engineer-java--4f190bc155"
NOW = datetime.now(timezone.utc)
CONFIG = {"max_pages": 10, "max_job_age_days": 7}


def posted(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def row(title="Senior Backend Engineer (Java)", job_hash="4f190bc155", **fields):
    base_row = {
        "slug": f"northwind-labs--senior-backend-engineer-java--{job_hash}",
        "titleSlug": f"senior-backend-engineer-java--{job_hash}", "title": title,
        "company": "Northwind Labs", "companySlug": "northwind-labs", "location": "Madrid, Spain",
        "provinceCodes": ["28"], "countryCodes": ["ES"], "salaryMin": None, "salaryMax": None,
        "salaryCurrency": None, "salaryInterval": None, "postedAt": posted(1), "isVisaSponsor": False,
        "workAuthorizationRoute": "none", "workAuthorizationEvidenceLabel": None, "language": "en",
        "isEnglishRequired": True, "spanishRequirement": "none", "remotePolicy": "hybrid",
        "visaSponsorshipStatus": "unknown", "relocationSupport": "none", "seniorityLevel": "senior",
        "languageRequirements": [], "category": "engineering", "roleFamily": "backend",
        "descriptionSnippet": "Board summary.",
    }
    return {**base_row, **fields}


class Reply:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self.payload, self.text = status, payload, text

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class FakeGet:
    """Serves queued replies (or raises queued exceptions) and records every call."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def page(rows, cursor=None):
    return Reply(payload={"jobs": rows, "total": len(rows), "matchMode": "none", "nextCursor": cursor})


def scrape(get, location="Madrid, Spain", config=CONFIG):
    return list(sj.scrape(mock.Mock(), None, location, get=get, config=config))


# ── board contract ──────────────────────────────────────────────────────────


def test_board_is_registered_with_its_identity():
    assert jobhunter_sources.by_name("SpainJobs.io") is sj.BOARD
    assert jobhunter_sources.by_host("spainjobs.io") is sj.BOARD
    assert jobhunter_sources.by_job_id("sj-4f190bc155") is sj.BOARD
    assert sj.BOARD.high_priority and not sj.BOARD.keyword_search
    assert availability._url_identity(DETAIL_URL) == ("sj", "4f190bc155")
    assert availability._url_identity("https://spainjobs.io/companies/acme/4f190bc155/") == ("sj", "4f190bc155")
    assert availability._url_identity("https://www.spainjobs.io/companies/acme") is None


# ── listing ─────────────────────────────────────────────────────────────────


def test_card_parsing_sets_every_field_and_queries_the_engineering_category():
    get = FakeGet(page([row(salaryMin=80000, salaryMax=90000, salaryCurrency="EUR", salaryInterval="year",
                            location="Las Rozas de Madrid, Spain / València, Spain", provinceCodes=["28", "46"],
                            visaSponsorshipStatus="likely", workAuthorizationRoute="relocation_support",
                            workAuthorizationEvidenceLabel="Mentions relocation support",
                            relocationSupport="relocation-package")]))
    [[job]] = scrape(get, "Valencia, Spain")
    assert job == {
        "id": "sj-4f190bc155",
        "title": "Senior Backend Engineer (Java)",
        "company": "Northwind Labs",
        "location": "València, Spain",
        "location_raw": "Las Rozas de Madrid, Spain / València, Spain",
        "url": DETAIL_URL,
        "source": "SpainJobs.io",
        "date_posted": datetime.fromisoformat(posted(1).replace("Z", "+00:00")).isoformat(),
        "language_requirement": "board: no Spanish needed (ad in English)",
        "board_facts": "visa sponsorship: likely; work authorization route: relocation_support; "
                       "work authorization evidence: Mentions relocation support; relocation: relocation-package; "
                       "remote policy: hybrid; seniority: senior",
        "salary_text": "€80,000 - €90,000 a year",
    }
    [(url, kwargs)] = get.calls
    assert url == "https://www.spainjobs.io/api/jobs"
    assert kwargs["params"] == {"city": "valencia", "category": "engineering", "sort": "newest", "limit": 48}
    assert kwargs["headers"]["Accept"] == "application/json"


@pytest.mark.parametrize("fields,expected", [
    ({}, "board: no Spanish needed (ad in English)"),
    ({"language": "es", "isEnglishRequired": False}, "board: ad written in Spanish, Spanish requirement not stated"),
    ({"language": "es", "isEnglishRequired": True}, "board: no Spanish needed (English required)"),
    ({"spanishRequirement": "fluent"}, "board: fluent Spanish required"),
    ({"spanishRequirement": "native", "language": "es"}, "board: native Spanish required"),
    ({"languageRequirements": ["french"]}, "board: no Spanish needed (ad in English); French required"),
    ({"spanishRequirement": None, "language": None, "isEnglishRequired": None, "languageRequirements": ["german"]},
     "board: German required"),
    ({"spanishRequirement": None}, "board: ad in English, Spanish requirement not stated"),
    ({"spanishRequirement": None, "language": None, "isEnglishRequired": None}, ""),
])
def test_language_requirement_follows_the_board_rule(fields, expected):
    assert sj.language_requirement(row(**fields)) == expected


@pytest.mark.parametrize("fields,expected", [
    ({"salaryMin": 80000, "salaryMax": 90000, "salaryCurrency": "EUR", "salaryInterval": "year"}, "€80,000 - €90,000 a year"),
    ({"salaryMin": 30000, "salaryMax": 110000, "salaryCurrency": "USD", "salaryInterval": "year"}, "$30,000 - $110,000 a year"),
    ({"salaryMax": 80000, "salaryCurrency": "EUR", "salaryInterval": "year"}, "up to €80,000 a year"),
    ({"salaryMin": 50000, "salaryCurrency": "EUR"}, "from €50,000"),
    ({"salaryMin": 9.61, "salaryMax": 9.61, "salaryCurrency": "EUR", "salaryInterval": "hour"}, "€9.61 an hour"),
    ({"salaryMin": 50000, "salaryMax": 60000}, ""),
    ({}, ""),
])
def test_salary_text_is_only_what_the_ad_stated(fields, expected):
    assert sj.salary_text(row(**fields)) == expected


def test_card_without_salary_has_no_salary_text():
    [[job]] = scrape(FakeGet(page([row()])))
    assert "salary_text" not in job


def test_pages_follow_the_cursor_until_it_is_null():
    get = FakeGet(page([row(job_hash="aaaaaaaaaa")], cursor="c1"), page([row(job_hash="bbbbbbbbbb")]))
    pages = scrape(get)
    assert [[job["id"] for job in jobs] for jobs in pages] == [["sj-aaaaaaaaaa"], ["sj-bbbbbbbbbb"]]
    assert "cursor" not in get.calls[0][1]["params"]
    assert get.calls[1][1]["params"]["cursor"] == "c1"


@pytest.mark.parametrize("second", [
    Reply(status=500), Reply(status=429), Reply(text="<html>challenge</html>"), Reply(payload=["not", "a", "dict"]),
    page([]), requests.ConnectionError("reset"),
])
def test_paging_stops_quietly_on_errors_and_empty_pages(second):
    get = FakeGet(page([row()], cursor="c1"), second)
    pages = scrape(get)
    assert [len(jobs) for jobs in pages] == [1]
    assert len(get.calls) == 2


@pytest.mark.parametrize("first", [Reply(status=500), requests.Timeout("slow"), Reply(text="oops")])
def test_failed_first_page_yields_nothing_and_never_raises(first):
    assert scrape(FakeGet(first)) == []


def test_a_page_older_than_the_window_stops_without_yielding():
    get = FakeGet(page([row(postedAt=posted(8)), row(job_hash="bbbbbbbbbb", postedAt=posted(30))], cursor="c1"))
    assert scrape(get) == []
    assert len(get.calls) == 1


def test_a_page_reaching_old_rows_is_the_last_one_fetched():
    get = FakeGet(page([row(postedAt=posted(2)), row(job_hash="bbbbbbbbbb", postedAt=posted(9))], cursor="c1"))
    [jobs] = scrape(get)
    assert len(jobs) == 2  # the scraper's own freshness filter drops the old row before any detail fetch
    assert len(get.calls) == 1


def test_max_pages_caps_the_listing():
    get = FakeGet(*[page([row(job_hash=f"{n:010x}")], cursor=f"c{n}") for n in range(5)])
    assert len(scrape(get, config={**CONFIG, "max_pages": 3})) == 3
    assert len(get.calls) == 3


def test_titles_outside_the_search_and_broken_rows_are_dropped():
    rows = [row(title="Product Designer", job_hash="aaaaaaaaaa"), row(title="Lead Software Architect", job_hash="bbbbbbbbbb"),
            row(job_hash="cccccccccc", titleSlug="no-hash-here"), row(job_hash="dddddddddd", title=""),
            row(title="Lead Software Architect", job_hash="bbbbbbbbbb")]
    [jobs] = scrape(FakeGet(page(rows)))
    assert [job["id"] for job in jobs] == ["sj-bbbbbbbbbb"]
    [jobs] = scrape(FakeGet(page(rows)), config={**CONFIG, "city_listing_title_terms": ["designer"]})
    assert [job["title"] for job in jobs] == ["Product Designer"]


@pytest.mark.parametrize("location", ["Sevilla, Spain", "Dubai", "Dubai, United Arab Emirates", ""])
def test_unknown_city_makes_no_request(location):
    get = FakeGet()
    assert scrape(get, location) == []
    assert get.calls == []


def test_rows_with_odd_field_types_never_raise():
    rows = [row(location="Coslada", provinceCodes=28, languageRequirements="french", salaryMin="lots", salaryCurrency="EUR")]
    [[job]] = scrape(FakeGet(page(rows)))
    assert (job["location"], job["language_requirement"]) == ("Coslada", "board: no Spanish needed (ad in English)")
    assert "salary_text" not in job


def test_rows_without_a_date_do_not_stop_paging():
    get = FakeGet(page([row(postedAt=None)], cursor="c1"), page([row(job_hash="bbbbbbbbbb")]))
    pages = scrape(get)
    assert [[job["date_posted"] for job in jobs] for jobs in pages][0] == [""]
    assert len(pages) == 2 and len(get.calls) == 2


@pytest.mark.parametrize("raw,codes,city,expected", [
    ("València, Spain (Remote)", ["46"], "Valencia", "València, Spain (Remote)"),
    ("Las Rozas de Madrid, Spain / A Coruña, Spain / València, Spain", ["28", "15", "46"], "Valencia", "València, Spain"),
    ("Las Rozas de Madrid, Spain / A Coruña, Spain / València, Spain", ["28", "15", "46"], "Madrid", "Las Rozas de Madrid, Spain"),
    ("Cortes Valencianas, Spain", ["46"], "Valencia", "Cortes Valencianas (province of Valencia), Spain"),
    ("Coslada", ["28"], "Madrid", "Coslada (province of Madrid), Spain"),
    ("Pozuelo de Alarcón, Spain (Hybrid)", ["28"], "Madrid", "Pozuelo de Alarcón (province of Madrid), Spain (Hybrid)"),
    ("Mollet del Valles, Spain", ["08"], "Barcelona", "Mollet del Valles (province of Barcelona), Spain"),
    ("Valencia | Valencia | España | Hibrido", ["46"], "Valencia", "Valencia, Valencia, España, Hibrido"),
    ("Remote, Spain", [], "Barcelona", "Barcelona, Spain (remote)"),
    ("Valencian Community, Spain", ["46"], "Valencia", "Valencian Community, Spain"),  # a region, not a town: rejected downstream
    # Several places, none of them the listed city: another chosen city wins,
    # then the province code, which cannot say which town is in the province.
    ("València, Spain / Getxo, Spain / Cerdanyola del Vallès, Spain / Tres Cantos, Spain", ["08", "28", "46", "48"],
     "Madrid", "Valencia, Spain"),
    ("Getxo, Spain / Tres Cantos, Spain", ["28", "48"], "Madrid", "Province of Madrid, Spain"),
    ("", ["46"], "Valencia", "Valencia, Spain"),
])
def test_locations_become_a_chosen_city_in_spain(raw, codes, city, expected):
    location = sj.display_location(raw, codes, city)
    assert location == expected
    # A region-only string is the one case that must be rejected downstream.
    assert job_scoring.location_allowed(location, list(job_scoring.DEFAULT_MARKETS)) is (expected != "Valencian Community, Spain")


@pytest.mark.parametrize("raw,codes,city", [
    ("Valencian Community, Spain", [], "Valencia"),
    ("Sevilla, Spain", ["41"], "Madrid"),
    ("Lisbon, Portugal", [], "Madrid"),
])
def test_other_places_keep_their_text_and_are_rejected(raw, codes, city):
    location = sj.display_location(raw, codes, city)
    assert location == raw
    assert not job_scoring.location_allowed(location, list(job_scoring.DEFAULT_MARKETS))


def test_card_location_passes_the_scraper_location_filter():
    [[job]] = scrape(FakeGet(page([row(location="Alcobendas, Spain")])))
    assert job["location"] == "Alcobendas (province of Madrid), Spain"
    assert scraper.is_allowed_location(job)


# ── detail ──────────────────────────────────────────────────────────────────


def card(**fields):
    return {"id": "sj-4f190bc155", "title": "Senior Backend Engineer (Java)", "company": "Northwind Labs",
            "location": "Madrid, Spain", "url": DETAIL_URL, "source": "SpainJobs.io", **fields}


def describe(job, body=DETAIL, status=200):
    return sj.fetch_description(mock.Mock(), job, get=FakeGet(Reply(status=status, text=body)))


def test_description_is_the_employer_advert_without_board_notes():
    job = card(date_posted="2026-09-30T10:00:00+00:00", language_requirement="board: fluent Spanish required",
               salary_text="€60,000 - €70,000 a year")
    text = describe(job)
    assert text.startswith("Requirements\n\nWhat we're looking for")
    for kept in ("At least 5 years building Java 17 and Spring Boot services.", "Native or fluent Spanish level.",
                 "Design and run the payment APIs", "About Northwind Labs", "We are Northwind Labs"):
        assert kept in text
    for dropped in ("automatically translated", "doesn't mention sponsorship", "after tax", "Beckham", "Gross, as stated",
                    "Board profile", "visa", "Good to know", "relocation", "All open roles", "Founded", "Board summary",
                    "Other Role"):
        assert dropped not in text
    assert text.endswith("\n\nSalary: €60,000 - €70,000 a year (as stated in the ad)")
    assert scraper.extract_salary(text, job["location"]) == "Salary: €60,000 - €70,000"
    assert job_scoring.sponsorship_signal(text) == ("", "")
    assert job["apply_url"] == "https://jobs.ats.example/northwind/1234"
    assert job["company_website"] == "https://northwind.example"
    assert job["translated"] is True
    assert job["date_posted"] == "2026-09-30T10:00:00+00:00"
    assert job["language_requirement"] == "board: fluent Spanish required"


def test_detail_fills_fields_the_card_did_not_have():
    job = card()
    describe(job)
    assert job["date_posted"] == "2026-09-28T08:51:29.157000+00:00"
    assert job["language_requirement"] == "board: fluent Spanish required"
    assert "Salary:" not in describe(card())


def test_overview_without_a_spanish_line_but_english_required_reads_as_no_spanish():
    body = DETAIL.replace("Speak fluent Spanish", "Work in English").replace("This job was automatically translated to English.", "")
    job = card()
    describe(job, body)
    assert job["language_requirement"] == "board: no Spanish needed (English required)"
    assert job["translated"] is False


def test_apply_url_falls_back_to_the_page_payload():
    body = DETAIL.replace('<span class="truncate">Apply</span>', '<span class="truncate">Continue</span>')
    job = card()
    describe(job, body)
    assert job["apply_url"] == "https://jobs.ats.example/northwind/1234"
    assert scraper.official_apply_url(job) == "https://jobs.ats.example/northwind/1234"


def test_page_without_advert_uses_structured_data_then_nothing():
    start, end = DETAIL.index('<div class="mt-6 pb-6 scroll-mt-4" id="job-advert">'), DETAIL.index("<aside>")
    without_advert = DETAIL[:start] + DETAIL[end:]
    assert describe(card(), without_advert) == "Build Java services."
    bare = without_advert.replace('"@type":"JobPosting"', '"@type":"Thing"')
    assert describe(card(), bare) == ""
    odd = without_advert.replace('"description":"<p>Build Java services.</p>"}', '"description":{"text":"Build"}}')
    assert odd != without_advert and describe(card(), odd) == ""


@pytest.mark.parametrize("failure", [requests.ConnectionError("reset"), Reply(status=500), Reply(status=404)])
def test_detail_failures_return_empty_without_raising(failure):
    job = card()
    assert sj.fetch_description(mock.Mock(), job, get=FakeGet(failure)) == ""
    assert "apply_url" not in job


def test_evaluate_job_stores_the_board_fields(monkeypatch, tmp_path):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    config["db_path"] = str(tmp_path / "jobs.db")
    monkeypatch.setattr(scraper, "CONFIG", config)
    monkeypatch.setattr(scraper, "rate_limited_get", lambda session, url, **kwargs: Reply(text=DETAIL))
    [[job]] = scrape(FakeGet(page([row(salaryMin=60000, salaryMax=70000, salaryCurrency="EUR", salaryInterval="year")])))
    conn = scraper.init_db()
    scraper.evaluate_job(job, conn=conn, session=mock.Mock(), seen_titles={}, skip_counts=Counter())
    stored = conn.execute("SELECT apply_url, language_requirement, salary, sponsorship_signal, description FROM jobs "
                          "WHERE id = ?", (job["id"],)).fetchone()
    conn.close()
    assert stored[:4] == ("https://jobs.ats.example/northwind/1234", "board: no Spanish needed (ad in English)",
                          "Salary: €60,000 - €70,000", "")
    assert "after tax" not in stored[4] and "doesn't mention sponsorship" not in stored[4]


def test_scraper_routes_board_jobs_to_the_module(monkeypatch):
    monkeypatch.setattr(scraper, "rate_limited_get", lambda session, url, **kwargs: Reply(text=DETAIL))
    job = card()
    assert "Spring Boot" in scraper.fetch_job_description(mock.Mock(), job)
    assert job["apply_url"] == "https://jobs.ats.example/northwind/1234"


# ── availability ────────────────────────────────────────────────────────────


class Response:
    def __init__(self, body):
        self.body, self.status_code, self.headers = body.encode(), 200, {}

    def iter_content(self, chunk_size):
        yield from (self.body[i:i + chunk_size] for i in range(0, len(self.body), chunk_size))

    def close(self):
        pass


class Transport:
    def __init__(self, response):
        self.response, self.requests = response, []

    def send(self, request, **kwargs):
        self.requests.append(request.url)
        return self.response


def test_live_detail_markup_verifies_as_open():
    transport = Transport(Response(DETAIL))
    result = availability.check(card(), transport, now=NOW)
    assert (result["state"], result["reason"], result["matched"]) == ("open", "application_control", True)
    assert result["source_job_id"] == "4f190bc155"
    assert transport.requests == [DETAIL_URL]


@pytest.mark.parametrize("changed", [{"title": "Staff Backend Engineer"}, {"company": "Another Company"}])
def test_page_for_another_title_or_company_is_not_verified(changed):
    result = availability.check(card(**changed), Transport(Response(DETAIL)), now=NOW)
    assert (result["state"], result["matched"]) == ("unknown", False)


def test_page_without_a_visible_apply_control_is_not_open():
    body = DETAIL.replace('<div class="pt-4 md:hidden">', '<div class="pt-4 hidden">')
    result = availability.check(card(), Transport(Response(body)), now=NOW)
    assert (result["state"], result["reason"]) == ("unknown", "no_open_evidence")


# ── collection ──────────────────────────────────────────────────────────────


def test_collection_lists_each_city_once_before_linkedin(monkeypatch):
    monkeypatch.setattr(scraper, "CONFIG", deepcopy(scraper.DEFAULT_CONFIG))
    buckets = scraper.build_collection_buckets(mock.Mock())
    per_city = 1 if not sj.BOARD.keyword_search else len(scraper.DEFAULT_CONFIG["keywords"])
    for city in ("Valencia", "Madrid", "Barcelona"):
        assert len(buckets[f"SpainJobs.io/{city}"]["generators"]) == per_city == 1
    names = [name for name, _ in scraper.SCRAPERS]
    assert names.index("SpainJobs.io") < names.index("LinkedIn")


def test_hardware_roles_are_skipped_before_any_detail_fetch():
    pages = scrape(FakeGet(page([row(title="Mechanical Engineer", roleFamily="hardware-industrial"),
                                 row(job_hash="bbbbbbbbbb")])))
    assert [job["id"] for job in pages[0]] == ["sj-bbbbbbbbbb"]
