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


def test_research_job_uses_web_results_and_warns_on_scam_terms(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_INTERESTED_WEB_RESEARCH", "true")
    calls = []

    def fake_search(query, **kwargs):
        calls.append(query)
        if "salary" in query.lower():
            return [{"title": "Dubai Full Stack Developer salary", "url": "https://salary.example", "snippet": "Monthly salary AED 18k to AED 25k."}]
        return [{"title": "AGAPI complaint check", "url": "https://company.example", "snippet": "No scam report found, but verify contract."}]

    monkeypatch.setattr(flow, "web_search_results", fake_search)

    research = flow.research_job(sample_job())

    assert calls
    assert "Top web result" in research.company_summary
    assert "Salary evidence includes" in research.salary_range
    assert research.salary_sources
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


def test_build_research_brief_is_brief_warn_only_and_includes_salary_target(monkeypatch):
    monkeypatch.setenv("JOBHUNTER_TARGET_SALARY_AED_MONTHLY", "30000")
    research = flow.JobResearch(
        company_summary="AGAPI appears to be a Dubai software/data/security consultancy.",
        legitimacy="Looks plausible; verify contract and compensation before investing time.",
        recruiter="Francisco Cabilatazan — public LinkedIn job poster.",
        salary_range="AED 15k–22k/month likely; ask AED 30k/month as configured target.",
        sources=["https://agapi.ae/", "https://example.com/job"],
        warnings=["Salary not published."],
    )

    message = flow.build_research_brief_message(sample_job(), research)

    assert "Research brief" in message
    assert "Warn only" in message
    assert "Confidence:" in message
    assert "Verified signals:" in message
    assert "Missing / not verified:" in message
    assert "Recommendation:" in message
    assert "AED 30k/month" in message
    assert "Salary not published" in message
    assert len(message) < 3000


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
    assert "Official company website/careers page not confirmed" in message
    assert "Published salary not found" in message
    assert "Low-confidence: verify the employer and official application path" in message


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
