import scraper


def test_normalize_recruiter_metadata_keeps_known_fields_and_defaults():
    metadata = scraper.normalize_recruiter_metadata(
        {
            "recruiter_name": " Jane Recruiter ",
            "recruiter_company": " Hiring Agency ",
            "recruiter_profile_url": " https://linkedin.com/in/jane ",
            "company_website": " https://example.com/careers ",
            "credibility_notes": " verified company domain ",
        }
    )

    assert metadata == {
        "recruiter_name": "Jane Recruiter",
        "recruiter_company": "Hiring Agency",
        "recruiter_profile_url": "https://linkedin.com/in/jane",
        "company_website": "https://example.com/careers",
        "credibility_notes": "verified company domain",
    }


def test_format_job_message_includes_recruiter_and_credibility_notes():
    job = {
        "id": "li-1",
        "title": "Lead Backend Engineer",
        "company": "ExampleCo",
        "location": "Dubai",
        "url": "https://example.com/job",
        "source": "LinkedIn",
        "score": 25,
        "date_posted": "",
        "tech_required": "Java, AWS",
        "tech_nice_to_have": "Kubernetes",
        "min_experience": 6,
        "salary": "AED 40k",
        "work_model": "hybrid",
        "score_breakdown": "backend +3",
        "recruiter_name": "Jane Recruiter",
        "recruiter_company": "Hiring Agency",
        "recruiter_profile_url": "https://linkedin.com/in/jane",
        "company_website": "https://example.com",
        "credibility_notes": "verified company domain",
    }

    message = scraper.format_job_message(job)

    assert "Recruiter: Jane Recruiter" in message
    assert "Hiring Agency" in message
    assert "https://linkedin.com/in/jane" in message
    assert "Company site: https://example.com" in message
    assert "Credibility: verified company domain" in message


def test_assess_company_recruiter_credibility_rewards_direct_traceable_roles():
    score, notes = scraper.assess_company_recruiter_credibility(
        {
            "company": "ExampleCo",
            "source": "LinkedIn",
            "recruiter_company": "ExampleCo",
            "recruiter_profile_url": "https://linkedin.com/in/jane",
            "company_website": "https://example.com/careers",
        }
    )

    assert score == 4
    assert notes == [
        "company website present",
        "recruiter profile present",
        "recruiter appears internal",
    ]


def test_assess_company_recruiter_credibility_penalizes_agencies_and_aggregators():
    score, notes = scraper.assess_company_recruiter_credibility(
        {
            "company": "TALENTMATE",
            "source": "Foundit",
        }
    )

    assert score == -3
    assert "posted by agency/aggregator" in notes
    assert "aggregated job-board source" in notes


def test_score_job_scores_an_agency_posting_down_on_the_employer_dimension():
    job = {
        "title": "Lead Backend Engineer",
        "location": "Dubai, United Arab Emirates",
        "tech_required": "Java AWS",
        "tech_nice_to_have": "",
        "company": "TALENTMATE",
        "source": "Foundit",
    }

    score, breakdown = scraper.score_job(job)
    direct_score, _ = scraper.score_job({**job, "company": "ExampleCo"})

    assert any(item.startswith("employer 0.30x") for item in breakdown)
    assert score < direct_score
