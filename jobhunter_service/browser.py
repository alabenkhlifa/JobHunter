"""Unprivileged client of the fixed, operator-installed browser broker."""

from __future__ import annotations

import json
import math
import re
import subprocess
import time

BROKER_HELPER = "/usr/local/libexec/jobhunter-browser"
PROFILE_PATTERN = re.compile(r"u[1-9][0-9]{0,19}")


class BrowserError(ValueError):
    """A safe browser-service failure suitable for a candidate response."""


class BrowserCapacityError(BrowserError):
    """The Pi's configured single browser slot is occupied."""


def valid_profile(profile_id: str) -> str:
    if not isinstance(profile_id, str) or PROFILE_PATTERN.fullmatch(profile_id) is None:
        raise BrowserError("Invalid JobHunter browser profile.")
    return profile_id


def validate_browser_result(result: dict, profile_id: str) -> dict:
    if not isinstance(result, dict) or result.get("profile_id") != profile_id:
        raise BrowserError("The browser broker returned an invalid session.")
    status = result.get("status")
    if status == "stopped" and set(result) == {"profile_id", "status"}:
        return result
    if status != "running" or set(result) != {"profile_id", "status", "viewer_port", "cdp_port", "expires_at"}:
        raise BrowserError("The browser broker returned an invalid session.")
    for field in ("viewer_port", "cdp_port"):
        if type(result[field]) is not int or not 1024 <= result[field] <= 65535:
            raise BrowserError("The browser broker returned an invalid private port.")
    expiry = result["expires_at"]
    if (type(expiry) not in {int, float} or not math.isfinite(expiry) or
            not time.time() < expiry <= time.time() + 3605 or result["viewer_port"] == result["cdp_port"]):
        raise BrowserError("The browser session expired or returned invalid limits.")
    return result


class DockerBrowserManager:
    """Only start, stop, and inspect a registered candidate's browser.

    No Docker socket is mounted into the service, and no caller can supply an
    image, filesystem path, network, port, container command, or shell text.
    """

    def __init__(self, *, runner=None, timeout: int = 45):
        self._runner = runner or subprocess.run
        self.timeout = timeout

    def _call(self, operation: str, profile_id: str) -> dict:
        valid_profile(profile_id)
        try:
            completed = self._runner(
                ["/usr/bin/sudo", "-n", "--", BROKER_HELPER, operation, profile_id],
                capture_output=True, text=True, timeout=self.timeout,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"},
            )
        except (OSError, subprocess.TimeoutExpired):
            raise BrowserError("Your isolated browser is temporarily unavailable.") from None
        try:
            if len(completed.stdout) > 4096:
                raise ValueError
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError):
            raise BrowserError("The isolated browser broker is unavailable.") from None
        if completed.returncode:
            code = payload.get("error") if isinstance(payload, dict) else None
            if code == "capacity":
                raise BrowserCapacityError("Another application browser is in use. Please retry after it closes; your LinkedIn account remains separate.")
            if code == "inactive":
                raise BrowserError("An active JobHunter registration is required for browser access.")
            raise BrowserError("The isolated browser could not be opened. Contact the JobHunter owner.")
        result = validate_browser_result(payload, profile_id)
        if operation == "start" and result["status"] != "running":
            raise BrowserError("The isolated browser did not start.")
        if operation == "stop" and result["status"] != "stopped":
            raise BrowserError("The isolated browser did not stop.")
        return result

    def start(self, profile_id: str) -> dict:
        return self._call("start", profile_id)

    def stop(self, profile_id: str) -> dict:
        return self._call("stop", profile_id)

    def status(self, profile_id: str) -> dict:
        return self._call("status", profile_id)
