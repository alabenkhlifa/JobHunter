"""Run with python -m resume_review --profile data/master-profile.json.

The source profile is read once. Reviews, requests, proposals and PDFs are kept
in the separate ignored state directory. This service never deploys a profile.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from resume_blocks import base_for_variant, catalog as validate_catalog, fit, project_for_variant

STATIC = Path(__file__).parent / "static"
ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".review-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class ReviewError(ValueError):
    status = 400


class Conflict(ReviewError):
    status = 409


def find(items, identifier):
    for item in items:
        if item["id"] == identifier:
            return item
    raise ReviewError("The selected item no longer exists.")


def compose(catalog, selection):
    """Preview draft wording without changing any confirmation flags."""
    validate_catalog({"resume_blocks": catalog})
    flavor = find(catalog["flavors"], selection.get("flavor_id"))
    if not selection.get("variant_id"):
        raise ReviewError("Add a variant for this flavor to see its preview.")
    variant = find(catalog["variants"], selection.get("variant_id"))
    if variant["flavor_id"] != flavor["id"]:
        raise ReviewError("Choose a variant belonging to this flavor.")
    ids = selection.get("project_ids")
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise ReviewError("Select at least one unique project.")
    if set(ids) - set(variant["project_ids"]):
        raise ReviewError("A selected project is not allowed by this variant.")
    result = base_for_variant(catalog, variant)
    result.update(headline=flavor.get("headline", ""), summary=flavor.get("summary", ""))
    employers = {}
    for identifier in ids:
        project = project_for_variant(catalog, variant, identifier)
        fields = {k: copy.deepcopy(v) for k, v in project["role"].items() if k != "bullets"}
        company = project["company"]
        if company not in employers:
            employers[company] = {**fields, "company": company, "bullets": [], "engagements": []}
        role = employers[company]
        if any(role.get(k) != v for k, v in fields.items()):
            raise ReviewError(f"Projects for {company} need the same employer details.")
        entry = project.get("entry")
        if entry is None:
            role["bullets"].extend(copy.deepcopy(project["role"]["bullets"]))
        else:
            role["engagements"].append(copy.deepcopy(entry))
            role["bullets"].extend(copy.deepcopy(entry["bullets"]))
    result["experience"] = list(employers.values())
    return result, variant


def get_path(value, path):
    for key in path:
        if isinstance(value, list) and isinstance(key, int):
            value = value[key]
        elif isinstance(value, dict) and isinstance(key, str):
            value = value[key]
        else:
            raise ReviewError("Invalid text target.")
    return value


def validate_text_path(catalog, path):
    if (not isinstance(path, list) or not path or len(path) > 10
            or path[0] not in ("base", "flavors", "projects", "variants")
            or any(key in ("confirmation", "id", "flavor_id", "project_ids", "default_project_ids") for key in path)):
        raise ReviewError("Choose an editable text field.")
    try:
        value = get_path(catalog, path)
    except (KeyError, IndexError, TypeError):
        raise ReviewError("The requested text field no longer exists.") from None
    if not isinstance(value, str):
        raise ReviewError("Change proposals must target a text field.")
    return value


def invalidate_changed(old, new):
    for kind in ("flavors", "projects", "variants"):
        before = {item["id"]: item for item in old[kind]}
        for item in new[kind]:
            previous = before.get(item["id"])
            comparable = lambda x: {k: v for k, v in x.items() if k != "confirmation"}
            if previous and comparable(previous) == comparable(item):
                item["confirmation"] = previous["confirmation"]
            else:
                item["confirmation"] = "draft"


class ReviewStore:
    def __init__(self, profile, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory.chmod(0o700)
        self.path = self.directory / "review.json"
        self.lock = threading.RLock()
        self.render_lock = threading.Lock()
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
            validate_catalog({"resume_blocks": self.state["catalog"]})
        else:
            catalog = json.loads(Path(profile).read_text())["resume_blocks"]
            validate_catalog({"resume_blocks": catalog})
            flavor = catalog["flavors"][0]
            variant = next(v for v in catalog["variants"] if v["flavor_id"] == flavor["id"])
            self.state = {"version": 1, "revision": 0, "catalog": catalog,
                          "baseline": copy.deepcopy(catalog), "source_digest": digest(catalog),
                          "selection": {"flavor_id": flavor["id"], "variant_id": variant["id"],
                                        "project_ids": variant["default_project_ids"][:]},
                          "requests": [], "proposals": [], "approvals": [], "history": [], "undo": [], "updated_at": now()}
            atomic_json(self.path, self.state)

    def read(self):
        with self.lock:
            return copy.deepcopy(self.state)

    def mutate(self, revision, action, operation):
        with self.lock:
            if type(revision) is not int or revision != self.state["revision"]:
                raise Conflict("The review changed in another tab. Reload the latest version before saving.")
            state = copy.deepcopy(self.state)
            operation(state)
            if action in ("Saved edits", "Reviewed text proposal") and (
                    state["catalog"] != self.state["catalog"] or state["selection"] != self.state["selection"]):
                snapshots = self.directory / "snapshots"
                snapshots.mkdir(exist_ok=True, mode=0o700)
                checkpoint = str(self.state["revision"])
                atomic_json(snapshots / f"{checkpoint}.json", {
                    "catalog": self.state["catalog"], "selection": self.state["selection"]})
                state.setdefault("undo", []).append(checkpoint)
            state["revision"] += 1
            state["updated_at"] = now()
            state["history"].append({"revision": state["revision"], "action": action, "at": now()})
            atomic_json(self.path, state)
            self.state = state
            return copy.deepcopy(state)

    def save(self, body):
        def operation(state):
            catalog = copy.deepcopy(body["catalog"])
            invalidate_changed(state["catalog"], catalog)
            validate_catalog({"resume_blocks": catalog})
            # A flavor may still be empty or have no variant during editing.
            selection = body["selection"]
            if not isinstance(selection, dict) or set(selection) != {"flavor_id", "variant_id", "project_ids"}:
                raise ReviewError("Invalid preview selection.")
            state["catalog"], state["selection"] = catalog, copy.deepcopy(selection)
        return self.mutate(body.get("revision"), "Saved edits", operation)

    def request(self, body):
        def operation(state):
            path, message = body.get("path"), str(body.get("message", "")).strip()
            if not message or len(message) > 8000:
                raise ReviewError("Write a change request of up to 8,000 characters.")
            before = validate_text_path(state["catalog"], path) if path else None
            state["requests"].append({"id": uuid.uuid4().hex, "path": path, "before": before,
                                      "message": message, "status": "open", "author": "user", "at": now()})
        return self.mutate(body.get("revision"), "Added change request", operation)

    def undo(self, body):
        def operation(state):
            if not state.get("undo"):
                raise ReviewError("There are no saved edits to undo.")
            checkpoint = state["undo"].pop()
            previous = json.loads((self.directory / "snapshots" / f"{checkpoint}.json").read_text())
            state["catalog"], state["selection"] = previous["catalog"], previous["selection"]
        return self.mutate(body.get("revision"), "Undid saved edit", operation)

    def propose(self, body):
        def operation(state):
            path, after = body.get("path"), body.get("after")
            before = validate_text_path(state["catalog"], path)
            if not isinstance(after, str) or len(after) > 16000 or after == before:
                raise ReviewError("Provide different replacement text of up to 16,000 characters.")
            request_id = body.get("request_id")
            if request_id and not any(r["id"] == request_id for r in state["requests"]):
                raise ReviewError("The linked request does not exist.")
            state["proposals"].append({"id": uuid.uuid4().hex, "path": path, "before": before, "after": after,
                                       "rationale": str(body.get("rationale", ""))[:8000], "request_id": request_id,
                                       "status": "pending", "author": "assistant", "at": now()})
        return self.mutate(body.get("revision"), "Received text proposal", operation)

    def decide(self, body):
        def operation(state):
            proposal = find(state["proposals"], body.get("id"))
            if proposal["status"] != "pending":
                raise Conflict("This proposal was already reviewed.")
            accept = body.get("decision") == "accept"
            if body.get("decision") not in ("accept", "reject"):
                raise ReviewError("Choose accept or reject.")
            if accept:
                current = validate_text_path(state["catalog"], proposal["path"])
                if current != proposal["before"]:
                    raise Conflict("This text changed after the proposal. Ask for an updated proposal.")
                old = copy.deepcopy(state["catalog"])
                parent = get_path(state["catalog"], proposal["path"][:-1])
                parent[proposal["path"][-1]] = proposal["after"]
                invalidate_changed(old, state["catalog"])
                validate_catalog({"resume_blocks": state["catalog"]})
            proposal["status"], proposal["decided_at"] = "accepted" if accept else "rejected", now()
            for request in state["requests"]:
                if request["id"] == proposal.get("request_id") and accept:
                    request["status"] = "resolved"
        return self.mutate(body.get("revision"), "Reviewed text proposal", operation)

    def approval(self, body):
        snapshot = self.read()
        if body.get("revision") != snapshot["revision"]:
            raise Conflict("Render the current saved revision before approving it.")
        preview = self.preview()
        if body.get("content_digest") != preview["content_digest"]:
            raise Conflict("The rendered version changed. Review the current preview before approving it.")
        if preview["warnings"] or not preview["audit_ok"]:
            raise ReviewError("Resolve the preview warnings before approving this version.")
        def operation(state):
            state["approvals"].append({"at": now(), "selection": copy.deepcopy(state["selection"]),
                                       "digest": preview["content_digest"], "preview_id": preview["id"],
                                       "revision": state["revision"], "scope": "review-workspace-only"})
        return self.mutate(body.get("revision"), "Approved this resume version", operation)

    def preview(self):
        from resume_pdf_audit import audit_resume_pdf, resume_layout_metrics
        from styled_resume_pdf import StyledResumePDF
        import fitz

        snapshot = self.read()
        payload, variant = compose(snapshot["catalog"], snapshot["selection"])
        content_digest = digest({"payload": payload, "selection": snapshot["selection"], "variant": variant})
        identifier = content_digest[:32]
        output = self.directory / "previews" / identifier
        with self.render_lock:
            metadata = output / "metadata.json"
            if metadata.exists():
                result = json.loads(metadata.read_text())
                return {**result, "revision": snapshot["revision"]}
            output.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = output / "resume.pdf"
            warnings = []
            for field in ("headline", "summary"):
                if not payload.get(field, "").strip():
                    warnings.append(f"This flavor has no {field} yet.")

            def render(value):
                pdf = StyledResumePDF(value)
                pdf.render()
                pdf.output(str(path))
                return resume_layout_metrics(path)

            try:
                fitted, layout = fit(payload, render, variant["max_pages"])
            except ValueError as error:
                # Keep the complete overflow visible for editing; never cut text.
                warnings.append(str(error))
                fitted = payload
                render(fitted)
                layout = {**resume_layout_metrics(path), "target_pages": variant["max_pages"]}
            if layout.get("page_two_sparse"):
                warnings.append("The second page is less than half full.")
            audit_ok = True
            try:
                audit_resume_pdf(path, fitted, max_pages=variant["max_pages"])
            except ValueError as error:
                audit_ok = False
                warnings.append(str(error))
            with fitz.open(path) as document:
                for index, page in enumerate(document):
                    page.get_pixmap(matrix=fitz.Matrix(1.7, 1.7)).save(output / f"page-{index + 1}.png")
                text = "\n".join(page.get_text() for page in document)
            result = {"id": identifier, "content_digest": content_digest, "layout": layout,
                      "pages": layout["pages"], "warnings": warnings, "audit_ok": audit_ok,
                      "text": text, "revision": snapshot["revision"]}
            atomic_json(metadata, result)
            return result


def handler_for(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log candidate content, request bodies or document URLs.

        def reply(self, status, value, content_type="application/json"):
            body = json.dumps(value, ensure_ascii=False).encode() if content_type == "application/json" else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob:; font-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def trusted(self, mutation=False):
            port = self.server.server_port
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            if self.headers.get("Host") not in hosts:
                raise ReviewError("This review service only accepts local requests.")
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://{host}" for host in hosts}:
                raise ReviewError("Cross-origin access is disabled.")
            if mutation and (self.headers.get("X-Review-Client") != "studio"
                             or self.headers.get_content_type() != "application/json"):
                raise ReviewError("Use the local review client with JSON requests.")

        def do_GET(self):
            try:
                self.trusted()
                route = urlsplit(self.path).path
                if route == "/api/state":
                    return self.reply(200, store.read())
                if route == "/api/preview":
                    return self.reply(200, store.preview())
                if route == "/api/export":
                    return self.reply(200, store.read())
                match = re.fullmatch(r"/previews/([a-f0-9]{32})/(resume\.pdf|page-[1-9][0-9]*\.png)", route)
                if match:
                    path = store.directory / "previews" / match[1] / match[2]
                    return self.reply(200, path.read_bytes(), "application/pdf" if path.suffix == ".pdf" else "image/png")
                assets = {"/": (STATIC / "index.html", "text/html; charset=utf-8"),
                          "/app.js": (STATIC / "app.js", "text/javascript; charset=utf-8"),
                          "/style.css": (STATIC / "style.css", "text/css; charset=utf-8"),
                          "/font.ttf": (ROOT / "assets/resume_template/Inter-Regular-full.ttf", "font/ttf")}
                if route in assets:
                    path, kind = assets[route]
                    return self.reply(200, path.read_bytes(), kind)
                self.reply(404, {"error": "Not found"})
            except FileNotFoundError:
                self.reply(404, {"error": "Not found"})
            except (ValueError, KeyError, TypeError, IndexError) as error:
                self.reply(getattr(error, "status", 400), {"error": str(error)})
            except Exception:
                self.reply(500, {"error": "Preview could not be rendered. Your saved edits are intact."})

        def do_POST(self):
            try:
                self.trusted(mutation=True)
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ReviewError("The request is empty or too large.")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ReviewError("A JSON object is required.")
                routes = {"/api/save": store.save, "/api/requests": store.request,
                          "/api/proposals": store.propose, "/api/proposals/decide": store.decide,
                          "/api/approve": store.approval, "/api/undo": store.undo}
                action = routes.get(urlsplit(self.path).path)
                if not action:
                    return self.reply(404, {"error": "Not found"})
                self.reply(200, action(body))
            except (ValueError, KeyError, TypeError, IndexError) as error:
                self.reply(getattr(error, "status", 400), {"error": str(error)})
            except Exception:
                self.reply(500, {"error": "The operation failed. Reload to check the latest saved state."})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, default=Path("data/resume-review"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    store = ReviewStore(args.profile, args.state_dir)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler_for(store))
    print(f"Resume review: http://127.0.0.1:{server.server_port}", flush=True)
    print(f"Saved review: {store.path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
