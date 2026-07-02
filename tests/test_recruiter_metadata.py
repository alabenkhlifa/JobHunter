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
