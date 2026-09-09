"""Send scheduled application-reply alerts with a private, retryable outbox.

Success is silent because this module delivers directly to JobHunter's Telegram
chat. Hermes should deliver only failures. Gmail permissions remain read-only.
"""

from __future__ import annotations

import fcntl
import os

from jobhunter_integrations import gmail_watcher
from jobhunter_integrations.gmail_auth import GmailAuthError, write_private_json


def notification_batches(matches):
    """Keep every reply visible and each HTML message within Telegram's limit."""
    batches = []
    batch = []
    for match in matches:
        item = dict(match)
        # Bound untrusted headers before escaping; never truncate HTML entities.
        for field, limit in (("subject", 120), ("from", 160), ("date", 64)):
            if field in item:
                item[field] = str(item[field])[:limit]
        if item.get("matched_job"):
            item["matched_job"] = {
                key: str(value)[:200] for key, value in item["matched_job"].items()
            }
        item["reasons"] = [str(reason)[:60] for reason in item.get("reasons", [])[:2]]
        candidate = gmail_watcher.format_alert([*batch, item])
        if batch and (len(batch) == 5 or len(candidate) > 3500):
            batches.append(gmail_watcher.format_alert(batch))
            batch = []
        batch.append(item)
    if batch:
        batches.append(gmail_watcher.format_alert(batch))
    return batches


def run_monitor(args, send) -> None:
    if args.max_messages <= 0:
        raise ValueError("The mail inspection limit must be positive.")
    state_path = args.state_path
    state_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_path.with_suffix(".lock")
    outbox_path = state_path.with_suffix(".outbox.json")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        if outbox_path.exists():
            pending = gmail_watcher.load_json(outbox_path, {})
            if not isinstance(pending.get("notifications"), list) or not isinstance(pending.get("state"), dict):
                raise ValueError("Invalid mail outbox; restore it before retrying.")
        else:
            matches, state = gmail_watcher.collect_mail(args)
            pending = {"notifications": notification_batches(matches), "state": state}
            write_private_json(outbox_path, pending)
        while pending["notifications"]:
            if not send(pending["notifications"][0]):
                raise RuntimeError("Telegram did not confirm delivery; the mail alert remains queued.")
            pending["notifications"].pop(0)
            write_private_json(outbox_path, pending)
        # Keep the outbox until the ledger is durable. If saving fails, retry
        # only the checkpoint, without sending already acknowledged messages.
        write_private_json(state_path, pending["state"])
        outbox_path.unlink()


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv(gmail_watcher.default_repo_root() / ".env")
    args = gmail_watcher.parse_args(argv)
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("JobHunter mail check failed: Telegram configuration is missing.")
        return 1
    from scraper import _env_flag, send_telegram, sync_application_tracker_if_enabled
    try:
        run_monitor(args, lambda message: send_telegram(token, chat, message))
        # Retry an earlier tracker failure even when no new email changes a stage.
        if _env_flag("JOBHUNTER_AUTO_SYNC_TRACKER") and not sync_application_tracker_if_enabled():
            print("JobHunter mail check completed, but the application tracker sync failed; it will retry at the next check.")
            return 1
    except GmailAuthError as exc:
        print(f"JobHunter mail check failed: {exc}")
        return 1
    except Exception:
        # Provider exceptions can contain tokens, mailbox data or URLs.
        print("JobHunter mail check failed. Pending alerts are retained; check Gmail access, Telegram delivery and the local state files.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
