from pathlib import Path

import pytest

import jobhunter_interest_flow as flow
from jobhunter_research import search

FIXTURES = Path(__file__).parent / "fixtures"

DDG_LITE = """
<table border="0">
  <tr><td valign="top">1.&nbsp;</td><td>
    <a rel="nofollow" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.levels.fyi%2Ft%2Fsoftware%2Dengineer%2Flevels%2Fsenior%2Flocations%2Fmadrid%2Desp&amp;rut=89b7" class='result-link'>Senior Software Engineer Salary in Madrid, Spain - Levels.fyi</a>
  </td></tr>
  <tr><td>&nbsp;&nbsp;&nbsp;</td>
    <td class='result-snippet'>
      The median <b>Senior</b> <b>Software</b> <b>Engineer</b> <b>salary</b> in <b>Madrid</b>, Spain is $84,437.
    </td></tr>
  <tr><td>&nbsp;</td><td><span class='link-text'>www.levels.fyi/t/software-engineer/levels/senior/locations/madrid-esp</span></td></tr>
</table>
"""


class Response:
    def __init__(self, status_code=200, text=""):
        self.status_code = status_code
        self.text = text


@pytest.fixture(autouse=True)
def no_firecrawl(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(flow.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(search, "BRAVE_THROTTLE", search.Throttle(0))
    monkeypatch.setattr(search, "BRAVE_RETRY_DELAY", 0)


def test_brave_parser_reads_captured_result_markup():
    results = search.parse_brave_results((FIXTURES / "brave_market_madrid_senior.html").read_text(encoding="utf-8"))

    assert len(results) == 10
    assert results[0] == {
        "title": "Average Senior Software Engineer Salary in Spain - Madrid",
        "url": "https://www.payscale.com/research/ES/Job=Senior_Software_Engineer/Salary/9d0a3474/Madrid",
        "snippet": "Our data indicates that the highest pay for a Senior Software Engineer in Madrid is €96k / year",
    }
    glassdoor = results[1]
    assert glassdoor["url"].startswith("https://www.glassdoor.co.uk/Salaries/madrid-spain-senior-software-engineer")
    assert "€48,500 per year" in glassdoor["snippet"]


def test_brave_parser_returns_nothing_for_a_challenge_page():
    assert search.parse_brave_results("<html><body>Please verify you are a human</body></html>") == []


def test_ddg_lite_parser_unwraps_redirects():
    results = search.parse_ddg_results(DDG_LITE)

    assert results == [{
        "title": "Senior Software Engineer Salary in Madrid, Spain - Levels.fyi",
        "url": "https://www.levels.fyi/t/software-engineer/levels/senior/locations/madrid-esp",
        "snippet": "The median Senior Software Engineer salary in Madrid, Spain is $84,437.",
    }]


def test_web_search_uses_brave_with_a_browser_user_agent():
    calls = []
    html = (FIXTURES / "brave_company_cabify.html").read_text(encoding="utf-8")

    def fetcher(url, **kwargs):
        calls.append((url, kwargs))
        return Response(200, html)

    results = flow.web_search_results('"Cabify" company official site about', timeout=3, fetcher=fetcher)

    assert results[0]["url"] == "https://cabify.com/en/about-us"
    assert calls[0][0] == search.BRAVE_URL
    assert calls[0][1]["params"] == {"q": '"Cabify" company official site about', "source": "web"}
    assert "Chrome/" in calls[0][1]["headers"]["User-Agent"]
    assert len(calls) == 1


def test_web_search_falls_back_to_ddg_lite_and_records_why():
    notes = []
    calls = []

    def fetcher(url, **kwargs):
        calls.append(url)
        if url == search.BRAVE_URL:
            return Response(429, "Too many requests")
        return Response(200, DDG_LITE)

    results = flow.web_search_results("senior software engineer salary madrid", timeout=3, fetcher=fetcher, notes=notes)

    assert calls == [search.BRAVE_URL, search.BRAVE_URL, search.DDG_LITE_URL]  # one retry after 429
    assert results[0]["url"].startswith("https://www.levels.fyi/")
    assert notes == ["Brave search returned HTTP 429."]


def test_brave_retry_after_rate_limit_can_succeed():
    answers = [Response(429, ""), Response(200, (FIXTURES / "brave_company_cabify.html").read_text(encoding="utf-8"))]

    results = search.brave_search("Cabify", timeout=3, fetcher=lambda url, **kwargs: answers.pop(0))

    assert results[0]["url"] == "https://cabify.com/en/about-us"


def test_web_search_returns_empty_with_notes_when_every_provider_fails():
    notes = []

    def fetcher(url, **kwargs):
        if url == search.BRAVE_URL:
            raise OSError("network down")
        return Response(202, "challenge")

    assert flow.web_search_results("anything", timeout=3, fetcher=fetcher, notes=notes) == []
    assert notes == ["Brave search failed: OSError.", "DuckDuckGo lite returned HTTP 202."]


def test_firecrawl_sends_bearer_key_when_configured(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.local")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-secret")
    posted = []

    class FirecrawlResponse:
        status_code = 200

        def json(self):
            return {"data": [{"title": "Cabify", "url": "https://cabify.com/", "description": "Mobility."}]}

    def poster(url, **kwargs):
        posted.append(kwargs["headers"])
        return FirecrawlResponse()

    results = flow.web_search_results("Cabify", timeout=3, poster=poster,
                                      fetcher=lambda *a, **k: pytest.fail("fallback not expected"))

    assert results == [{"title": "Cabify", "url": "https://cabify.com/", "snippet": "Mobility."}]
    assert posted[0]["Authorization"] == "Bearer fc-secret"


def test_firecrawl_without_key_sends_no_authorization(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl.local")
    assert "Authorization" not in flow.firecrawl_headers()


def test_throttle_spaces_calls():
    clock = [100.0]
    slept = []

    def sleep(seconds):
        slept.append(round(seconds, 3))
        clock[0] += seconds

    throttle = search.Throttle(1.0, clock=lambda: clock[0], sleep=sleep)
    throttle.wait()
    clock[0] += 0.25
    throttle.wait()
    clock[0] += 5
    throttle.wait()

    assert slept == [0.75]


def test_curl_get_builds_an_http2_capable_request_and_reads_status():
    seen = {}

    class Completed:
        returncode = 0
        stdout = b"<html>ok</html>\n200"

    def runner(command, **kwargs):
        seen["command"] = command
        return Completed()

    response = search.curl_get(search.BRAVE_URL, params={"q": "a b", "source": "web"},
                               headers={"User-Agent": "UA"}, timeout=4, runner=runner)

    assert (response.status_code, response.text) == (200, "<html>ok</html>")
    assert seen["command"][0] == "curl"
    assert seen["command"][-1] == "https://search.brave.com/search?q=a+b&source=web"
    assert "--http1.1" not in seen["command"]
    assert ["-H", "User-Agent: UA"] == seen["command"][seen["command"].index("-H"):seen["command"].index("-H") + 2]


def test_curl_failure_is_an_oserror():
    class Completed:
        returncode = 28
        stdout = b""

    with pytest.raises(OSError):
        search.curl_get("https://x.example", runner=lambda command, **kwargs: Completed())


def test_brave_uses_curl_when_available(monkeypatch):
    monkeypatch.setattr(search.shutil, "which", lambda name: "/usr/bin/curl")
    assert search.default_brave_fetcher() is search.curl_get
    monkeypatch.setattr(search.shutil, "which", lambda name: None)
    assert search.default_brave_fetcher() is search.requests.get
