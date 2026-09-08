from render_pdf import ResumePDF


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


def test_wrapped_certification_text_keeps_links_and_order():
    # Capture the final PDF text operations and actual link rectangles together,
    # so wrapping cannot silently leave part of a credential unclickable.
    class RecordingResumePDF(ResumePDF):
        def __init__(self, profile):
            super().__init__(profile)
            self.certification_fragments = []

        def _render_styled_text_line(self, text_line, *args, **kwargs):
            result = super()._render_styled_text_line(text_line, *args, **kwargs)
            if kwargs.get("link"):
                self.certification_fragments.append(
                    ("".join(fragment.string for fragment in text_line.fragments), kwargs["link"])
                )
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
