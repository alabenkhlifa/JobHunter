"""Web search providers for job research: Brave HTML, then DuckDuckGo lite.

Firecrawl (when configured) runs first in ``jobhunter_interest_flow``; this
module holds the keyless fallbacks. Every provider failure returns ``[]`` and,
when a ``notes`` list is passed, records why, so a silent empty result can be
told apart from a blocked provider.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import parse_qs, unquote, urlencode, urlparse

import requests

CHROME_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {
    "User-Agent": CHROME_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,es;q=0.8",
}
BRAVE_URL = "https://search.brave.com/search"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
# Brave answers 429 to bursts; two seconds apart stays well under 1 req/s.
BRAVE_MIN_INTERVAL = 2.0
BRAVE_RETRY_DELAY = 3.0


class Throttle:
    """Space calls at least ``interval`` seconds apart (thread safe)."""

    def __init__(self, interval: float, *, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            if self._last is not None:
                delay = self._last + self.interval - now
                if delay > 0:
                    self._sleep(delay)
                    now = self._clock()
            self._last = now


BRAVE_THROTTLE = Throttle(BRAVE_MIN_INTERVAL)


@dataclass
class CurlResponse:
    status_code: int
    text: str


def curl_get(url: str, *, params: dict | None = None, headers: dict | None = None, timeout: float = 10,
             runner=subprocess.run) -> CurlResponse:
    """GET through the curl binary, which speaks HTTP/2.

    Brave answers HTTP/1.1 clients (requests, urllib) with 429 but serves the
    same request over HTTP/2, so the Brave provider prefers curl.
    """
    full_url = f"{url}?{urlencode(params)}" if params else url
    command = ["curl", "-sS", "-L", "--compressed", "--max-time", str(max(1, int(timeout))),
               "-o", "-", "-w", "\n%{http_code}"]
    for name, value in (headers or {}).items():
        command += ["-H", f"{name}: {value}"]
    command.append(full_url)
    completed = runner(command, capture_output=True, timeout=timeout + 5, check=False)
    if completed.returncode != 0:
        raise OSError(f"curl exited with {completed.returncode}")
    body, _, status = completed.stdout.rpartition(b"\n")
    return CurlResponse(int(status.strip() or 0), body.decode("utf-8", errors="replace"))


def default_brave_fetcher():
    return curl_get if shutil.which("curl") else requests.get


def _text(fragment: str) -> str:
    inline_free = re.sub(r"</?(?:b|strong|em|i|mark)\b[^>]*>", "", fragment or "", flags=re.IGNORECASE)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", inline_free)).split())


def _note(notes: list[str] | None, message: str) -> None:
    if notes is not None:
        notes.append(message)


_BRAVE_BLOCK = re.compile(r'<div class="snippet\b[^"]*"[^>]*data-type="web"', re.IGNORECASE)
_BRAVE_LINK = re.compile(r'<a\b[^>]*href="(https?://[^"]+)"', re.IGNORECASE)
_BRAVE_TITLE = re.compile(r'<div class="title\b[^"]*"[^>]*?(?:title="([^"]*)")?[^>]*>(.*?)</div>', re.I | re.S)
_BRAVE_SNIPPET = re.compile(
    r'<div class="(?:content|snippet-description)\b[^"]*"[^>]*>(.*?)</div>', re.IGNORECASE | re.DOTALL
)


def parse_brave_results(html_text: str, *, limit: int = 10) -> list[dict[str, str]]:
    """Title/url/snippet triples from Brave Search's server-rendered web results."""
    text = html_text or ""
    starts = [match.start() for match in _BRAVE_BLOCK.finditer(text)]
    results: list[dict[str, str]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(text)
        block = text[start:end]
        link = _BRAVE_LINK.search(block)
        title_match = _BRAVE_TITLE.search(block)
        if not link or not title_match:
            continue
        title = _text(title_match.group(1) or "") or _text(title_match.group(2))
        snippet_match = _BRAVE_SNIPPET.search(block)
        snippet = _text(snippet_match.group(1)) if snippet_match else ""
        if title:
            results.append({"title": title, "url": html.unescape(link.group(1)), "snippet": snippet})
        if len(results) >= limit:
            break
    return results


_DDG_LITE = re.compile(
    r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*class=[\"']result-link[\"'][^>]*>(?P<title>.*?)</a>"
    r"(?P<rest>.*?)(?=class=[\"']result-link[\"']|$)",
    re.IGNORECASE | re.DOTALL,
)
_DDG_LITE_SNIPPET = re.compile(r"class=[\"']result-snippet[\"'][^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
_DDG_HTML = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>.*?'
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)


def _unwrap_ddg(href: str) -> str:
    href = html.unescape(href or "")
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        return unquote(parse_qs(parsed.query).get("uddg", [href])[0])
    return href


def parse_ddg_results(html_text: str, *, limit: int = 10) -> list[dict[str, str]]:
    """Parse DuckDuckGo lite markup, or the classic html endpoint as a fallback."""
    results: list[dict[str, str]] = []
    for match in _DDG_LITE.finditer(html_text or ""):
        snippet = _DDG_LITE_SNIPPET.search(match.group("rest"))
        title = _text(match.group("title"))
        url = _unwrap_ddg(match.group("href"))
        if title and url.startswith("http"):
            results.append({"title": title, "url": url, "snippet": _text(snippet.group(1)) if snippet else ""})
        if len(results) >= limit:
            return results
    if results:
        return results
    for match in _DDG_HTML.finditer(html_text or ""):
        title = _text(match.group("title"))
        url = _unwrap_ddg(match.group("href"))
        if title and url:
            results.append({"title": title, "url": url, "snippet": _text(match.group("snippet"))})
        if len(results) >= limit:
            break
    return results


def brave_search(query: str, *, timeout: float, fetcher=None, throttle: Throttle | None = None,
                 notes: list[str] | None = None) -> list[dict[str, str]]:
    throttle = throttle or BRAVE_THROTTLE
    fetcher = fetcher or default_brave_fetcher()
    for attempt in range(2):
        throttle.wait()
        try:
            response = fetcher(BRAVE_URL, params={"q": query, "source": "web"}, headers=dict(BROWSER_HEADERS),
                               timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - provider failure is a note, not an error
            _note(notes, f"Brave search failed: {exc.__class__.__name__}.")
            return []
        status = getattr(response, "status_code", 0)
        if status == 429 and attempt == 0:
            time.sleep(BRAVE_RETRY_DELAY)  # one retry after a rate-limit answer
            continue
        break
    if status != 200:
        _note(notes, f"Brave search returned HTTP {status}.")
        return []
    results = parse_brave_results(getattr(response, "text", "") or "")
    if not results:
        _note(notes, "Brave search returned no parsable results.")
    return results


def ddg_lite_search(query: str, *, timeout: float, fetcher=None,
                    notes: list[str] | None = None) -> list[dict[str, str]]:
    fetcher = fetcher or requests.get
    try:
        response = fetcher(DDG_LITE_URL, params={"q": query}, headers=dict(BROWSER_HEADERS), timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        _note(notes, f"DuckDuckGo lite failed: {exc.__class__.__name__}.")
        return []
    status = getattr(response, "status_code", 0)
    if status != 200:
        _note(notes, f"DuckDuckGo lite returned HTTP {status}.")
        return []
    results = parse_ddg_results(getattr(response, "text", "") or "")
    if not results:
        _note(notes, "DuckDuckGo lite returned no parsable results.")
    return results


def fallback_search(query: str, *, timeout: float, fetcher=None,
                    notes: list[str] | None = None) -> list[dict[str, str]]:
    """Brave HTML (curl, HTTP/2), then DuckDuckGo lite; ``[]`` when both fail.

    An injected ``fetcher`` serves both providers (tests, callers with a session).
    """
    results = brave_search(query, timeout=timeout, fetcher=fetcher, notes=notes)
    if results:
        return results
    return ddg_lite_search(query, timeout=timeout, fetcher=fetcher, notes=notes)
