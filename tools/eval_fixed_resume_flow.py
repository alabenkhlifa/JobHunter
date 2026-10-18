#!/usr/bin/env python3
"""Exercise the real Interested -> fixed resume choice -> package flow on a DB clone.

The source database is opened read-only and copied with SQLite's backup API.
Telegram transport is always stubbed. Network research is disabled unless
``--research-live`` is passed. No application is submitted.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import sqlite3
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class EvaluationError(RuntimeError):
    """A safe evaluator failure that does not include private profile text."""


def _source_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise EvaluationError("Source database does not exist")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _backup_database(source: sqlite3.Connection, target_path: Path) -> None:
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        target.commit()
    except sqlite3.Error:
        raise EvaluationError("Could not create a consistent SQLite sandbox backup") from None
    finally:
        target.close()
    target_path.chmod(0o600)


def _sample_jobs(connection: sqlite3.Connection, count: int, seed: int) -> list[dict[str, Any]]:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "jobs" not in tables:
        raise EvaluationError("Source database has no jobs table")
    app_clause = ""
    if "applications" in tables:
        app_clause = "AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id=j.id)"
    query = f"""
        SELECT j.id, j.title, j.company, j.location, j.url, j.source
        FROM jobs j
        WHERE j.title IS NOT NULL AND TRIM(j.title) <> ''
          AND j.company IS NOT NULL AND TRIM(j.company) <> ''
          AND COALESCE(j.status, '') NOT IN ('submitted', 'rejected', 'skipped', 'expired')
          {app_clause}
        ORDER BY j.id
    """
    candidates = [dict(row) for row in connection.execute(query)]
    random.Random(seed).shuffle(candidates)
    return candidates[:count]


def _evaluation_environment(db_path: Path, profile_path: Path, package_dir: Path,
                            research_live: bool) -> dict[str, str]:
    return {
        # Dummy values prevent the live handler from loading real Telegram
        # credentials. Its transport methods are patched for the whole flow.
        "TELEGRAM_BOT_TOKEN": "fixed-resume-eval-transport-disabled",
        "TELEGRAM_CHAT_ID": "fixed-resume-eval-transport-disabled",
        "JOBHUNTER_DB_PATH": str(db_path),
        "JOBHUNTER_PROFILE_PATH": str(profile_path),
        "JOBHUNTER_OUTPUT_DIR": str(package_dir),
        "JOBHUNTER_AUTO_SYNC_TRACKER": "false",
        "JOBHUNTER_INTERESTED_WEB_RESEARCH": "true" if research_live else "false",
    }


def _choice_buttons(message: dict[str, Any]) -> list[dict[str, str]]:
    markup = message.get("reply_markup") or {}
    keyboard = markup.get("inline_keyboard") or []
    return [button for row in keyboard for button in row if isinstance(button, dict)]


def _profile_summary_and_bullets(profile: dict, selection: dict, rendered_resume: dict) -> bool:
    import resume_blocks

    expected = resume_blocks.build(profile, selection)
    expected_bullets = [bullet for role in expected.get("experience") or []
                        for item in role.get("engagements") or [role]
                        for bullet in item.get("bullets") or []]
    actual_bullets = [bullet for role in rendered_resume.get("experience") or []
                      for item in role.get("engagements") or [role]
                      for bullet in item.get("bullets") or []]
    return (rendered_resume.get("summary") == expected.get("summary")
            and rendered_resume.get("headline") == expected.get("headline")
            and actual_bullets == expected_bullets)


def _evaluate_one(callback, profile: dict, job: dict[str, Any], index: int,
                  package_calls: list[str], package_dir: Path, research_live: bool) -> dict[str, Any]:
    import resume_blocks
    import scraper

    job_id = str(job["id"])
    messages: list[dict[str, Any]] = []
    delivered_documents: list[str] = []
    callback_acks: list[str] = []

    def send_message(text, reply_markup=None):
        messages.append({"text": str(text), "reply_markup": copy.deepcopy(reply_markup)})
        return True

    def send_document(path, caption):
        candidate_path = Path(path)
        if not candidate_path.is_file() or candidate_path.suffix.lower() != ".pdf":
            return False
        delivered_documents.append(candidate_path.name)
        return True

    denied_network: list[str] = []
    real_request = callback.requests.sessions.Session.request

    def guarded_request(session, method, url, *args, **kwargs):
        host = urlsplit(str(url)).netloc.lower()
        if host == "api.telegram.org" or not research_live:
            denied_network.append(host or "unknown")
            raise EvaluationError("Unexpected outbound network request in sandbox evaluation")
        return real_request(session, method, url, *args, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(callback, "send_message", send_message))
        stack.enter_context(mock.patch.object(callback, "send_document", send_document))
        stack.enter_context(mock.patch.object(
            callback, "answer_callback", lambda _callback_id, text=None: callback_acks.append(str(text or ""))))
        stack.enter_context(mock.patch.object(callback, "get_db", lambda: sqlite3.connect(str(callback.DB_PATH))))
        stack.enter_context(mock.patch.object(scraper, "sync_application_tracker_if_enabled", lambda: False))
        stack.enter_context(mock.patch.object(
            callback.requests.sessions.Session, "request", guarded_request))
        return _evaluate_one_active(callback, profile, job, index, package_calls, package_dir,
                                   messages, delivered_documents, callback_acks, denied_network,
                                   research_live)


def _evaluate_one_active(callback, profile, job, index, package_calls, package_dir,
                         messages, delivered_documents, callback_acks, denied_network, research_live):
    import resume_blocks
    from resume_pdf_audit import audit_resume_pdf, resume_layout_metrics

    job_id = str(job["id"])
    calls_before = len(package_calls)

    if not callback.handle_interested(job_id, callback_query_id=f"eval-{index}-interested"):
        raise EvaluationError("Interested flow did not deliver its research card")
    research_message = next((message for message in messages
                             if any(button.get("callback_data") == f"apply:{job_id}"
                                    for button in _choice_buttons(message))), None)
    auto_picker = next((message for message in messages
                        if any(str(button.get("callback_data", "")).startswith("resume_choice:")
                               for button in _choice_buttons(message))), None)
    if auto_picker is None:
        raise EvaluationError("Interested did not open the approved resume picker")
    if not any(button.get("callback_data") == f"apply:{job_id}"
               for button in _choice_buttons(research_message or {})):
        raise EvaluationError("Research card did not include the Apply action")

    if not callback.handle_apply(job_id, callback_query_id=f"eval-{index}-apply"):
        raise EvaluationError("Apply flow did not open the resume picker")
    if len(package_calls) != calls_before:
        raise EvaluationError("Package generation started before the resume selection")
    connection = callback.get_db()
    try:
        after_picker = connection.execute(
            "SELECT stage FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
    finally:
        connection.close()
    if after_picker and after_picker[0] not in {"interested"}:
        raise EvaluationError("Application package state changed before the selection was complete")

    catalog = resume_blocks.catalog(profile)
    confirmed_flavors = [item for item in catalog["flavors"] if item["confirmation"] == "candidate-confirmed"]
    combinations = [(flavor, variant) for flavor in confirmed_flavors for variant in catalog["variants"]
                    if variant["flavor_id"] == flavor["id"] and variant["confirmation"] == "candidate-confirmed"]
    if not combinations:
        raise EvaluationError("The fixed catalog has no candidate-confirmed flavor and variant")
    flavor, variant = combinations[index % len(combinations)]

    def latest_tokens(action: str | None = None) -> list[str]:
        for message in reversed(messages):
            tokens = [button["callback_data"].split(":", 1)[1] for button in _choice_buttons(message)
                      if str(button.get("callback_data", "")).startswith("resume_choice:")]
            if tokens:
                connection = callback.get_db()
                try:
                    rows = connection.execute(
                        "SELECT token,action FROM resume_choice_tokens WHERE job_id=?", (job_id,)
                    ).fetchall()
                finally:
                    connection.close()
                actions = {row[0]: row[1] for row in rows}
                filtered = [token for token in tokens if token in actions and
                            (action is None or actions[token] == action)]
                if filtered:
                    return filtered
        return []

    def click(action: str, preferred_value: str | None = None) -> str:
        tokens = latest_tokens(action)
        if not tokens:
            raise EvaluationError("Resume picker did not offer the expected next action")
        connection = callback.get_db()
        try:
            rows = {row[0]: row[1] for row in connection.execute(
                "SELECT token,value FROM resume_choice_tokens WHERE job_id=?", (job_id,)
            ).fetchall()}
        finally:
            connection.close()
        token = next((value for value in tokens if preferred_value is not None and rows.get(value) == preferred_value), tokens[0])
        result = callback.dispatch_callback_data(f"resume_choice:{token}", f"eval-{index}-{action}-{time.time_ns()}")
        if result != 0:
            raise EvaluationError("A resume picker choice was rejected")
        return token

    click("flavor", flavor["id"])
    click("variant", variant["id"])
    selection_ids = list(variant["default_project_ids"])
    toggled_id = None
    if index > 0 and len(selection_ids) > 1:
        toggled_id = selection_ids[0]
        click("toggle", toggled_id)
        click("toggle", toggled_id)
    if not selection_ids:
        raise EvaluationError("The selected project variant left no project selected")
    selection = {"flavor_id": flavor["id"], "variant_id": variant["id"], "project_ids": selection_ids}
    generate_tokens = latest_tokens("generate")
    if not generate_tokens:
        raise EvaluationError("Resume picker did not offer Generate resume")
    generate_token = generate_tokens[-1]
    generation_result = callback.dispatch_callback_data(
        f"resume_choice:{generate_token}", f"eval-{index}-generate"
    )
    if generation_result != 0 or len(package_calls) != calls_before + 1:
        raise EvaluationError("Selected package generation failed")

    package_call_count = len(package_calls)
    duplicate_result = callback.dispatch_callback_data(
        f"resume_choice:{generate_token}", f"eval-{index}-duplicate-generate"
    )
    if duplicate_result == 0 or len(package_calls) != package_call_count:
        raise EvaluationError("A spent Generate token was accepted or generated a second package")

    connection = callback.get_db()
    try:
        row = connection.execute(
            "SELECT stage,package_path,submitted_at FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1",
            (job_id,),
        ).fetchone()
    finally:
        connection.close()
    if not row or row[0] != "package_generated" or row[2] is not None or not row[1]:
        raise EvaluationError("Generated package did not reach the expected pre-submission state")
    directory = Path(row[1])
    if directory.parent.resolve() != package_dir.resolve():
        raise EvaluationError("Package escaped the private sandbox output directory")
    manifest_path = directory / "tailoring_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resume_payload = json.loads((directory / "resume.json").read_text(encoding="utf-8"))
    if manifest.get("resume_selection") != selection:
        raise EvaluationError("Manifest does not record the selected fixed blocks")
    exact_content = _profile_summary_and_bullets(profile, selection, resume_payload)
    if not exact_content:
        raise EvaluationError("Generated resume changed the fixed summary or project bullets")
    audit = audit_resume_pdf(directory / "Resume.pdf", resume_payload, max_pages=variant["max_pages"])
    layout = manifest.get("layout") or {}
    actual_layout = resume_layout_metrics(directory / "Resume.pdf")
    if manifest.get("resume_pages") != actual_layout.get("pages"):
        raise EvaluationError("PDF page count differs from its package manifest")
    if layout.get("main_fills") != actual_layout.get("main_fills"):
        raise EvaluationError("Measured PDF page fill differs from its package manifest")
    if len(delivered_documents) != 1 or delivered_documents[0] != "Resume.pdf":
        raise EvaluationError("The resume PDF was not delivered through the transport stub")
    if not any(button.get("callback_data") == f"proceed_apply:{job_id}"
               for message in messages for button in _choice_buttons(message)):
        raise EvaluationError("Successful delivery did not offer the Proceed to apply action")

    proceed_result = callback.dispatch_callback_data(f"proceed_apply:{job_id}", f"eval-{index}-proceed")
    if proceed_result != 0:
        raise EvaluationError("Proceed to apply failed in the sandbox flow")
    connection = callback.get_db()
    try:
        final = connection.execute(
            "SELECT stage,submitted_at FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)
        ).fetchone()
    finally:
        connection.close()
    if not final or final[0] != "approved_to_prepare_apply" or final[1] is not None:
        raise EvaluationError("Proceed flow did not stop before application submission")
    if denied_network:
        raise EvaluationError("An outbound request was attempted outside the configured research allowance")

    return {
        "job_id": job_id,
        "title": str(job.get("title") or ""),
        "company": str(job.get("company") or ""),
        "selection": selection,
        "toggled_project_id": toggled_id,
        "no_generation_before_selection": True,
        "duplicate_generate_token_rejected": True,
        "fixed_summary_and_bullets_exact": exact_content,
        "resume_pages": manifest.get("resume_pages"),
        "page_limit": variant["max_pages"],
        "page_two_sparse": bool(layout.get("page_two_sparse")),
        "main_fills": layout.get("main_fills", []),
        "pdf_audit": audit,
        "transport": {"research_cards": 1, "resume_documents": len(delivered_documents),
                      "messages": len(messages), "callback_acks": len(callback_acks),
                      "telegram_network_attempts": 0},
        "final_stage": final[0],
        "submitted": False,
    }


def _run_sandbox_flows(callback, scraper, interest_flow, profile, selected_jobs,
                       sandbox_db, profile_path, package_dir, research_live):
    package_calls: list[str] = []
    real_prepare = interest_flow.prepare_application_package

    def counted_prepare(job_id, **kwargs):
        package_calls.append(str(job_id))
        return real_prepare(job_id, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(mock.patch.object(callback, "DB_PATH", sandbox_db))
        stack.enter_context(mock.patch.object(callback, "PROFILE_PATH", profile_path))
        stack.enter_context(mock.patch.object(callback, "OUTPUT_DIR", package_dir))
        stack.enter_context(mock.patch.object(interest_flow, "DEFAULT_DB_PATH", sandbox_db))
        stack.enter_context(mock.patch.object(interest_flow, "DEFAULT_PROFILE_PATH", profile_path))
        stack.enter_context(mock.patch.object(interest_flow, "DEFAULT_OUTPUT_DIR", package_dir))
        stack.enter_context(mock.patch.object(interest_flow, "prepare_application_package", counted_prepare))
        stack.enter_context(mock.patch.object(scraper, "sync_application_tracker_if_enabled", lambda: False))
        results = [
            _evaluate_one(callback, profile, job, index, package_calls, package_dir, research_live)
            for index, job in enumerate(selected_jobs)
        ]
    return results, package_calls


def run_evaluation(db_path: Path, profile_path: Path, output_path: Path, *, sample_size: int = 3,
                   seed: int = 0, research_live: bool = False) -> dict[str, Any]:
    if sample_size < 1:
        raise EvaluationError("Sample size must be positive")
    db_path, profile_path, output_path = map(lambda path: Path(path).expanduser().resolve(),
                                             (db_path, profile_path, output_path))
    if output_path in {db_path, profile_path}:
        raise EvaluationError("Report path must be separate from the source database and profile")
    if output_path.exists():
        raise EvaluationError("Report destination already exists")
    if not profile_path.is_file():
        raise EvaluationError("Candidate profile does not exist")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        import resume_blocks
        import resume_refiner
        resume_blocks.catalog(profile)
        resume_refiner.validate_profile(profile)
    except (OSError, ValueError, TypeError):
        raise EvaluationError("Candidate profile has no valid fixed resume catalog") from None
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    sandbox_dir = Path(tempfile.mkdtemp(prefix=f".{output_path.stem}-sandbox-", dir=output_path.parent))
    sandbox_dir.chmod(0o700)
    sandbox_db = sandbox_dir / "sandbox.db"
    package_dir = sandbox_dir / "packages"
    package_dir.mkdir(mode=0o700)
    package_dir.chmod(0o700)

    source = _source_connection(db_path)
    try:
        selected_jobs = _sample_jobs(source, sample_size, seed)
        if not selected_jobs:
            raise EvaluationError("No eligible unsubmitted job offers were found")
        _backup_database(source, sandbox_db)
    finally:
        source.close()

    callback_was_loaded = "callback_handler" in sys.modules
    try:
        with mock.patch.dict(os.environ, _evaluation_environment(sandbox_db, profile_path, package_dir, research_live)):
            # The imported handler reads its configuration once. Environment and all
            # module globals are restored when the evaluator returns.
            try:
                import callback_handler as callback
                import scraper
                import jobhunter_interest_flow as interest_flow
            except SystemExit:
                raise EvaluationError("Callback handler could not initialize for sandbox evaluation") from None
            results, package_calls = _run_sandbox_flows(
                callback, scraper, interest_flow, profile, selected_jobs,
                sandbox_db, profile_path, package_dir, research_live)
    finally:
        if not callback_was_loaded:
            sys.modules.pop("callback_handler", None)

    report = {
        "format_version": 1,
        "source_db": str(db_path),
        "source_db_opened_read_only": True,
        "sqlite_backup_api_used": True,
        "sandbox_db": str(sandbox_db),
        "candidate_profile": str(profile_path),
        "research_mode": "live_best_effort" if research_live else "offline_best_effort",
        "seed": seed,
        "requested_sample_size": sample_size,
        "evaluated_count": len(results),
        "all_stopped_before_submission": all(not item["submitted"] for item in results),
        "package_generation_count": len(package_calls),
        "results": results,
    }
    temp_output = output_path.with_name(output_path.name + f".{os.getpid()}.tmp")
    try:
        with temp_output.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temp_output.chmod(0o600)
        os.replace(temp_output, output_path)
        output_path.chmod(0o600)
    except OSError:
        try:
            temp_output.unlink(missing_ok=True)
        except OSError:
            pass
        raise EvaluationError("Could not write the private evaluation report") from None
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path, help="SQLite source database (read-only)")
    parser.add_argument("--profile", required=True, type=Path, help="Candidate profile with a valid fixed catalog")
    parser.add_argument("--output", required=True, type=Path, help="Private JSON report destination")
    parser.add_argument("--sample-size", type=int, default=3)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--research-live", action="store_true",
                        help="Allow the existing best-effort company research to use network search")
    args = parser.parse_args(argv)
    try:
        report = run_evaluation(args.db, args.profile, args.output, sample_size=args.sample_size,
                                seed=args.seed, research_live=args.research_live)
    except EvaluationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"evaluated_count": report["evaluated_count"],
                      "report": str(Path(args.output).expanduser().resolve()),
                      "sandbox_db": report["sandbox_db"],
                      "submitted": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
