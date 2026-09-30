#!/usr/bin/env python3
"""Telegram callback handler for job button clicks."""

import argparse
import html
import os
import re
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
RESEARCH_DELIVERED_ACTION = "research_delivered"
PACKAGE_DELIVERY_STARTED_ACTION = "package_delivery_started"
DOCUMENTS_DELIVERED_ACTION = "documents_delivered"
JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
CALLBACK_ACTIONS = {
    "skip", "interested", "apply", "apply_cover", "resume_refine", "pause", "ignore", "proceed_apply", "details"
}
COVER_DRAFT_REQUIRED_MESSAGE = (
    "⚠️ A reviewed cover-letter draft is required. Ask Hermes to prepare one from "
    "confirmed public evidence, then use --apply JOB_ID --cover-letter --cover-draft PATH."
)

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


def record_research_delivery(job_id):
    """Persist the acknowledged research card before Apply can generate files."""
    conn = get_db()
    try:
        scraper.record_job_feedback(
            conn, job_id, RESEARCH_DELIVERED_ACTION, source="telegram_research_card"
        )
    finally:
        conn.close()


def record_document_delivery_step(job_id, action):
    conn = get_db()
    try:
        scraper.record_job_feedback(conn, job_id, action, source="telegram_document_delivery")
    finally:
        conn.close()


def has_delivered_documents(job_id):
    """The latest package attempt must have delivered every generated PDF."""
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT action FROM job_feedback
            WHERE job_id = ? AND action IN ('package_delivery_started', 'documents_delivered')
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return bool(row and row[0] == DOCUMENTS_DELIVERED_ACTION)


def has_delivered_research(job_id):
    """An Interested retry, Pause, or Ignore invalidates an older research card."""
    conn = get_db()
    try:
        row = conn.execute(
            """
            SELECT action FROM job_feedback
            WHERE job_id = ? AND action IN ('interested', 'research_delivered', 'skip', 'paused')
            ORDER BY id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return bool(row and row[0] == RESEARCH_DELIVERED_ACTION)


def get_job(job_id):
    conn = get_db()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    return dict(row)


def application_already_submitted(job_id):
    conn = get_db()
    try:
        return conn.execute(
            "SELECT 1 FROM applications WHERE job_id = ? AND submitted_at IS NOT NULL LIMIT 1",
            (job_id,),
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


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
        return resp.status_code == 200 and resp.json().get("ok") is True
    except Exception as e:
        log.warning("Failed to send message (%s)", type(e).__name__)
        return False


def send_document(path, caption):
    """Send a generated PDF and require Telegram's successful acknowledgement."""
    url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    try:
        document_path = Path(path)
        with document_path.open("rb") as document:
            response = requests.post(
                url,
                data={"chat_id": CHAT_ID, "caption": caption},
                files={"document": (document_path.name, document, "application/pdf")},
                timeout=60,
            )
        acknowledgement = response.json() if response.status_code == 200 else None
        return isinstance(acknowledgement, dict) and acknowledgement.get("ok") is True
    except Exception as exc:
        log.warning("Failed to send document (%s)", type(exc).__name__)
        return False


def build_documents_delivered_message(job, *, include_cover_letter=False):
    title = html.escape(str(job.get("title") or "Job"))
    company = html.escape(str(job.get("company") or "Company"))
    location = html.escape(str(job.get("location") or ""))
    return (
        f"📦 <b>Application {'documents' if include_cover_letter else 'resume'} delivered</b>\n\n"
        f"<b>{title}</b>\n{company} — {location}\n\n"
        f"Review the {'resume and cover letter' if include_cover_letter else 'resume'} above. "
        "Proceed to application preparation? "
        "Final submission needs separate approval."
    )


def answer_callback(callback_query_id, text=None):
    if not callback_query_id:
        return
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


def acknowledge(callback_query_id, message):
    """Text replies use the same handlers without a Telegram callback to answer."""
    if callback_query_id:
        answer_callback(callback_query_id, message)


def report_action_failure(callback_query_id, callback_text, message):
    acknowledge(callback_query_id, callback_text)
    if not callback_query_id:
        send_message(message)


def report_cover_draft_required(callback_query_id):
    acknowledge(callback_query_id, "Reviewed cover draft required")
    send_message(COVER_DRAFT_REQUIRED_MESSAGE)


def current_package_includes_cover_letter(job_id):
    """Read the current package so a retry cannot replace its cover with a resume-only run."""
    db = get_db()
    try:
        row = db.execute(
            "SELECT package_path FROM applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        db.close()
    if not row or not row[0]:
        return False
    try:
        package_dir = Path(row[0]).resolve()
    except (OSError, RuntimeError, TypeError):
        return False
    if package_dir.parent != OUTPUT_DIR.resolve():
        return False
    try:
        manifest_path = package_dir / "tailoring_manifest.json"
        if not manifest_path.is_symlink():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if manifest.get("job_id") == job_id and manifest.get("cover_letter_included") is True:
                return True
    except (OSError, ValueError, AttributeError, RuntimeError):
        pass
    return (package_dir / "CoverLetter.pdf").is_file()


def retry_apply_instruction(job_id, *, include_cover_letter=False):
    if include_cover_letter:
        safe_job_id = html.escape(str(job_id))
        return (
            "Ask Hermes to rerun <code>callback_handler.py --apply "
            f"{safe_job_id} --cover-letter --cover-draft SAME_REVIEWED_JSON_PATH</code> "
            "with the same reviewed draft. The research card's Apply button prepares only a resume."
        )
    return "Retry Apply to prepare and deliver the resume before proceeding."


def handle_interested(job_id, callback_query_id=None):
    job = get_job(job_id)
    if not job:
        report_action_failure(callback_query_id, "Job not found", "⚠️ This job is no longer in JobHunter. Request its details again.")
        return False
    if application_already_submitted(job_id):
        report_action_failure(callback_query_id, "Already submitted", "This application is already submitted. Its record was preserved.")
        return False

    mark_interested(job_id)
    try:
        research = interest_flow.research_job(job)
        message = interest_flow.build_research_brief_message(job, research)
    except Exception as exc:
        report_action_failure(callback_query_id, "Research unavailable", "⚠️ The research brief could not be prepared. Retry Interested before Apply.")
        log.warning("Research brief generation failed (%s)", type(exc).__name__)
        return False
    sent = send_message(message, reply_markup=interest_flow.research_brief_keyboard(job_id, job.get("url")))
    if sent:
        try:
            record_research_delivery(job_id)
        except Exception as exc:
            report_action_failure(callback_query_id, "Research sent; state not saved", "⚠️ The research card was sent, but its delivery state was not saved. Retry Interested before Apply.")
            log.warning("Research delivery state could not be saved (%s)", type(exc).__name__)
            return False
        acknowledge(callback_query_id, "✓ Research brief sent")
        log.info(f"Interested: {job['title']} @ {job['company']} - sent research brief")
    else:
        report_action_failure(callback_query_id, "Research delivery failed", "⚠️ The research card was not delivered. Retry Interested before Apply.")
        log.warning("Interested research brief was not delivered; retry before preparing a package")
    return sent


def handle_apply(job_id, callback_query_id=None, *, include_cover_letter=False, cover_letter_draft=None):
    job = get_job(job_id)
    if not job:
        report_action_failure(callback_query_id, "Job not found", "⚠️ This job is no longer in JobHunter. Request its details again.")
        return False
    if application_already_submitted(job_id):
        report_action_failure(callback_query_id, "Already submitted", "This application is already submitted. Its record was preserved.")
        return False
    if not has_delivered_research(job_id):
        report_action_failure(callback_query_id, "Open the research brief first", "⚠️ The research brief has not been delivered for this job. Choose Interested before Apply.")
        log.warning("Apply requested without a delivered research card for job %s", job_id)
        return False
    if not include_cover_letter and current_package_includes_cover_letter(job_id):
        acknowledge(callback_query_id, "Cover retry needs the same draft")
        send_message(
            "⚠️ This job already has a cover-letter package. "
            + retry_apply_instruction(job_id, include_cover_letter=True)
        )
        return False
    if include_cover_letter and cover_letter_draft is None:
        report_cover_draft_required(callback_query_id)
        return False
    try:
        record_document_delivery_step(job_id, PACKAGE_DELIVERY_STARTED_ACTION)
    except Exception as exc:
        report_action_failure(
            callback_query_id,
            "Package delivery state unavailable",
            "⚠️ Package delivery tracking is unavailable. "
            + retry_apply_instruction(job_id, include_cover_letter=include_cover_letter),
        )
        log.warning("Package delivery state could not be saved (%s)", type(exc).__name__)
        return False

    try:
        package_options = {
            "db_path": DB_PATH,
            "profile_path": PROFILE_PATH,
            "output_dir": OUTPUT_DIR,
            "include_cover_letter": include_cover_letter,
        }
        if cover_letter_draft is not None:
            package_options["cover_letter_draft"] = cover_letter_draft
        package = interest_flow.prepare_application_package(job_id, **package_options)
    except interest_flow.TailoringReadinessError as exc:
        sent = send_message(
            interest_flow.build_tailoring_blocked_message(job, str(exc)),
            reply_markup=interest_flow.tailoring_blocked_keyboard(job_id, job.get("url")),
        )
        if sent:
            acknowledge(callback_query_id, "Resume refinement needed")
        else:
            report_action_failure(callback_query_id, "Refinement notice failed", "⚠️ Resume refinement is required, but the notice was not delivered. Retry Apply.")
        if sent:
            log.info(f"Application package paused for resume refinement: {job['title']} @ {job['company']}")
        else:
            log.warning("Resume refinement notice was not delivered for job %s", job_id)
        return False
    if include_cover_letter and package.cover_pdf is None:
        acknowledge(callback_query_id, "Cover letter unavailable")
        send_message(
            "⚠️ The requested cover letter was not generated. "
            + retry_apply_instruction(job_id, include_cover_letter=True)
        )
        return False
    documents = [("Resume", package.resume_pdf, "Tailored resume")]
    if include_cover_letter:
        documents.append(("Cover letter", package.cover_pdf, "Tailored cover letter"))
    for label, path, caption in documents:
        if not send_document(path, caption):
            acknowledge(callback_query_id, f"{label} delivery failed")
            send_message(
                f"⚠️ <b>{label} delivery failed</b> for "
                f"{html.escape(str(job.get('title') or 'this job'))}. "
                "The package was saved. "
                + retry_apply_instruction(job_id, include_cover_letter=include_cover_letter)
            )
            log.warning("%s document was not delivered for job %s", label, job_id)
            return False
    try:
        record_document_delivery_step(job_id, DOCUMENTS_DELIVERED_ACTION)
    except Exception as exc:
        acknowledge(callback_query_id, "Documents sent; state not saved")
        send_message(
            "⚠️ The generated documents were sent, but delivery state was not saved. "
            + retry_apply_instruction(job_id, include_cover_letter=include_cover_letter)
        )
        log.warning("Document delivery state could not be saved (%s)", type(exc).__name__)
        return False
    sent = send_message(
        build_documents_delivered_message(job, include_cover_letter=include_cover_letter),
        reply_markup=interest_flow.package_ready_keyboard(job_id),
    )
    if sent:
        acknowledge(callback_query_id, "✓ Documents delivered")
    else:
        acknowledge(callback_query_id, "Documents sent; card delivery failed")
        send_message(
            "⚠️ The generated documents were sent, but the next-step card was not delivered. "
            + retry_apply_instruction(job_id, include_cover_letter=include_cover_letter)
        )
    if sent:
        log.info(f"Application documents delivered: {job['title']} @ {job['company']}")
    else:
        log.warning("Package ready card was not delivered for job %s", job_id)
    return sent


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


def handle_proceed_apply(job_id, callback_query_id=None):
    job = get_job(job_id)
    if not job:
        report_action_failure(callback_query_id, "Job not found", "⚠️ This job is no longer in JobHunter. Request its details again.")
        return False
    db = get_db()
    try:
        application = db.execute(
            "SELECT stage, package_path FROM applications WHERE job_id = ?", (job_id,)
        ).fetchone()
        if not application or application[0] not in {"package_generated", "approved_to_prepare_apply"} or not application[1]:
            report_action_failure(callback_query_id, "Generate the package first", "⚠️ The application package is not ready. Choose Apply after reviewing the research brief.")
            log.warning("Apply prep requested before a package was ready for job %s", job_id)
            return False
        if application[0] == "package_generated":
            if not has_delivered_documents(job_id):
                acknowledge(callback_query_id, "Deliver the documents first")
                send_message(
                    "⚠️ The generated application documents must be delivered before proceeding. "
                    + retry_apply_instruction(
                        job_id, include_cover_letter=current_package_includes_cover_letter(job_id)
                    )
                )
                log.warning("Apply prep requested before document delivery for job %s", job_id)
                return False
            scraper.record_application_stage(
                db,
                job_id,
                "approved_to_prepare_apply",
                notes="User clicked Proceed to apply after package generation; final submit still requires approval.",
            )
    finally:
        db.close()
    sent = send_message(
        f"🚀 <b>Apply prep approved</b>\n\n<b>{job['title']}</b>\n{job['company']} — {job['location']}\n\nI can now open/fill the application path, but final submission remains approval-gated."
    )
    if sent:
        acknowledge(callback_query_id, "✓ Apply prep approved")
    else:
        report_action_failure(callback_query_id, "Approval saved; notice delivery failed", "⚠️ Apply preparation was approved, but its notice was not delivered. Retry Proceed to apply.")
    if not sent:
        log.warning("Apply prep notice was not delivered for job %s", job_id)
    return sent


def handle_details(job_id, callback_query_id):
    job = get_job(job_id)
    if not job:
        answer_callback(callback_query_id, "Job not found")
        return

    record_feedback(job_id, "details", reason="user requested details")
    answer_callback(callback_query_id, "Opening details")
    send_message(build_details_message(job))
    log.info(f"Details requested: {job['title']} @ {job['company']}")


def dispatch_callback_data(callback_data, callback_query_id=None):
    """Return 0 on success, 1 for invalid input, or 3 for a handled action failure."""
    if callback_query_id is not None and (
        not isinstance(callback_query_id, str) or not callback_query_id or len(callback_query_id) > 128
    ):
        return 1
    if not isinstance(callback_data, str) or len(callback_data.encode("utf-8")) > 64:
        answer_callback(callback_query_id, "Invalid callback")
        return 1
    action, separator, payload = callback_data.partition(":")
    if not separator:
        answer_callback(callback_query_id, "Invalid callback")
        return 1
    if action == "skip_reason":
        reason_code, reason_separator, job_id = payload.partition(":")
        if not reason_separator or reason_code not in SKIP_REASON_LABELS:
            answer_callback(callback_query_id, "Invalid skip reason")
            return 1
        payload = job_id
    elif action not in CALLBACK_ACTIONS:
        answer_callback(callback_query_id, "Unknown action")
        return 1
    if not JOB_ID_PATTERN.fullmatch(payload):
        answer_callback(callback_query_id, "Invalid job ID")
        return 1
    if not get_job(payload):
        answer_callback(callback_query_id, "Job not found")
        return 1

    if action == "skip":
        handle_skip(payload, callback_query_id)
    elif action == "skip_reason":
        handle_skip(payload, callback_query_id, reason_code=reason_code)
    elif action == "interested":
        return 0 if handle_interested(payload, callback_query_id) else 3
    elif action == "apply":
        return 0 if handle_apply(payload, callback_query_id) else 3
    elif action == "apply_cover":
        report_cover_draft_required(callback_query_id)
        return 3
    elif action == "resume_refine":
        handle_resume_refine(payload, callback_query_id)
    elif action == "pause":
        handle_pause(payload, callback_query_id)
    elif action == "ignore":
        handle_ignore(payload, callback_query_id)
    elif action == "proceed_apply":
        return 0 if handle_proceed_apply(payload, callback_query_id) else 3
    elif action == "details":
        handle_details(payload, callback_query_id)
    return 0


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
                dispatch_callback_data(callback_data, callback_id)
        
        except requests.Timeout:
            # Normal timeout, just continue polling
            continue
        except Exception as e:
            log.error(f"Polling error: {e}")
            time.sleep(10)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Handle JobHunter Telegram buttons or text-reply actions.")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--interested", metavar="JOB_ID", help="Send a research card for an interested job")
    actions.add_argument("--apply", metavar="JOB_ID", help="Generate a tailored resume after the research choice")
    actions.add_argument("--proceed-apply", metavar="JOB_ID", help="Approve apply preparation after package generation")
    actions.add_argument("--callback-data", metavar="DATA", help="Handle one validated Telegram button payload")
    parser.add_argument("--callback-query-id", metavar="ID", help="Optional callback query ID if it has not been answered")
    parser.add_argument("--cover-letter", action="store_true", help="With --apply, also generate a cover letter when needed")
    parser.add_argument("--cover-draft", metavar="PATH", help="Private reviewed JSON draft for --apply --cover-letter")
    args = parser.parse_args(argv)

    if args.cover_letter and not args.apply:
        parser.error("--cover-letter requires --apply")
    if args.cover_letter and not args.cover_draft:
        parser.error("--cover-letter requires a reviewed JSON draft: --cover-draft data/cover-drafts/JOB_ID.json")
    if args.cover_draft and not (args.apply and args.cover_letter):
        parser.error("--cover-draft requires --apply and --cover-letter")
    if args.callback_data is not None:
        return dispatch_callback_data(args.callback_data, args.callback_query_id)
    if args.callback_query_id:
        parser.error("--callback-query-id requires --callback-data")
    if args.interested:
        return 0 if handle_interested(args.interested) else 1
    if args.apply:
        draft = None
        if args.cover_draft:
            draft_root = (OUTPUT_DIR.parent / "cover-drafts").resolve()
            draft_path = Path(args.cover_draft).resolve()
            if not draft_path.is_relative_to(draft_root) or draft_path.suffix.lower() != ".json":
                parser.error("--cover-draft must be a JSON file in the private data/cover-drafts directory")
            if not draft_path.is_file() or draft_path.stat().st_size > 20_000:
                parser.error("--cover-draft must be an existing JSON file under 20 KB")
            try:
                draft = json.loads(draft_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                parser.error("--cover-draft is not readable JSON")
        options = {"include_cover_letter": args.cover_letter}
        if draft is not None:
            options["cover_letter_draft"] = draft
        return 0 if handle_apply(args.apply, **options) else 1
    if args.proceed_apply:
        return 0 if handle_proceed_apply(args.proceed_apply) else 1

    log.info("Starting Telegram callback handler...")
    log.info("Listening for button clicks...")
    try:
        poll_updates()
    except KeyboardInterrupt:
        log.info("Shutting down...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
