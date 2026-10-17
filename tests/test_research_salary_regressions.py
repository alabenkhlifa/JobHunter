from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jobhunter_research
from jobhunter_research import roles, salary

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def _spain_job(title, company="BNP"):
    return {
        "title": title,
        "company": company,
        "location": "Madrid, Spain",
        "description": "",
        "url": "https://jobs.example.test/offer",
    }


def test_technical_leader_titles_map_to_lead_software_engineer_searches():
    for title in ("Tech Leader", "Technical Leader", "Team Leader", "Líder Técnico", "Jefe de Equipo"):
        ctx = jobhunter_research.build_context(_spain_job(title))
        assert ctx.family == "lead_software_engineer", title
        assert ctx.level == "lead", title
        assert ctx.search_role == "Lead Software Engineer", title


def test_company_wide_aggregate_is_not_used_as_senior_salary_basis():
    ctx = SimpleNamespace(
        job=_spain_job("Senior Java Developer", "Miratech"), currency="EUR", period="year",
        level="senior", family="senior_software_engineer",
    )
    sources = [
        {
            "kind": "company", "name": "Levels.fyi: Miratech",
            "url": "https://www.levels.fyi/companies/miratech/salaries/software-engineer/locations/spain",
            "snippet": "ALL Software Engineers in Spain (total comp): low €37,300 · median €38,500 · high €39,900 per year",
            "evidence": {"level": "unknown", "role_family": "backend_engineer", "geography": "Spain",
                         "compensation_basis": "total_comp", "statistic": "percentile_range", "scope": "company_wide"},
        },
        {
            "kind": "market", "name": "Levels.fyi Spain",
            "url": "https://www.levels.fyi/t/software-engineer/levels/senior/locations/spain",
            "snippet": "Senior Software Engineer, Spain (25th–75th percentile total comp): low €56,599 · median €72,877 · high €92,177 per year",
            "evidence": {"level": "senior", "role_family": "senior_software_engineer", "geography": "Spain",
                         "compensation_basis": "total_comp", "statistic": "percentile_range", "scope": "role_specific"},
        },
        {
            "kind": "market", "name": "talent.com Madrid",
            "url": "https://es.talent.com/salary?job=senior&location=madrid",
            "snippet": "Senior Java Developer, Madrid (job-ad salaries): low €33,000 · median €40,000 · high €46,000 per year",
            "evidence": {"level": "senior", "role_family": "senior_software_engineer", "geography": "Madrid",
                         "compensation_basis": "unspecified", "statistic": "range", "scope": "role_specific"},
        },
        {
            "kind": "market", "name": "Glassdoor Madrid",
            "url": "https://www.glassdoor.es/Salaries/madrid-salary-SRCH_IL.0,6_IM1030.htm",
            "snippet": "Senior Java Engineer highest €65,603 / €5,467 monthly / €32 hourly",
            "evidence": {"level": "senior", "role_family": "senior_software_engineer", "geography": "Madrid",
                         "compensation_basis": "unspecified", "statistic": "single_point", "scope": "role_specific"},
        },
    ]

    company = salary.summarize_sources(sources, kind="company", currency="EUR", period="year")
    result = salary.decide(ctx, None, sources)

    assert company["low"] == 37300 and company["high"] == 39900
    assert company["scope"] == "company_wide"
    assert result["basis"] == "market"
    assert result["low"] == 56599 and result["high"] == 92177
    assert result["compensation_basis"] == "total_comp"
    assert result["ask_basis"] == "market"
    assert result["confidence"] == "medium"  # Only Levels is comparable total-comp evidence.


def test_single_high_or_low_observation_is_not_a_range():
    assert salary.summarize_figures([(65603, 65603, "high")]) is None
    assert salary.summarize_figures([(33000, 33000, "low")]) is None


def test_cross_provider_domains_do_not_inflate_independent_source_count():
    sources = [
        {"kind": "market", "name": "Glassdoor", "url": "https://www.glassdoor.com/a",
         "snippet": "Senior Engineer salary range €50,000–€80,000 per year"},
        {"kind": "market", "name": "Glassdoor Spain", "url": "https://www.glassdoor.es/b",
         "snippet": "Senior Engineer salary range €51,000–€81,000 per year"},
    ]
    result = salary.summarize_sources(sources, kind="market", currency="EUR", period="year")
    assert result["agreeing"] == 1


def test_levels_senior_distribution_is_marked_as_a_lead_proxy():
    ctx = SimpleNamespace(level="lead", family="lead_software_engineer", market="spain", city="Madrid",
                          currency="EUR", period="year", search_role="Lead Software Engineer")

    class Response:
        status_code = 200
        text = '<script id="__NEXT_DATA__">{"props":{"pageProps":{"jobFamilyLocationPercentiles":{"p25":56000,"p50":70000,"p75":90000,"p90":105000},"locationExchangeRate":1,"locationCurrency":"EUR","level":"Senior","jobFamily":"Software Engineer","location":"Spain"}}}</script>'

    source = salary.fetch_levels(ctx, lambda _url: Response(), "now", [])

    assert source["evidence"]["level"] == "senior"
    assert source["evidence"]["proxy_for_level"] == "lead"
    assert source["evidence"]["statistic"] == "percentile_range"
    assert "median–90th percentile" in source["snippet"]
    ctx.job = _spain_job("Tech Leader")
    with patch("jobhunter_interest_flow.salary_target", return_value=None):
        result = salary.decide(ctx, None, [source])
    assert result["basis"] == "market"
    assert result["proxy_for_level"] == "lead"
    assert result["low"] == 70000 and result["high"] == 105000


def test_configured_target_is_labeled_as_the_ask_basis():
    ctx = SimpleNamespace(job=_spain_job("Senior Java Developer"), currency="EUR", period="year",
                          level="senior", family="senior_software_engineer")
    source = {
        "kind": "market", "name": "Levels.fyi Spain", "url": "https://www.levels.fyi/t/software-engineer/levels/senior/locations/spain",
        "snippet": "Senior Software Engineer, Spain (total comp): low €56,000 · high €90,000 per year",
        "evidence": {"level": "senior", "scope": "role_specific", "compensation_basis": "total_comp",
                     "statistic": "range", "role_family": "senior_software_engineer", "geography": "Spain"},
    }
    with patch("jobhunter_interest_flow.salary_target", return_value={"currency": "EUR", "period": "year", "amount": 82000}):
        result = salary.decide(ctx, None, [source])

    assert result["basis"] == "market"
    assert result["ask_amount"] == 82000
    assert result["ask_basis"] == "configured"


def test_talent_redirect_with_different_city_or_role_is_rejected():
    madrid_ctx = jobhunter_research.build_context(_spain_job("Senior Software Engineer"))
    notes = []
    wrong_city = salary.fetch_talent(
        madrid_ctx, lambda _url: SimpleNamespace(status_code=200, text=_fixture("talent_es_valencia_lead.html")), "t", notes)
    assert wrong_city is None
    assert notes == ["talent.com page identity did not match the requested role and location."]

    valencia_ctx = jobhunter_research.build_context(_spain_job("Senior Software Engineer"))
    valencia_ctx.city = "Valencia"
    notes = []
    wrong_role = salary.fetch_talent(
        valencia_ctx, lambda _url: SimpleNamespace(status_code=200, text=_fixture("talent_es_valencia_lead.html")), "t", notes)
    assert wrong_role is None
    assert notes == ["talent.com page identity did not match the requested role and location."]


def test_talent_salary_without_an_identifiable_result_header_is_rejected():
    ctx = jobhunter_research.build_context(_spain_job("Senior Software Engineer"))
    response = "<html><body>Mediana €37.500 Bajo €33.000 Alto €46.000 Basado en 10000 salarios</body></html>"
    notes = []

    result = salary.fetch_talent(ctx, lambda _url: SimpleNamespace(status_code=200, text=response), "t", notes)

    assert result is None
    assert notes == ["talent.com page identity did not match the requested role and location."]


def test_levels_redirect_with_wrong_role_or_region_is_rejected():
    ctx = jobhunter_research.build_context(_spain_job("Senior Software Engineer"))
    original = _fixture("levels_es_senior.html")
    wrong_region = original.replace('"location": "Spain"', '"location": "France"')
    notes = []
    assert salary.fetch_levels(ctx, lambda _url: SimpleNamespace(status_code=200, text=wrong_region), "t", notes) is None
    assert notes == ["Levels.fyi page identity did not match the requested Spain role and level."]

    wrong_role = original.replace('"jobFamily": "Software Engineer"', '"jobFamily": "Software Architect"')
    notes = []
    assert salary.fetch_levels(ctx, lambda _url: SimpleNamespace(status_code=200, text=wrong_role), "t", notes) is None
    assert notes == ["Levels.fyi page identity did not match the requested Spain role and level."]
