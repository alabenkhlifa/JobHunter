"""The dedicated application browser: Chromium on CDP 9333, shown on the Pi desktop.

``status`` tells whether VNC would show it (reachable, not headless, window not
minimized) and whether its profile holds a LinkedIn session (``li_at`` cookie
present and unexpired; the value is never read out). ``open_tab`` puts a page
in a new foreground tab so a hand-off starts on the right page. Only the
loopback DevTools port is used.
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit

from jobhunter_auto_apply.cdp import CDPClient, CDPError

CDP_HOST = "127.0.0.1"
CDP_PORT = 9333
SERVICE = "hermes-job-browser.service"
LINKEDIN_LOGIN_URL = "https://www.linkedin.com/login"
LINKEDIN_SESSION_COOKIE = "li_at"
RESTART_HINT = f"systemctl --user restart {SERVICE}"


class BrowserUnavailable(RuntimeError):
    """The DevTools port did not answer."""


def _fetch_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback http only
        return json.load(response)


def _version(port, fetch):
    try:
        data = fetch(f"http://{CDP_HOST}:{port}/json/version")
    except Exception as exc:  # noqa: BLE001 - refused, timeout or junk all mean unavailable
        raise BrowserUnavailable(f"DevTools port {port} is not answering ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
        raise BrowserUnavailable(f"DevTools port {port} returned no browser endpoint")
    return data


def is_headless(version):
    text = f"{version.get('Browser', '')} {version.get('User-Agent', '')}"
    return "headless" in text.lower()


def linkedin_session(cookies, now=None):
    """``{"logged_in", "expires_at"}`` from the browser's cookies; the value is never returned."""
    now = (now or datetime.now(timezone.utc)).timestamp()
    found, expiries = False, []
    for cookie in cookies or []:
        if not isinstance(cookie, dict) or cookie.get("name") != LINKEDIN_SESSION_COOKIE or not cookie.get("value"):
            continue
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        if domain != "linkedin.com" and not domain.endswith(".linkedin.com"):
            continue
        expires = cookie.get("expires")
        if isinstance(expires, (int, float)) and expires > 0:
            if expires <= now:
                continue
            expiries.append(expires)
        found = True  # a positive expiry still ahead, or a session cookie
    expires_at = (datetime.fromtimestamp(max(expiries), timezone.utc).isoformat(timespec="seconds")
                  if expiries else None)
    return {"logged_in": found, "expires_at": expires_at}


def _pages(client):
    targets = client.call("Target.getTargets")["result"].get("targetInfos") or []
    return [t for t in targets if t.get("type") == "page"]


def _window_state(client, target_id):
    try:
        found = client.call("Browser.getWindowForTarget", {"targetId": target_id})["result"]
    except CDPError:
        return None, None
    return found.get("windowId"), (found.get("bounds") or {}).get("windowState")


def _connect(version, connect):
    try:
        return connect(version["webSocketDebuggerUrl"], timeout=10)
    except (OSError, CDPError) as exc:
        raise BrowserUnavailable(f"browser endpoint refused the connection ({type(exc).__name__})") from None


def status(*, port=CDP_PORT, now=None, fetch=_fetch_json, connect=CDPClient):
    """What VNC would show and whether LinkedIn is signed in. Never raises."""
    result = {"reachable": False, "visible": False, "headless": None, "window_state": None,
              "tabs": [], "linkedin": {"logged_in": None, "expires_at": None}, "service": SERVICE}
    try:
        version = _version(port, fetch)
    except BrowserUnavailable as exc:
        result["problem"] = str(exc)
        result["hint"] = f"{RESTART_HINT}, then check again"
        return result
    result.update(reachable=True, headless=is_headless(version), browser=version.get("Browser"))
    try:
        client = _connect(version, connect)
    except BrowserUnavailable as exc:
        result["problem"] = str(exc)
        return result
    try:
        pages = _pages(client)
        result["tabs"] = [{"title": str(p.get("title") or "")[:120], "url": str(p.get("url") or "")[:300]}
                          for p in pages[:10]]
        if pages:
            _, result["window_state"] = _window_state(client, pages[0].get("targetId"))
        try:
            cookies = client.call("Storage.getCookies")["result"].get("cookies") or []
            result["linkedin"] = linkedin_session(cookies, now)
        except CDPError:
            pass
    except (OSError, CDPError) as exc:
        result["problem"] = f"browser did not answer ({type(exc).__name__})"
    finally:
        client.close()
    result["visible"] = result["headless"] is False and result["window_state"] not in ("minimized", None)
    if not result["visible"]:
        result["hint"] = (f"the browser runs headless or minimized, so VNC shows nothing; {RESTART_HINT} "
                          "(the unit starts it on the Pi desktop), then check again")
    return result


def summary(**kwargs):
    """The plan's browser facts: ``{"visible", "linkedin_logged_in"}``; None when unknown."""
    state = status(**kwargs)
    return {"visible": state["visible"] if state["reachable"] else None,
            "linkedin_logged_in": state["linkedin"]["logged_in"]}


def open_tab(url, *, port=CDP_PORT, fetch=_fetch_json, connect=CDPClient):
    """Open ``url`` in a new foreground tab and raise the window; returns ``{"tab_id", "url"}``.

    Raises BrowserUnavailable when the port does not answer.
    """
    parts = urlsplit(str(url or ""))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("only http(s) pages can be opened")
    version = _version(port, fetch)
    client = _connect(version, connect)
    try:
        target_id = client.call("Target.createTarget", {"url": url, "background": False})["result"]["targetId"]
        client.call("Target.activateTarget", {"targetId": target_id})
        window_id, state = _window_state(client, target_id)
        if window_id is not None and state == "minimized":
            try:
                client.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": "normal"}})
            except CDPError:
                pass
    except (OSError, CDPError) as exc:
        raise BrowserUnavailable(f"browser did not open the page ({type(exc).__name__})") from None
    finally:
        client.close()
    return {"tab_id": target_id, "url": url}
