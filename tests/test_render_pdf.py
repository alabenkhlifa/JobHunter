from io import BytesIO

import pytest
from pypdf import PdfReader

from render_pdf import CoverLetterPDF, ResumePDF


def _pdf(profile):
    pdf = ResumePDF(profile)
    pdf.render()
    return pdf, bytes(pdf.output())


def _links(document):
    return [annotation.get_object()["/A"]["/URI"]
            for page in document.pages
            for annotation in page.get("/Annots", [])
            if annotation.get_object().get("/Subtype") == "/Link"]


def test_resume_certification_links_are_clickable_except_unlinked_entries(tmp_path):
    spring_url = "https://credentials.example/spring"
    output_path = tmp_path / "resume.pdf"
    profile = {
        "name": "Candidate",
        "certifications": [
            "Spring Certified Professional 2024 v2",
            "Claude Certified Architect",
        ],
        "certification_links": {
            "Spring Certified Professional 2024 v2": spring_url,
        },
    }

    pdf = ResumePDF(profile)
    pdf.render()
    pdf.output(str(output_path))
    output = output_path.read_bytes()

    assert spring_url.encode() in output
    assert output.count(b"/Subtype /Link") == 1


def test_contact_and_credential_links_are_real_annotations():
    profile = {
        "name": "Candidate",
        "website": "candidate.example",
        "email": "candidate@example.com",
        "phone": "+216 12 345 678",
        "linkedin": "linkedin.com/in/candidate",
        "certifications": ["Verified credential", "No credential URL", "Invalid credential URL"],
        "certification_links": {
            "Verified credential": "https://credentials.example/verified",
            "Invalid credential URL": "javascript:alert(1)",
        },
    }
    _, output = _pdf(profile)
    reader = PdfReader(BytesIO(output))

    assert set(_links(reader)) == {
        "https://candidate.example",
        "mailto:candidate@example.com",
        "https://linkedin.com/in/candidate",
        "https://credentials.example/verified",
    }
    assert len(_links(reader)) == 4
    text = " ".join(page.extract_text() for page in reader.pages)
    assert "No credential URL" in text
    assert "Invalid credential URL" in text
    assert "+216 12 345 678" not in text


def test_verse_company_name_links_to_candidate_provided_url():
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Technical leadership and backend engineering.",
        "experience": [{
            "title": "Chief Technology Officer (CTO)",
            "company": "VERSE",
            "company_url": "https://verse.ad",
            "dates": "October 2025 - Present",
            "bullets": ["Led engineering delivery."],
        }],
    })
    reader = PdfReader(BytesIO(output))
    text = " ".join(page.extract_text() for page in reader.pages)

    assert "Chief Technology Officer (CTO)" in text
    assert "Co-Founder" not in text
    assert "VERSE" in text
    assert _links(reader) == ["https://verse.ad"]


def test_cover_contact_links_and_long_subject():
    letter = CoverLetterPDF({
        "name": "Candidate",
        "contact": "candidate@example.com | +216 12 345 678 | linkedin.com/in/candidate",
        "subject": "Application for a senior backend engineering role with a long descriptive title " * 2,
        "opening": "I build reliable APIs and lead delivery teams.",
    })
    letter.render()
    reader = PdfReader(BytesIO(bytes(letter.output())))

    assert len(reader.pages) == 1
    assert set(_links(reader)) == {
        "mailto:candidate@example.com",
        "https://linkedin.com/in/candidate",
    }
    assert "I build reliable APIs" in reader.pages[0].extract_text()


def test_wrapped_certification_text_keeps_links_and_order():
    # A wrapped credential needs one real link rectangle per visible line.
    class RecordingResumePDF(ResumePDF):
        def __init__(self, profile):
            super().__init__(profile)
            self.certification_fragments = []

        def _text(self, value, *args, **kwargs):
            result = super()._text(value, *args, **kwargs)
            if kwargs.get("link"):
                self.certification_fragments.append((value, kwargs["link"]))
            return result

    spring = "Spring Certified Professional " * 8
    architecture = "Architecture Foundation Certificate"
    spring_url = "https://credentials.example/spring"
    architecture_url = "https://credentials.example/architecture"
    pdf = RecordingResumePDF({
        "name": "Candidate",
        "certifications": [spring, architecture, "Unlinked Certificate"],
        "certification_links": {spring: spring_url, architecture: architecture_url},
    })

    pdf.render()
    output = bytes(pdf.output())
    fragments = pdf.certification_fragments
    spring_fragments = [text for text, url in fragments if url == spring_url]

    assert len(spring_fragments) > 1
    assert " ".join(" ".join(spring_fragments).split()) == " ".join(spring.split())
    architecture_fragments = [text for text, url in fragments if url == architecture_url]
    assert " ".join(architecture_fragments) == architecture
    assert [url for _, url in fragments] == (
        [spring_url] * len(spring_fragments)
        + [architecture_url] * len(architecture_fragments)
    )
    assert output.count(b"/Subtype /Link") == len(fragments)


def test_long_resume_keeps_every_entry_and_both_columns_inside_a4():
    fitz = pytest.importorskip("fitz")
    certification = "VMware Spring Certified Professional 2024 v2"
    profile = {
        "name": "Candidate",
        "headline": "Senior Software Engineer",
        "email": "candidate@example.com",
        "summary": "Cloud architecture and backend engineering. " * 10,
        "skills": {f"Category {index}": ["Architecture", "Engineering", "Kubernetes"] * 5
                   for index in range(7)},
        "certifications": [certification] * 8,
        "certification_links": {certification: "https://credentials.example/spring"},
        "experience": [{
            "title": f"Distinct Role {index}",
            "company": "Example Company",
            "dates": f"202{index} - Present",
            "bullets": [f"Delivered reliable backend services for customer group {index}. " * 4] * 6,
            "tech": "Spring Boot, AWS, Kubernetes, PostgreSQL",
        } for index in range(7)],
        "education": [{"degree": "Software Engineering Diploma", "school": "Example University"}],
        "additional": {"languages": "Arabic, English, French"},
    }
    pdf, output = _pdf(profile)
    reader = PdfReader(BytesIO(output))
    text = "\n".join(page.extract_text() for page in reader.pages)

    assert pdf.page_no() >= 3
    for index in range(7):
        assert f"Distinct Role {index}" in text
    assert "Professional" in text
    assert "Profes\nsional" not in text
    assert "Software Engineering Diploma" in text
    assert len(_links(reader)) >= 8

    document = fitz.open(stream=output, filetype="pdf")
    for page in document:
        assert abs(page.rect.width - 595.28) < 1
        assert abs(page.rect.height - 841.89) < 1
        for x0, y0, x1, y1, *_ in page.get_text("words"):
            assert 0 <= x0 < x1 <= page.rect.width + 0.5
            assert 0 <= y0 < y1 <= page.rect.height + 0.5
            if y0 > 100:  # Below the identity block, words stay in their column.
                assert x1 <= pdf.MAIN_X + pdf.MAIN_W + 1 or x0 >= pdf.SIDE_X - 1


def test_keywords_cannot_start_a_page_without_role_continuation():
    profile = {
        "name": "Candidate",
        "summary": "Backend engineer with reliable production systems. " * 4,
        "experience": [{
            "title": "Senior Software Engineer",
            "company": "Example",
            "dates": "2022 - Present",
            "bullets": ["Built and operated reliable backend services on AWS for client applications."] * 75,
            "tech": "Spring Boot, AWS, Kubernetes, PostgreSQL, GitHub Actions, Docker, REST APIs",
        }],
    }
    _, output = _pdf(profile)
    pages = PdfReader(BytesIO(output)).pages

    assert len(pages) >= 2
    for page in pages[1:]:
        text = page.extract_text()
        if "Keywords:" in text:
            assert "Senior Software Engineer (continued)" in text
            assert text.index("Keywords:") > text.index("(continued)")


def test_reviewed_template_fonts_languages_and_optional_sections():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "headline": "Software Architect | Tech Lead",
        "summary": "Seven years of backend engineering and microservice architecture.",
        "skills": {"Backend & Architecture": ["Spring Boot", "NestJS", "Microservices"]},
        "certifications": ["Claude Certified Architect"],
        "additional": {
            "languages": "Arabic (Native) · French (C1) · English (C1)",
            "teaching": "Excluded teaching section",
            "interests": "Excluded interests section",
        },
        "education": [{"degree": "Software Engineering Diploma", "school": "ESPRIT"}],
    })
    document = fitz.open(stream=output, filetype="pdf")
    all_text = "\n".join(page.get_text() for page in document)
    fonts = {font[3] for page in document for font in page.get_fonts(full=True)}

    assert len(document) == 1
    assert any("Inter" in name for name in fonts)
    assert any("Rubik" in name for name in fonts)
    assert all(term in all_text for term in ("Arabic", "Native", "English", "French", "C1"))
    assert "Excluded teaching section" not in all_text
    assert "Excluded interests section" not in all_text
    assert "Claude Certified Architect" in all_text
    assert not any("certifications/#cert-architect" in link.get("uri", "")
                   for page in document for link in page.get_links())


def test_languages_show_compact_level_labels_without_filling_bars():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Backend engineering experience.",
        "additional": {"languages": "Arabic (Native) | English (C1) | French (C1)"},
        "education": [{"degree": "Software Engineering Diploma", "school": "ESPRIT"}],
    })
    document = fitz.open(stream=output, filetype="pdf")
    assert len(document) == 1
    page = document[0]
    words = {word[4]: word for word in page.get_text("words") if word[4] in {"Arabic", "Native", "English", "French"}}
    levels = [word for word in page.get_text("words") if word[4] == "C1"]

    assert abs(words["Arabic"][1] - words["Native"][1]) < 0.5
    assert len(levels) == 2
    assert words["English"][1] - words["Arabic"][1] <= 23
    assert words["French"][1] - words["English"][1] <= 23
    assert not any(
        drawing["fill"] is not None
        and drawing["rect"].x0 >= 380
        and drawing["rect"].y0 < 120
        for drawing in page.get_drawings()
    )


def test_long_experience_keeps_personal_details_on_second_page():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Backend engineering experience.",
        "experience": [{
            "title": "Senior Software Engineer", "company": "Example",
            "dates": "2020 - Present",
            "bullets": [f"Delivered production backend capability {index} with reliable operations."
                        for index in range(75)],
        }],
        "additional": {"languages": "Arabic (Native) | English (C1) | French (C1)"},
        "education": [{"degree": "Software Engineering Diploma", "school": "ESPRIT"}],
    })
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) >= 2
    assert "LANGUAGES" not in document[0].get_text()
    assert "LANGUAGES" in document[1].get_text()
    assert "(continued)" in document[1].get_text()


def test_roles_that_fit_stay_on_first_page_and_certifications_stay_together():
    fitz = pytest.importorskip("fitz")
    profile = {
        "name": "Candidate",
        "summary": "Backend engineer and technical lead with seven years of experience.",
        "skills": {f"Category {i}": [f"Skill {j}" for j in range(9)] for i in range(6)},
        "certifications": [f"Verified Certificate {i}" for i in range(6)],
        "experience": [
            {"title": title, "company": "Example Company", "dates": dates,
             "bullets": [f"Built reliable production services as {title}." for _ in range(4)]}
            for title, dates in (("Lead Engineer", "2024 - Present"),
                                 ("Senior Engineer", "2022 - 2024"),
                                 ("Software Engineer", "2020 - 2022"))
        ],
        "additional": {"languages": "Arabic (Native) · English (C1) · French (C1)"},
        "education": [{"degree": "Software Engineering Diploma", "school": "ESPRIT"}],
    }
    _, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")
    assert len(document) == 2
    first_page = document[0].get_text()
    assert all(title in first_page for title in
               ("Lead Engineer", "Senior Engineer", "Software Engineer"))
    assert "EXPERIENCE" not in document[1].get_text()
    certification_pages = [i for i, page in enumerate(document)
                           if "Verified Certificate 0" in page.get_text()]
    assert len(certification_pages) == 1
    cert_page = certification_pages[0]
    text = document[cert_page].get_text()
    assert "CERTIFICATIONS" in text
    assert all(f"Verified Certificate {i}" in text for i in range(6))


def test_reviewed_section_spacing_and_compact_skill_rows():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Software architect with experience delivering reliable cloud services.",
        "skills": {"Backend & Architecture": [
            "NestJS", "Spring Boot", "Spring Security", "Microservices", "REST APIs",
        ]},
    })
    page = fitz.open(stream=output, filetype="pdf")[0]
    lines = {}
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            label = "".join(span["text"] for span in line["spans"])
            lines[label] = line["bbox"]
    rules = [drawing["rect"] for drawing in page.get_drawings()
             if drawing["width"] and drawing["width"] > 2
             and 90 < drawing["rect"].y0 < 110]

    assert len(rules) == 2
    summary_rule = min(rules, key=lambda rect: rect.x0)
    skills_rule = max(rules, key=lambda rect: rect.x0)
    summary = lines["Software architect with experience delivering reliable cloud services."]
    assert summary[1] - summary_rule.y0 > 3
    assert lines["Backend & Architecture"][1] - skills_rule.y0 > 3
    assert lines["NestJS"][1] - lines["Microservices"][1] <= 21


def test_grouped_company_uses_one_header_and_only_engagement_bullets():
    fitz = pytest.importorskip("fitz")
    class RecordingResumePDF(ResumePDF):
        def __init__(self, profile):
            super().__init__(profile)
            self.company_logos = []

        def _image(self, path, *args, **kwargs):
            if path == "maibornwolff.png":
                self.company_logos.append(path)
            return super()._image(path, *args, **kwargs)

    profile = {
        "name": "Candidate",
        "summary": "Backend engineering and architecture experience.",
        "experience": [{
            "title": "Lead Software Engineer",
            "company": "MaibornWolff GmbH",
            "dates": "October 2020 - Present",
            "location": "Tunis, Tunisia",
            "progression": "Progressed from Software Engineer to Senior, then Lead Software Engineer.",
            "bullets": ["Parent bullet must not render."],
            "engagements": [
                {"name": "MSR-Electronic - PolyCTRL", "dates": "August 2026 - Present",
                 "bullets": ["Built developer tools for every change."], "tech": "NestJS, Azure"},
                {"name": "GIZ - African Engagement", "dates": "March 2026 - June 2026",
                 "bullets": ["Built an API for mobile users."], "tech": "NestJS, AWS"},
            ],
        }],
    }
    pdf = RecordingResumePDF(profile)
    pdf.render()
    document = fitz.open(stream=bytes(pdf.output()), filetype="pdf")
    text = "\n".join(page.get_text() for page in document)

    assert pdf.company_logos == ["maibornwolff.png"]
    assert text.count("MaibornWolff GmbH") == 1
    assert "Parent bullet must not render" not in text
    assert all(part in text for part in (
        "Progressed from Software Engineer", "MSR-Electronic - PolyCTRL",
        "10/2020 - Present", "GIZ - African Engagement",
        "Built developer tools", "Built an API"))
    assert "08/2026 - Present" not in text
    assert "03/2026 - 06/2026" not in text
    assert "August 2026" not in text
    assert profile["experience"][0]["dates"] == "October 2020 - Present"
    assert text.index("MSR-Electronic") < text.index("GIZ - African")


def test_grouped_engagements_paginate_without_clipping_or_repeating_logo():
    fitz = pytest.importorskip("fitz")
    class RecordingResumePDF(ResumePDF):
        def __init__(self, profile):
            super().__init__(profile)
            self.company_logos = 0

        def _image(self, path, *args, **kwargs):
            if path == "maibornwolff.png":
                self.company_logos += 1
            return super()._image(path, *args, **kwargs)

    engagements = [{
        "name": f"Client Engagement {index}",
        "dates": f"January 202{index} - February 202{index}",
        "bullets": [f"Delivered distinct capability {index} with backend and cloud services. " * 3]
                   * 4,
        "tech": "Spring Boot, AWS, Kubernetes, PostgreSQL",
    } for index in range(6)]
    pdf = RecordingResumePDF({
        "name": "Candidate",
        "summary": "Backend architect with production engineering experience.",
        "experience": [{
            "title": "Lead Software Engineer", "company": "MaibornWolff GmbH",
            "dates": "October 2020 - Present",
            "engagements": engagements,
        }],
    })
    pdf.render()
    document = fitz.open(stream=bytes(pdf.output()), filetype="pdf")
    text = "\n".join(page.get_text() for page in document)

    assert len(document) >= 2
    assert pdf.company_logos == 1
    assert "MaibornWolff GmbH (continued)" in text
    for index in range(6):
        assert f"Client Engagement {index}" in text
        assert f"distinct capability {index}" in text
    for page in document:
        for x0, y0, x1, y1, *_ in page.get_text("words"):
            assert 0 <= x0 < x1 <= page.rect.width + 0.5
            assert 0 <= y0 < y1 <= page.rect.height + 0.5


def test_renderer_places_maibornwolff_before_cto_even_for_direct_json():
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Backend architecture and technical leadership.",
        "experience": [
            {"title": "Chief Technology Officer", "company": "VERSE",
             "dates": "October 2025 - Present", "bullets": ["Led delivery."]},
            {"title": "Lead Software Engineer", "company": "MaibornWolff GmbH",
             "dates": "October 2020 - Present",
             "engagements": [{"name": "Client project", "dates": "March 2026 - June 2026",
                              "bullets": ["Built backend services."]}]},
        ],
    })
    text = "\n".join(page.extract_text() for page in PdfReader(BytesIO(output)).pages)

    assert text.index("MaibornWolff GmbH") < text.index("Chief Technology Officer")
    assert "10/2020 - Present" in text
    assert "03/2026 - 06/2026" not in text
