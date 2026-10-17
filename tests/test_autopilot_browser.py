from datetime import datetime, timezone

import pytest

from jobhunter_auto_apply.cdp import CDPError
from jobhunter_autopilot import browser

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)
HEADED = {"Browser": "Chrome/151.0.7922.173", "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) Chrome/151",
          "webSocketDebuggerUrl": "ws://127.0.0.1:9333/devtools/browser/x"}
HEADLESS = dict(HEADED, Browser="HeadlessChrome/151.0.7922.173")
LI_AT = {"name": "li_at", "value": "secret-session", "domain": ".www.linkedin.com",
         "expires": NOW.timestamp() + 86400 * 300}


class FakeClient:
    def __init__(self, *, window="normal", cookies=(), fail=()):
        self.window, self.cookies, self.fail = window, list(cookies), set(fail)
        self.calls, self.closed = [], False

    def __call__(self, url, timeout=10):
        self.url = url
        return self

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method in self.fail:
            raise CDPError(f"{method} failed")
        if method == "Target.getTargets":
            return {"result": {"targetInfos": [
                {"targetId": "T1", "type": "page", "title": "New Tab", "url": "about:blank"},
                {"targetId": "W1", "type": "service_worker", "title": "", "url": "https://x"}]}}
        if method == "Browser.getWindowForTarget":
            return {"result": {"windowId": 7, "bounds": {"windowState": self.window}}}
        if method == "Storage.getCookies":
            return {"result": {"cookies": self.cookies}}
        if method == "Target.createTarget":
            return {"result": {"targetId": "NEW"}}
        return {"result": {}}

    def close(self):
        self.closed = True


def fetch_returning(version):
    def fetch(url):
        assert url == "http://127.0.0.1:9333/json/version"
        return version
    return fetch


def refused(url):
    raise OSError("connection refused")


def test_status_of_a_headed_signed_in_browser():
    client = FakeClient(cookies=[LI_AT])
    state = browser.status(now=NOW, fetch=fetch_returning(HEADED), connect=client)
    assert state["reachable"] and state["visible"] and state["headless"] is False
    assert state["window_state"] == "normal" and state["tabs"] == [{"title": "New Tab", "url": "about:blank"}]
    assert state["linkedin"] == {"logged_in": True, "expires_at": "2027-08-13T09:00:00+00:00"}
    assert "secret-session" not in repr(state) and client.closed and "hint" not in state


def test_headless_or_minimized_browser_is_not_visible():
    state = browser.status(now=NOW, fetch=fetch_returning(HEADLESS), connect=FakeClient())
    assert state["reachable"] and not state["visible"] and state["headless"] is True
    assert "systemctl --user restart hermes-job-browser.service" in state["hint"]
    state = browser.status(now=NOW, fetch=fetch_returning(HEADED), connect=FakeClient(window="minimized"))
    assert not state["visible"] and state["linkedin"]["logged_in"] is False


def test_unreachable_browser():
    state = browser.status(fetch=refused)
    assert state["reachable"] is False and state["visible"] is False and state["linkedin"]["logged_in"] is None
    assert "not answering" in state["problem"] and "restart" in state["hint"]
    assert browser.summary(fetch=refused) == {"visible": None, "linkedin_logged_in": None}
    assert browser.summary(now=NOW, fetch=fetch_returning(HEADED), connect=FakeClient(cookies=[LI_AT])) == {
        "visible": True, "linkedin_logged_in": True}


def test_cookie_failure_leaves_linkedin_unknown():
    state = browser.status(now=NOW, fetch=fetch_returning(HEADED), connect=FakeClient(fail={"Storage.getCookies"}))
    assert state["visible"] and state["linkedin"] == {"logged_in": None, "expires_at": None}


@pytest.mark.parametrize("cookie, logged_in", [
    (LI_AT, True),
    (dict(LI_AT, expires=-1), True),  # session cookie
    (dict(LI_AT, expires=NOW.timestamp() - 1), False),
    (dict(LI_AT, value=""), False),
    (dict(LI_AT, domain=".linkedin.com.evil.example"), False),
    (dict(LI_AT, name="JSESSIONID"), False),
])
def test_linkedin_session(cookie, logged_in):
    assert browser.linkedin_session([cookie], NOW)["logged_in"] is logged_in


def test_open_tab_brings_a_new_foreground_tab_and_unminimizes():
    client = FakeClient(window="minimized")
    opened = browser.open_tab("https://jobs.example.test/apply/1", fetch=fetch_returning(HEADED), connect=client)
    assert opened == {"tab_id": "NEW", "url": "https://jobs.example.test/apply/1"}
    methods = [method for method, _ in client.calls]
    assert methods == ["Target.createTarget", "Target.activateTarget", "Browser.getWindowForTarget",
                       "Browser.setWindowBounds"]
    assert client.calls[0][1] == {"url": "https://jobs.example.test/apply/1", "background": False}
    assert client.calls[3][1] == {"windowId": 7, "bounds": {"windowState": "normal"}}


def test_open_tab_refuses_other_schemes_and_reports_a_dead_browser():
    with pytest.raises(ValueError):
        browser.open_tab("file:///etc/passwd", fetch=fetch_returning(HEADED), connect=FakeClient())
    with pytest.raises(browser.BrowserUnavailable):
        browser.open_tab("https://jobs.example.test/1", fetch=refused)
    with pytest.raises(browser.BrowserUnavailable):
        browser.open_tab("https://jobs.example.test/1", fetch=fetch_returning(HEADED),
                         connect=FakeClient(fail={"Target.createTarget"}))
