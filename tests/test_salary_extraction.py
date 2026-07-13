import scraper


GOOGLE_MULTI_LOCATION_PAY = (
    "Individual pay depends on skills and experience. "
    "Spain: €88000 - €90500 (EUR) + 42.86% bonus target + equity + benefits "
    "Netherlands: €114000 - €117000 (EUR) + 42.86% bonus target + equity + benefits"
)


def test_extract_salary_rejects_ranges_for_other_locations():
    assert scraper.extract_salary(GOOGLE_MULTI_LOCATION_PAY, "Dubai, United Arab Emirates") == ""


def test_extract_salary_accepts_range_matching_job_location():
    assert scraper.extract_salary(GOOGLE_MULTI_LOCATION_PAY, "Madrid, Spain") == "€88000 - €90500"


def test_extract_salary_accepts_matching_uae_range():
    text = "Dubai: AED 35,000 - AED 45,000 per month."

    assert scraper.extract_salary(text, "Dubai, United Arab Emirates") == "AED 35,000 - AED 45,000 per month"


def test_extract_salary_keeps_unlabelled_range():
    assert scraper.extract_salary("Salary: AED 30,000 monthly", "Dubai") == "Salary: AED 30,000 monthly"


def test_extract_actual_employer_from_known_aggregator_description():
    description = "Job Description About Revolut People deserve more from their money."

    assert scraper.extract_actual_employer("TALENTMATE", description) == "Revolut"


def test_extract_actual_employer_does_not_override_direct_company():
    description = "About Revolut People deserve more from their money."

    assert scraper.extract_actual_employer("Google", description) == "Google"
