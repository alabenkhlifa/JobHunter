import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import jobhunter_interest_flow as flow
import jobhunter_research
from jobhunter_research import roles, salary, search

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
BENCHMARKS = {
    "schema_version": 1,
    "currency": "EUR",
    "period": "year",
    "entries": [
        {"country": "es", "city": "*", "role_family": "senior_software_engineer", "level": "senior",
         "p25": 41000, "p50": 55000, "p75": 60000,
         "sources": [{"name": "PayScale", "url": "https://www.payscale.com/x", "retrieved": "2026-10-15"}]},
        {"country": "es", "city": "madrid", "role_family": "senior_software_engineer", "level": "senior",
         "p25": 50000, "p50": 58000, "p75": 68000,
         "sources": [{"name": "Glassdoor", "url": "https://www.glassdoor.com/x", "retrieved": "2026-10-15"}]},
        {"country": "es", "city": "*", "role_family": "lead_software_engineer", "level": "lead",
         "p25": 55000, "p50": 65000, "p75": 75000, "sources": [{"name": "Manfred", "url": "https://m.example"}]},
    ],
}


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


class Response:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code


@pytest.fixture(autouse=True)
def benchmark_file(tmp_path, monkeypatch):
    path = tmp_path / "salary_benchmarks.json"
    path.write_text(json.dumps(BENCHMARKS), encoding="utf-8")
    monkeypatch.setattr(salary, "BENCHMARKS_PATH", path)
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    return path


def spain_job(**overrides):
    job = {
        "id": "sdj-1",
        "title": "Senior Backend Engineer (Java)",
        "company": "Cabify",
        "location": "Madrid, Spain",
        "url": "https://spaindevjobs.com/jobs/1",
        "source": "LinkedIn",
        "description": "Build Java services for our mobility platform.",
        "salary": "",
        "company_website": "https://cabify.com/",
        "min_experience": 5,
    }
    job.update(overrides)
    return job


# ----------------------------------------------------------------- parsing


@pytest.mark.parametrize(("text", "expected"), [
    ("45.000 - 55.000 € brutos anuales", (45000, 55000, "EUR", "year")),
    ("Salary: 50.000€ - 60.000€ per year", (50000, 60000, "EUR", "year")),
    ("EUR 50,000 - 60,000 per year", (50000, 60000, "EUR", "year")),
    ("55.000 EUR", (55000, 55000, "EUR", None)),
    ("60000-70000 EUR gross/year", (60000, 70000, "EUR", "year")),
    ("€50k–60k", (50000, 60000, "EUR", None)),
    ("€70k–90k / year", (70000, 90000, "EUR", "year")),
    ("3.500 €/mes", (3500, 3500, "EUR", "month")),
    ("CHF 120,000 per year", (120000, 120000, "CHF", "year")),
    ("Salario: 45.000,00 € anuales", (45000, 45000, "EUR", "year")),
    ("€80,000 - €90,000 a year", (80000, 90000, "EUR", "year")),
    ("[€75-90K]", (75000, 90000, "EUR", None)),
])
def test_parse_salary_text_reads_european_and_english_formats(text, expected):
    parsed = salary.parse_salary_text(text)

    assert (parsed["low"], parsed["high"], parsed["currency"], parsed["period"]) == expected


def test_parse_salary_text_reads_fourteen_payments():
    parsed = salary.parse_salary_text("3.500 €/mes en 14 pagas")

    assert parsed["payments"] == 14
    assert salary.to_period(parsed["low"], parsed["period"], "year", payments=parsed["payments"]) == 49000


def test_parse_salary_text_rejects_text_without_amounts():
    assert salary.parse_salary_text("Competitive salary") is None
    assert salary.parse_salary_text("€9.61 an hour") is None


@pytest.mark.parametrize(("token", "value"), [
    ("45.000", 45000), ("45,000", 45000), ("45.000,00", 45000), ("58.6K", 58600), ("1.04M", 1040000),
    ("88000", 88000), ("31,2k", 31200),
])
def test_parse_number(token, value):
    assert salary.parse_number(token) == value


def test_ask_from_range_uses_quantile_and_rounding():
    assert salary.ask_from_range(52000, 66000) == 62000
    assert jobhunter_research.ask_from_range(48500, 91800, quantile=0.5, round_to=500) == 70000
    assert salary.ask_from_range(60000, None) == 60000
    assert salary.ask_from_range(None, None) is None


# ----------------------------------------------------------------- direct pages


def test_levels_page_parser_converts_usd_percentiles_to_eur():
    data = salary.parse_levels_page(fixture("levels_es_senior.html"))

    assert data["currency"] == "EUR"
    assert (data["p25"], data["p50"], data["p75"]) == (58582, 72961, 91785)
    assert data["count"] == 836


def test_levels_page_parser_falls_back_to_visible_figures():
    html = fixture("levels_es_senior.html").split('<script id="__NEXT_DATA__"')[0]

    data = salary.parse_levels_page(html)

    assert (data["p25"], data["p50"], data["p75"]) == (58600, 72961, 91800)


def test_talent_page_parser_reads_low_median_high():
    data = salary.parse_talent_page(fixture("talent_es_valencia_lead.html"))

    assert data == {"median": 45760, "low": 37500, "high": 63440, "count": 10000}


def test_levels_and_talent_fetchers_build_market_sources():
    ctx = jobhunter_research.build_context(spain_job())
    requested = []

    def fetch(url, **kwargs):
        requested.append(url)
        return Response(fixture("levels_es_senior.html") if "levels.fyi" in url else fixture("talent_es_madrid_senior.html"))

    notes = []
    levels = salary.fetch_levels(ctx, fetch, "2026-10-15T12:00:00Z", notes)
    talent = salary.fetch_talent(ctx, fetch, "2026-10-15T12:00:00Z", notes)

    assert requested == [
        "https://www.levels.fyi/t/software-engineer/levels/senior/locations/spain",
        "https://es.talent.com/salary?job=senior+backend+engineer&location=madrid",
    ]
    assert levels["kind"] == "market" and levels["name"] == "Levels.fyi Spain"
    assert salary.summarize_sources([levels], kind="market", currency="EUR", period="year")["low"] == 58582
    assert talent["name"] == "talent.com Madrid"
    summary = salary.summarize_sources([talent], kind="market", currency="EUR", period="year")
    assert (summary["low"], summary["high"]) == (33000, 46000)
    assert notes == []


def test_direct_fetch_failures_become_notes():
    ctx = jobhunter_research.build_context(spain_job())
    notes = []

    assert salary.fetch_levels(ctx, lambda url: Response("", 403), "t", notes) is None
    assert salary.fetch_talent(ctx, lambda url: (_ for _ in ()).throw(OSError("down")), "t", notes) is None
    assert notes == ["Levels.fyi returned HTTP 403.", "talent.com fetch failed: OSError."]


def test_direct_fetchers_only_run_for_spain():
    ctx = jobhunter_research.build_context(spain_job(location="Dubai, United Arab Emirates"))

    assert salary.fetch_levels(ctx, lambda url: pytest.fail("no fetch"), "t", []) is None
    assert salary.fetch_talent(ctx, lambda url: pytest.fail("no fetch"), "t", []) is None


# ----------------------------------------------------------------- search evidence


def test_market_sources_from_brave_keep_spanish_euro_senior_results():
    ctx = jobhunter_research.build_context(spain_job(title="Senior Software Engineer"))
    results = search.parse_brave_results(fixture("brave_market_madrid_senior.html"))

    sources = salary.market_sources_from_results(ctx, results, "t")

    urls = [source["url"] for source in sources]
    assert all("madrid-ia" not in url and "Madrid--SK" not in url for url in urls)  # Iowa / Canada in USD
    assert all("reddit" not in url and "eurotoptech" not in url for url in urls)  # not salary sites
    assert {source["name"] for source in sources} >= {"PayScale Madrid", "Glassdoor Madrid", "Levels.fyi Madrid"}
    summary = salary.summarize_sources(sources, kind="market", currency="EUR", period="year")
    assert 55000 <= summary["low"] <= 65000
    assert 80000 <= summary["high"] <= 97000
    assert summary["agreeing"] >= 2


def test_company_sources_need_the_employer_role_and_location():
    ctx = jobhunter_research.build_context(spain_job(title="Senior Software Engineer"))
    results = search.parse_brave_results(fixture("brave_company_salary_cabify.html"))

    sources = salary.company_sources_from_results(ctx, results, "t")

    assert [source["url"].split("/")[2] for source in sources] == ["www.glassdoor.com"]
    assert sources[0]["kind"] == "company"
    assert sources[0]["name"] == "Glassdoor: Cabify"
    summary = salary.summarize_sources(sources, kind="company", currency="EUR", period="year")
    assert (summary["low"], summary["high"]) == (65000, 94700)  # average, then "up to" (90th percentile)


def test_market_query_and_company_query_stay_within_budget():
    ctx = jobhunter_research.build_context(spain_job(title="Senior Software Engineer"))

    assert salary.market_query(ctx) == "Senior Software Engineer salary Madrid Spain"
    assert salary.company_query(ctx) == '"Cabify" Senior Software Engineer salary Madrid'


# ----------------------------------------------------------------- benchmarks


def test_benchmark_prefers_exact_city_then_star_then_nearest_level():
    assert salary.benchmark_entry("es", "Madrid", "senior_software_engineer", "senior", BENCHMARKS)["p25"] == 50000
    assert salary.benchmark_entry("es", "Valencia", "senior_software_engineer", "senior", BENCHMARKS)["p25"] == 41000
    assert salary.benchmark_entry("es", "", "lead_software_engineer", "staff", BENCHMARKS)["p75"] == 75000
    assert salary.benchmark_entry("es", "Madrid", "engineering_manager", "lead", BENCHMARKS) is None


def test_missing_or_empty_benchmark_file_is_tolerated(tmp_path):
    assert salary.load_benchmarks(tmp_path / "missing.json") == {"entries": []}
    empty = tmp_path / "empty.json"
    empty.write_text("", encoding="utf-8")
    assert salary.load_benchmarks(empty) == {"entries": []}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert salary.benchmark_entry("es", "madrid", "senior_software_engineer", "senior", salary.load_benchmarks(broken)) is None


# ----------------------------------------------------------------- research()


def offline(query, **kwargs):
    return []


def no_fetch(url, **kwargs):
    return None


def test_published_board_salary_wins_and_drives_the_ask():
    job = spain_job(source="Spain Dev Jobs", salary="€70k–90k / year", description="Build services.")

    result = jobhunter_research.research(job, search=offline, fetch=no_fetch, now=NOW)

    pay = result.to_dict()["salary"]
    assert pay == {"low": 70000, "high": 90000, "currency": "EUR", "period": "year", "basis": "published",
                   "ask_amount": 85000, "published_text": "€70k–90k / year", "confidence": "high"}
    assert result.sources[0]["kind"] == "published" and result.sources[0]["name"] == "Spain Dev Jobs"


def test_monthly_published_salary_with_fourteen_payments_is_annualised():
    job = spain_job(
        salary="3.500 €/mes",
        description="Salario: 3.500 €/mes en 14 pagas. Contrato indefinido.",
    )

    pay = jobhunter_research.research(job, search=offline, fetch=no_fetch, now=NOW).salary

    assert (pay["low"], pay["high"], pay["period"], pay["basis"]) == (49000, 49000, "year", "published")


def test_market_research_combines_direct_pages_and_brave_snippets():
    queries = []

    def fake_search(query, **kwargs):
        queries.append(query)
        if query.startswith('"Cabify" company'):
            return search.parse_brave_results(fixture("brave_company_cabify.html"))
        if query.startswith('"Cabify"'):
            return []
        return search.parse_brave_results(fixture("brave_market_madrid_senior.html"))

    def fetch(url, **kwargs):
        if "levels.fyi" in url:
            return Response(fixture("levels_es_senior.html"))
        if "talent.com" in url:
            return Response(fixture("talent_es_madrid_senior.html"))
        return None

    result = jobhunter_research.research(spain_job(title="Senior Software Engineer", company_website=""),
                                         search=fake_search, fetch=fetch, now=NOW)

    data = result.to_dict()
    assert len(queries) <= 4
    assert data["salary"]["basis"] == "market"
    assert data["salary"]["confidence"] == "high"
    assert data["salary"]["currency"] == "EUR" and data["salary"]["period"] == "year"
    assert data["salary"]["low"] < data["salary"]["ask_amount"] <= data["salary"]["high"]
    assert data["salary"]["ask_amount"] % 1000 == 0
    names = {source["name"] for source in data["sources"]}
    assert {"Levels.fyi Spain", "talent.com Madrid"} <= names
    assert "Levels.fyi Madrid" not in names  # direct page replaces Levels snippets
    lines = jobhunter_research.format_salary_lines(result)
    assert lines[0].startswith("Market: €") and "Levels.fyi Spain" in lines[0]
    assert lines[-1].startswith("Ask: €") and "75th pct of market range" in lines[-1]
    assert not any(line.startswith(("Published:", "Estimate:")) for line in lines)


def test_company_figures_take_precedence_over_market():
    def fake_search(query, **kwargs):
        if "salary" in query and query.startswith('"Cabify"'):
            return search.parse_brave_results(fixture("brave_company_salary_cabify.html"))
        if "salary" in query:
            return search.parse_brave_results(fixture("brave_market_madrid_senior.html"))
        return []

    result = jobhunter_research.research(spain_job(title="Senior Software Engineer"), search=fake_search,
                                         fetch=no_fetch, now=NOW)

    assert result.salary["basis"] == "company"
    assert result.salary["confidence"] == "medium"
    lines = jobhunter_research.format_salary_lines(result)
    assert lines[0] == "Company: €65k–€95k/year (Glassdoor: Cabify)"
    assert lines[1].startswith("Market: €")


def test_estimate_is_used_only_without_live_figures_and_is_labelled():
    result = jobhunter_research.research(spain_job(title="Senior Software Engineer"), search=offline,
                                         fetch=no_fetch, now=NOW)

    pay = result.salary
    assert (pay["basis"], pay["low"], pay["high"], pay["confidence"]) == ("estimate", 50000, 68000, "low")
    assert pay["ask_amount"] == 64000
    assert jobhunter_research.format_salary_lines(result) == [
        "Estimate: €50k–€68k/year (Benchmark (Glassdoor); estimate, not employer data)",
        "Ask: €64,000/year (75th pct of estimate range)",
    ]


def test_no_data_anywhere_gives_basis_none():
    job = spain_job(title="Head of Marketing", company="Nobody")

    result = jobhunter_research.research(job, search=offline, fetch=no_fetch, now=NOW)

    assert result.salary["basis"] == "none"
    assert result.salary["ask_amount"] is None
    assert result.salary["confidence"] == "low"
    assert jobhunter_research.format_salary_lines(result) == []


def test_spain_has_no_configured_target_or_floor():
    job = spain_job()

    assert flow.salary_market(job) == "spain"
    assert flow.salary_target(job) is None
    assert flow.salary_market(spain_job(location="València, Spain")) == "spain"
    assert flow.salary_market(spain_job(location="Remote, Spain")) == "spain"


def test_uae_keeps_its_configured_target_as_the_ask(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", raising=False)
    job = spain_job(location="Dubai, United Arab Emirates", title="Software Architect", company="AGAPI")

    result = jobhunter_research.research(job, search=offline, fetch=no_fetch, now=NOW)

    assert result.salary["basis"] == "estimate"
    assert result.salary["ask_amount"] == 30000
    assert jobhunter_research.format_salary_lines(result) == [
        "Estimate: AED 22k–30k/month (Configured target band; estimate, not employer data)",
        "Ask: AED 30,000/month (configured target)",
    ]


def test_format_salary_lines_escapes_html():
    result = jobhunter_research.research(
        spain_job(source="SpainJobs.io", salary="€60,000 - €70,000 a year <b>"), search=offline, fetch=no_fetch, now=NOW
    )

    lines = jobhunter_research.format_salary_lines(result)

    assert lines[0] == "Published: €60,000 - €70,000 a year &lt;b&gt; (SpainJobs.io)"


def test_research_never_raises_when_search_fails():
    def broken(query, **kwargs):
        raise RuntimeError("boom")

    result = jobhunter_research.research(spain_job(), search=broken, fetch=no_fetch, now=NOW)

    assert result.salary["basis"] == "estimate"
    assert any("Search failed" in note for note in result.notes)


def test_to_dict_has_exactly_the_contract_shape():
    data = jobhunter_research.research(spain_job(), search=offline, fetch=no_fetch, now=NOW).to_dict()

    assert set(data) == {"job_id", "version", "researched_at", "salary", "company", "sources", "notes"}
    assert set(data["salary"]) == {"low", "high", "currency", "period", "basis", "ask_amount", "published_text",
                                   "confidence"}
    assert set(data["company"]) == {"name", "website", "summary", "industry", "size", "hq", "sources"}
    assert all(set(source) == {"kind", "name", "url", "snippet", "retrieved_at"} for source in data["sources"])
    assert data["researched_at"] == "2026-10-15T12:00:00Z"
    assert data["version"] == 1
    json.dumps(data)


# ----------------------------------------------------------------- roles


@pytest.mark.parametrize(("title", "experience", "family", "level"), [
    ("Senior Software Engineer", None, "senior_software_engineer", "senior"),
    ("Senior Backend Engineer (Java)", None, "senior_software_engineer", "senior"),
    ("Backend Developer", 6, "senior_software_engineer", "senior"),
    ("Backend Developer", 2, "backend_engineer", "mid"),
    ("Lead Java Developer", None, "lead_software_engineer", "lead"),
    ("Tech Lead - Plataforma", None, "lead_software_engineer", "lead"),
    ("Staff Engineer", None, "lead_software_engineer", "staff"),
    ("Ingeniero de Software Sénior", None, "senior_software_engineer", "senior"),
    ("Arquitecto de Software", None, "software_architect", "mid"),
    ("Solutions Architect", None, "software_architect", "mid"),
    ("Engineering Manager", None, "engineering_manager", "lead"),
    ("Responsable de Ingeniería", None, "engineering_manager", "lead"),
    ("Head of Marketing", None, None, "mid"),
])
def test_role_family_and_level(title, experience, family, level):
    assert roles.role_family(title, experience) == family
    assert roles.role_level(title, experience) == level


def test_search_role_phrases():
    assert roles.search_role("Senior Backend Engineer (Java)") == "Senior Backend Engineer"
    assert roles.search_role("Lead Java Developer") == "Lead Software Engineer"
    assert roles.search_role("Solutions Architect / Lead Consultant") == "Solutions Architect"
    assert roles.levels_fyi_path("engineering_manager", "lead") == "software-engineering-manager/locations/spain"
    assert roles.levels_fyi_path("backend_engineer", "mid") == "software-engineer/locations/spain"


def test_backend_roles_accept_software_engineer_salary_pages():
    ctx = jobhunter_research.build_context(spain_job(title="Senior Backend Engineer (Java)"))
    results = search.parse_brave_results(fixture("brave_company_salary_cabify.html"))

    sources = salary.company_sources_from_results(ctx, results, "t")

    assert sources and sources[0]["url"].startswith("https://www.glassdoor.com/Salary/Cabify-Senior-Software-Engineer")


def test_ask_takes_the_higher_of_company_and_market_evidence():
    from types import SimpleNamespace
    ctx = SimpleNamespace(job={"location": "Valencia, Spain"}, currency="EUR", period="year")
    sources = [
        {"kind": "company", "name": "Levels.fyi: Miratech", "url": "https://www.levels.fyi/companies/miratech/salaries/software-engineer/locations/spain",
         "snippet": "Software Engineer, Spain: low €37,300 · median €38,500 · high €39,900 per year"},
        {"kind": "market", "name": "Levels.fyi Spain", "url": "https://www.levels.fyi/t/software-engineer/levels/senior/locations/spain",
         "snippet": "Senior Software Engineer, Spain: low €58,515 · median €72,877 · high €91,680 per year"},
    ]
    result = salary.decide(ctx, None, sources)
    # The displayed range stays company-specific; the ask does not undersell against the market.
    assert result["basis"] == "company" and result["low"] == 37300 and result["high"] == 39900
    assert result["ask_amount"] == salary.ask_from_range(58515, 91680)
    assert result["ask_amount"] > 39000

    company_only = salary.decide(ctx, None, sources[:1])
    assert company_only["ask_amount"] == salary.ask_from_range(37300, 39900)
