"""The pre-run's second look at LinkedIn listings in the signed-in browser: fake CDP only."""

from contextlib import closing
from datetime import datetime, timezone
from functools import partial
import sqlite3
from urllib.error import URLError

import pytest

import jobhunter_availability
from jobhunter_autopilot import availability

NOW = datetime(2026, 10, 17, 3, 16, tzinfo=timezone.utc)
JOB = {"id": "li-4475459352", "source": "LinkedIn", "title": "Senior Software Engineer", "company": "Ingram Micro",
       "url": "https://es.linkedin.com/jobs/view/senior-software-engineer-at-ingram-micro-4475459352"}
OTHER = {"id": "li-4478799309", "source": "LinkedIn", "title": "Senior Java Developer", "company": "Luxoft Spain",
         "url": "https://es.linkedin.com/jobs/view/senior-java-developer-at-luxoft-spain-4478799309"}
BOARD = {"id": "sdj-7ufonly", "source": "Spain Dev Jobs", "title": "Senior Java Software Developer (m/f/d)",
         "company": "Allianz", "url": "https://spain-devjobs.com/jobs/allianz-senior-java-software-developer-m-f-d-7ufonly"}
VERSION = {"Browser": "Chrome/151.0.7922.173", "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/abc"}
CLOSED = "Not currently accepting applications"
EASY_APPLY = {"tag": "button", "label": "Easy Apply", "aria": "Easy Apply to this job", "href": "", "disabled": False}


def seen(job=JOB, *, texts=(), controls=(), found=True, href=None, title=None):
    """One read of the signed-in top card, as _TOP_CARD_JS returns it (shapes from the Pi, 2026-10-17)."""
    number = job["id"].split("-")[-1]
    return {"href": href or f"https://www.linkedin.com/jobs/view/{number}/",
            "title": title or f"{job['title']} | {job['company']} | LinkedIn", "found": found,
            "texts": ["Barcelona, Catalonia, Spain · 1 week ago · 55 people clicked apply", "On-site", *texts],
            "controls": [{"tag": "button", "label": "On-site", "aria": "", "href": "", "disabled": False}, *controls]}


def http_unknown(job, reason="no_open_evidence"):
    listing = jobhunter_availability._listing(job)
    return {"state": "unknown", "reason": reason, "verification_scope": "source_listing",
            "checked_at": NOW.isoformat(), "job_id": job["id"], "url": listing.url,
            "source_job_id": listing.source_id, "matched": reason == "no_open_evidence"}


def http_open(job):
    return {**http_unknown(job), "state": "open", "reason": "application_control", "matched": True}


class Page:
    def __init__(self, reads):
        self.reads, self.expressions, self.closed = list(reads), [], False

    def evaluate(self, expression):
        self.expressions.append(expression)
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        if isinstance(read, Exception):
            raise read
        return read

    def close(self):
        self.closed = True


class Chromium:
    """Browser and page endpoints; reads are keyed by the URL each tab was opened on."""

    def __init__(self, reads):
        self.reads, self.calls, self.pages, self.urls, self.closed = reads, [], [], {}, False

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "Target.createTarget":
            target = f"T{len(self.urls) + 1}"
            self.urls[target] = params["url"]
            return {"result": {"targetId": target}}
        return {"result": {}}

    def close(self):
        self.closed = True

    def connect(self, url, timeout):
        if url == VERSION["webSocketDebuggerUrl"]:
            return self
        assert url.startswith("ws://127.0.0.1:9333/devtools/page/")
        page = Page(self.reads[self.urls[url.rsplit("/", 1)[1]]])
        self.pages.append(page)
        return page

    def opened(self):
        return [params["url"] for method, params in self.calls if method == "Target.createTarget"]

    def closed_tabs(self):
        return [params["targetId"] for method, params in self.calls if method == "Target.closeTarget"]


class Clock:
    def __init__(self, step=1.0):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


def confirm_with(chromium, clock=None, fetch=None):
    return partial(availability.confirm_in_browser, fetch=fetch or (lambda url: VERSION), connect=chromium.connect,
                   clock=clock or Clock(), sleep=lambda seconds: None, now=NOW)


def listing_url(job):
    return jobhunter_availability._listing(job).url


def test_closed_listing_the_anonymous_page_left_unknown_is_closed_by_the_signed_in_page(tmp_path):
    chromium = Chromium({listing_url(JOB): [seen(texts=[CLOSED])]})
    results = availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium))
    result = results[JOB["id"]]
    assert (result["state"], result["reason"], result["matched"]) == ("closed", "closed_marker", True)
    assert result["checked_via"] == "browser" and availability.state_of(result) == "closed"
    # One background tab on the canonical listing URL, closed again.
    assert chromium.calls[0] == ("Target.createTarget", {"url": "https://www.linkedin.com/jobs/view/4475459352",
                                                         "background": True})
    assert chromium.closed_tabs() == ["T1"] and chromium.pages[0].closed and chromium.closed
    # The evidence is the shape the anonymous check records.
    with closing(sqlite3.connect(tmp_path / "jobs.db")) as conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, url TEXT, status TEXT)")
        conn.execute("INSERT INTO jobs VALUES(?,?,'new')", (JOB["id"], JOB["url"]))
        assert availability.record(conn, JOB, result) == "closed"
        assert conn.execute("SELECT status FROM jobs").fetchone() == ("unavailable",)


def test_apply_button_on_the_signed_in_page_opens_a_listing_the_anonymous_check_could_not_read():
    chromium = Chromium({listing_url(JOB): [seen(controls=[EASY_APPLY])]})
    result = availability.check_jobs([JOB], check=partial(http_unknown, reason="http_unavailable"),
                                     confirm=confirm_with(chromium))[JOB["id"]]
    assert (result["state"], result["reason"], result["matched"]) == ("open", "application_control", True)
    offsite = {"tag": "a", "label": "Apply", "aria": "Apply on company website",
               "href": "https://www.linkedin.com/jobs/view/externalApply/4475459352", "disabled": False}
    assert availability.browser_result(JOB, seen(controls=[offsite]))["state"] == "open"


def test_closed_marker_wins_over_an_apply_button():
    assert availability.browser_result(JOB, seen(texts=[CLOSED], controls=[EASY_APPLY]))["state"] == "closed"


@pytest.mark.parametrize("read", [
    seen(texts=[CLOSED], title="Senior Software Engineer | Another Company | LinkedIn"),
    seen(texts=[CLOSED], title="Staff Engineer | Ingram Micro | LinkedIn"),
    seen(texts=[CLOSED], href="https://www.linkedin.com/jobs/view/4479890909/"),
    seen(texts=[CLOSED], href="https://www.linkedin.com/login?session_redirect=x"),
    seen(texts=[CLOSED], href="https://www.linkedin.com/checkpoint/challenge/x"),
    seen(texts=[CLOSED], found=False),
    seen(texts=["Similar jobs are no longer accepting applications"]),
    seen(controls=[{**EASY_APPLY, "disabled": True}]),
    seen(controls=[{"tag": "a", "label": "Apply", "aria": "", "href": "#", "disabled": False}]),
    seen(controls=[{"tag": "a", "label": "Apply", "aria": "", "href": "javascript:apply()", "disabled": False}]),
    seen(controls=[{**EASY_APPLY, "label": "Applied 2 days ago"}]),
    "not a read", None,
], ids=["other-company", "other-title", "other-job", "login", "checkpoint", "no-top-card", "quoted-marker",
        "disabled", "anchor", "script-link", "already-applied", "junk", "nothing"])
def test_unidentified_or_inconclusive_reads_leave_the_anonymous_result(read):
    chromium = Chromium({listing_url(JOB): [read]})
    result = availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium))[JOB["id"]]
    assert result == http_unknown(JOB)
    assert chromium.closed_tabs() == ["T1"]


def test_notification_count_in_the_page_title_still_identifies_the_listing():
    read = seen(texts=[CLOSED], title="(3) Senior Software Engineer | Ingram Micro | LinkedIn")
    assert availability.browser_result(JOB, read)["state"] == "closed"
    company_with_bar = {**JOB, "company": "Amaris Consulting | Part of Mantu"}
    read = seen(company_with_bar, texts=[CLOSED], title="Senior Software Engineer | Amaris Consulting  | Part of Mantu | LinkedIn")
    assert availability.browser_result(company_with_bar, read)["state"] == "closed"


def test_page_is_polled_until_the_top_card_renders():
    chromium = Chromium({listing_url(JOB): [RuntimeError("Execution context was destroyed"), seen(found=False),
                                            seen(texts=[CLOSED])]})
    result = availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium))[JOB["id"]]
    assert result["state"] == "closed" and len(chromium.pages[0].expressions) == 3


def test_a_page_that_never_decides_is_given_up_at_its_deadline():
    chromium = Chromium({listing_url(JOB): [seen()]})
    clock = Clock(step=1.0)
    result = availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium, clock))[JOB["id"]]
    assert result == http_unknown(JOB)
    assert len(chromium.pages[0].expressions) <= availability.BROWSER_PAGE_SECONDS + 1
    assert chromium.closed_tabs() == ["T1"]


def test_total_browser_time_is_bounded():
    chromium = Chromium({listing_url(JOB): [seen()], listing_url(OTHER): [seen(OTHER, controls=[EASY_APPLY])]})
    clock = Clock(step=availability.BROWSER_TOTAL_SECONDS / 4)
    results = availability.check_jobs([JOB, OTHER], check=http_unknown, confirm=confirm_with(chromium, clock))
    assert chromium.opened() == ["https://www.linkedin.com/jobs/view/4475459352"]
    assert results[OTHER["id"]] == http_unknown(OTHER)


@pytest.mark.parametrize("fetch", [URLError("refused"), TimeoutError("slow"), {"Browser": "no endpoint"},
                                   {"webSocketDebuggerUrl": "ws://192.168.1.20:9333/devtools/browser/abc"}])
def test_browser_that_is_down_or_not_on_loopback_never_fails_the_plan(fetch):
    def answer(url):
        assert url == "http://127.0.0.1:9333/json/version"
        if isinstance(fetch, Exception):
            raise fetch
        return fetch

    chromium = Chromium({})
    results = availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium, fetch=answer))
    assert results == {JOB["id"]: http_unknown(JOB)} and chromium.calls == []


def test_browser_that_dies_mid_run_keeps_the_anonymous_results():
    class Dying(Chromium):
        def call(self, method, params=None):
            raise ConnectionResetError("browser went away")

    chromium = Dying({})
    results = availability.check_jobs([JOB, OTHER], check=http_unknown, confirm=confirm_with(chromium))
    assert results == {JOB["id"]: http_unknown(JOB), OTHER["id"]: http_unknown(OTHER)} and chromium.closed


def test_only_linkedin_listings_left_unknown_get_the_second_look():
    asked = []

    def confirm(jobs):
        asked.append([job["id"] for job in jobs])
        return {job["id"]: {**http_open(job), "checked_via": "browser"} for job in jobs}

    def check(job):
        return http_open(job) if job is OTHER else http_unknown(job, "http_unavailable")

    results = availability.check_jobs([JOB, OTHER, BOARD], check=check, confirm=confirm)
    assert asked == [[JOB["id"]]]
    assert results[JOB["id"]]["checked_via"] == "browser" and results[OTHER["id"]] == http_open(OTHER)
    assert results[BOARD["id"]]["reason"] == "http_unavailable"


def test_undecided_or_crashing_confirmation_keeps_the_anonymous_result():
    unknown = {JOB["id"]: {**http_unknown(JOB), "reason": "login_or_challenge", "matched": False}}
    assert availability.check_jobs([JOB], check=http_unknown, confirm=lambda jobs: unknown) == {JOB["id"]: http_unknown(JOB)}

    def crash(jobs):
        raise RuntimeError("boom")

    assert availability.check_jobs([JOB], check=http_unknown, confirm=crash) == {JOB["id"]: http_unknown(JOB)}


def test_injected_check_skips_the_browser_and_the_default_uses_it(monkeypatch):
    def forbidden(jobs):
        raise AssertionError("tests that inject a check must not reach the browser")

    monkeypatch.setattr(availability, "confirm_in_browser", forbidden)
    assert availability.check_jobs([JOB], check=http_unknown) == {JOB["id"]: http_unknown(JOB)}

    asked = []
    monkeypatch.setattr(jobhunter_availability, "check", http_unknown)
    monkeypatch.setattr(availability, "confirm_in_browser", lambda jobs: asked.append(jobs) or {})
    availability.check_jobs([JOB])
    assert asked == [[JOB]]


def test_the_browser_check_only_reads_the_page():
    chromium = Chromium({listing_url(JOB): [seen(controls=[EASY_APPLY])]})
    availability.check_jobs([JOB], check=http_unknown, confirm=confirm_with(chromium))
    assert {method for method, _ in chromium.calls} == {"Target.createTarget", "Target.closeTarget"}
    [expression] = chromium.pages[0].expressions
    for action in (".click(", "dispatchEvent", ".submit(", ".focus(", "location.href =", "location.assign",
                   ".value =", "window.open", "fetch("):
        assert action not in expression
    assert '"Senior Software Engineer"' in expression
