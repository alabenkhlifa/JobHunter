#!/usr/bin/env python3
"""PDF renderer for tailored resumes and cover letters using fpdf2."""

import json
import sys
from pathlib import Path

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
    """FPDF subclass that sanitizes Unicode text for latin-1 core fonts."""
    def normalize_text(self, text):
        return super().normalize_text(_sanitize(text))


class ResumePDF(SanitizedPDF):
    def __init__(self, profile):
        super().__init__()
        self.profile = profile
        self.set_auto_page_break(auto=True, margin=15)

    def header(self):
        pass  # We render the header manually on the first page

    def footer(self):
        self.set_y(-10)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*LIGHT_GRAY)
        self.cell(0, 5, f"Page {self.page_no()}/{{nb}}", align="C")

    def section_header(self, title):
        self.ensure_space(16)
        self.set_font("Helvetica", "B", 11)
        self.set_text_color(*DARK)
        self.cell(0, 7, title.upper(), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*DARK)
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def ensure_space(self, height):
        if self.get_y() + height > self.h - self.b_margin:
            self.add_page()

    def bullet(self, text):
        self.set_font("Helvetica", "", 9)
        self.set_text_color(*TEXT)
        x = self.get_x()
        self.cell(5, 4.5, "-", new_x="END")
        self.multi_cell(
            self.w - self.r_margin - x - 6,
            4.5,
            f" {text}",
            new_x="LMARGIN",
            new_y="NEXT",
            align="L",
        )

    def render(self):
        p = self.profile
        self.alias_nb_pages()
        self.add_page()
        self.set_margins(15, 10, 15)

        # ── Name & headline ──
        self.set_font("Helvetica", "B", 20)
        self.set_text_color(*DARK)
        self.cell(0, 10, p["name"], new_x="LMARGIN", new_y="NEXT", align="C")

        self.set_font("Helvetica", "", 10)
        self.set_text_color(*MEDIUM)
        self.cell(0, 5, p.get("headline", ""), new_x="LMARGIN", new_y="NEXT", align="C")

        # ── Contact row ──
        contact_parts = []
        if p.get("email"):
            contact_parts.append(p["email"])
        if p.get("phone"):
            contact_parts.append(p["phone"])
        if p.get("linkedin"):
            contact_parts.append(p["linkedin"])
        if p.get("location"):
            contact_parts.append(p["location"])

        self.set_font("Helvetica", "", 8)
        self.set_text_color(*ACCENT)
        self.cell(0, 5, "  |  ".join(contact_parts), new_x="LMARGIN", new_y="NEXT", align="C")
        self.ln(5)

        # ── Summary ──
        if p.get("summary"):
            self.section_header("Professional Summary")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*TEXT)
            self.multi_cell(0, 4.5, p["summary"], new_x="LMARGIN", new_y="NEXT", align="L")
            self.ln(3)

        # ── Skills ──
        if p.get("skills"):
            self.section_header("Technical Skills")
            for category, skills in p["skills"].items():
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*MEDIUM)
                skill_text = ", ".join(skills) if isinstance(skills, list) else skills
                self.set_font("Helvetica", "B", 9)
                cat_width = self.get_string_width(f"{category}: ") + 2
                self.cell(cat_width, 4.5, f"{category}: ", new_x="END")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*TEXT)
                self.multi_cell(
                    self.w - self.r_margin - self.get_x(),
                    4.5,
                    skill_text,
                    new_x="LMARGIN",
                    new_y="NEXT",
                    align="L",
                )
            self.ln(2)

        # ── Certifications ──
        if p.get("certifications"):
            self.section_header("Certifications")
            self.set_font("Helvetica", "", 9)
            self.set_text_color(*TEXT)
            self.multi_cell(
                0,
                4.5,
                "  |  ".join(p["certifications"]),
                new_x="LMARGIN",
                new_y="NEXT",
                align="L",
            )
            self.ln(3)

        # ── Experience ──
        if p.get("experience"):
            self.section_header("Professional Experience")
            for i, exp in enumerate(p["experience"]):
                self.ensure_space(min(66, 30 + (len(exp.get("bullets", [])) * 8)))
                # Title + Company on same line
                self.set_font("Helvetica", "B", 10)
                self.set_text_color(*DARK)
                title_text = exp["title"]
                self.cell(0, 5, title_text, new_x="LMARGIN", new_y="NEXT")

                # Company + subtitle + location + dates
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*MEDIUM)
                company_line = exp.get("company", "")
                if exp.get("subtitle"):
                    company_line += f" — {exp['subtitle']}"
                right_text = exp.get("dates", "")
                # Company on left, dates on right
                self.cell(0, 4.5, company_line)
                self.set_x(self.l_margin)
                self.cell(0, 4.5, right_text, new_x="LMARGIN", new_y="NEXT", align="R")

                if exp.get("location"):
                    self.set_font("Helvetica", "", 8)
                    self.set_text_color(*LIGHT_GRAY)
                    self.cell(0, 4, exp["location"], new_x="LMARGIN", new_y="NEXT")

                self.ln(1)

                # Bullets
                for b in exp.get("bullets", []):
                    self.bullet(b)

                # Tech line
                if exp.get("tech"):
                    self.set_font("Helvetica", "I", 8)
                    self.set_text_color(*ACCENT)
                    self.multi_cell(
                        0,
                        4,
                        f"Tech: {exp['tech']}",
                        new_x="LMARGIN",
                        new_y="NEXT",
                        align="L",
                    )

                if i < len(p["experience"]) - 1:
                    self.ln(3)

            self.ln(2)

        # ── Education ──
        if p.get("education"):
            self.section_header("Education")
            for edu in p["education"]:
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*DARK)
                self.cell(0, 5, edu.get("degree", ""))
                self.set_x(self.l_margin)
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*MEDIUM)
                self.cell(0, 5, edu.get("dates", ""), new_x="LMARGIN", new_y="NEXT", align="R")
                self.set_font("Helvetica", "I", 9)
                self.set_text_color(*TEXT)
                school_line = edu.get("school", "")
                if edu.get("location"):
                    school_line += f", {edu['location']}"
                self.cell(0, 4.5, school_line, new_x="LMARGIN", new_y="NEXT")
            self.ln(3)

        # ── Additional ──
        if p.get("additional"):
            self.section_header("Additional")
            add = p["additional"]
            if add.get("teaching"):
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*MEDIUM)
                self.cell(self.get_string_width("Teaching: ") + 2, 4.5, "Teaching: ", new_x="END")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*TEXT)
                self.multi_cell(0, 4.5, add["teaching"], new_x="LMARGIN", new_y="NEXT", align="L")
            if add.get("languages"):
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*MEDIUM)
                self.cell(self.get_string_width("Languages: ") + 2, 4.5, "Languages: ", new_x="END")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*TEXT)
                self.multi_cell(0, 4.5, add["languages"], new_x="LMARGIN", new_y="NEXT", align="L")
            if add.get("interests"):
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(*MEDIUM)
                self.cell(self.get_string_width("Interests: ") + 2, 4.5, "Interests: ", new_x="END")
                self.set_font("Helvetica", "", 9)
                self.set_text_color(*TEXT)
                self.multi_cell(0, 4.5, add["interests"], new_x="LMARGIN", new_y="NEXT", align="L")


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
        self.set_text_color(*ACCENT)
        self.cell(0, 5, d.get("contact", ""), new_x="LMARGIN", new_y="NEXT")
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
            self.cell(0, 6, f"Re: {d['subject']}", new_x="LMARGIN", new_y="NEXT")
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
