"""Telegram HTML for the autopilot: per-offer reports, the run summary and hand-offs.

Every untrusted value is truncated first and escaped second, so a cut never
leaves half an entity, and every message stays within Telegram's 4096 limit.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

import job_scoring

from . import codes

TELEGRAM_LIMIT = 4096
LIST_LIMIT = 10
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def esc(value, limit=300):
    text = " ".join(str(value if value is not None else "").split())
    if len(text) > limit:
        text = text[:max(0, limit - 1)].rstrip() + "…"
    return html.escape(text, quote=True)


def safe_url(value):
    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    return url if parts.scheme in ("http", "https") and parts.netloc and len(url) <= 2048 else ""


def link(url, label):
    url = safe_url(url)
    return f'<a href="{html.escape(url, quote=True)}">{esc(label, 60)}</a>' if url else ""


def fit(lines, limit=TELEGRAM_LIMIT):
    """Join lines, dropping whole lines from the end (tags never span lines) until it fits."""
    lines = list(lines)
    text = "\n".join(lines)
    trimmed = False
    while len(text) > limit and len(lines) > 1:
        if trimmed:
            lines.pop(-2)
        else:
            lines[-1], trimmed = "…", True
        text = "\n".join(lines)
    if len(text) > limit:
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        text = html.escape(plain[:limit // 2 - 1] + "…", quote=False)
    return text


def city_of(job):
    region = job_scoring.market_region(job.get("location"))
    if region and region != "unknown":
        return region.title()
    return str(job.get("location") or "").split(",")[0].strip()


def money(amount, currency="EUR", *, short=True):
    if not isinstance(amount, (int, float)):
        return ""
    symbol = CURRENCY_SYMBOLS.get(str(currency or "").upper())
    value = f"{round(amount / 1000)}k" if short and amount >= 1000 else f"{int(round(amount)):,}"
    return f"{symbol}{value}" if symbol else f"{value} {currency}"


def _plain_salary(research):
    salary = (research or {}).get("salary") or {}
    parts = []
    low, high, currency = salary.get("low"), salary.get("high"), salary.get("currency") or "EUR"
    period = salary.get("period") or "year"
    basis = str(salary.get("basis") or "none")
    if isinstance(low, (int, float)) and isinstance(high, (int, float)) and basis != "none":
        names = [s.get("name") for s in (research.get("sources") or [])
                 if isinstance(s, dict) and s.get("name")][:3]
        source = f" ({basis.title()}: {', '.join(names)})" if names else f" ({basis})"
        parts.append(f"Expected salary: {money(low, currency)}–{money(high, currency)}/{period}{source}")
    else:
        parts.append("Expected salary: no reliable range found")
    if salary.get("published_text"):
        parts.append(f"Published: {salary['published_text']}")
    return parts


def salary_lines(research):
    """Plain-text salary lines (unescaped) from jobhunter_research, or a local fallback.

    ``format_salary_lines`` returns HTML-escaped lines and its own Ask line;
    they are unescaped here so the caller truncates, then escapes once, and
    the Ask line is replaced by the template's ``Asked:``.
    """
    if not research:
        return ["Expected salary: not researched"]
    try:
        from jobhunter_research import format_salary_lines
        lines = format_salary_lines(research)
        if isinstance(lines, (list, tuple)) and all(isinstance(line, str) for line in lines):
            lines = [html.unescape(line) for line in lines if not line.startswith("Ask:")]
            if lines:
                return ["Expected salary: " + lines[0], *lines[1:]]
    except Exception:  # noqa: BLE001 - research formatting must never block a report
        pass
    return _plain_salary(research)


def ask_amount(research):
    salary = (research or {}).get("salary") or {}
    amount = salary.get("ask_amount")
    return amount if isinstance(amount, (int, float)) and amount > 0 else None


def company_line(research, job):
    company = (research or {}).get("company") or {}
    parts = []
    summary = company.get("summary")
    if summary:
        parts.append(esc(summary, 260))
    website = safe_url(company.get("website") or job.get("company_website"))
    if website:
        parts.append(link(website, urlsplit(website).netloc or "website"))
    extra = " / ".join(str(company.get(k)) for k in ("industry", "size") if company.get(k))
    if extra:
        parts.append(esc(extra, 80))
    return "Company: " + (" · ".join(parts) if parts else "no verified details")


def evidence_proposal_line(proposal):
    """``Resume: +ev-a, +ev-b · −ev-c (per-job evidence proposal)``, or "" when none was used."""
    if not isinstance(proposal, dict):
        return ""
    added = ", ".join("+" + esc(item, 80) for item in (proposal.get("added") or [])[:LIST_LIMIT])
    removed = ", ".join("−" + esc(item, 80) for item in (proposal.get("removed") or [])[:LIST_LIMIT])
    changes = " · ".join(part for part in (added, removed) if part)
    return f"Resume: {changes} (per-job evidence proposal)" if changes else ""


def per_offer(job, *, mode, criteria, research=None, tracker_synced=None, evidence_attached=False,
              evidence_proposal=None):
    """The report for one offer: live submission or rehearsal."""
    if mode == "rehearsal":
        head = "🧪 Rehearsed (not submitted)"
        tracker = "Tracker: unchanged (rehearsal)"
    else:
        head = "✅ Auto-applied (live)"
        tracker = "Tracker: row submitted ✔" if tracker_synced else "Tracker: saved, Sheet sync pending"
    city = city_of(job)
    title = f"{head} · <b>{esc(job.get('title'), 120)} — {esc(job.get('company'), 80)}</b>" + (
        f" ({esc(city, 40)})" if city else "")
    salary = " · ".join(esc(line, 220) for line in salary_lines(research))
    ask = ask_amount(research)
    currency = ((research or {}).get("salary") or {}).get("currency") or "EUR"
    if ask:
        salary += " · Asked: " + esc(money(ask, currency, short=False), 40)
    evidence = "Evidence: screenshot attached" if evidence_attached else "Evidence: none attached"
    footer = " · ".join(p for p in (tracker, evidence, link(job.get("url"), "View offer")) if p)
    resume = evidence_proposal_line(evidence_proposal)
    return fit([
        title,
        "Why: " + " · ".join(esc(item, 120) for item in (criteria or [])),
        salary,
        company_line(research, job),
        *([resume] if resume else []),
        footer,
    ])


def _items(rows, render):
    lines = [render(row) for row in rows[:LIST_LIMIT]]
    if len(rows) > LIST_LIMIT:
        lines.append(f"  … and {len(rows) - LIST_LIMIT} more")
    return lines


def _job_label(row):
    return f"{esc(row.get('title'), 80)} — {esc(row.get('company'), 60)}"


def summary(stats):
    """The end-of-run summary: applied N/M, rehearsed, handed off, closed, deferred, cap left."""
    mode = stats.get("mode", "rehearsal")
    lines = [f"🤖 <b>Autopilot {esc(stats.get('run_id'), 20)}</b> ({esc(mode, 12)})"]
    if mode == "live":
        lines.append(f"Applied: {stats.get('applied', 0)}/{stats.get('planned', 0)}")
    for key, label in (("rehearsed", "Rehearsed"), ("unverified", "Submitted, not confirmed"),
                       ("not_attempted", "Not attempted")):
        rows = stats.get(key) or []
        if rows:
            lines.append(f"{label}: {len(rows)}")
            lines.extend(_items(rows, lambda r: "  • " + _job_label(r)))
    handed = stats.get("handed_off") or []
    if handed:
        lines.append(f"Handed off to you: {len(handed)}")
        lines.extend(_items(handed, lambda r: f"  • {_job_label(r)} ({esc(codes.describe(r.get('reason')), 120)})"))
    closed = stats.get("closed") or []
    if closed:
        lines.append(f"Closed offers: {len(closed)}")
        lines.extend(_items(closed, lambda r: "  • " + _job_label(r)))
    deferred = stats.get("deferred") or []
    if deferred:
        lines.append(f"Deferred to the next run: {len(deferred)}")
    proposals = stats.get("evidence_proposals") or 0
    if proposals:
        lines.append(f"Resumes with a per-job evidence proposal: {proposals}")
    lines.append(f"Cap left today: {stats.get('cap_left', 0)}/{stats.get('max_per_day', 0)}")
    return fit(lines)


HANDOFF_HINTS = {
    codes.LINKEDIN_SIGNED_OUT: "Ask Hermes to open the LinkedIn login in the application browser, then sign in "
                               "over VNC; the next runs use that session.",
    codes.CAPTCHA: "To finish it in the application browser, ask Hermes to open this offer there, then "
                   "connect with VNC.",
}


def handoff(job, code, detail="", *, after_submit=False):
    """The lines put above the research card when a hard stop hands a job back."""
    reason = codes.describe(code)
    if detail:
        reason = f"{reason} — {detail}"
    lines = [f"✋ <b>Autopilot handed this offer to you</b>: {esc(reason, 300)}"]
    if after_submit:
        lines.append("The submit button was already clicked. Check the application browser (VNC) and the "
                     "jobs mailbox before applying again.")
    elif code in HANDOFF_HINTS:
        lines.append(HANDOFF_HINTS[code])
    return "\n".join(lines)


def manual_apply(job, *, host, apply_url, criteria=None, research=None, documents=0):
    """The card for an offer on an anti-bot site: the apply link and the resume, for him to submit."""
    city = city_of(job)
    title = (f"✋ <b>Apply by hand</b> · <b>{esc(job.get('title'), 120)} — {esc(job.get('company'), 80)}</b>"
             + (f" ({esc(city, 40)})" if city else ""))
    attached = ("The tailored resume is attached below." if documents == 1
                else "The tailored resume and cover letter are attached below." if documents > 1
                else "The tailored resume could not be attached; ask Hermes for it.")
    lines = [
        title,
        f"{esc(host, 60)} blocks automated browsers, so the autopilot did not try. Submit it yourself. {attached}",
        "Apply: " + (link(apply_url, urlsplit(safe_url(apply_url)).netloc or host) or "link unavailable"),
    ]
    if criteria:
        lines.append("Why: " + " · ".join(esc(item, 120) for item in criteria))
    salary = " · ".join(esc(line, 220) for line in salary_lines(research))
    ask = ask_amount(research)
    if ask:
        currency = ((research or {}).get("salary") or {}).get("currency") or "EUR"
        salary += " · Ask: " + esc(money(ask, currency, short=False), 40)
    lines.append(salary)
    return fit(lines)


def manual_keyboard(job_id, apply_url):
    row = []
    if safe_url(apply_url):
        row.append({"text": "🔗 Open application", "url": safe_url(apply_url)})
    row.append({"text": "🚫 Ignore", "callback_data": f"ignore:{job_id}"})
    return {"inline_keyboard": [row]}


def with_card(prefix, card):
    return fit((prefix + "\n\n" + card).split("\n"))


class Telegram:
    """The owner chat, through scraper's confirmed-delivery helpers."""

    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    def send(self, text, reply_markup=None):
        import scraper
        return bool(scraper.send_telegram(self.token, self.chat_id, text, reply_markup=reply_markup))

    def document(self, path, caption=None):
        import scraper
        return bool(scraper.send_telegram_document(self.token, self.chat_id, str(path), caption))


def telegram_from_env():
    import os
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    return Telegram(token, chat_id) if token and chat_id else None
