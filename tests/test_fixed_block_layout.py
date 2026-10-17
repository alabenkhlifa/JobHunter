import copy

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
