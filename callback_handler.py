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
import jobhunter_interest_flow as interest_flow

# Setup
PROJECT_DIR = Path(__file__).resolve().parent


def project_path(value):
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path.resolve()


load_dotenv(dotenv_path=PROJECT_DIR / ".env")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DB_PATH = project_path(os.getenv("JOBHUNTER_DB_PATH", "data/jobs.db"))
PROFILE_PATH = project_path(os.getenv("JOBHUNTER_PROFILE_PATH", "data/master-profile.json"))
OUTPUT_DIR = project_path(os.getenv("JOBHUNTER_OUTPUT_DIR", "data/output"))

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
    return sqlite3.connect(str(DB_PATH))


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


def send_message(text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
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
    answer_callback(callback_query_id, "✓ Research brief ready")

    research = interest_flow.research_job(job)
    message = interest_flow.build_research_brief_message(job, research)

    send_message(message, reply_markup=interest_flow.research_brief_keyboard(job_id, job.get("url")))
    log.info(f"Interested: {job['title']} @ {job['company']} - sent research brief")


def handle_apply(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return

    try:
        package = interest_flow.prepare_application_package(
            job_id,
            db_path=DB_PATH,
            profile_path=PROFILE_PATH,
            output_dir=OUTPUT_DIR,
        )
    except interest_flow.TailoringReadinessError as exc:
        answer_callback(callback_query_id, "Resume refinement needed")
        send_message(
            interest_flow.build_tailoring_blocked_message(job, str(exc)),
            reply_markup=interest_flow.tailoring_blocked_keyboard(job_id, job.get("url")),
        )
        log.info(f"Application package paused for resume refinement: {job['title']} @ {job['company']}")
        return
    answer_callback(callback_query_id, "✓ Package generated")
    send_message(
        interest_flow.build_package_ready_message(job, package),
        reply_markup=interest_flow.package_ready_keyboard(job_id),
    )
    log.info(f"Application package generated: {job['title']} @ {job['company']}")


def handle_ignore(job_id, callback_query_id):
    mark_skipped(job_id, reason="ignored after research brief")
    answer_callback(callback_query_id, "✓ Ignored")


def handle_resume_refine(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return
    record_feedback(job_id, "resume_refine", reason="resume refinement requested after tailoring gate")
    answer_callback(callback_query_id, "Resume refinement instructions ready")
    send_message(interest_flow.build_resume_refinement_message(job))


def handle_pause(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return
    record_feedback(job_id, "paused", reason="user paused application workflow")
    answer_callback(callback_query_id, "✓ Paused")


def handle_proceed_apply(job_id, callback_query_id):
    db = get_db()
    try:
        scraper.record_application_stage(
            db,
            job_id,
            "approved_to_prepare_apply",
            notes="User clicked Proceed to apply after package generation; final submit still requires approval.",
        )
    finally:
        db.close()
    answer_callback(callback_query_id, "✓ Apply prep approved")
    job = get_job(job_id)
    if job:
        send_message(
            f"🚀 <b>Apply prep approved</b>\n\n<b>{job['title']}</b>\n{job['company']} — {job['location']}\n\nI can now open/fill the application path, but final submission remains approval-gated."
        )


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
                elif action == "apply":
                    handle_apply(payload, callback_id)
                elif action == "resume_refine":
                    handle_resume_refine(payload, callback_id)
                elif action == "pause":
                    handle_pause(payload, callback_id)
                elif action == "ignore":
                    handle_ignore(payload, callback_id)
                elif action == "proceed_apply":
                    handle_proceed_apply(payload, callback_id)
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
