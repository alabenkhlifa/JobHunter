"""The policy gate the engine asks before every unattended browser action.

``authorize`` is the only path from the plan to a real form: it re-reads the
policy, requires today's log row to be ``packaged`` or ``applying``, refuses
any second submission, counts the daily cap, and moves the row to
``applying`` inside ``BEGIN IMMEDIATE`` so two engine processes cannot both
take the last slot. Pages on ``codes.MANUAL_HOSTS`` are refused outright.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from . import codes
from . import policy as policy_module
from . import store

ACTIONS = ("inspect", "upload", "submit", "verify-email", "credential")
LINKEDIN_HOSTS = ("linkedin.com",)


class PolicyRefused(PermissionError):
    """The autopilot policy does not allow this action now; ``reason`` is safe to print."""

    def __init__(self, reason, code=None):
        super().__init__(reason)
        self.reason = reason
        self.code = code


def _linkedin(host):
    host = (host or "").lower()
    return any(host == base or host.endswith("." + base) for base in LINKEDIN_HOSTS)


def authorize(db_path, job_id, action, *, page_url, now=None, policy_path=None):
    """Return ``{"run_id", "mode"}`` or raise PolicyRefused."""
    now = now or datetime.now(timezone.utc)
    try:
        policy = policy_module.load(policy_path)
    except policy_module.PolicyError as exc:
        raise PolicyRefused(f"autopilot policy is invalid: {exc}") from None
    if action not in ACTIONS:
        raise PolicyRefused(f"unknown action {action!r}")
    if not policy["enabled"]:
        raise PolicyRefused("autopilot is disabled")
    if policy_module.is_paused(policy, now):
        raise PolicyRefused("autopilot is paused")
    parts = None
    if page_url:
        try:
            parts = urlsplit(str(page_url))
        except ValueError:
            parts = None
        if parts is None or parts.scheme not in ("http", "https") or not parts.netloc:
            raise PolicyRefused("page_url must be an http(s) URL")
    if action != "inspect" and parts is not None and _linkedin(parts.hostname):
        raise PolicyRefused("LinkedIn Easy Apply is not automated", code=codes.LINKEDIN_EASY_APPLY)
    manual = codes.manual_host(page_url) if parts is not None else None
    if manual:
        raise PolicyRefused(f"{codes.ANTI_BOT_ATS}: {manual} blocks automated browsers; hand the job off",
                            code=codes.ANTI_BOT_ATS)
    mode = policy["mode"]
    if action == "submit" and mode != "live":
        raise PolicyRefused("rehearsal mode: the final submit is refused")
    run_id = store.run_id_for(now, policy["timezone"])

    path = Path(db_path).expanduser().resolve()
    if not path.exists():
        raise PolicyRefused("database not found")
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    try:
        store.ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT status FROM auto_apply_log WHERE run_id=? AND job_id=?",
                               (run_id, job_id)).fetchone()
            if row is None or row[0] not in store.ACTIVE_STATUSES:
                raise PolicyRefused(f"job {job_id} is not planned for today's run"
                                    + (f" (status {row[0]})" if row else ""))
            if action == "submit" and store.already_submitted(conn, job_id):
                raise PolicyRefused("this application was already submitted or attempted")
            if row[0] == "packaged" or action == "submit":
                others = conn.execute(
                    f"SELECT COUNT(*) FROM auto_apply_log WHERE run_id=? AND job_id<>? AND status IN "
                    f"({','.join('?' * len(store.CAP_STATUSES))})",
                    (run_id, job_id, *store.CAP_STATUSES)).fetchone()[0]
                if others >= policy["max_per_day"]:
                    raise PolicyRefused(f"daily cap of {policy['max_per_day']} reached")
            if row[0] == "packaged":
                conn.execute("UPDATE auto_apply_log SET status='applying', updated_at=? WHERE run_id=? AND job_id=?",
                             (store.iso(now), run_id, job_id))
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()
    return {"run_id": run_id, "mode": mode}
