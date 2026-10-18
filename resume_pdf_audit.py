"""Mandatory checks against the actual PDF, not renderer success assertions."""

from pathlib import Path
import re

import fitz
from pypdf import PdfReader

from resume_inline import fragments, normalized_text
from resume_quality import ResumeQualityError
from styled_resume_pdf import StyledResumePDF


def _norm(value):
    return normalized_text(value).replace("\ufb01", "fi").replace("\ufb02", "fl").replace("\u00b7", "-")


def _bullet_texts(resume):
    for role in resume.get("experience") or []:
        for item in role.get("engagements") or [role]:
            yield from item.get("bullets") or []


def _links(resume):
    expected = set()
    for field in ("website", "linkedin"):
        target = StyledResumePDF._web_link(resume.get(field), linkedin=field == "linkedin")
        if target:
            expected.add(target)
    for item in resume.get("volunteering") or []:
        target = StyledResumePDF._web_link(item.get("url"), require_scheme=True)
        if target:
            expected.add(target)
    if resume.get("email"):
        expected.add("mailto:" + resume["email"])
    for cert in resume.get("certifications") or []:
        target = StyledResumePDF._web_link((resume.get("certification_links") or {}).get(cert), require_scheme=True)
        if target:
            expected.add(target)
    for role in resume.get("experience") or []:
        target = StyledResumePDF._web_link(role.get("company_url"), require_scheme=True)
        if target:
            expected.add(target)
    return expected


def resume_layout_metrics(path):
    """Page count, the main column's fill on every page, and both columns' fill for a one-page PDF."""
    with fitz.open(path) as document:
        result = {"pages": len(document), "main_fills": []}
        for index, page in enumerate(document, 1):
            top_limit = 85 if index == 1 else StyledResumePDF.PAGE_TOP - 12
            body = [word for word in page.get_text("words")
                    if StyledResumePDF.MAIN_X <= word[0] < StyledResumePDF.MAIN_X + StyledResumePDF.MAIN_W
                    and word[1] >= top_limit]
            fill = 0.0
            if body:
                top, bottom = min(w[1] for w in body), max(w[3] for w in body)
                fill = round(min(1, (bottom - top) / (StyledResumePDF.BOTTOM - top)), 4)
            result["main_fills"].append(fill)
        if len(document) != 1:
            return result
        words = document[0].get_text("words")
        for column, left, right in (("main", StyledResumePDF.MAIN_X, StyledResumePDF.MAIN_X + StyledResumePDF.MAIN_W),
                                    ("sidebar", StyledResumePDF.SIDE_X, StyledResumePDF.SIDE_X + StyledResumePDF.SIDE_W)):
            body = [word for word in words if left <= word[0] < right and word[1] >= 85]
            if body:
                top, bottom = min(w[1] for w in body), max(w[3] for w in body)
                result[column + "_fill"] = round(min(1, (bottom - top) / (StyledResumePDF.BOTTOM - top)), 4)
        return result


def audit_resume_pdf(path, resume, *, max_pages=2):
    failures = []
    document = fitz.open(path)
    reader = PdfReader(str(path))
    if not 1 <= len(document) <= max_pages or len(reader.pages) != len(document):
        failures.append("page_limit")
    text = _norm(" ".join(page.get_text() for page in document))
    expected = [resume.get("headline", ""), resume.get("summary", ""), resume.get("location", "")]
    for role in resume.get("experience") or []:
        dates = StyledResumePDF._compact_dates(role.get("dates", ""))
        expected.extend([role.get("title", ""), dates])
    expected.extend(_bullet_texts(resume))
    expected.extend(StyledResumePDF._certification_label(c) for c in resume.get("certifications") or [])
    if any(value and _norm(value) not in text for value in expected):
        failures.append("selected_text")
    if "**" in text or "\ufffd" in text:
        failures.append("literal_markup_or_invalid_glyph")
    if (resume.get("additional") or {}).get("languages") and "LANGUAGES" not in text:
        failures.append("languages")
    for name, level in StyledResumePDF(resume)._parse_languages():
        if _norm(name) not in text or level and _norm(level) not in text:
            failures.append("language_text")
    if resume.get("education") and "EDUCATION" not in text:
        failures.append("education")
    if any(value and _norm(value) not in text for item in resume.get("education") or []
           for value in (item.get("display_degree") or item.get("degree"),
                         item.get("display_school") or item.get("school"),
                         StyledResumePDF._compact_dates(item.get("dates")))):
        failures.append("education_text")
    if resume.get("volunteering"):
        if str((resume.get("layout") or {}).get("volunteering_section_title") or "Volunteering").upper() not in text:
            failures.append("volunteering")
        if any(_norm(item[k]) not in text for item in resume["volunteering"]
               for k in ("title", "organization", "dates", "description")):
            failures.append("volunteering_text")
    for role in resume.get("experience") or []:
        for item in role.get("engagements") or []:
            dates = item.get("dates")
            if dates and dates != role.get("dates") and _norm(dates) in text:
                failures.append("client_dates_visible")
    actual_links = {str(a.get_object()["/A"]["/URI"])
        for page in reader.pages for a in page.get("/Annots", [])
        if a.get_object().get("/Subtype") == "/Link" and "/URI" in a.get_object().get("/A", {})}
    if actual_links != _links(resume):
        failures.append("link_targets")
    bold_stream, spans_count = [], 0
    for page in document:
        spans, ink = [], []
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    spans.append(span)
                    spans_count += 1
                    box = fitz.Rect(span["bbox"])
                    if box.x0 < 23.5 or box.x1 > page.rect.width - 23.5 or box.y0 < 18 or box.y1 > StyledResumePDF.BOTTOM + 4:
                        failures.append("margins")
                    baseline = span["origin"][1]
                    ink.append(fitz.Rect(box.x0, baseline - span["size"] * .78,
                                         box.x1, baseline + span["size"] * .18))
        for index, box in enumerate(ink):
            for other in ink[index + 1:]:
                intersection = box & other
                if intersection.width > .8 and intersection.height > .8:
                    failures.append("text_overlap")
                    break
        for heading in spans:
            if heading["text"] in {"VOLUNTEERING", "TEACHING & TRAINING", "LANGUAGES", "EDUCATION"} and not any(
                other["bbox"][1] > heading["bbox"][3]
                and abs(other["bbox"][0] - heading["bbox"][0]) < 30
                for other in spans
            ):
                failures.append("orphan_section_heading")
        for section in ("VOLUNTEERING", "TEACHING & TRAINING", "LANGUAGES", "EDUCATION"):
            if sum(span["text"] == section for span in spans) > 1:
                failures.append("duplicate_sections")
        raw, flags = "", []
        for span in spans:
            piece = span["text"] + " "
            is_bold = "bold" in span["font"].lower() or bool(span["flags"] & 16)
            raw += piece
            flags.extend([is_bold] * len(piece))
        # Normalize whitespace while retaining a mask for the actual font spans.
        normalized, mask = "", []
        for token in re.finditer(r"\S+", raw):
            if normalized:
                normalized += " "
                mask.append(True)
            part = raw[token.start():token.end()]
            for char, bold in zip(part, flags[token.start():token.end()]):
                converted = _norm(char)
                normalized += converted
                mask.extend([bold] * len(converted))
        bold_stream.append((normalized, mask))
    for bullet in _bullet_texts(resume):
        for phrase, bold in fragments(bullet):
            if not bold:
                continue
            phrase = _norm(phrase)
            valid = False
            for stream, mask in bold_stream:
                start = stream.find(phrase)
                while start >= 0:
                    if all(mask[start:start + len(phrase)]):
                        valid = True
                        break
                    start = stream.find(phrase, start + 1)
            if not valid:
                failures.append("bold_spans")
    document.close()
    if failures:
        raise ResumeQualityError("PDF verification failed: " + ", ".join(sorted(set(failures))))
    return {"pdf_text_verified": True, "pdf_margins_verified": True, "pdf_no_text_overlap": True,
            "pdf_emphasis_verified": True, "pdf_links_verified": True, "pdf_sections_verified": True,
            "pdf_page_limit_verified": True, "pdf_text_spans": spans_count, "pdf_link_count": len(actual_links)}


def render_review_pages(path, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with fitz.open(path) as document:
        for index, page in enumerate(document, 1):
            target = directory / f"page-{index}.png"
            page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).save(target)
            target.chmod(0o600)
