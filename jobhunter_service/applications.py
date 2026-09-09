"""Candidate-only application packages and short-lived, exact browser approvals.

The Telegram transport supplies actor IDs. Models may request a proposal but
cannot execute its token. Browser/package code runs in a fixed subprocess with
owner environment and automatic tracker commands disabled.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

from .state import private_json

PURPOSE = "application_approval"
JOB_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,100}")


def _inside(root: Path, path: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise PermissionError("Application paths must belong to your JobHunter profile.")
    return resolved


def _job(root, job_id):
    if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
        raise ValueError("Use a valid job ID from your digest.")
    db_path = _inside(root, root / "jobs.db")
    if not db_path.is_file():
        raise ValueError("No collected jobs are available for this profile yet.")
    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as db, db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise ValueError("That job is not in your profile's collected jobs.")
    return dict(row)


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _file_hash(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def _page_url(value):
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Open the employer's HTTPS application page in your connected browser first.")
    host = parsed.hostname.lower()
    if host == "localhost" or ":" in host or re.fullmatch(r"[0-9.]+", host) or host.endswith((".local", ".internal")):
        raise ValueError("This page is not a public application page.")
    return value


def _card(job):
    return f"{str(job.get('title') or '')[:160]}\n{str(job.get('company') or '')[:160]} — {str(job.get('location') or '')[:160]}"


@contextmanager
def _lock(root):
    state = _inside(root, root / "state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(state / "application.lock", os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Another application action is running for your profile; retry when it finishes.") from None
        yield


def run_worker(root: Path, request: dict) -> dict:
    repo = Path(__file__).resolve().parents[1]
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(repo),
           "PYTHON_DOTENV_DISABLED": "1", "JOBHUNTER_AUTO_SYNC_TRACKER": "false",
           "JOBHUNTER_INTERESTED_WEB_RESEARCH": "false", "JOBHUNTER_CANDIDATE_ROOT": str(root)}
    try:
        result = subprocess.run([sys.executable, "-m", "jobhunter_service.applications", "worker"],
                                input=json.dumps({**request, "root": str(root)}), capture_output=True,
                                text=True, cwd=root, env=env, timeout=180, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ValueError("Application action could not finish. Inspect the current page before retrying; no successful submission was assumed.") from None
    if result.returncode != 0:
        raise ValueError("Application action could not be completed. Review your confirmed resume or browser page and retry; no success was assumed.")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError()
        return response
    except (ValueError, TypeError):
        raise ValueError("Application action returned an invalid result; inspect the current status before retrying.") from None


class ApplicationService:
    def __init__(self, service, browser_manager, telegram_client, planner=None):
        self.service = service
        self.browser = browser_manager
        self.telegram = telegram_client
        self.planner = planner
        self._run = run_worker

    def _context(self, actor, job_id):
        member = self.service._member(actor)
        root = self.service.profile_dir(member)
        return member, root, _job(root, job_id)

    def _send(self, actor, text, markup=None):
        self.service._member(actor)
        return self.telegram.send_message(actor, text[:3900], reply_markup=markup)

    def details(self, actor, job_id):
        _, _, job = self._context(actor, job_id)
        text = _card(job) + "\n\n" + str(job.get("description") or "No description collected.")[:2300]
        text += "\n\n" + str(job.get("url") or "")[:500]
        self._send(actor, text)
        return {key: job.get(key) for key in ("id", "title", "company", "location", "url", "description")}

    def _sync(self, actor):
        callback = getattr(self.service, "sync_tracker", None)
        if callback:
            try:
                callback(actor)
            except Exception:
                self._send(actor, "Your application record is saved. Tracker synchronization will need a retry.")

    def interested(self, actor, job_id):
        member, root, job = self._context(actor, job_id)
        with _lock(root):
            self.service._member(actor)
            result = self._run(root, {"operation": "interested", "job_id": job_id})
        text = _card(job) + "\n\nMarked interested.\n"
        text += "Company information: " + str(job.get("credibility_notes") or "Not independently verified.")[:700]
        text += "\nAdvertised salary: " + str(job.get("salary") or "Not published.")[:200]
        from jobhunter_matching import salary_target_for_job
        target = salary_target_for_job(job, json.loads(member["settings"])["search"].get("markets", []))
        if target:
            text += f"\nYour salary expectation: {target['currency']} {target['amount']:,}/{target['period']}"
        text += f"\n\nUse /apply {job_id} to prepare your resume and cover letter."
        self._send(actor, text)
        self._sync(actor)
        return result

    def prepare(self, actor, job_id):
        member, root, job = self._context(actor, job_id)
        if not hasattr(self.telegram, "send_document"):
            raise ValueError("Private document delivery is not configured yet.")
        profile = _inside(root, root / "master-profile.json")
        if not profile.is_file():
            raise ValueError("Upload and confirm your resume before preparing applications.")
        with _lock(root):
            result = self._run(root, {"operation": "prepare", "job_id": job_id})
            if self.service._member(actor)["revision"] != member["revision"]:
                raise ValueError("Your profile changed during preparation. Prepare this package again before using it.")
            documents = {}
            for key in ("resume_pdf", "cover_pdf"):
                path = _inside(root / "output", Path(result[key]))
                if not path.is_file() or path.suffix.casefold() != ".pdf":
                    raise ValueError("The generated package is incomplete; prepare it again.")
                documents[key] = path
            for key, path in documents.items():
                self.service._member(actor)
                self.telegram.send_document(actor, path, caption=f"{job.get('title', '')[:100]} — {'Resume' if key == 'resume_pdf' else 'Cover letter'}")
        self._send(actor, "Your application documents are ready for review. Open the correct application in your connected browser, "
                   f"then use /inspect {job_id}. Upload and final submission each require a separate confirmation.")
        self._sync(actor)
        return {"job_id": job_id, "status": "package_generated"}

    def _browser(self, member):
        if self.browser is None:
            raise ValueError("Connect your isolated LinkedIn browser first.")
        status = self.browser.status(member["profile_id"])
        if status.get("status") != "running" or status.get("profile_id") != member["profile_id"]:
            raise ValueError("Connect your isolated LinkedIn browser first.")
        port = status.get("cdp_port")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise PermissionError("The candidate browser connection is invalid.")
        return port

    def _observe(self, root, job_id, port, target_id=None):
        result = self._run(root, {"operation": "observe", "job_id": job_id, "cdp_port": port,
                                  "target_id": target_id})
        _page_url(result.get("url", ""))
        if not isinstance(result.get("target_id"), str) or not result["target_id"]:
            raise ValueError("The application browser page is unavailable.")
        return result

    def inspect(self, actor, job_id):
        member, root, job = self._context(actor, job_id)
        with _lock(root):
            port = self._browser(member)
            page = self._observe(root, job_id, port)
            snapshot = {"job_id": job_id, "revision": member["revision"], "cdp_port": port,
                        "target_id": page["target_id"], "url": page["url"], "job_digest": _digest(job)}
            private_json(root / "state" / f"application-page-{job_id}.json", snapshot)
        text = _card(job) + "\n\nCurrent browser page:\n" + str(page.get("title", ""))[:250] + "\n" + page["url"][:700]
        if page["url"] != job.get("url"):
            text += "\nThis differs from the collected posting URL. Verify that this page is the application for the role shown above."
        blockers = page.get("blockers", [])
        questions = page.get("sensitive_questions", [])
        if blockers or questions:
            text += "\n\nComplete these items yourself in the browser:\n" + "\n".join(str(x)[:180] for x in [*blockers, *questions][:7])
        text += f"\n\nUse /upload {job_id} to review a resume upload or /submit {job_id} to review final submission."
        self._send(actor, text)
        return {"job_id": job_id, "url": page["url"], "title": page.get("title"), "blockers": blockers, "sensitive_questions": questions}

    def _bound_page(self, member, root, job):
        path = _inside(root, root / "state" / f"application-page-{job['id']}.json")
        if not path.is_file():
            raise ValueError("Inspect this job's current application page before requesting an upload or submission.")
        snapshot = json.loads(path.read_text())
        port = self._browser(member)
        if snapshot.get("revision") != member["revision"] or snapshot.get("job_digest") != _digest(job) or snapshot.get("cdp_port") != port:
            raise ValueError("The profile, job or browser changed. Inspect this application again.")
        page = self._observe(root, job["id"], port, snapshot["target_id"])
        if page["url"] != snapshot["url"] or page["target_id"] != snapshot["target_id"]:
            raise ValueError("The browser page changed. Inspect the current application before approving an action.")
        return page, port

    def _proposal(self, actor, job_id, operation):
        member, root, job = self._context(actor, job_id)
        with _lock(root):
            page, port = self._bound_page(member, root, job)
            if page.get("blockers") or page.get("sensitive_questions"):
                raise ValueError("Complete the browser's verification and sensitive questions yourself before requesting this action.")
            selector_key = "upload_selector" if operation == "upload" else "submit_selector"
            selector = page.get(selector_key)
            if not selector:
                raise ValueError("A unique safe control could not be identified. Complete this step yourself in your isolated browser.")
            if operation == "submit" and page.get("missing_required"):
                raise ValueError("Complete every required field yourself before requesting final submission.")
            if operation == "submit":
                self._check_prior_submission(root, job_id)
            payload = {"job_id": job_id, "operation": operation, "profile_id": member["profile_id"],
                       "revision": member["revision"], "job_digest": _digest(job), "cdp_port": port,
                       "target_id": page["target_id"], "url": page["url"], "selector": selector}
            if operation == "upload":
                document = self._resume(root, job_id)
                payload.update(document=str(document), document_sha256=_file_hash(document))
            token = self.service.store.token(actor, PURPOSE, payload, ttl=600)
        text = _card(job) + "\n\n" + ("Upload your prepared resume" if operation == "upload" else "Submit this application")
        text += "\nPage: " + str(page.get("title", ""))[:200] + "\n" + page["url"][:700]
        if operation == "upload":
            text += "\nDocument: " + Path(payload["document"]).name
        else:
            text += "\nConfirm only after reviewing the complete application and your answers in the browser."
        self._send(actor, text, {"inline_keyboard": [[{"text": "Confirm resume upload" if operation == "upload" else "Confirm final submission",
                                                        "callback_data": f"jh:application:{token}"}]]})
        return {"token": token, "operation": operation, "job_id": job_id}

    def propose_upload(self, actor, job_id):
        return self._proposal(actor, job_id, "upload")

    def propose_submit(self, actor, job_id):
        return self._proposal(actor, job_id, "submit")

    @staticmethod
    def _resume(root, job_id):
        with closing(sqlite3.connect((root / "jobs.db").as_uri() + "?mode=ro", uri=True)) as db, db:
            row = db.execute("SELECT package_path FROM applications WHERE job_id=? AND package_path IS NOT NULL ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if not row or not row[0]:
            raise ValueError("Prepare this job's resume package before uploading.")
        package = Path(row[0])
        package = package if package.is_absolute() else root / package
        package = _inside(root / "output", package)
        path = _inside(root / "output", package / "Resume.pdf" if package.is_dir() else package)
        if path.suffix.casefold() != ".pdf" or not path.is_file() or path.stat().st_size == 0:
            raise ValueError("The prepared resume is unavailable; prepare the package again.")
        manifest_path = _inside(root / "output", path.parent / "tailoring_manifest.json")
        profile_path = _inside(root, root / "master-profile.json")
        if not manifest_path.is_file() or not profile_path.is_file():
            raise ValueError("Prepare a verified resume package for this job before uploading.")
        manifest = json.loads(manifest_path.read_text())
        profile = json.loads(profile_path.read_text())
        profile_hash = hashlib.sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if manifest.get("job_id") != job_id or manifest.get("profile_sha256") != profile_hash:
            raise ValueError("The package was prepared for another job or an older resume; prepare it again.")
        return path

    @staticmethod
    def _check_prior_submission(root, job_id):
        if _inside(root, root / "state" / f"application-attempt-{job_id}.json").exists():
            raise ValueError("This application has an unresolved submission attempt. Review its receipt before trying again.")
        with closing(sqlite3.connect((root / "jobs.db").as_uri() + "?mode=ro", uri=True)) as db, db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='applications'").fetchone():
                return
            row = db.execute("SELECT stage,submitted_at FROM applications WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
        if row and (row[1] or row[0] in {"submitted", "submission_attempted"}):
            raise ValueError("This application already has a submission or an unresolved attempt. Review its receipt before trying again.")

    def execute(self, actor, token):
        # Settings changes and revocation use this same per-member lock. Hold it
        # through the final validation, token consumption and browser mutation.
        with self.service.mutation(actor):
            return self._execute_locked(actor, token)

    def _execute_locked(self, actor, token):
        self.service._member(actor)
        owner, payload = self.service.store.read_token(token, PURPOSE, consume=False)
        if owner != actor:
            raise PermissionError("This application confirmation belongs to another user.")
        member, root, job = self._context(actor, payload["job_id"])
        with _lock(root):
            member = self.service._member(actor)
            if (payload.get("revision") != member["revision"] or payload.get("profile_id") != member["profile_id"]
                    or payload.get("job_digest") != _digest(job) or payload.get("cdp_port") != self._browser(member)):
                raise ValueError("The application context changed; request a fresh confirmation.")
            page = self._observe(root, job["id"], payload["cdp_port"], payload["target_id"])
            key = "upload_selector" if payload["operation"] == "upload" else "submit_selector"
            if (page["url"] != payload["url"] or page["target_id"] != payload["target_id"]
                    or page.get(key) != payload["selector"] or page.get("blockers") or page.get("sensitive_questions")
                    or (payload["operation"] == "submit" and page.get("missing_required"))):
                raise ValueError("The application page changed; inspect it and request a fresh confirmation.")
            if payload["operation"] == "upload":
                current = self._resume(root, job["id"])
                if str(current) != payload["document"] or _file_hash(current) != payload["document_sha256"]:
                    raise ValueError("The prepared resume changed; request a fresh upload confirmation.")
            else:
                self._check_prior_submission(root, job["id"])
            consumed_owner, _ = self.service.store.read_token(token, PURPOSE, consume=True)
            if consumed_owner != actor:
                raise PermissionError("This application confirmation belongs to another user.")
            # Invalidate sibling proposals for this job before an external action.
            with self.service.store.connect() as db:
                rows = db.execute("SELECT digest,payload FROM tokens WHERE user_id=? AND purpose=? AND consumed=0", (actor, PURPOSE)).fetchall()
                for row in rows:
                    if json.loads(row["payload"]).get("job_id") == job["id"]:
                        db.execute("UPDATE tokens SET consumed=1 WHERE digest=?", (row["digest"],))
            if payload["operation"] == "submit":
                private_json(root / "state" / f"application-attempt-{job['id']}.json",
                             {"job_id": job["id"], "url": payload["url"], "status": "outcome_unknown"})
            result = self._run(root, {**payload, "approved": True})
            if payload["operation"] == "submit":
                private_json(root / "state" / f"application-attempt-{job['id']}.json",
                             {"job_id": job["id"], "url": payload["url"], "status": "submission_attempted"})
        message = ("Your resume was uploaded to the reviewed application page." if payload["operation"] == "upload"
                   else "The submit control was clicked. Review the resulting page for a receipt matching this role; submission is recorded as attempted until confirmed.")
        self._send(actor, message)
        self._sync(actor)
        return {"job_id": job["id"], "status": "resume_uploaded" if payload["operation"] == "upload" else "submission_attempted"}


def _selector(items, *, upload=False):
    candidates = []
    for item in items:
        if upload:
            if item.get("type") != "file":
                continue
        elif item.get("disabled") or str(item.get("text", "")).strip().casefold() not in {"submit", "submit application", "send application"}:
            continue
        identifier = item.get("id", "")
        name = item.get("name", "")
        if isinstance(identifier, str) and SAFE_ID.fullmatch(identifier):
            candidates.append("[id=" + json.dumps(identifier) + "]")
        elif isinstance(name, str) and SAFE_ID.fullmatch(name):
            candidates.append(("input[type=file]" if upload else "button") + "[name=" + json.dumps(name) + "]")
    return candidates[0] if len(candidates) == 1 else None


def _browser_page(port, target_id=None):
    from jobhunter_auto_apply.cdp import CDPClient, list_targets
    from jobhunter_auto_apply.engine import inspect_page

    if type(port) is not int or not 1024 <= port <= 65535:
        raise PermissionError("Invalid candidate browser port.")
    targets = [target for target in list_targets("127.0.0.1", port) if target.type == "page"]
    if target_id:
        targets = [target for target in targets if target.id == target_id]
    if len(targets) != 1:
        raise ValueError("Keep only the intended application tab open, then inspect it again.")
    target = targets[0]
    endpoint = urlsplit(target.websocket_url)
    # Chromium may advertise its container's 9222 listener despite discovery
    # arriving through the broker's dynamic host port. Only its page path is
    # accepted; the connection authority always comes from the trusted broker.
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target.id)
            or endpoint.scheme != "ws" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}
            or endpoint.port not in {port, 9222, 9223}
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
            or endpoint.path != f"/devtools/page/{target.id}"):
        raise PermissionError("The candidate browser target is not on its assigned loopback port.")
    client = CDPClient(f"ws://127.0.0.1:{port}/devtools/page/{target.id}")
    try:
        inspection = inspect_page(client)
        _page_url(inspection.url)
        result = asdict(inspection)
        result.update(target_id=target.id, upload_selector=_selector(inspection.inputs, upload=True),
                      submit_selector=_selector(inspection.buttons),
                      missing_required=any(item.get("required") and not item.get("value_present") for item in inspection.inputs))
        return client, result
    except Exception:
        client.close()
        raise


def worker(request):
    root = Path(request["root"]).resolve()
    job = _job(root, request["job_id"])
    operation = request["operation"]
    if operation == "prepare":
        from jobhunter_interest_flow import prepare_application_package
        result = prepare_application_package(job["id"], db_path=root / "jobs.db",
                                             profile_path=_inside(root, root / "master-profile.json"),
                                             output_dir=_inside(root, root / "output"))
        return {"resume_pdf": str(result.resume_pdf), "cover_pdf": str(result.cover_pdf)}
    if operation == "interested":
        import scraper
        with closing(sqlite3.connect(root / "jobs.db")) as db, db:
            scraper.init_application_tracking(db)
            db.execute("UPDATE jobs SET status='interested' WHERE id=?", (job["id"],))
            scraper.record_job_feedback(db, job["id"], "interested", source="candidate_telegram")
            scraper.record_application_stage(db, job["id"], "interested", sync=False)
        return {"job_id": job["id"], "status": "interested"}
    if operation not in {"observe", "upload", "submit"}:
        raise ValueError("Unknown application operation.")
    client, page = _browser_page(request["cdp_port"], request.get("target_id"))
    try:
        if operation == "observe":
            return page
        if request.get("approved") is not True or request.get("url") != page["url"]:
            raise PermissionError("A current explicit application confirmation is required.")
        key = "upload_selector" if operation == "upload" else "submit_selector"
        if (request.get("selector") != page.get(key) or not page.get(key)
                or page.get("blockers") or page.get("sensitive_questions")
                or (operation == "submit" and page.get("missing_required"))):
            raise PermissionError("The reviewed application controls changed; inspect the page again.")
        from jobhunter_auto_apply.engine import ApplyConfig, AutoApplyEngine
        config = ApplyConfig(db_path=str(root / "jobs.db"), output_dir=str(_inside(root, root / "output")),
                             cdp_port=request["cdp_port"], tracker_sync=False, verify_submission=True,
                             expected_page_url=request["url"], browser_output_dir="/documents")
        engine = AutoApplyEngine(config, client)
        if operation == "upload":
            path = _inside(root / "output", Path(request["document"]))
            if _file_hash(path) != request["document_sha256"]:
                raise PermissionError("The approved resume changed.")
            result = engine.upload_file(job["id"], request["selector"], str(path), approved=True)
        else:
            result = engine.click_submit(job["id"], request["selector"], approved=True)
        return {"url": result.url, "blockers": result.blockers, "sensitive_questions": result.sensitive_questions}
    finally:
        client.close()


def main():
    if sys.argv[1:] != ["worker"]:
        return 1
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.read(100000))
        result = worker(request)
        print(json.dumps(result))
        return 0
    except Exception:
        print("Application worker could not complete the request.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
