"""Fill ATS account credentials over CDP without exposing the password.

The password is read from (or generated into) the encrypted vault and typed with
``Input.insertText`` into a focused ``input[type=password]``. It never appears in
return values, exceptions, stdout/stderr or the application notes.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Callable
from urllib.parse import urlsplit

from .vault import CredentialVault, generate_password


class CredentialError(PermissionError):
    """A safe-to-display credential failure (never contains the secret)."""


def vault_key(ats_key: str, mailbox: str) -> str:
    return f"ats/{ats_key.strip().casefold()}/{mailbox.strip().casefold()}"


def _host_matches(page_url: str, ats_key: str) -> bool:
    host = (urlsplit(page_url).hostname or "").casefold()
    key = ats_key.strip().casefold()
    return bool(host and key) and (host == key or host.endswith("." + key))


def _focus(client: Any, page_url: str, selector: str, *, password: bool) -> bool:
    check = "el.type === 'password'" if password else "['email', 'text', ''].includes(el.type)"
    result = client.evaluate(f"""(() => {{
  if (location.href !== {json.dumps(page_url)}) return false;
  const el = document.querySelector({json.dumps(selector)});
  if (!el || el.tagName !== 'INPUT' || el.disabled || el.readOnly || !({check})) return false;
  const r = el.getBoundingClientRect(), s = getComputedStyle(el);
  if (r.width <= 0 || r.height <= 0 || s.display === 'none' || s.visibility === 'hidden') return false;
  el.focus();
  if (el.select) el.select();
  return document.activeElement === el;
}})()""")
    return result is True


def _type(client: Any, page_url: str, selector: str, value: str, *, password: bool, name: str) -> None:
    if not _focus(client, page_url, selector, password=password):
        raise CredentialError(f"The {name} field is unavailable or the page changed; nothing was typed.")
    try:
        client.call("Input.insertText", {"text": value})
    except Exception:
        # Provider errors may echo request parameters: never chain them.
        raise CredentialError(f"Typing into the {name} field failed.") from None


def fill_credentials(client: Any, *, page_url: str, ats_key: str, email_selector: str,
                     password_selector: str, mailbox: str, confirm_selector: str | None = None,
                     create: bool = False, vault: CredentialVault | None = None,
                     password_factory: Callable[[], str] | None = None,
                     now: dt.datetime | None = None) -> dict[str, Any]:
    """Fill email + password (+ confirmation) for ``ats_key`` on ``page_url``.

    Reuses stored credentials for the host and mailbox. With ``create=True`` and
    nothing stored, generates a strong password and stores it before typing.
    Returns non-secret metadata only.
    """

    if not mailbox or "@" not in mailbox:
        raise CredentialError("The jobs mailbox is not configured.")
    if urlsplit(page_url).scheme != "https" and urlsplit(page_url).hostname not in {"127.0.0.1", "localhost"}:
        raise CredentialError("Credentials are only filled on https application pages.")
    if not _host_matches(page_url, ats_key):
        raise CredentialError("The ATS key does not match the application page host.")
    vault = vault or CredentialVault()
    key = vault_key(ats_key, mailbox)
    stored = vault.get(key)
    created = False
    if stored is None or not stored.get("password"):
        if not create:
            raise CredentialError("No stored credentials for this ATS host; pass --create to register.")
        stored = {"username": mailbox, "password": (password_factory or generate_password)(), "ats_key": ats_key.strip().casefold(),
                  "created_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
                  "created_on": page_url}
        created = True
    username = str(stored.get("username") or mailbox)
    secret = str(stored["password"])
    if client.evaluate("location.href") != page_url:
        raise CredentialError("The application page changed; nothing was typed.")
    filled = []
    _type(client, page_url, email_selector, username, password=False, name="email")
    filled.append("email")
    _type(client, page_url, password_selector, secret, password=True, name="password")
    filled.append("password")
    if confirm_selector:
        _type(client, page_url, confirm_selector, secret, password=True, name="password confirmation")
        filled.append("confirm")
    if created:
        # Only a password that reached the sign-up form is worth keeping: a
        # failed fill must not leave a credential for an account that never existed.
        vault.put(key, stored)
    return {"ok": True, "ats_key": ats_key.strip().casefold(), "username": username,
            "created": created, "filled": filled}
