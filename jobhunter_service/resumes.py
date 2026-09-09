"""Bounded extraction of untrusted resumes; imports never confirm candidate facts."""

from __future__ import annotations

import hashlib
import io
import multiprocessing
import os
from pathlib import PurePath
import re
import time
import zipfile
from xml.etree import ElementTree

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 100_000
MAX_PAGES = 60
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
EXTRACTION_TIMEOUT = 12


class ResumeImportError(ValueError):
    """A resume could not be imported within the permitted limits."""


def _extract(data: bytes, extension: str) -> str:
    if extension == ".txt":
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ResumeImportError("Text resumes must use UTF-8 encoding.") from None
    if extension == ".docx":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                entries = archive.infolist()
                if len(entries) > 512 or sum(item.file_size for item in entries) > MAX_ARCHIVE_BYTES:
                    raise ResumeImportError("The document expands beyond the import limit.")
                if len({item.filename for item in entries}) != len(entries):
                    raise ResumeImportError("The document contains duplicate archive entries.")
                info = archive.getinfo("word/document.xml")
                if info.file_size > MAX_XML_BYTES or info.flag_bits & 1:
                    raise ResumeImportError("The document is too large or encrypted.")
                xml = archive.read(info)
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise ResumeImportError("Documents containing XML entity declarations are unsupported.")
            root = ElementTree.fromstring(xml)
            paragraphs = []
            for paragraph in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"):
                paragraphs.append("".join(element.text or "" for element in paragraph.iter()
                                          if element.tag.endswith("}t")))
            return "\n".join(paragraphs)
        except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, RuntimeError):
            raise ResumeImportError("This DOCX document could not be read.") from None
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ResumeImportError("PDF import is unavailable until the server installs pypdf. Upload DOCX or UTF-8 text.") from None
    try:
        reader = PdfReader(io.BytesIO(data), strict=True)
        if reader.is_encrypted:
            raise ResumeImportError("Upload an unencrypted PDF.")
        if len(reader.pages) > MAX_PAGES:
            raise ResumeImportError("The PDF exceeds the page limit.")
        parts = []
        length = 0
        for page in reader.pages:
            part = page.extract_text() or ""
            length += len(part)
            if length > MAX_TEXT_CHARS:
                raise ResumeImportError("The resume exceeds the text limit.")
            parts.append(part)
        return "\n".join(parts)
    except ResumeImportError:
        raise
    except Exception:
        raise ResumeImportError("This PDF could not be read. Upload a text-based PDF, DOCX, or UTF-8 text.") from None


def _extraction_worker(connection, data: bytes, extension: str) -> None:
    try:
        import logging
        logging.disable(logging.CRITICAL)
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
            if os.uname().sysname == "Linux":
                limit = 512 * 1024 * 1024
                resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, OSError, ValueError):
            pass
        text = _extract(data, extension)
        if len(text) > MAX_TEXT_CHARS:
            raise ResumeImportError("The resume exceeds the text limit.")
        connection.send((True, text))
    except ResumeImportError as error:
        connection.send((False, str(error)))
    except BaseException:
        connection.send((False, "Resume extraction failed within the permitted resource limits."))
    finally:
        connection.close()


def import_resume(data: bytes, filename: str, mime_type: str | None = None) -> dict:
    """Return only unconfirmed source text, never inferred or verified facts.

    A separate process bounds parser CPU/memory and wall time. No attachment is
    executed or unpacked onto the filesystem, and no source filename is a path.
    """
    if not isinstance(data, bytes) or not data or len(data) > MAX_UPLOAD_BYTES:
        raise ResumeImportError("Upload a nonempty resume no larger than 8 MiB.")
    if not isinstance(filename, str) or len(filename) > 240:
        raise ResumeImportError("The resume filename is invalid.")
    safe_name = PurePath(filename.replace("\\", "/")).name
    if any(ord(char) < 32 for char in safe_name):
        raise ResumeImportError("The resume filename is invalid.")
    extension = PurePath(safe_name).suffix.lower()
    allowed = {
        ".txt": {"text/plain", "application/octet-stream"},
        ".pdf": {"application/pdf", "application/octet-stream"},
        ".docx": {"application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/octet-stream"},
    }
    if extension not in allowed:
        raise ResumeImportError("Upload a PDF, DOCX, or UTF-8 text resume.")
    if mime_type and mime_type.split(";", 1)[0].lower() not in allowed[extension]:
        raise ResumeImportError("The attachment type does not match its filename.")
    if extension == ".pdf" and not data.startswith(b"%PDF-"):
        raise ResumeImportError("The attachment is not a PDF.")
    if extension == ".docx" and not data.startswith(b"PK\x03\x04"):
        raise ResumeImportError("The attachment is not a DOCX document.")
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    process = context.Process(target=_extraction_worker, args=(child, data, extension), daemon=True)
    process.start()
    child.close()
    try:
        deadline = time.monotonic() + EXTRACTION_TIMEOUT
        while not parent.poll(0.1):
            if not process.is_alive() or time.monotonic() >= deadline:
                raise ResumeImportError("Resume extraction exceeded the permitted resource limits.")
        try:
            success, value = parent.recv()
        except EOFError:
            raise ResumeImportError("Resume extraction failed within the permitted resource limits.") from None
        if not success:
            raise ResumeImportError(value)
    finally:
        parent.close()
        process.join(timeout=0.2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=1)
        if process.is_alive():
            process.kill()
            process.join(timeout=1)
        process.close()
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value).strip()
    if not text:
        raise ResumeImportError("No readable text was found. Scanned resumes need a text-based PDF or DOCX.")
    return {
        "filename": safe_name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "text": text,
        "confirmation": "unconfirmed",
        "source": "candidate-upload",
        "trusted": False,
    }
