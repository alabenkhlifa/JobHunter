import pytest

import jobhunter_interest_flow as flow


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


@pytest.mark.parametrize('envelope', ['v1', 'v2'])
def test_firecrawl_accepts_both_documented_search_response_envelopes(monkeypatch, envelope):
    monkeypatch.setattr(flow, 'firecrawl_api_url', lambda: 'http://local-search.test')
    rows = [{'title': 'Example employer', 'url': 'https://employer.test/about', 'description': 'Verified company text.'}]
    body = {'success': True, 'data': rows if envelope == 'v1' else {'web': rows}}
    notes = []
    result = flow.firecrawl_search_results('company query', timeout=2,
                                            poster=lambda *a, **k: Response(body), notes=notes)
    assert result == [{'title': 'Example employer', 'url': 'https://employer.test/about',
                       'snippet': 'Verified company text.'}]
    assert not notes


def test_explicit_api_version_path_is_preserved(monkeypatch):
    monkeypatch.setattr(flow, 'firecrawl_api_url', lambda: 'http://local-search.test/v2')
    called = []

    def post(url, **kwargs):
        called.append(url)
        return Response({'success': True, 'data': {'web': []}})

    notes = []
    assert not flow.firecrawl_search_results('query', timeout=2, poster=post, notes=notes)
    assert called == ['http://local-search.test/v2/search']
    assert notes == ['Firecrawl search returned no results.']


@pytest.mark.parametrize('body', [{'success': False, 'error': 'upstream failed'}, {'success': True, 'data': 'invalid'}])
def test_provider_errors_are_recorded_and_do_not_become_no_salary_claims(monkeypatch, body):
    monkeypatch.setattr(flow, 'firecrawl_api_url', lambda: 'http://local-search.test')
    notes = []
    assert flow.firecrawl_search_results('query', timeout=2, poster=lambda *a, **k: Response(body), notes=notes) == []
    assert notes
    assert 'upstream failed' not in ' '.join(notes)  # Response text is not a safe diagnostic.


def test_incomplete_live_lookup_is_not_reported_as_absence_of_company_data():
    job = {'id': 'j1', 'title': 'Senior Engineer', 'company': 'Example', 'location': 'Madrid, Spain'}
    research = flow.JobResearch(company_summary='', legitimacy='', research={
        'company': {'sources': []}, 'salary': {}, 'sources': [],
        'notes': ['Brave search returned HTTP 429.', 'DuckDuckGo lite returned HTTP 202.']})
    card = flow.build_research_brief_message(job, research)
    assert 'Live lookup was incomplete' in card
    assert 'No comparable company salary range verified.' in card
    assert 'no Example pay data on' not in card
