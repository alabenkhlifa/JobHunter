#!/usr/bin/env python3
"""Telegram callback handler for job button clicks."""

import os
import sys
import time
import json
import logging
import requests
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

import scraper

# Setup
load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = "./data/jobs.db"

SKIP_REASON_LABELS = {
    "wrong_stack": "wrong stack or weak backend fit",
    "too_junior": "too junior / low seniority",
    "too_senior": "too senior / over-scoped",
    "low_quality": "low-quality or suspicious posting",
}

if not TOKEN or not CHAT_ID:
    log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set")
    sys.exit(1)


def get_db():
    return sqlite3.connect(DB_PATH)


def skip_reason_label(reason_code):
    return SKIP_REASON_LABELS.get(reason_code, reason_code.replace("_", " "))


def mark_skipped(job_id, reason=None):
    reason_text = reason or "user selected skip"
    conn = get_db()
    conn.execute("UPDATE jobs SET status = 'skipped' WHERE id = ?", (job_id,))
    scraper.record_job_feedback(
        conn,
        job_id,
        "skip",
        reason=reason_text,
        source="telegram_button",
    )
    conn.commit()
    conn.close()


def record_feedback(job_id, action, reason=None):
    conn = get_db()
    scraper.record_job_feedback(
        conn,
        job_id,
        action,
        reason=reason,
        source="telegram_button",
    )
    conn.close()


def mark_interested(job_id):
    conn = get_db()
    scraper.mark_interested(conn, job_id)
    conn.close()


def get_job(job_id):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def send_message(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.warning(f"Failed to send message: {e}")
        return False


def answer_callback(callback_query_id, text=None):
    url = f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        requests.post(url, json=payload, timeout=5)
    except Exception:
        pass


def handle_skip(job_id, callback_query_id, reason_code=None):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return

    reason = skip_reason_label(reason_code) if reason_code else None
    mark_skipped(job_id, reason=reason)
    answer_callback(callback_query_id, f"✓ Skipped: {reason}" if reason else "✓ Skipped")
    log.info(f"Skipped: {job['title']} @ {job['company']}" + (f" ({reason})" if reason else ""))


def build_interested_message(job):
    """Build the Hermes-native action request emitted after an Interested click."""
    job_id = job["id"]
    return f"""🎯 <b>HERMES JOBHUNTER ACTION</b>

<b>Interested job selected</b>
<b>{job['title']}</b>
{job['company']} - {job['location']}

<b>Job ID:</b> {job_id}
<b>Score:</b> {job['score']}
<b>URL:</b> {job['url']}

<b>Required Tech:</b> {job.get('tech_required', 'N/A')}
<b>Nice to Have:</b> {job.get('tech_nice_to_have', 'N/A')}

Please generate a tailored resume and cover letter, record application stage <code>package_generated</code>, then prepare LinkedIn safely:
- detect Easy Apply vs external apply
- reuse only confirmed cached answers
- save screenshots/evidence for blockers or draft-ready state
- stop before final Submit until the user approves."""


def build_details_message(job):
    """Build a compact detail view for the Telegram Details button."""
    description = (job.get("description") or "No description stored.").strip()
    if len(description) > 1800:
        description = description[:1800].rstrip() + "…"
    return f"""📄 <b>Job details</b>

<b>{job['title']}</b>
{job['company']} - {job['location']}

<b>Score:</b> {job.get('score', 'N/A')}
<b>Work model:</b> {job.get('work_model', 'N/A')}
<b>Salary:</b> {job.get('salary') or 'N/A'}
<b>Required Tech:</b> {job.get('tech_required') or 'N/A'}
<b>Nice to Have:</b> {job.get('tech_nice_to_have') or 'N/A'}

<b>Description:</b>
{description}

{job['url']}"""


def handle_interested(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return

    mark_interested(job_id)
    answer_callback(callback_query_id, "✓ Marked as interested")

    message = build_interested_message(job)

    send_message(message)
    log.info(f"Interested: {job['title']} @ {job['company']} - notified Hermes JobHunter")


def handle_details(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return

    record_feedback(job_id, "details", reason="user requested details")
    answer_callback(callback_query_id, "Opening details")
    send_message(build_details_message(job))
    log.info(f"Details requested: {job['title']} @ {job['company']}")


def poll_updates(offset=0):
    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"offset": offset, "timeout": 30, "allowed_updates": ["callback_query"]}
    
    while True:
        try:
            resp = requests.get(url, params=params, timeout=35)
            if resp.status_code != 200:
                log.warning(f"getUpdates failed: {resp.status_code}")
                time.sleep(5)
                continue
            
            data = resp.json()
            if not data.get("ok"):
                log.warning(f"Telegram API error: {data}")
                time.sleep(5)
                continue
            
            updates = data.get("result", [])
            
            for update in updates:
                params["offset"] = update["update_id"] + 1
                
                if "callback_query" not in update:
                    continue
                
                callback = update["callback_query"]
                callback_data = callback.get("data", "")
                callback_id = callback["id"]
                
                # Parse callback: "skip:job_id" or "interested:job_id"
                if ":" not in callback_data:
                    answer_callback(callback_id, "Invalid callback")
                    continue
                
                action, payload = callback_data.split(":", 1)

                if action == "skip":
                    handle_skip(payload, callback_id)
                elif action == "skip_reason":
                    if ":" not in payload:
                        answer_callback(callback_id, "Invalid skip reason")
                        continue
                    reason_code, job_id = payload.split(":", 1)
                    handle_skip(job_id, callback_id, reason_code=reason_code)
                elif action == "interested":
                    handle_interested(payload, callback_id)
                elif action == "details":
                    handle_details(payload, callback_id)
                else:
                    answer_callback(callback_id, "Unknown action")
        
        except requests.Timeout:
            # Normal timeout, just continue polling
            continue
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(10)


if __name__ == "__main__":
    log.info("Starting Telegram callback handler...")
    log.info(f"Listening for button clicks...")
    
    try:
        poll_updates()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        sys.exit(0)
