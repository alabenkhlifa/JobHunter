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


@pytest.mark.parametrize("prefix", ["", "VMware "])
def test_spring_label_omits_edition_year_and_keeps_original_verification_link(prefix):
    title = f"{prefix}Spring Certified Professional 2024 v2"
    spring_url = "https://credentials.example/spring"
    profile = {
        "name": "Candidate",
        "certifications": [title, "Other Certificate 2024"],
        "certification_links": {title: spring_url},
        "experience": [{
            "title": "Engineer", "company": "Example", "dates": "2024 - Present",
        }],
    }
    _, output = _pdf(profile)
    reader = PdfReader(BytesIO(output))
    text = " ".join(" ".join(page.extract_text() for page in reader.pages).split())

    assert f"{prefix}Spring Certified Professional v2" in text
    assert "Spring Certified Professional 2024" not in text
    assert "Other Certificate 2024" in text
    assert "2024 - Present" in text
    assert spring_url in _links(reader)
    assert profile["certifications"][0] == title
    assert profile["certification_links"] == {title: spring_url}


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


def test_reviewed_cover_paragraphs_take_precedence_over_legacy_highlights():
    letter = CoverLetterPDF({
        "name": "Candidate",
        "paragraphs": [
            "I handled a production integration failure and resolved duplicate events.",
            "I kept the team close to the implementation and verified the release.",
            "Those examples are directly relevant to the integration work in this role.",
        ],
        "opening": "Generic opening that should not render.",
        "highlights": [{"text": "Copied resume bullet that should not render."}],
        "closing": "Generic closing that should not render.",
    })
    letter.render()
    text = PdfReader(BytesIO(bytes(letter.output()))).pages[0].extract_text()

    assert "production integration failure" in text
    assert "Copied resume bullet" not in text
    assert "Generic opening" not in text
    assert "Generic closing" not in text


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


def _resume_with_sidebar_overflow(bullet_count):
    return {
        "name": "Candidate",
        "summary": "Backend engineering and technical leadership in production services.",
        "skills": {f"Category {index}": [f"Skill {skill}" for skill in range(9)]
                   for index in range(5)},
        "certifications": [f"Certificate {index}" for index in range(6)],
        "experience": [{
            "title": "Senior Software Engineer",
            "company": "Example Company",
            "dates": "2020 - Present",
            "bullets": [f"Delivered reliable production backend services for customer group {index}."
                        for index in range(bullet_count)],
        }],
        "additional": {"languages": "Arabic (Native) | English (C1) | French (C1)"},
        "education": [{
            "degree": "Software Engineering Diploma",
            "school": "Example University",
            "dates": "2018 - 2023",
        }],
    }


def test_personal_details_fill_main_column_when_sidebar_alone_would_need_page_two():
    fitz = pytest.importorskip("fitz")
    profile = _resume_with_sidebar_overflow(12)
    planned = ResumePDF(profile)
    planned._plan_sidebar()
    assert planned._sidebar_page_count == 2
    assert planned._second_page_only_has_personal_details()

    _, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) == 1
    page = document[0]
    words = page.get_text("words")
    experience = next(word for word in words if word[4] == "EXPERIENCE")
    languages = next(word for word in words if word[4] == "LANGUAGES")
    education = next(word for word in words if word[4] == "EDUCATION")
    assert languages[0] < planned.SIDE_X
    assert languages[1] > experience[1]
    assert languages[1] > page.search_for("customer group 11.")[-1].y1 + 6
    assert education[1] > languages[1]
    assert "Senior Software Engineer" in page.get_text()
    assert "Software Engineering Diploma" in page.get_text()
    assert all(0 <= x0 < x1 <= page.rect.width + 0.5 and
               0 <= y0 < y1 <= page.rect.height + 0.5
               for x0, y0, x1, y1, *_ in words)


def test_personal_details_use_compact_columns_when_full_width_stack_does_not_fit():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf(_resume_with_sidebar_overflow(42))
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) == 1
    page = document[0]
    words = page.get_text("words")
    languages = next(word for word in words if word[4] == "LANGUAGES")
    education = next(word for word in words if word[4] == "EDUCATION")
    assert abs(languages[1] - education[1]) < 0.5
    assert languages[2] < education[0]
    assert languages[1] > page.search_for("customer group 41.")[-1].y1 + 6
    assert "French" in page.get_text()
    assert "Software Engineering Diploma" in page.get_text()
    assert all(y1 <= page.rect.height + 0.5 for _, _, _, y1, *_ in words)


def test_languages_can_finish_sidebar_while_education_fits_below_experience():
    fitz = pytest.importorskip("fitz")
    profile = _resume_with_sidebar_overflow(48)
    profile["education"][0]["school"] = (
        "Example University of Applied Engineering and Computer Science"
    )
    _, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) == 1
    page = document[0]
    words = page.get_text("words")
    languages = next(word for word in words if word[4] == "LANGUAGES")
    education = next(word for word in words if word[4] == "EDUCATION")
    assert languages[0] >= ResumePDF.SIDE_X - 1
    assert education[0] < ResumePDF.SIDE_X
    assert education[1] > page.search_for("customer group 47.")[-1].y1 + 6
    assert all(language in page.get_text() for language in ("Arabic", "English", "French", "Native"))
    assert "Software Engineering Diploma" in page.get_text()
    assert all(y1 <= page.rect.height + 0.5 for _, _, _, y1, *_ in words)


@pytest.mark.parametrize("bullets_per_client, expected_pages", [(8, 1), (9, 2)])
def test_grouped_resume_recovers_spacing_only_when_personal_details_can_fit(
    bullets_per_client, expected_pages,
):
    import copy

    fitz = pytest.importorskip("fitz")
    profile = _resume_with_sidebar_overflow(0)
    profile["education"].append({
        "degree": "Bachelor of Engineering", "school": "Example Institute",
        "dates": "2014 - 2018",
    })
    profile["experience"] = [
        {
            "title": "Lead Engineer", "company": "Example Company", "dates": "2020 - Present",
            "progression": "Progressed from Engineer to Lead Engineer.",
            "engagements": [
                {
                    "name": f"Client {index}",
                    "bullets": [
                        f"Delivered reliable production backend services for customer group {bullet}."
                        for bullet in range(bullets_per_client)
                    ],
                    "tech": "Java, AWS",
                } for index in range(3)
            ],
        },
        {
            "title": "Chief Technology Officer (CTO)", "company": "Example Platform",
            "dates": "2025 - Present",
            "bullets": ["Designed and delivered backend services for enterprise customers."] * 2,
            "tech": "AWS, Terraform",
        },
    ]
    original = copy.deepcopy(profile)
    pdf, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")
    text = "\n".join(page.get_text() for page in document)

    assert len(document) == expected_pages
    assert profile == original
    assert text.count("Delivered reliable production backend services") == 3 * bullets_per_client
    assert text.count("Designed and delivered backend services") == 2
    assert all(value in text for value in (
        "Arabic", "English", "French", "Native", "Software Engineering Diploma",
        "Bachelor of Engineering", "Example University", "Example Institute",
    ))
    assert all(word[3] <= pdf.BOTTOM for page in document for word in page.get_text("words"))
    if expected_pages == 1:
        page = document[0]
        headings = [page.search_for(f"Client {index}")[0] for index in range(3)]
        assert all(headings[index].y1 < headings[index + 1].y0 for index in range(2))
        for heading in headings[1:]:
            divider = max(
                drawing["rect"].y0 for drawing in page.get_drawings()
                if drawing["rect"].x0 < pdf.SIDE_X and drawing["rect"].y0 < heading.y0
            )
            assert heading.y0 - divider >= 2.5
        body_sizes = [span["size"] for block in page.get_text("dict")["blocks"]
                      for line in block.get("lines", []) for span in line["spans"]
                      if "Delivered reliable production" in span["text"]]
        assert all(abs(size - 7.7) < 0.01 for size in body_sizes)


def test_compact_sidebar_languages_clear_the_heading_rule():
    fitz = pytest.importorskip("fitz")
    profile = _resume_with_sidebar_overflow(48)
    profile["skills"]["Category 4"] = [f"Skill {index}" for index in range(27)]
    profile["education"][0]["school"] = (
        "Example University of Applied Engineering and Computer Science"
    )
    _, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) == 1
    page = document[0]
    heading = page.search_for("LANGUAGES")[0]
    language = page.search_for("Arabic: Native")[0]
    rule = next(
        drawing["rect"] for drawing in page.get_drawings()
        if (drawing["width"] or 0) > 2 and drawing["rect"].x0 >= ResumePDF.SIDE_X - 1
        and heading.y1 <= drawing["rect"].y0 < language.y0
    )
    assert language.x0 >= ResumePDF.SIDE_X - 1
    assert 5 <= language.y0 - rule.y0 <= 11
    assert all(value in page.get_text() for value in ("English: C1", "French: C1"))


def test_personal_details_keep_second_page_when_main_column_has_no_room():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf(_resume_with_sidebar_overflow(52))
    document = fitz.open(stream=output, filetype="pdf")

    assert len(document) == 2
    assert "Senior Software Engineer" in document[0].get_text()
    assert "Senior Software Engineer" not in document[1].get_text()
    assert "LANGUAGES" not in document[0].get_text()
    assert "EDUCATION" not in document[0].get_text()
    assert "LANGUAGES" in document[1].get_text()
    assert "EDUCATION" in document[1].get_text()
    assert all(word[0] >= ResumePDF.SIDE_X - 1 for word in document[1].get_text("words"))


def test_roles_that_fit_stay_on_first_page_and_certifications_stay_together():
    fitz = pytest.importorskip("fitz")
    profile = {
        "name": "Candidate",
        "summary": "Backend engineer and technical lead with seven years of experience.",
        "skills": {f"Category {i}": [f"Skill {j}" for j in range(15)] for i in range(6)},
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


def test_skill_and_certification_headings_have_compact_visible_gaps():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Backend engineering experience.",
        "skills": {"Backend & Architecture": ["Java", "Kotlin", "Spring Boot"]},
        "certifications": ["AWS Solutions Architect - Associate (SAA-C03)"],
    })
    page = fitz.open(stream=output, filetype="pdf")[0]
    category = page.search_for("Backend & Architecture")[0]
    first_skill = page.search_for("Java")[0]
    heading = page.search_for("CERTIFICATIONS")[0]
    certificate = page.search_for("AWS Solutions Architect")[0]
    rule = next(
        drawing["rect"] for drawing in page.get_drawings()
        if drawing["width"] > 2 and drawing["rect"].x0 >= ResumePDF.SIDE_X - 1
        and heading.y1 <= drawing["rect"].y0 < certificate.y0
    )

    assert 3 <= first_skill.y0 - category.y1 <= 8
    assert 5 <= certificate.y0 - rule.y0 <= 11


def test_language_names_without_levels_are_visible_without_invented_proficiency():
    fitz = pytest.importorskip("fitz")
    _, output = _pdf({
        "name": "Candidate",
        "summary": "Backend engineering experience.",
        "additional": {"languages": "Arabic, English, French"},
    })
    text = fitz.open(stream=output, filetype="pdf")[0].get_text()

    assert all(language in text for language in ("LANGUAGES", "Arabic", "English", "French"))
    assert all(level not in text for level in ("Native", "C1", "Fluent"))


def test_employer_highlights_render_once_with_client_context_and_source_unchanged():
    import copy

    fitz = pytest.importorskip("fitz")
    recent = "Built tools for code review."
    leadership = "Led a team of four engineers."
    services = "Built production backend services."
    profile = {"name": "Candidate", "experience": [{
        "title": "Lead Engineer", "company": "Example", "dates": "2020 - Present",
        "engagements": [
            {"name": "Recent Client - Review Product", "bullets": [recent], "tech": "Python, AWS"},
            {"name": "Older Client - Backend Product", "bullets": [leadership, services],
             "tech": "Java, AWS", "aliases": ["PRIVATE MATCHING ALIAS"]},
        ],
        "highlights": [{"context": "Older Client - Backend Product", "text": leadership},
                       {"context": "Older Client - Backend Product", "text": services}],
    }]}
    original = copy.deepcopy(profile)
    pdf, output = _pdf(profile)
    document = fitz.open(stream=output, filetype="pdf")
    text = " ".join(" ".join(page.get_text() for page in document).split())

    assert profile == original
    assert text.index(leadership) < text.index("Recent Client - Review Product")
    assert f"Older Client: {leadership}" in text
    assert f"Older Client: {services}" in text
    assert all(text.count(bullet) == 1 for bullet in (recent, leadership, services))
    assert "PRIVATE MATCHING ALIAS" not in text
    assert text.count("Keywords:") == 1
    assert "Python, AWS, Java" in text
    assert all(word[3] <= pdf.BOTTOM for page in document for word in page.get_text("words"))


def test_employer_highlights_reject_unattributed_or_missing_evidence():
    profile = {"name": "Candidate", "experience": [{
        "engagements": [{"name": "Client", "bullets": ["Approved fact."]}],
        "highlights": [{"context": "Client", "text": "Invented fact."}],
    }]}
    with pytest.raises(ValueError, match="must match an included client bullet"):
        _pdf(profile)


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
