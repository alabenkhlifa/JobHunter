"""Fetch the newest recent ATS email into a private file during an approved flow.

No message bodies, links or verification codes are printed. This command does
not follow links, mark mail read, send mail or update application state.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import os
import re
import sys
from pathlib import Path

from jobhunter_integrations.gmail_auth import (
    GmailAuthError, default_token_path, expected_account, gmail_service, write_private_json,
)
from jobhunter_integrations.gmail_watcher import extract_text, header_value


def find_message(service, *, account: str, sender_domain: str, after: dt.datetime, now: dt.datetime | None = None):
    now = now or dt.datetime.now(dt.timezone.utc)
    if after.tzinfo is None or not dt.timedelta(0) <= now - after <= dt.timedelta(minutes=15):
        raise ValueError("--after must be a timezone-aware time within the last 15 minutes.")
    if not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.[a-zA-Z]{2,}", sender_domain):
        raise ValueError("Use the exact expected ATS sender domain.")
    # Search only reduces traffic; validate the returned headers and timestamps.
    query = f"after:{int(after.timestamp())} to:{account} from:({sender_domain})"
    page_token = None
    newest = None
    while True:
        request = {"userId": "me", "q": query, "maxResults": 100}
        if page_token:
            request["pageToken"] = page_token
        page = service.users().messages().list(**request).execute()
        for item in page.get("messages", []):
            message = service.users().messages().get(userId="me", id=item["id"], format="full").execute()
            payload = message.get("payload", {})
            headers = payload.get("headers", [])
            sender = email.utils.parseaddr(header_value(headers, "From"))[1]
            recipients = email.utils.getaddresses([
                h.get("value", "") for h in headers
                if h.get("name", "").casefold() in {"to", "delivered-to"}
            ])
            stamp = int(message.get("internalDate", "0"))
            if (
                sender.rpartition("@")[2].casefold() != sender_domain.casefold()
                or account.casefold() not in {address.casefold() for _, address in recipients}
                or not after.timestamp() * 1000 <= stamp <= now.timestamp() * 1000
            ):
                continue
            if newest is None or stamp > newest["internal_date_ms"]:
                newest = {
                    "message_id": message["id"], "internal_date_ms": stamp,
                    "subject": header_value(headers, "Subject"), "sender": sender,
                    "text": extract_text(payload),
                }
        page_token = page.get("nextPageToken")
        if not page_token:
            return newest


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv
    load_dotenv(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sender-domain", required=True)
    parser.add_argument("--after", required=True, help="UTC/offset timestamp captured when requesting the ATS code.")
    parser.add_argument("--google-token", type=Path, default=default_token_path())
    parser.add_argument("--account", default=os.getenv("JOBHUNTER_GMAIL_ACCOUNT"))
    # No reusable output filename: a failed/new lookup must not expose a stale code.
    args = parser.parse_args(argv)
    try:
        account = expected_account(args.account)
        after = dt.datetime.fromisoformat(args.after.replace("Z", "+00:00"))
        result = find_message(gmail_service(args.google_token, account), account=account, sender_domain=args.sender_domain, after=after)
        if result is None:
            print("No matching recent verification email.")
            return 1
        import tempfile
        folder = Path.home() / ".jobhunter" / "verification"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix="message-", suffix=".json", dir=folder)
        os.close(fd)
        try:
            write_private_json(Path(name), result)
        except Exception:
            Path(name).unlink(missing_ok=True)
            raise
        print(f"Verification email saved privately: {name}")
        return 0
    except GmailAuthError as exc:
        print(f"Gmail unavailable: {exc}", file=sys.stderr)
    except Exception:
        print("Verification lookup failed; check the time, sender domain and Gmail connection.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
