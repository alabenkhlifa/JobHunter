#!/usr/bin/env python3
"""Import a candidate-reviewed rendered resume into the fixed block catalog.

The command is intentionally dry-run by default. It copies only public renderer
fields and public experience wording from the reviewed payload.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import unicodedata
from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resume_blocks import RENDERER_BASE_FIELDS, catalog as validate_catalog


class ImportErrorSafe(ValueError):
    """A structural error safe to show without exposing candidate content."""


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ImportErrorSafe("Could not read a valid JSON input") from None
    if not isinstance(value, dict):
        raise ImportErrorSafe("JSON input must be an object")
    return value


def _slug(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or "project"


def _normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).replace("\ufb01", "fi").replace("\ufb02", "fl")
    return re.sub(r"\s+", " ", value).strip()


def _pdf_fingerprint(path: Path) -> dict:
    """Content/render fingerprint used to compare the supplied PDF to a fresh render."""
    try:
        with fitz.open(path) as doc:
            pages = []
            for page in doc:
                spans = []
                raw = page.get_text("dict")
                for block in raw.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if not span.get("text", "").strip():
                                continue
                            spans.append({
                                "text": _normalized_text(span["text"]),
                                "font": span.get("font", ""),
                                "size": round(float(span.get("size", 0)), 2),
                                "flags": int(span.get("flags", 0)),
                                "bbox": [round(float(x), 1) for x in span.get("bbox", [])],
                            })
                links = []
                for link in page.get_links():
                    if link.get("uri"):
                        links.append({"uri": link["uri"], "from": [round(float(x), 1) for x in link["from"]]})
                pages.append({"size": [round(page.rect.width, 2), round(page.rect.height, 2)],
                              "spans": spans, "links": links})
    except Exception:
        raise ImportErrorSafe("Could not inspect the reference PDF") from None
    return {"pages": pages}


def _render(payload: dict, output: Path) -> None:
    try:
        from styled_resume_pdf import StyledResumePDF

        pdf = StyledResumePDF(payload)
        pdf.render()
        pdf.output(str(output))
    except Exception:
        raise ImportErrorSafe("Reviewed payload could not be rendered") from None


def _same_render(expected: Path, actual: Path) -> bool:
    left, right = _pdf_fingerprint(expected), _pdf_fingerprint(actual)
    if len(left["pages"]) != len(right["pages"]):
        return False
    for old, new in zip(left["pages"], right["pages"]):
        if old["size"] != new["size"] or old["spans"] != new["spans"]:
            return False
        if old["links"] != new["links"]:
            return False
    return True


def _public_entry(item: dict) -> dict:
    fields = ("name", "dates", "role", "tech", "bullets")
    return {key: copy.deepcopy(item[key]) for key in fields if key in item}


def _role_fields(role: dict, *, with_bullets: bool) -> dict:
    fields = ("title", "dates", "location", "company_url", "subtitle", "tech")
    result = {key: copy.deepcopy(role[key]) for key in fields if key in role}
    if with_bullets:
        result["bullets"] = copy.deepcopy(role.get("bullets") or [])
    return result


def _make_catalog(source: dict, reference_pdf: Path) -> dict:
    if set(source) - (RENDERER_BASE_FIELDS | {"experience", "headline", "summary"}):
        raise ImportErrorSafe("Reviewed payload contains unsupported fields")
    base = {key: copy.deepcopy(source[key]) for key in RENDERER_BASE_FIELDS if key in source}
    if not isinstance(source.get("experience"), list):
        raise ImportErrorSafe("Reviewed payload requires an experience list")
    headline, summary = source.get("headline"), source.get("summary")
    if not isinstance(headline, str) or not headline.strip() or not isinstance(summary, str) or not summary.strip():
        raise ImportErrorSafe("Reviewed payload requires headline and summary text")
    projects = []
    seen = set()
    for role in source.get("experience") or []:
        if not isinstance(role, dict):
            raise ImportErrorSafe("Experience entries must be objects")
        company = role.get("company")
        if not isinstance(company, str) or not company.strip():
            raise ImportErrorSafe("Experience entries require a company")
        grouped = role.get("engagements") or []
        units = [(entry, False) for entry in grouped] if grouped else [(role, True)]
        for entry, standalone in units:
            if not isinstance(entry, dict):
                raise ImportErrorSafe("Project entries must be objects")
            label = entry.get("name") if not standalone else (entry.get("name") or entry.get("title") or company)
            if not isinstance(label, str) or not label.strip():
                raise ImportErrorSafe("Project entries require a label")
            base_id = _slug(f"{company}-{label}")
            identifier, suffix = base_id, 2
            while identifier in seen:
                identifier, suffix = f"{base_id}-{suffix}", suffix + 1
            seen.add(identifier)
            project = {
                "id": identifier,
                "label": label,
                "confirmation": "candidate-confirmed",
                "company": company,
                "role": _role_fields(role, with_bullets=standalone),
                "entry": None if standalone else _public_entry(entry),
            }
            projects.append(project)
    if not projects:
        raise ImportErrorSafe("Reviewed payload must contain at least one project")

    project_ids = [item["id"] for item in projects]
    catalog = {
        "version": 1,
        "enabled": True,
        "metadata": {"source_pdf_sha256": hashlib.sha256(reference_pdf.read_bytes()).hexdigest()},
        "base": base,
        "flavors": [
            {"id": "software-engineer", "label": "Software Engineer (Senior Backend / Senior Software)",
             "confirmation": "candidate-confirmed", "headline": headline, "summary": summary},
            {"id": "architect", "label": "Architect", "confirmation": "draft", "headline": "", "summary": ""},
        ],
        "projects": projects,
        "variants": [
            {"id": "software-engineer-one-page", "label": "1 page", "flavor_id": "software-engineer",
             "confirmation": "candidate-confirmed", "max_pages": 1,
             "project_ids": project_ids, "default_project_ids": project_ids},
            {"id": "software-engineer-two-pages", "label": "Up to 2 pages", "flavor_id": "software-engineer",
             "confirmation": "candidate-confirmed", "max_pages": 2,
             "project_ids": project_ids, "default_project_ids": project_ids},
        ],
    }
    test_profile = {"resume_blocks": catalog}
    try:
        validate_catalog(test_profile)
    except (TypeError, ValueError):
        raise ImportErrorSafe("Generated catalog failed structural validation") from None
    return catalog


def import_catalog(profile_path: Path, payload_path: Path, reference_pdf: Path, *, apply: bool = False) -> bool:
    profile_path, payload_path, reference_pdf = map(Path, (profile_path, payload_path, reference_pdf))
    try:
        initial_bytes = profile_path.read_bytes()
        original = json.loads(initial_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ImportErrorSafe("Could not read a valid profile") from None
    if not isinstance(original, dict):
        raise ImportErrorSafe("Profile must be an object")
    source = _read_json(payload_path)
    if not reference_pdf.is_file():
        raise ImportErrorSafe("Reference PDF does not exist")
    catalog = _make_catalog(source, reference_pdf)

    with tempfile.TemporaryDirectory(prefix="resume-block-verify-") as temp_dir:
        rendered = Path(temp_dir) / "reviewed.pdf"
        _render(source, rendered)
        if not _same_render(reference_pdf, rendered):
            raise ImportErrorSafe("Reference PDF does not match the reviewed payload render")

    existing = original.get("resume_blocks")
    if existing is not None:
        if existing == catalog:
            return False
        raise ImportErrorSafe("Profile already contains a different fixed catalog")
    if not apply:
        return True

    try:
        if hashlib.sha256(profile_path.read_bytes()).digest() != hashlib.sha256(initial_bytes).digest():
            raise ImportErrorSafe("Profile changed during import; no changes were written")
    except OSError:
        raise ImportErrorSafe("Could not recheck profile before writing") from None
    updated = copy.deepcopy(original)
    updated["resume_blocks"] = catalog
    backup = profile_path.with_name(profile_path.name + ".bak")
    if backup.exists():
        raise ImportErrorSafe("Profile backup already exists; refusing to overwrite it")
    try:
        backup.write_bytes(initial_bytes)
        backup.chmod(0o600)
        fd, temp_name = tempfile.mkstemp(prefix=profile_path.name + ".", suffix=".tmp", dir=profile_path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o600)
        if hashlib.sha256(profile_path.read_bytes()).digest() != hashlib.sha256(initial_bytes).digest():
            os.unlink(temp_name)
            raise ImportErrorSafe("Profile changed during import; no changes were written")
        os.replace(temp_name, profile_path)
        profile_path.chmod(0o600)
    except OSError:
        try:
            if backup.exists() and profile_path.read_bytes() == initial_bytes:
                backup.unlink()
        except OSError:
            pass
        raise ImportErrorSafe("Could not safely write the profile") from None
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--reviewed-payload", required=True, type=Path)
    parser.add_argument("--reference-pdf", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="write the catalog; default is dry-run")
    args = parser.parse_args(argv)
    try:
        changed = import_catalog(args.profile, args.reviewed_payload, args.reference_pdf, apply=args.apply)
    except ImportErrorSafe as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print("catalog would be added" if changed and not args.apply else
          "catalog added" if changed else "identical catalog already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
