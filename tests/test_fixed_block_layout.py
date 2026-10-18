import copy

import fitz

import pytest

from resume_blocks import ResumeBlocksError, fit
from resume_pdf_audit import audit_resume_pdf, resume_layout_metrics
from styled_resume_pdf import StyledResumePDF


@pytest.mark.parametrize('bullet_count,sparse', [(28, True), (36, False)])
def test_actual_two_page_pdf_keeps_whole_block_and_measures_page_two(tmp_path, bullet_count, sparse):
    payload = {'name': 'Example Candidate', 'headline': 'Software Engineer',
               'summary': 'Approved fixed summary.', 'additional': {'languages': 'English (C1)'},
               'experience': [{'company': 'Example Company', 'title': 'Software Engineer',
                               'dates': '2020 - Present', 'bullets': [
                                   f'Delivered component {i} using confirmed Java services with PostgreSQL persistence, '
                                   'automated tests and documented release procedures across the application platform.'
                                   for i in range(bullet_count)]}]}
    original = copy.deepcopy(payload)
    path = tmp_path / 'two-pages.pdf'

    def render(value):
        pdf = StyledResumePDF(value)
        pdf.render()
        pdf.output(str(path))
        return resume_layout_metrics(path)

    fitted, report = fit(payload, render, 2)
    assert fitted['experience'] == original['experience']
    assert report['pages'] == 2
    assert report['page_two_sparse'] is sparse
    assert len(report['main_fills']) == 2
    render(fitted)
    assert audit_resume_pdf(path, fitted, max_pages=2)['pdf_text_verified']
    with pytest.raises(ResumeBlocksError, match='above the selected 1-page limit'):
        fit(payload, render, 1)


def test_scoped_two_page_layout_places_continuation_and_personal_details_on_page_two(tmp_path):
    payload = {'name': 'Example Candidate', 'headline': 'Software Architect', 'summary': 'Designs service platforms.',
               'skills': {'Backend': ['Java'], 'Leadership & Mentorship': ['Technical leadership', 'Engineer mentoring']},
               'additional': {'languages': 'English (C1)'},
               'education': [{'degree': 'Engineering Diploma', 'school': 'Example University', 'dates': '2016 - 2019'}],
               'volunteering': [{'title': 'Technical Instructor', 'organization': 'Example School', 'dates': '2022 - Present', 'description': ''}],
               'layout': {'compact_spacing': True, 'personal_details_page_two': True,
                          'page_break_before_engagement': 'Farm Network'},
               'experience': [{'company': 'Group Co', 'title': 'Lead Engineer', 'dates': '2020 - Present',
                               'engagements': [{'name': 'Cars Platform', 'dates': '2020 - 2024',
                                                'bullets': ['Owned five Java services.']},
                                               {'name': 'Farm Network', 'dates': '2025 - 2026',
                                                'bullets': ['Designed the service platform.']}]},
                              {'company': 'Startup', 'title': 'CTO', 'dates': '2025 - Present',
                               'bullets': ['Built the advertising platform.']}]}
    path = tmp_path / 'scoped.pdf'
    pdf = StyledResumePDF(payload)
    pdf.render()
    pdf.output(str(path))
    with fitz.open(path) as document:
        assert len(document) == 2
        first, second = (page.get_text() for page in document)
        assert 'Leadership & Mentorship' in first
        assert 'Cars Platform' in first and 'Farm Network' not in first
        assert 'Group Co (continued)' in second and 'Farm Network' in second
        assert 'LANGUAGES' not in first and 'EDUCATION' not in first
        assert 'LANGUAGES' in second and 'EDUCATION' in second
        assert second.count('English') == 1 and second.count('Engineering Diploma') == 1
    assert audit_resume_pdf(path, payload, max_pages=2)['pdf_text_verified']
    default = copy.deepcopy(payload)
    default['layout'].pop('personal_details_page_two')
    default['layout'].pop('page_break_before_engagement')
    pdf = StyledResumePDF(default)
    pdf.render()
    pdf.output(str(tmp_path / 'default.pdf'))
    with fitz.open(tmp_path / 'default.pdf') as document:
        assert len(document) == 1
        assert 'LANGUAGES' in document[0].get_text() and 'EDUCATION' in document[0].get_text()
