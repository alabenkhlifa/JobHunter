"""Complete recognized email-code steps in an already approved application.

Codes stay in memory. CAPTCHA, phone and identity challenges remain manual.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from urllib.parse import urlsplit

from jobhunter_integrations.gmail_auth import default_token_path, expected_account, gmail_service
from jobhunter_integrations.gmail_verification import find_message


# Observed Greenhouse sender domains, matched exactly, never by suffix.
SENDERS = {
    "job-boards.eu.greenhouse.io": "eu.greenhouse-mail.io",
}


class EmailVerificationError(PermissionError):
    """A safe-to-display pending verification reason."""


def challenge_state(client, page_url: str):
    if urlsplit(page_url).hostname not in SENDERS:
        return None
    state = client.evaluate(r"""
(() => {
  const visible = el => {
    if (!el) return false;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
  };
  const fields = Array.from({length:8}, (_,i) => document.querySelector('#security-input-'+i));
  const text = document.body ? document.body.innerText : '';
  const captcha = [...document.querySelectorAll('iframe')].some(el => {
    const r = el.getBoundingClientRect();
    return /recaptcha|hcaptcha/i.test(el.src || '') && visible(el) && r.width >= 120 && r.height >= 100;
  });
  return {url:location.href, present:fields.some(visible),
    ready:fields.every(el => visible(el) && !el.disabled && el.maxLength === 1),
    email_prompt:/\b(?:email|e-mail)\b/i.test(text) && /\b(?:code|verification)\b/i.test(text),
    manual:captcha || /verify your phone|phone verification|identity verification/i.test(text),
    recipient:(document.querySelector('#email') || {}).value || ''};
})()
""")
    return state if isinstance(state, dict) and state.get("present") else None


def complete_email_verification(client, *, page_url: str, requested_at: dt.datetime,
                                approved: bool, attempts: int = 10, interval: float = 3,
                                service_factory=gmail_service, lookup=find_message,
                                account_resolver=expected_account, sleep=time.sleep) -> bool:
    if not approved:
        raise EmailVerificationError("Email verification requires the approved application submission.")
    if not 1 <= attempts <= 20 or not 0 <= interval <= 5:
        raise ValueError("Email verification polling must be bounded.")
    state = challenge_state(client, page_url)
    if state is None:
        return False
    if (urlsplit(page_url).scheme != "https" or state.get("url") != page_url
            or not state.get("ready") or not state.get("email_prompt") or state.get("manual")):
        raise EmailVerificationError("Email verification is not a recognized safe email-code step.")
    try:
        account = account_resolver()
        if state.get("recipient", "").strip().casefold() != account.casefold():
            raise EmailVerificationError("The application email does not match the configured jobs mailbox.")
        service = service_factory(default_token_path(), account)
        message = None
        for attempt in range(attempts):
            message = lookup(service, account=account, sender_domain=SENDERS[urlsplit(page_url).hostname],
                             after=requested_at, code_length=8)
            if message is not None:
                break
            if attempt + 1 < attempts:
                sleep(interval)
        if message is None:
            raise EmailVerificationError("Email verification is pending; no fresh unambiguous code was received. Do not repeat submission.")
        code = message["code"]
        if len(code) != 8 or not code.isascii() or not code.isalnum():
            raise EmailVerificationError("The received email code has an unsupported format.")
        # Recheck the exact page and challenge immediately before typing.
        current = challenge_state(client, page_url)
        if current != state:
            raise EmailVerificationError("The approved email-verification page changed; no code was entered.")
        for index, character in enumerate(code):
            focused = client.evaluate(f"""(() => {{
  if (location.href !== {json.dumps(page_url)}) return false;
  const el = document.querySelector('#security-input-{index}');
  if (!el || el.disabled) return false;
  el.focus(); el.select(); return true;
}})()""")
            if focused is not True:
                raise EmailVerificationError("The email-code input changed; verification remains pending.")
            client.call("Input.insertText", {"text": character})
        if challenge_state(client, page_url) != state:
            raise EmailVerificationError("The approved email-verification page changed; verification remains pending.")
        # Never return input values or the code to the caller/logs.
        clicked = client.evaluate(f"""(() => {{
  if (location.href !== {json.dumps(page_url)}) return false;
  const fields = Array.from({{length:8}}, (_,i) => document.querySelector('#security-input-'+i));
  if (fields.some(el => !el || el.disabled || el.value.length !== 1)) return false;
  const el = [...document.querySelectorAll('button')].find(el =>
    !el.disabled && el.getBoundingClientRect().width > 0 && /^submit application$/i.test(el.innerText.trim()));
  if (!el) return false;
  el.click(); return true;
}})()""")
        if clicked is not True:
            raise EmailVerificationError("The email-verification continuation is unavailable; do not repeat submission.")
        sleep(2)
        return True
    except EmailVerificationError:
        raise
    except Exception:
        # Gmail/CDP provider exceptions can contain tokens or response bodies.
        raise EmailVerificationError("Automatic email verification could not complete; submission remains pending. Do not repeat submission.") from None
    finally:
        # Clear any still-visible code before later inspection or screenshots.
        try:
            client.evaluate(f"""(() => {{
  if (location.href !== {json.dumps(page_url)}) return;
  for (let i=0; i<8; i++) {{
    const el = document.querySelector('#security-input-'+i);
    if (el) {{el.value=''; el.dispatchEvent(new Event('input', {{bubbles:true}}));}}
  }}
}})()""")
        except Exception:
            pass
