"""Archive superseded per-candidate Google state without deleting its history."""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .state import private_json

GMAIL = ("secrets/gmail_token.json", "secrets/gmail_account.json", "state/gmail_seen.json", "state/gmail_seen.outbox.json")
TRACKER_DATA = ("state/tracker_setup.json", "state/tracker_connection.json", "state/tracker_drive_files.json",
                "state/tracker_sync_state.json", "state/tracker_before_sync.json")
TRACKER_AUTH = ("secrets/tracker_token.json", "secrets/tracker_account.json")


def _account_settings(settings):
    accounts = (settings or {}).get("accounts", {})
    gmail, tracker = accounts.get("gmail", {}), accounts.get("tracker", {})
    return {"gmail": {"account": str(gmail.get("account") or "").strip().casefold()},
            "tracker": {"account": str(tracker.get("account") or "").strip().casefold(),
                        "spreadsheet_id": str(tracker.get("spreadsheet_id") or ""),
                        "viewer_email": str(tracker.get("viewer_email") or "").strip().casefold()}}


def connection_matches(info, account_settings):
    """Connection metadata must match the separately confirmed tracker intent."""
    if not isinstance(info, dict) or not isinstance(account_settings, dict):
        return False
    wanted = _account_settings({"accounts": {"tracker": account_settings}})["tracker"]
    return (bool(wanted["account"]) and info.get("account") == wanted["account"]
            and (info.get("configured_spreadsheet_id") or "") == wanted["spreadsheet_id"]
            and (info.get("viewer_email") or "") == wanted["viewer_email"]
            and bool(info.get("spreadsheet_id")) and type(info.get("sheet_id")) is int)


def _path(root, relative):
    path = root / relative
    if not path.resolve().is_relative_to(root) or any(parent.is_symlink() for parent in (path, *path.parents) if parent.is_relative_to(root)):
        raise PermissionError("Google account state cannot follow paths outside this candidate.")
    return path


def _identity(path):
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("Google account state must contain regular private files.")
    stat = path.stat()
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {"device": stat.st_dev, "inode": stat.st_ino, "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns, "sha256": digest}


@contextmanager
def _lock(path):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("A Google account operation is running. Retry this account change when it finishes.") from None
        yield
    finally:
        os.close(fd)


def reconcile_account_settings(root, previous_settings, new_settings):
    """Move superseded files to a private archive before new grants are saved.

    The service calls this before writing settings.json and before OAuth token
    persistence. Journals record original file identities, so a crash replay
    never mistakes a subsequently saved new grant for an old file to archive.
    """
    root = Path(root).resolve()
    target = _account_settings(new_settings)
    checkpoint = _path(root, "state/account_state.json")
    with _lock(_path(root, "state/account_transition.lock")):
        recorded = json.loads(checkpoint.read_text()) if checkpoint.exists() else None
        if recorded is not None and recorded.get("accounts") == target:
            return
        previous = recorded["accounts"] if recorded is not None else (_account_settings(previous_settings) if previous_settings is not None else target)
        generation = recorded.get("generation", 0) if recorded is not None else 0
        affected = []
        locks = []
        if previous["gmail"] != target["gmail"]:
            affected.extend(GMAIL)
            locks.append("state/gmail_seen.lock")
        if previous["tracker"]["account"] != target["tracker"]["account"]:
            affected.extend((*TRACKER_AUTH, *TRACKER_DATA))
            locks.extend(("state/tracker_setup.lock", "state/tracker_sync.lock"))
        elif previous["tracker"]["spreadsheet_id"] != target["tracker"]["spreadsheet_id"]:
            affected.extend(TRACKER_DATA)
            locks.extend(("state/tracker_setup.lock", "state/tracker_sync.lock"))
        elif previous["tracker"]["viewer_email"] != target["tracker"]["viewer_email"]:
            affected.append("state/tracker_connection.json")
            locks.extend(("state/tracker_setup.lock", "state/tracker_sync.lock"))
        with ExitStack() as stack:
            for name in locks:
                stack.enter_context(_lock(_path(root, name)))
            transition_id = hashlib.sha256(json.dumps([generation, previous, target], sort_keys=True).encode()).hexdigest()
            journal_path = _path(root, f"state/account_changes/{transition_id}.json")
            if affected:
                if journal_path.exists():
                    journal = json.loads(journal_path.read_text())
                    if journal.get("previous") != previous or journal.get("target") != target:
                        raise ValueError("Account transition state is inconsistent; restore its private journal.")
                else:
                    journal = {"previous": previous, "target": target,
                               "files": {name: _identity(_path(root, name)) for name in affected}}
                    private_json(journal_path, journal)
                for name, identity in journal["files"].items():
                    if name not in affected or identity is None:
                        continue
                    source = _path(root, name)
                    archive = _path(root, f"state/account_history/{transition_id}/{name}")
                    if archive.exists() or not source.exists():
                        continue
                    if _identity(source) != identity:
                        # The original was already moved or replaced by a new
                        # connection before the checkpoint completed.
                        continue
                    archive.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    os.replace(source, archive)
            private_json(checkpoint, {"version": 1, "generation": generation + 1, "accounts": target})
