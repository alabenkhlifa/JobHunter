from types import SimpleNamespace

import jobhunter_interest_flow as flow
import jobhunter_research
from jobhunter_research import company


def _research(name, description, *, rows=(), website="", fetch=None):
    job = {
        "id": "company-regression",
        "title": "Senior Software Engineer",
        "company": name,
        "location": "Madrid, Spain",
        "description": description,
        "company_website": website,
    }
    ctx = jobhunter_research.build_context(job)
    notes = []
    original = flow.fetch_verified_company_pages
    flow.fetch_verified_company_pages = lambda *_args, **_kwargs: []
    try:
        result = company.research_company(
            ctx, search=lambda _query: list(rows), fetch=fetch or (lambda _url: None), notes=notes
        )
    finally:
        flow.fetch_verified_company_pages = original
    return result, notes


def test_bnp_about_copy_is_preserved_and_it_team_copy_is_not_promoted():
    about = (
        "About Bnp Paribas: We are BNP Paribas, a leading European bank with a strong international presence in 64 countries. "
        "Through our Retail Banking & Services division, we lead the European market in consumer finance via "
        "BNP Paribas Personal Finance. We support our clients in their projects through financing, investment, and savings solutions. "
        "About WhoWeAre SETIberia IT team Java Python AI. About The Role Build backend services."
    )

    result, _ = _research("BNP Paribas", about)

    assert result.summary_origin == "job_post"
    assert result.company["summary"] == (
        "We are BNP Paribas, a leading European bank with a strong international presence in 64 countries. "
        "Through our Retail Banking & Services division, we lead the European market in consumer finance via "
        "BNP Paribas Personal Finance."
    )
    assert result.company["industry"] == "bank"
    assert "AI" not in result.company["summary"]


def test_ust_regional_label_resolves_brand_and_rejects_leadiq_as_official():
    assert flow.company_search_name("UST España & Latam") == "UST"
    assert not flow._is_official_company_result("UST España & Latam", "https://leadiq.com/company/ust")
    assert flow._is_official_company_result("UST España & Latam", "https://www.ust.com/about")
    assert flow._is_official_company_result("UST España & Latam", "https://www.ustglobal.com/about")
    assert flow._is_official_company_result("Miratech", "https://miratechgroup.com")
    assert flow._result_matches_company(
        "Miratech", {"title": "Careers", "url": "https://miratechgroup.com/careers"}
    )
    assert not flow._result_matches_company(
        "UST España & Latam", {"title": "Trustpilot reviews", "url": "https://trustpilot.com/ust"}
    )


def test_job_description_team_and_requirements_do_not_become_company_summary():
    result, _ = _research(
        "UST España & Latam",
        "The SETIberia IT team uses Java, Python and AI. Requirements include cloud architecture and AI. "
        "About The Role Build software for a regional team.",
    )

    assert result.company["summary"] == ""
    assert result.summary_origin == ""


def test_ust_employer_about_is_separated_from_who_we_are_team_copy():
    result, _ = _research(
        "UST España & Latam",
        "About UST España & Latam: UST is a multinational company based in North America. "
        "The company has over 35,000 employees in 35 countries. About Who We Are "
        "The SETIberia IT team uses Java, Python and AI. About The Role Build backend services.",
    )

    assert result.company["summary"] == (
        "UST is a multinational company based in North America. "
        "The company has over 35,000 employees in 35 countries."
    )


def test_unverified_stored_domain_and_redirect_do_not_become_company_site():
    class Response:
        status_code = 200
        url = "https://leadiq.com/company/ust"
        text = '<meta name="description" content="UST is a technology company.">'

    result, notes = _research(
        "UST España & Latam",
        "No employer description provided.",
        website="https://leadiq.com/company/ust",
        fetch=lambda _url: Response(),
    )

    assert result.company["website"] == ""
    assert result.company["summary"] == ""
    assert any("did not verify" in note for note in notes)


def test_generic_or_role_only_search_snippets_never_get_a_default_company_type():
    summary = flow._compact_company_summary(
        "Miratech",
        "Madrid, Spain",
        [{"title": "Miratech careers", "url": "https://jobs.example/jobs", "snippet": "Join our global technology team working on AI."}],
    )

    assert summary == ""
