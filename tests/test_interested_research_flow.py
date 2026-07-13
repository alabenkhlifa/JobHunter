import os
import sqlite3
from pathlib import Path

import pytest

import jobhunter_interest_flow as flow


def sample_job(**overrides):
    job = {
        "id": "li-1",
        "title": "Full Stack Developer",
        "company": "AGAPI",
        "location": "Dubai, United Arab Emirates",
        "url": "https://example.com/job",
        "source": "LinkedIn",
        "score": 20,
        "description": "Build Java Spring Boot or Golang microservices with React and Kubernetes.",
        "tech_required": "java, spring boot, golang, microservices, kubernetes, aws",
        "tech_nice_to_have": "observability",
        "min_experience": 4,
        "salary": "",
        "work_model": "on-site",
        "recruiter_name": "Francisco Cabilatazan",
        "recruiter_company": "AGAPI",
        "recruiter_profile_url": "https://linkedin.example/recruiter",
        "company_website": "https://agapi.ae/",
        "credibility_notes": "",
    }
    job.update(overrides)
    return job


def test_salary_target_defaults_to_user_configured_30000(monkeypatch):
    monkeypatch.delenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", raising=False)

    assert flow.target_salary_aed_monthly() == 30000


def test_salary_target_can_be_configured(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "24000")

    assert flow.target_salary_aed_monthly() == 24000


def test_web_research_parser_extracts_result_and_unwraps_redirect():
    html = '''
    <a class="result__a" href="/l/?uddg=https%3A%2F%2Fagapi.ae%2F">AGAPI Information Technology</a>
    <a class="result__snippet">Custom software and data intelligence in Dubai.</a>
    '''

    results = flow.parse_duckduckgo_results(html)

    assert results == [
        {
            "title": "AGAPI Information Technology",
            "url": "https://agapi.ae/",
            "snippet": "Custom software and data intelligence in Dubai.",
        }
    ]


def test_web_search_prefers_firecrawl_when_configured(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427/")
    get_calls = []
    post_calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "success": True,
                "data": [
                    {
                        "title": "Careers | TrueForge FZ-LLC",
                        "url": "https://trueforge.ae/career/",
                        "description": "Solutions Architect / Lead Consultant in Dubai.",
                    }
                ],
            }

    def fake_post(url, **kwargs):
        post_calls.append((url, kwargs))
        return Response()

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        raise AssertionError("DuckDuckGo fallback should not be used when Firecrawl returns results")

    results = flow.web_search_results("TrueForge Dubai", timeout=4, fetcher=fake_get, poster=fake_post)

    assert results == [
        {
            "title": "Careers | TrueForge FZ-LLC",
            "url": "https://trueforge.ae/career/",
            "snippet": "Solutions Architect / Lead Consultant in Dubai.",
        }
    ]
    assert post_calls[0][0] == "http://127.0.0.1:58427/v1/search"
    assert not get_calls


def test_web_search_falls_back_to_duckduckgo_when_firecrawl_empty(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://127.0.0.1:58427")

    class FirecrawlResponse:
        status_code = 200

        def json(self):
            return {"success": True, "data": []}

    class DuckDuckGoResponse:
        status_code = 200
        text = '<a class="result__a" href="https://agapi.ae/">AGAPI</a><a class="result__snippet">Dubai software.</a>'

    results = flow.web_search_results(
        "AGAPI Dubai",
        timeout=4,
        poster=lambda *args, **kwargs: FirecrawlResponse(),
        fetcher=lambda *args, **kwargs: DuckDuckGoResponse(),
    )

    assert results[0]["url"] == "https://agapi.ae/"


def test_research_job_does_not_add_general_salary_results(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "salary" in query.lower():
            return [{"title": "Dubai Full Stack Developer salary", "url": "https://salary.example", "snippet": "Monthly salary AED 18k to AED 25k."}]
        return [{"title": "AGAPI complaint check", "url": "https://company.example", "snippet": "No scam report found, but verify contract."}]

    monkeypatch.setattr(flow, "web_search_results", fake_search)
    monkeypatch.setattr(flow, "fetch_verified_company_pages", lambda *args, **kwargs: [])

    research = flow.research_job(sample_job())

    assert calls
    assert research.company_summary == "AGAPI is a technology company."
    assert not research.company_salary_sources
    assert not research.salary_sources
    assert "No company-specific salary range found." in research.missing_signals
    assert any("scam/fraud/fake/complaint" in warning for warning in research.warnings)


def test_collect_salary_sources_dedupes_multiple_salary_sites(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "gulftalent" in query.lower():
            return [
                {"title": "Senior Solutions Architect Salaries in UAE | GulfTalent", "url": "https://www.gulftalent.com/uae/salaries/senior-solutions-architect", "snippet": "Average AED 29,500 per month, up to AED 45,000."},
                {"title": "Duplicate GulfTalent", "url": "https://www.gulftalent.com/uae/salaries/solution-architect", "snippet": "AED 25,000 per month."},
            ]
        if "payscale" in query.lower():
            return [{"title": "Solutions Architect Salary in UAE | PayScale", "url": "https://www.payscale.com/research/AE/Job=Solutions_Architect/Salary", "snippet": "Average annual salary AED 300,000."}]
        if "glassdoor" in query.lower():
            return [{"title": "Solutions Architect Salaries in Dubai | Glassdoor", "url": "https://www.glassdoor.com/Salaries/dubai-solutions-architect-salary.htm", "snippet": "Average salary AED 28,133."}]
        return []

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    sources = flow.collect_salary_sources("Solutions Architect", "Dubai, UAE", max_sources=4)

    assert [s["source"] for s in sources] == ["GulfTalent", "PayScale", "Glassdoor"]
    assert len({s["url"].split('/')[2] for s in sources}) == 3
    assert len(calls) >= 3


def test_company_salary_search_rejects_snippet_only_false_positive(monkeypatch):
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "careers compensation" in query:
            return [
                {
                    "title": "Careers | TrueForge FZ-LLC",
                    "url": "https://trueforge.ae/career/",
                    "snippet": "Performance bonuses and profit sharing; no salary range published.",
                },
                {
                    "title": "Solutions Architect",
                    "url": "https://my.fa.ru/jobs/123",
                    "snippet": "Related result mentions TrueForge salary.",
                },
            ]
        if "glassdoor.com" in query:
            return [
                {
                    "title": "TrueForge Salaries",
                    "url": "https://www.glassdoor.com/Salary/TrueForge-Salaries.htm",
                    "snippet": "Solutions Architect AED 35k-45k/month.",
                }
            ]
        if "indeed.com" in query:
            return [
                {
                    "title": "Solutions Architect - S&P Global",
                    "url": "https://www.linkedin.com/jobs/view/4327227809/",
                    "snippet": "S&P never asks candidates to pay. Related: TrueForge Dubai.",
                }
            ]
        return []

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    sources = flow.collect_company_salary_sources("TrueForge", "Solutions Architect", "Dubai")

    assert len(calls) == len(flow.company_salary_search_queries("TrueForge", "Solutions Architect", "Dubai"))
    assert [source["source"] for source in sources] == ["Company careers page", "Glassdoor"]
    assert all("S&P" not in source["title"] for source in sources)
    assert all("my.fa.ru" not in source["url"] for source in sources)


def test_compact_company_summary_keeps_useful_verified_details():
    summary = flow._compact_company_summary(
        "TrueForge",
        "Dubai, UAE",
        [
            {
                "title": "TrueForge FZ-LLC",
                "snippet": "Independent technology consulting company focused on legacy modernization, systems integration and software architecture design.",
            },
            {
                "title": "TrueForge | LinkedIn",
                "snippet": "TrueForge is a Dubai-based technology consultancy. 2-10 employees.",
            },
        ],
    )

    assert summary == (
        "TrueForge is a Dubai-based technology consultancy focused on legacy-system modernization, "
        "systems integration, software architecture (2–10 employees)."
    )


def test_compact_company_summary_does_not_use_role_location_as_headquarters():
    summary = flow._compact_company_summary(
        "Google",
        "Dubai, UAE",
        [{"title": "About Google", "snippet": "Google is a global technology company working on cloud platforms and AI."}],
    )

    assert summary == "Google is a global technology company focused on cloud platforms, AI."
    assert "Dubai-based" not in summary


def test_research_resolves_aggregator_employer_and_keeps_posting_company(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "false")
    job = sample_job(
        company="TALENTMATE",
        description="Job Description About Revolut People deserve more from their money. We build financial products.",
        credibility_notes="posted by agency/aggregator",
    )

    research = flow.research_job(job)
    message = flow.build_research_brief_message(job, research)

    assert research.employer_name == "Revolut"
    assert research.posting_company == "TALENTMATE"
    assert "Revolut — Dubai, United Arab Emirates (via TALENTMATE)" in message
    assert "no Revolut pay data" in message


def test_legacy_salary_for_another_country_is_not_displayed():
    job = sample_job(
        company="Google",
        location="Dubai, United Arab Emirates",
        salary="€88000 - €90500",
        description=(
            "Spain: €88000 - €90500 (EUR) + bonus + equity "
            "Netherlands: €114000 - €117000 (EUR) + bonus + equity"
        ),
    )
    research = flow.JobResearch(
        company_summary="Google is a global technology company.",
        legitimacy="No obvious warning.",
        employer_name="Google",
    )

    message = flow.build_research_brief_message(job, research)

    assert "€88000" not in message
    assert "No published range; no Google pay data" in message


def test_fetch_verified_company_pages_uses_only_discovered_official_domain():
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>TrueForge technology consulting company focused on legacy modernization and systems integration.</body></html>"

        def __init__(self, url):
            self.url = url

    def fake_get(url, **kwargs):
        calls.append(url)
        return Response(url)

    pages = flow.fetch_verified_company_pages(
        "TrueForge",
        [{"url": "https://trueforge.ae/career/"}],
        fetcher=fake_get,
    )

    assert set(calls) == {
        "https://trueforge.ae/career/",
        "https://trueforge.ae/careers/",
        "https://trueforge.ae/about/",
        "https://trueforge.ae/",
    }
    assert len(pages) == 3
    assert all(page["url"].startswith("https://trueforge.ae/") for page in pages)


def test_fetch_verified_company_pages_probes_likely_domain_when_search_is_noisy():
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html><body>TrueForge careers compensation open positions in Dubai.</body></html>"

        def __init__(self, url):
            self.url = url

    def fake_get(url, **kwargs):
        calls.append(url)
        if url.startswith("https://trueforge.ae/"):
            return Response(url)
        raise OSError("not reachable")

    pages = flow.fetch_verified_company_pages("TrueForge", [], fetcher=fake_get)

    assert "https://trueforge.ae/career/" in calls
    assert pages
    assert all("trueforge.ae" in page["url"] for page in pages)


def test_build_research_brief_is_concise_and_company_salary_first(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "30000")
    research = flow.JobResearch(
        company_summary="AGAPI appears to be a Dubai software/data/security consultancy.",
        legitimacy="Looks plausible; verify contract and compensation before investing time.",
        recruiter="Francisco Cabilatazan — public LinkedIn job poster.",
        salary_range="No company-specific range found. Market benchmark available.",
        sources=["https://agapi.ae/", "https://example.com/job"],
        warnings=["Salary not published."],
        missing_signals=["Published salary not found."],
        salary_sources=[{"source": "GulfTalent", "snippet": "Average AED 25k/month, up to AED 35k.", "url": "https://salary.example"}],
        company_salary_checks=flow.company_salary_check_labels("AGAPI"),
    )

    message = flow.build_research_brief_message(sample_job(), research)

    assert "Research" in message
    assert "AGAPI appears to be a Dubai software/data/security consultancy." in message
    assert "Pay:" in message
    assert "No published range; no AGAPI pay data" in message
    assert "Glassdoor, Indeed, PayScale, GulfTalent or Levels.fyi" in message
    assert "market" not in message.lower()
    assert "AED 25k" not in message
    assert "Fixed monthly salary and bonus/equity terms" in message
    assert len(message) < 600


def test_research_brief_shows_company_salary_when_found():
    research = flow.JobResearch(
        company_summary="Official company page found.",
        legitimacy="No obvious warning.",
        sources=["https://agapi.ae/"],
        company_salary_sources=[
            {
                "source": "Glassdoor",
                "snippet": "AGAPI Solutions Architect in Dubai AED 35k–45k/month.",
                "url": "https://glassdoor.example/agapi",
            }
        ],
    )

    message = flow.build_research_brief_message(sample_job(), research)

    assert "Glassdoor: AGAPI Solutions Architect in Dubai AED 35k–45k/month." in message
    assert "No AGAPI pay data" not in message


def test_company_salary_for_another_location_is_not_displayed():
    research = flow.JobResearch(
        company_summary="Google is a global technology company.",
        legitimacy="No obvious warning.",
        employer_name="Google",
        company_salary_sources=[
            {
                "source": "Glassdoor",
                "title": "Google Partner Solution Architect Salary in Spain",
                "snippet": "Spain total pay €88,000–€90,500 per year.",
                "url": "https://glassdoor.example/google-spain",
            }
        ],
    )
    job = sample_job(company="Google", location="Dubai, United Arab Emirates", salary="")

    message = flow.build_research_brief_message(job, research)

    assert "€88,000" not in message
    assert "No published range; no Google pay data" in message


def test_official_compensation_note_is_shown_as_benefits_not_salary():
    research = flow.JobResearch(
        company_summary="TrueForge is a Dubai-based technology consultancy.",
        legitimacy="No obvious warning.",
        company_salary_sources=[
            {
                "source": "Company careers page",
                "title": "Careers | TrueForge",
                "snippet": "Compensation includes bonuses and equity. A client saved AED 340k/year.",
                "url": "https://trueforge.ae/career/",
            }
        ],
    )

    message = flow.build_research_brief_message(sample_job(company="TrueForge"), research)

    assert "No published range; no TrueForge pay data" in message
    assert "Benefits:</b> bonus, equity mentioned; no figures." in message
    assert "AED 340k" not in message


def test_low_confidence_research_is_actionable_not_generic(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "30000")
    job = sample_job(
        company="TrueForge",
        title="Solutions Architect / Lead Consultant",
        company_website="",
        recruiter_name="",
        recruiter_profile_url="",
        salary="",
    )

    research = flow.build_default_research(job)
    message = flow.build_research_brief_message(job, research)

    assert research.confidence == "Low"
    assert "quick public web/company-page check" not in message
    assert "No independent company evidence" in message
    assert "No published range; no TrueForge pay data" in message
    assert "Fixed monthly salary and bonus/equity terms" in message


def test_research_brief_keyboard_has_apply_ignore_and_details():
    keyboard = flow.research_brief_keyboard("li-1", "https://example.com/job")

    rows = keyboard["inline_keyboard"]
    flattened = [button for row in rows for button in row]
    assert {button["text"] for button in flattened} >= {"✅ Apply", "🚫 Ignore", "📄 Details"}
    assert {button.get("callback_data") for button in flattened if "callback_data" in button} >= {"apply:li-1", "ignore:li-1"}
    assert any(button.get("url") == "https://example.com/job" for button in flattened)


def test_render_pdf_uses_project_python_and_reports_errors(monkeypatch, tmp_path):
    calls = []

    class Result:
        returncode = 1
        stderr = "Traceback\nmissing dep"
        stdout = ""

    monkeypatch.setattr(flow, "_project_python", lambda: "/repo/.venv/bin/python")
    monkeypatch.setattr(flow.subprocess, "run", lambda cmd, **kwargs: calls.append((cmd, kwargs)) or Result())

    with pytest.raises(RuntimeError, match="missing dep"):
        flow._render_pdf("resume", tmp_path / "resume.json", tmp_path / "resume.pdf")

    assert calls[0][0][0] == "/repo/.venv/bin/python"
    assert calls[0][0][1] == "render_pdf.py"


def test_prepare_application_package_creates_resume_cover_and_records_stage(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    output_dir = tmp_path / "output"
    profile = tmp_path / "master-profile.json"
    profile.write_text(
        '{"name":"Ala Ben Khalifa","email":"jobs@example.com","headline":"Software Architect",'
        '"summary":"7+ years backend/cloud experience.","skills":{"Backend":["Java","Spring Boot","Go"]},'
        '"experience":[],"education":[]}',
        encoding="utf-8",
    )
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, url TEXT, source TEXT, description TEXT, tech_required TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("li-1", "Full Stack Developer", "AGAPI", "Dubai", "https://example.com/job", "LinkedIn", "Build backend services", "java, spring boot"),
    )
    conn.commit(); conn.close()

    package = flow.prepare_application_package(
        "li-1",
        db_path=db,
        profile_path=profile,
        output_dir=output_dir,
        render_pdfs=False,
    )

    assert package.package_dir.exists()
    assert package.resume_json.exists()
    assert package.cover_json.exists()
    assert package.resume_pdf.name.endswith(".pdf")
    assert package.cover_pdf.name.endswith(".pdf")
    conn = sqlite3.connect(db)
    row = conn.execute("SELECT stage, package_path, notes FROM applications WHERE job_id='li-1'").fetchone()
    conn.close()
    assert row[0] == "package_generated"
    assert str(package.package_dir) == row[1]
    assert "Resume and cover letter generated" in row[2]


def test_package_ready_keyboard_requires_final_apply_approval():
    keyboard = flow.package_ready_keyboard("li-1")
    buttons = [button for row in keyboard["inline_keyboard"] for button in row]

    assert {button["text"] for button in buttons} >= {"🚀 Proceed to apply", "⏸ Pause"}
    assert {button.get("callback_data") for button in buttons if "callback_data" in button} >= {"proceed_apply:li-1", "ignore:li-1"}


def test_research_dry_run_reads_job_without_creating_state_tables(tmp_path, monkeypatch):
    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT, salary TEXT, url TEXT)")
    conn.execute(
        "INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)",
        ("li-1", "Solutions Architect", "TrueForge", "Dubai", "", "https://example.com/job"),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(
        flow,
        "research_job",
        lambda job: flow.JobResearch(company_summary="", legitimacy=""),
    )

    message = flow.render_research_dry_run("li-1", db_path=db)

    assert "No published range; no TrueForge pay data" in message
    conn = sqlite3.connect(db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert tables == {"jobs"}
