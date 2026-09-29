#!/usr/bin/env python3
"""PDF renderer for tailored resumes and cover letters using fpdf2."""

import json
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

from fpdf import FPDF

# ── Colors ────────────────────────────────────────────────────────────────────

DARK = (44, 62, 80)       # Dark blue-gray for headers
MEDIUM = (52, 73, 94)     # Slightly lighter for subheaders
ACCENT = (41, 128, 185)   # Blue accent for links/highlights
TEXT = (33, 33, 33)        # Near-black body text
LIGHT_GRAY = (189, 195, 199)  # Divider lines
WHITE = (255, 255, 255)

# Unicode → latin-1 safe replacements
_UNICODE_MAP = {
    "\u2013": "-",   # en-dash
    "\u2014": "-",   # em-dash
    "\u2018": "'",   # left single quote
    "\u2019": "'",   # right single quote
    "\u201c": '"',   # left double quote
    "\u201d": '"',   # right double quote
    "\u2026": "...", # ellipsis
    "\u00b7": "-",   # middle dot
    "\u2022": "-",   # bullet
    "\u00a0": " ",   # non-breaking space
}


def _sanitize(text):
    """Replace common Unicode characters with latin-1 safe equivalents."""
    for uni, repl in _UNICODE_MAP.items():
        text = text.replace(uni, repl)
    return text


# ── Resume PDF ────────────────────────────────────────────────────────────────

class SanitizedPDF(FPDF):
    """Keep legacy cover-letter core fonts safe for common punctuation."""

    def normalize_text(self, text):
        return super().normalize_text(_sanitize(text))


from styled_resume_pdf import StyledResumePDF as ResumePDF


# ── Cover Letter PDF ──────────────────────────────────────────────────────────

class CoverLetterPDF(SanitizedPDF):
    def __init__(self, data):
        super().__init__()
        self.data = data
        self.set_auto_page_break(auto=True, margin=20)

    def render(self):
        d = self.data
        self.add_page()
        self.set_margins(25, 20, 25)

        # ── Sender name ──
        self.set_font("Helvetica", "B", 16)
        self.set_text_color(*DARK)
        self.cell(0, 10, d["name"], new_x="LMARGIN", new_y="NEXT")

        # ── Contact ──
        self.set_font("Helvetica", "", 9)
        contact_parts = [part.strip() for part in d.get("contact", "").split("|") if part.strip()]
        x, y = self.l_margin, self.get_y()
        right = self.w - self.r_margin
        for index, part in enumerate(contact_parts):
            email = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", part)
            link = (f"mailto:{part}" if email else
                    ResumePDF._web_link(part, linkedin="linkedin.com" in part.lower()))
            label = (" | " if index and x > self.l_margin else "") + part
            width = self.get_string_width(label) + 0.4
            if x > self.l_margin and x + width > right:
                x, y = self.l_margin, y + 5
                label = part
                width = self.get_string_width(label) + 0.4
            self.set_xy(x, y)
            self.set_text_color(*(ACCENT if link else MEDIUM))
            self.cell(width, 5, label, link=link or "")
            x += width
        self.set_xy(self.l_margin, y + 5)
        self.ln(8)

        # ── Date ──
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*TEXT)
        self.cell(0, 5, d.get("date", ""), new_x="LMARGIN", new_y="NEXT")
        self.ln(3)

        # ── Recipient ──
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*TEXT)
        self.cell(0, 5, d.get("recipient", ""), new_x="LMARGIN", new_y="NEXT")
        self.ln(5)

        # ── Subject line ──
        if d.get("subject"):
            self.set_font("Helvetica", "B", 10)
            self.set_text_color(*DARK)
            self.multi_cell(0, 6, f"Re: {d['subject']}", new_x="LMARGIN", new_y="NEXT", align="L")
            self.ln(5)

        # ── Divider ──
        self.set_draw_color(*LIGHT_GRAY)
        self.set_line_width(0.3)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(5)

        # ── Body ──
        self.set_font("Helvetica", "", 10)
        self.set_text_color(*TEXT)
        if d.get("salutation"):
            self.multi_cell(0, 5.5, d["salutation"], new_x="LMARGIN", new_y="NEXT", align="L")
            self.ln(3)

        if d.get("opening"):
            self.multi_cell(0, 5.5, d["opening"], new_x="LMARGIN", new_y="NEXT", align="L")
            self.ln(4)

        if d.get("highlights"):
            self.set_font("Helvetica", "B", 10)
            self.multi_cell(0, 5.5, d.get("highlights_heading", "Relevant experience:"), new_x="LMARGIN", new_y="NEXT", align="L")
            self.ln(1)
            for highlight in d["highlights"]:
                self.set_font("Helvetica", "", 9.5)
                x = self.get_x()
                self.cell(5, 5, "-", new_x="END")
                self.multi_cell(
                    self.w - self.r_margin - x - 6,
                    5,
                    f" {highlight.get('text', '')}",
                    new_x="LMARGIN",
                    new_y="NEXT",
                    align="L",
                )
                if highlight.get("context"):
                    self.set_x(self.l_margin + 5)
                    self.set_font("Helvetica", "I", 8)
                    self.set_text_color(*MEDIUM)
                    self.multi_cell(
                        0,
                        4,
                        highlight["context"],
                        new_x="LMARGIN",
                        new_y="NEXT",
                        align="L",
                    )
                    self.set_text_color(*TEXT)
                self.ln(1.5)
            self.ln(2)

        for field in ("motivation", "closing"):
            if d.get(field):
                self.set_font("Helvetica", "", 10)
                self.set_text_color(*TEXT)
                self.multi_cell(0, 5.5, d[field], new_x="LMARGIN", new_y="NEXT", align="L")
                self.ln(4)

        if d.get("signoff"):
            self.set_font("Helvetica", "", 10)
            self.multi_cell(0, 5.5, d["signoff"], new_x="LMARGIN", new_y="NEXT", align="L")
            self.set_font("Helvetica", "B", 10)
            self.multi_cell(0, 5.5, d.get("signature", d.get("name", "")), new_x="LMARGIN", new_y="NEXT", align="L")

        if not d.get("opening"):
            for para in d.get("paragraphs", []):
                self.set_font("Helvetica", "", 10)
                self.multi_cell(0, 5.5, para, new_x="LMARGIN", new_y="NEXT", align="L")
                self.ln(3)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 4:
        print("Usage: render_pdf.py <resume|cover> <input.json> <output.pdf>", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    input_path = Path(sys.argv[2])
    output_path = Path(sys.argv[3])

    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    with open(input_path, encoding="utf-8") as f:
        data = json.load(f)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    if mode == "resume":
        pdf = ResumePDF(data)
        pdf.render()
        pdf.output(str(output_path))
        print(json.dumps({"jobhunter_pdf_render": 1, "mode": mode, "pages": pdf.page_no()}))
    elif mode == "cover":
        pdf = CoverLetterPDF(data)
        pdf.render()
        pdf.output(str(output_path))
        print(json.dumps({"jobhunter_pdf_render": 1, "mode": mode, "pages": pdf.page_no()}))
    else:
        print(f"Unknown mode: {mode}. Use 'resume' or 'cover'.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
