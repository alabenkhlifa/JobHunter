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
    stride = 25 if source == "linkedin" else 15
    assert [call.kwargs["params"]["start"] for call in get.call_args_list] == list(range(0, 7 * stride, stride))
