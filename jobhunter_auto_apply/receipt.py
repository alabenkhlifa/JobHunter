"""Verify the post-submit confirmation page and write ``receipt.json``.

A receipt is ``verified`` only when the page shows a confirmation marker (EN or
ES) **and** it names the job title or the employer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import unicodedata
import urllib.parse
from pathlib import Path
from typing import Any

from .cdp import CDPClient, CDPError, list_targets


SCHEMA = "jobhunter.receipt/1"

RECEIPT_MARKERS = (
    # English
    "thank you for applying", "thanks for applying", "thank you for your application",
    "thanks for your application", "application submitted",
    "application has been submitted", "application was submitted", "application received",
    "application has been received", "we have received your application", "we've received your application",
    "we received your application", "your application has been received", "successfully submitted",
    "successfully applied", "you have applied", "you've applied", "your application is complete",
    # Spanish
    "gracias por tu candidatura", "gracias por su candidatura", "gracias por tu solicitud",
    "gracias por su solicitud", "gracias por aplicar", "gracias por postularte", "gracias por postular",
    "gracias por inscribirte", "solicitud enviada", "candidatura enviada",
    "candidatura recibida", "solicitud recibida", "hemos recibido tu candidatura",
    "hemos recibido su candidatura", "hemos recibido tu solicitud", "hemos recibido su solicitud",
    "tu candidatura se ha enviado", "tu candidatura ha sido enviada", "tu solicitud ha sido enviada",
    "tu solicitud se ha enviado", "inscripcion realizada", "te has inscrito", "ya te has inscrito",
    "postulacion enviada", "postulacion recibida",
)

# Text that means the form is still open with problems; never verify then.
FAILURE_MARKERS = (
    "please fix the errors", "there was an error", "this field is required", "required field",
    "something went wrong", "este campo es obligatorio", "campo obligatorio", "se ha producido un error",
    "ha ocurrido un error", "corrige los errores",
)


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("’", "'").replace("¡", " ").replace("¿", " ")
    return " ".join(text.casefold().split())


def find_markers(text: str) -> list[str]:
    normalized = _norm(text)
    return [marker for marker in RECEIPT_MARKERS if marker in normalized]


def _contains_tokens(haystack: str, needle: str, *, drop_legal: bool = False) -> bool:
    from .engine import _strip_legal_suffix, _title_tokens

    words = list(_title_tokens(_norm(needle)))
    if drop_legal:
        words = _strip_legal_suffix(words)
    if not words:
        return False
    tokens = _title_tokens(_norm(haystack))
    size = len(words)
    return any(list(tokens[index:index + size]) == words for index in range(len(tokens) - size + 1))


def _job_fields(job: Any) -> tuple[str, str, str | None, str | None]:
    if isinstance(job, dict):
        return (str(job.get("id") or job.get("job_id") or ""), str(job.get("title") or ""),
                job.get("company"), job.get("location"))
    if isinstance(job, (tuple, list)):
        values = list(job) + [None] * 4
        return str(values[0] or ""), str(values[1] or ""), values[2], values[3]
    raise TypeError("job must be a dict with id/title/company")


def _sha256(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def verify_receipt(client_or_inspection: Any, job: Any, *, screenshot_path: str | Path | None,
                   page_url: str | None = None, now: dt.datetime | None = None) -> dict[str, Any]:
    """Return a ``jobhunter.receipt/1`` dict for the current page.

    ``client_or_inspection`` is a CDP client (the page is inspected and a
    screenshot written to ``screenshot_path``) or an existing ``PageInspection``
    / dict (``screenshot_path`` must then already exist to be hashed).
    """

    from .engine import _heading_matches_job, _page_identifies_company, inspect_page

    job_id, title, company, location = _job_fields(job)
    if hasattr(client_or_inspection, "evaluate"):
        inspection = inspect_page(client_or_inspection, blocklist_terms=())
        if screenshot_path:
            try:
                Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
                client_or_inspection.screenshot(str(screenshot_path))
            except Exception:
                screenshot_path = None
    else:
        inspection = client_or_inspection
    if isinstance(inspection, dict):
        url = inspection.get("url") or ""
        page_title = inspection.get("title") or ""
        headings = list(inspection.get("headings") or [])
        text = inspection.get("page_text") or inspection.get("text") or inspection.get("text_excerpt") or ""
        invalid = inspection.get("invalid_fields") or []
    else:
        url = getattr(inspection, "url", "") or ""
        page_title = getattr(inspection, "title", "") or ""
        headings = list(getattr(inspection, "headings", []) or [])
        text = getattr(inspection, "page_text", None) or getattr(inspection, "text_excerpt", "") or ""
        invalid = getattr(inspection, "invalid_fields", []) or []

    page_text = "\n".join([page_title, *headings, text])
    markers = find_markers(page_text)
    failed = bool(invalid) or any(marker in _norm(text) for marker in FAILURE_MARKERS)
    title_match = bool(title) and (
        any(_heading_matches_job(item, title, company, location) for item in [page_title, *headings])
        or _contains_tokens(page_text, title)
    )
    company_match = bool(company) and (
        _page_identifies_company(url, page_title, headings, company)
        or _contains_tokens(page_text, company, drop_legal=True)
    )
    evidence = str(screenshot_path) if screenshot_path and Path(screenshot_path).is_file() else None
    return {
        "schema": SCHEMA,
        "job_id": job_id,
        "page_url": page_url or url,
        "final_url": url,
        "checked_at": (now or dt.datetime.now(dt.timezone.utc)).isoformat(),
        "verified": bool(markers) and (title_match or company_match) and not failed,
        "markers": markers,
        "title_match": title_match,
        "company_match": company_match,
        "evidence_path": evidence,
        "sha256": _sha256(evidence),
    }


def write_receipt(receipt: dict[str, Any], path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def connect_receipt_page(host: str, port: int, page_url: str, *, timeout: float = 10.0) -> CDPClient:
    """Connect to the tab that was submitted: the exact URL, else the only tab on the same host.

    After submit most ATS pages navigate (``/confirmation``), so the exact URL
    can be gone.
    """

    pages = [target for target in list_targets(host, port, timeout)
             if target.type == "page" and urllib.parse.urlsplit(target.url).scheme in {"http", "https"}]
    exact = [target for target in pages if target.url == page_url]
    if len(exact) > 1:
        raise CDPError("multiple tabs share the application URL; close the duplicates")
    if not exact:
        wanted = urllib.parse.urlsplit(page_url).hostname
        exact = [target for target in pages if urllib.parse.urlsplit(target.url).hostname == wanted]
        if len(exact) != 1:
            raise CDPError("confirmation page not found (no unique tab on the application host)")
    return CDPClient(exact[0].websocket_url, timeout=timeout)


def receipt_path(output_dir: str | Path, job_id: str) -> Path:
    from .engine import _job_output_dir

    return _job_output_dir(str(output_dir), job_id) / "receipt.json"


def read_receipt(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ValueError("not a jobhunter.receipt/1 file")
    return payload


__all__ = ["SCHEMA", "RECEIPT_MARKERS", "find_markers", "verify_receipt", "write_receipt",
           "connect_receipt_page", "receipt_path", "read_receipt"]
