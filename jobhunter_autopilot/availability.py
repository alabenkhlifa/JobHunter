"""Re-check that selected offers are still open before any work is spent on them.

Closed listings become ``unavailable`` (archived ones too, which
``jobhunter_availability.recordcheck`` leaves alone). A closed listing that
already has a tracker row goes through the listing monitor, which owns the
Sheet closure and the Telegram alert; it reuses this run's check instead of
fetching the listing again. ``unknown`` defers the job to the next run.

LinkedIn's anonymous page no longer states that a listing is closed: the
top card just loses its Apply and Save buttons. So a LinkedIn listing the
anonymous check leaves ``unknown`` is read once more in the signed-in
application browser (CDP 9333), in a background tab that is closed again.
That page says "Not currently accepting applications" or shows the Apply
button. The browser check only reads the page: it never clicks or types.
A browser that is down, slow or signed out leaves the anonymous result in
place.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.request
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import jobhunter_availability

from . import store

WORKERS = 3
CLOSED_REASONS = ("closed_marker", "expired_valid_through")
REPO = Path(__file__).resolve().parent.parent
BROWSER_HOST = "127.0.0.1"
BROWSER_PORT = 9333
BROWSER_PAGE_SECONDS = 20
BROWSER_TOTAL_SECONDS = 90
BROWSER_POLL_SECONDS = 0.5
_LOGIN_PATHS = ("/login", "/uas/", "/authwall", "/checkpoint", "/signup")
# Reads the top card only: from the job title to the next heading ("Use AI to
# assess how you fit" or "About the job"). Read-only: no clicks, no input.
_TOP_CARD_JS = r"""((title) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const out = {href: location.href, title: document.title, found: false, texts: [], controls: []};
  const main = document.querySelector('main');
  if (!main) return out;
  const want = norm(title).toLowerCase();
  const walker = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  let start = null;
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (norm(node.nodeValue).toLowerCase() === want) { start = node.parentElement; break; }
  }
  if (!start) return out;
  const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const end = [...main.querySelectorAll('h1, h2, h3, h4')].find(h => follows(start, h) && !start.contains(h) && norm(h.innerText));
  if (!end) return out;
  const shown = e => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const inCard = e => follows(start, e) && follows(e, end) && !e.contains(end);
  out.found = true;
  const texts = document.createTreeWalker(main, NodeFilter.SHOW_TEXT);
  for (let node = texts.nextNode(); node && out.texts.length < 200; node = texts.nextNode()) {
    const text = norm(node.nodeValue);
    if (text && text.length <= 200 && node.parentElement && inCard(node) && shown(node.parentElement)) out.texts.push(text);
  }
  for (const e of main.querySelectorAll('button, a')) {
    if (out.controls.length >= 40) break;
    if (!inCard(e) || !shown(e)) continue;
    out.controls.push({tag: e.tagName.toLowerCase(), label: norm(e.innerText), aria: e.getAttribute('aria-label') || '',
                       href: e.getAttribute('href') || '', disabled: !!e.disabled || e.getAttribute('aria-disabled') === 'true'});
  }
  return out;
})"""


def _unknown(job, reason="request_failed"):
    return {"state": "unknown", "reason": reason, "verification_scope": "source_listing",
            "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "job_id": str(job.get("id") or ""), "url": "", "source_job_id": "", "matched": False}


def check_jobs(jobs, *, check=None, workers=WORKERS, confirm=None):
    """{job_id: result} for each job, checked three at a time; a crash reads as unknown.

    LinkedIn listings left unknown go through ``confirm`` (the signed-in
    browser by default; off when a test injects ``check``).
    """
    if confirm is None and check is None:
        confirm = confirm_in_browser
    check = check or jobhunter_availability.check

    def one(job):
        try:
            result = check(job)
        except Exception:  # noqa: BLE001 - one broken listing must not stop the run
            return job["id"], _unknown(job)
        return job["id"], result if isinstance(result, dict) else _unknown(job)

    if not jobs:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        results = dict(pool.map(one, jobs))
    pending = [job for job in jobs if state_of(results[job["id"]]) == "unknown" and _linkedin(job)]
    if confirm and pending:
        try:
            confirmed = confirm(pending) or {}
        except Exception:  # noqa: BLE001 - the browser is a best-effort second look
            confirmed = {}
        for job_id, result in confirmed.items():
            if job_id in results and isinstance(result, dict) and state_of(result) != "unknown":
                results[job_id] = result
    return results


def _linkedin(job):
    listing = jobhunter_availability._listing(job)
    return listing is not None and listing.source == "linkedin"


def _fetch_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback http only
        return json.load(response)


def _norm(value):
    return " ".join(str(value or "").split()).casefold()


def _same_listing_title(page_title, job):
    """LinkedIn titles the page "<title> | <company> | LinkedIn"."""
    shown = _norm(re.sub(r"^\(\d+\+?\)\s*", "", str(page_title or "").strip()))
    title, company = _norm(job.get("title")), _norm(job.get("company"))
    if not title or not shown.endswith(" | linkedin"):
        return False
    if company and company != "unknown":
        return shown == f"{title} | {company} | linkedin"
    return shown.startswith(f"{title} | ")


def browser_result(job, seen, *, now=None):
    """The anonymous check's result shape from one top-card read; None when nothing was read."""
    listing = jobhunter_availability._listing(job)
    if listing is None or listing.source != "linkedin" or not isinstance(seen, dict):
        return None
    result = {"state": "unknown", "reason": "listing_unverified", "verification_scope": "source_listing",
              "checked_via": "browser",
              "checked_at": (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
              "job_id": str(job.get("id") or ""), "url": listing.url, "source_job_id": listing.source_id,
              "matched": False}
    parts = urlsplit(str(seen.get("href") or ""))
    if parts.path.startswith(_LOGIN_PATHS):
        result["reason"] = "login_or_challenge"
        return result
    page_url = f"https://{parts.hostname}{parts.path}" if parts.scheme == "https" and parts.hostname else ""
    if (jobhunter_availability._url_identity(page_url) != ("linkedin", listing.source_id)
            or not _same_listing_title(seen.get("title"), job) or seen.get("found") is not True):
        return result
    result.update(matched=True, reason="no_open_evidence")
    texts = [" ".join(str(text).split()) for text in seen.get("texts") or [] if isinstance(text, str)]
    if any(jobhunter_availability.CLOSED.fullmatch(text) for text in texts):
        result.update(state="closed", reason="closed_marker")
        return result
    for control in seen.get("controls") or []:
        if not isinstance(control, dict) or control.get("disabled"):
            continue
        label = " ".join(str(control.get("label") or control.get("aria") or "").split())
        href = str(control.get("href") or "")
        actionable = control.get("tag") == "button" or (
            href and not href.startswith("#") and jobhunter_availability._parsed_url(urljoin(page_url, href)))
        if actionable and jobhunter_availability.APPLY.fullmatch(label):
            result.update(state="open", reason="application_control")
            return result
    return result


def _read_top_card(browser, page_base, job, listing, *, connect, clock, sleep, deadline):
    target_id = browser.call("Target.createTarget", {"url": listing.url, "background": True})["result"]["targetId"]
    page, seen = None, None
    try:
        page = connect(f"{page_base}/devtools/page/{target_id}", timeout=5)
        expression = f"{_TOP_CARD_JS}({json.dumps(str(job.get('title') or ''))})"
        while True:
            try:
                seen = page.evaluate(expression)
            except Exception:  # noqa: BLE001 - the page is still loading or navigating
                pass
            read = browser_result(job, seen)
            if read and (read["state"] != "unknown" or read["reason"] == "login_or_challenge") or clock() >= deadline:
                return seen
            sleep(BROWSER_POLL_SECONDS)
    finally:
        if page is not None:
            page.close()
        try:
            browser.call("Target.closeTarget", {"targetId": target_id})
        except Exception:  # noqa: BLE001 - a crashed tab is already gone
            pass


def confirm_in_browser(jobs, *, port=BROWSER_PORT, fetch=_fetch_json, connect=None,
                       clock=time.monotonic, sleep=time.sleep, now=None):
    """{job_id: result} for LinkedIn listings read in the signed-in browser.

    One background tab at a time, 20 s each and 90 s in all. Never raises:
    a browser that is down returns {}.
    """
    if connect is None:
        from jobhunter_auto_apply.cdp import CDPClient as connect
    try:
        endpoint = str(fetch(f"http://{BROWSER_HOST}:{port}/json/version").get("webSocketDebuggerUrl") or "")
    except Exception:  # noqa: BLE001 - refused, timeout or junk all mean no browser
        return {}
    parts = urlsplit(endpoint)
    if parts.scheme != "ws" or parts.hostname not in ("127.0.0.1", "localhost"):
        return {}
    try:
        browser = connect(endpoint, timeout=5)
    except Exception:  # noqa: BLE001
        return {}
    results, stop = {}, clock() + BROWSER_TOTAL_SECONDS
    try:
        for job in jobs:
            listing = jobhunter_availability._listing(job)
            if listing is None or listing.source != "linkedin":
                continue
            if clock() >= stop:
                break
            try:
                seen = _read_top_card(browser, f"ws://{parts.netloc}", job, listing, connect=connect, clock=clock,
                                      sleep=sleep, deadline=min(stop, clock() + BROWSER_PAGE_SECONDS))
            except Exception:  # noqa: BLE001 - the browser went away mid-run
                break
            result = browser_result(job, seen, now=now)
            if result is not None:
                results[job["id"]] = result
    finally:
        browser.close()
    return results


def state_of(result):
    """open | closed | unknown, trusting only matched evidence."""
    state = result.get("state")
    if state == "closed" and result.get("matched") is True and result.get("reason") in CLOSED_REASONS:
        return "closed"
    if state == "open" and result.get("matched") is True:
        return "open"
    return "unknown"


def tracked(conn, job_id):
    if not store.table_exists(conn, "applications"):
        return False
    return conn.execute("SELECT 1 FROM applications WHERE job_id=? LIMIT 1", (job_id,)).fetchone() is not None


def record(conn, job, result, *, owned_interested=False):
    """Persist one check like recordcheck; returns the state actually trusted."""
    state = state_of(result)
    try:
        jobhunter_availability.recordcheck(conn, job, result)
    except ValueError:
        # Evidence for another listing, or the job row changed underneath us.
        return "unknown"
    if state == "closed":
        statuses = ("archived", "interested") if owned_interested else ("archived",)
        conn.execute(f"UPDATE jobs SET status='unavailable' WHERE id=? AND status IN "
                     f"({','.join('?' * len(statuses))})", (job["id"], *statuses))
    conn.commit()
    return state


def _tracker_args(db_path):
    """Listing-monitor arguments from the deployment .env; None when the tracker is not configured."""
    from dotenv import load_dotenv
    from jobhunter_integrations import google_tracker

    load_dotenv(REPO / ".env")
    if not os.getenv("JOBHUNTER_TRACKER_SPREADSHEET_ID"):
        return None
    try:
        args = google_tracker.parse_args([])
    except SystemExit:
        return None
    args.db_path = Path(db_path).resolve()
    args.use_recorded_closures = False
    return args


def sync_tracked_closures(db_path, results, *, send, monitor=None, args=None):
    """Run the listing monitor once with this run's cached checks.

    Returns ``({job_id: sheet_synced}, note)``. Never raises: a monitor failure
    leaves the closure queued for the 17:00 listing check.
    """
    closed_ids = sorted(results)
    if not closed_ids:
        return {}, None
    if monitor is None:
        from jobhunter_integrations import listing_monitor
        monitor = listing_monitor.run_monitor
    args = args if args is not None else _tracker_args(db_path)
    if args is None:
        return {job_id: False for job_id in closed_ids}, "tracker not configured"

    def cached_check(job):
        cached = results.get(str(job.get("id")))
        return cached if cached is not None else jobhunter_availability.check(job)

    note = None
    try:
        outcome = monitor(args, send, check=cached_check)
        if isinstance(outcome, dict) and outcome.get("skipped_overlap"):
            note = "listing monitor busy; closure retried at the next listing check"
    except Exception as exc:  # noqa: BLE001 - provider errors can hold credentials
        note = f"listing monitor failed ({type(exc).__name__}); closure retried at the next listing check"
    synced = {}
    with closing(sqlite3.connect(str(Path(db_path).resolve()), timeout=30)) as conn:
        has_table = store.table_exists(conn, "tracker_listing_closures")
        for job_id in closed_ids:
            row = conn.execute(
                "SELECT tracker_synced_at FROM tracker_listing_closures WHERE job_id=? ORDER BY closed_at DESC LIMIT 1",
                (job_id,)).fetchone() if has_table else None
            synced[job_id] = bool(row and row[0])
    return synced, note
