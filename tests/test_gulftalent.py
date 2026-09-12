"""Synthetic fixtures matching the browser-captured GulfTalent v2 responses."""

from unittest.mock import Mock

import pytest
import requests

import scraper


def row(identifier=12345, **changes):
    return {"id": identifier, "position_id": identifier, "title": "Backend Engineer",
            "company_name": "Example Team", "jb_company_name": "",
            "location": "Dubai, UAE", "country_id": "10111111000000",
            "posted_date_ts": 1785992400, "link": f"/uae/jobs/backend-engineer-{identifier}",
            "is_scraped": 0, "has_external_application": 1, **changes}


def response(rows):
    return Mock(status_code=200, json=Mock(return_value={"results": {"data": rows}}))


@pytest.fixture
def get(monkeypatch):
    monkeypatch.setitem(scraper.CONFIG, "max_pages", 7)
    get = Mock()
    monkeypatch.setattr(scraper, "rate_limited_get", get)
    return get


def test_result_contract_normalizes_ids_urls_and_seconds_timestamp():
    job = scraper.parse_gulftalent_job(row(link="/uae/jobs/backend-engineer_12345?tracking=unused"), "United Arab Emirates")
    assert job == {"id": "gulftalent-12345", "source": "GulfTalent", "title": "Backend Engineer",
                   "company": "Example Team", "location": "Dubai, UAE",
                   "url": "https://www.gulftalent.com/uae/jobs/backend-engineer_12345",
                   "query_country": "United Arab Emirates", "date_posted": "2026-08-06T05:00:00+00:00"}


@pytest.mark.parametrize("changes", [{"title": None}, {"title": " "}, {"link": None},
    {"position_id": False}, {"country_id": "10111112000000"},
    {"link": "/uae/jobs/backend-99999"}, {"link": "https://example.test/uae/jobs/backend-12345"},
    {"link": "https://www.gulftalent.com@localhost/uae/jobs/backend-12345"},
    {"link": "https://[broken/"}, {"link": "/uae/jobs/search"}, {"link": "/saudi-arabia/jobs/backend-12345"},
    {"link": "/uae/jobs/backend%2Frole-12345"}])
def test_invalid_or_mismatched_results_are_skipped(changes):
    assert scraper.parse_gulftalent_job(row(**changes), "United Arab Emirates") is None


@pytest.mark.parametrize("posted", [None, False, "unknown", 0, -1, 10**30])
def test_unknown_dates_and_locations_are_not_invented(posted):
    job = scraper.parse_gulftalent_job(row(posted_date_ts=posted, location=None), "United Arab Emirates")
    assert job["date_posted"] == "" and job["location"] == ""


@pytest.mark.parametrize("location", ["Dubai, UAE", "Remote"])
def test_remote_country_code_preserves_advertised_location(location):
    job = scraper.parse_gulftalent_job(row(country_id="1", location=location), "United Arab Emirates")
    assert job["location"] == location


def test_both_internal_and_external_jobs_are_collected_without_applying(get):
    get.return_value = response([row(), row(12346, is_scraped=1, has_external_application=0)])
    jobs = list(scraper.scrape_gulftalent(Mock(), "backend engineer", "Dubai"))[0]
    assert len(jobs) == 2
    assert get.call_args.kwargs["params"] == {
        "config[filters]": "DISABLED", "config[isDynamicSearchV2]": "true",
        "filters[country][0]": "10111111000000", "filters[search_keyword]": "backend engineer",
        "search_keyword": "backend engineer", "include_scraped": 1,
        "limit": 25, "offset": 0, "search_order": "r", "version": 2}
    assert "Cookie" not in get.call_args.kwargs["headers"]
    assert get.call_count == 1


def test_pagination_deduplicates_overlaps_and_stops_on_partial_page(get):
    get.side_effect = [response([row(i) for i in range(25)]), response([row(24), row(25)])]
    pages = list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai"))
    assert [len(page) for page in pages] == [25, 1]
    assert [call.kwargs["params"]["offset"] for call in get.call_args_list] == [0, 25]


def test_pagination_honors_page_cap(get):
    get.side_effect = [response([row(i) for i in range(n * 25, (n + 1) * 25)]) for n in range(7)]
    assert len(list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai"))) == 7
    assert [call.kwargs["params"]["offset"] for call in get.call_args_list] == list(range(0, 175, 25))


def test_repeated_full_page_stops(get):
    get.return_value = response([row(i) for i in range(25)])
    assert len(list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai"))) == 1
    assert get.call_count == 2


def test_malformed_rows_do_not_skip_the_next_page(get):
    get.side_effect = [response([None] * 25), response([row()])]
    assert [len(p) for p in scraper.scrape_gulftalent(Mock(), "engineer", "Dubai")] == [0, 1]


@pytest.mark.parametrize("data", [None, [], {}, {"results": []}, {"results": {"data": {}}}])
def test_unexpected_response_shape_stops_safely(get, data):
    get.return_value = Mock(status_code=200, json=Mock(return_value=data))
    assert list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai")) == []
    assert get.call_count == 1


@pytest.mark.parametrize("status", [403, 429])
def test_block_stops_all_gulftalent_generators_in_this_session(get, status):
    session = requests.Session()
    get.return_value = Mock(status_code=status)
    assert list(scraper.scrape_gulftalent(session, "engineer", "Dubai")) == []
    assert list(scraper.scrape_gulftalent(session, "architect", "Saudi Arabia")) == []
    assert scraper.fetch_job_description(session, scraper.parse_gulftalent_job(row(), "United Arab Emirates")) == ""
    assert get.call_count == 1


def test_gulftalent_transport_does_not_retry_http_blocks():
    with scraper.create_session() as session:
        assert session.get_adapter("https://www.gulftalent.com/api/jobs/search").max_retries.total == 0
        assert session.get_adapter("https://www.linkedin.com/").max_retries.total == 3


@pytest.mark.parametrize("error", [requests.Timeout(), requests.ConnectionError()])
def test_network_failure_ends_generator(get, error):
    get.side_effect = error
    assert list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai")) == []


def test_html_challenge_does_not_parse_as_jobs(get):
    get.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError()))
    assert list(scraper.scrape_gulftalent(Mock(), "engineer", "Dubai")) == []


@pytest.mark.parametrize("location,country", [("Dubai", "United Arab Emirates"),
    ("Abu Dhabi, UAE", "United Arab Emirates"), ("Riyadh", "Saudi Arabia"),
    ("Doha", "Qatar"), ("Kuwait City", "Kuwait"), ("Manama", "Bahrain"), ("Muscat", "Oman")])
def test_country_filter_uses_captured_country_ids(get, location, country):
    get.return_value = response([])
    assert list(scraper.scrape_gulftalent(Mock(), "engineer", location)) == []
    assert get.call_args.kwargs["params"]["filters[country][0]"] == scraper.GULFTALENT_COUNTRIES[country]


def test_non_gulf_destination_is_not_queried(get):
    assert list(scraper.scrape_gulftalent(Mock(), "engineer", "Switzerland")) == []
    get.assert_not_called()


def test_configured_countries_are_deduplicated_and_non_gulf_regions_excluded(monkeypatch):
    monkeypatch.setitem(scraper.CONFIG, "regions", {"UAE": ["Dubai", "Abu Dhabi"], "Saudi": ["Riyadh", "Jeddah"],
        "Qatar": ["Doha"], "Kuwait": ["Kuwait City"], "Bahrain": ["Manama"], "Oman": ["Muscat"], "Europe": ["Switzerland"]})
    buckets = scraper.build_collection_buckets(Mock())
    gulf = {key: value for key, value in buckets.items() if key.startswith("GulfTalent/")}
    assert set(gulf) == {"GulfTalent/" + country for country in scraper.GULFTALENT_COUNTRIES}
    assert all(len(value["generators"]) == len(scraper.CONFIG["keywords"]) for value in gulf.values())


def test_description_includes_requirements_and_salary_but_excludes_other_jobs(get):
    get.return_value = Mock(status_code=200, text='<div id="content"><div class="job-description">'
        '<p>Build backend services.</p><h5>Requirements</h5><ul><li>Java and Spring</li></ul>'
        '<p>Salary: AED 20,000 per month</p></div><aside>Unrelated job</aside></div>')
    job = scraper.parse_gulftalent_job(row(), "United Arab Emirates")
    description = scraper.fetch_job_description(Mock(), job)
    assert all(text in description for text in ("Build backend", "Requirements", "•", "Java and Spring", "20,000"))
    assert "Unrelated" not in description


def test_blocked_detail_stops_subsequent_searches(get):
    session = requests.Session()
    get.return_value = Mock(status_code=403)
    assert scraper.fetch_job_description(session, scraper.parse_gulftalent_job(row(), "United Arab Emirates")) == ""
    assert list(scraper.scrape_gulftalent(session, "engineer", "Dubai")) == []
    assert get.call_count == 1
