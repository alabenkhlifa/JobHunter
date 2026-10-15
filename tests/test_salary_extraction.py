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


def test_extract_salary_rejects_flight_allowance_as_pay():
    description = "Competitive compensation includes an annual flight allowance of 2,500 AED."

    assert scraper.extract_salary(description, "Dubai") == ""


def test_extract_salary_prefers_base_pay_over_flight_allowance():
    description = "Base salary AED 28,000 per month; annual flight allowance 2,500 AED."

    assert scraper.extract_salary(description, "Dubai") == "salary AED 28,000 per month"


def test_extract_actual_employer_from_known_aggregator_description():
    description = "Job Description About Revolut People deserve more from their money."

    assert scraper.extract_actual_employer("TALENTMATE", description) == "Revolut"


def test_extract_actual_employer_does_not_override_direct_company():
    description = "About Revolut People deserve more from their money."

    assert scraper.extract_actual_employer("Google", description) == "Google"


import pytest


@pytest.mark.parametrize(("text", "location", "expected"), [
    ("Salario: 45.000 - 55.000 € brutos anuales.", "Madrid, Spain", "Salario: 45.000 - 55.000 € brutos anuales"),
    ("Salary: 50.000€ - 60.000€ per year", "Madrid, Spain", "Salary: 50.000€ - 60.000€ per year"),
    ("We pay EUR 50,000 - 60,000 per year.", "Barcelona, Spain", "pay EUR 50,000 - 60,000 per year"),
    ("Sueldo: 55.000 EUR", "Valencia, Spain", "Sueldo: 55.000 EUR"),
    ("Compensation 60000-70000 EUR gross/year", "Madrid", "Compensation 60000-70000 EUR gross/year"),
    ("Salary range €50k–60k depending on experience", "Madrid", "Salary range €50k–60k"),
    ("Retribución: 3.500 €/mes en 14 pagas", "Madrid", "Retribución: 3.500 €/mes"),
    ("Base salary CHF 120,000 per year plus bonus", "Zurich, Switzerland", "salary CHF 120,000 per year"),
    ("Salario bruto anual de 48.000 euros", "Valencia", "48.000 euros"),
])
def test_extract_salary_reads_european_formats(text, location, expected):
    assert scraper.extract_salary(text, location) == expected


def test_extract_salary_ignores_funding_amounts_without_pay_context():
    assert scraper.extract_salary("We raised 55.000.000 EUR from investors in 2024.", "Madrid") == ""


def test_extract_salary_ignores_euro_word_prefix_inside_europe():
    assert scraper.extract_salary("Founded 2015 Europe-wide team; salary competitive.", "Madrid") == ""


def test_valencia_is_part_of_the_spain_salary_group():
    assert scraper._salary_location_group("València, Spain") == "spain"
    assert scraper._salary_location_group("Valencia") == "spain"
    text = "Valencia: €55,000 - €65,000 per year. Lisbon: €40,000 - €45,000 per year."
    assert scraper.extract_salary(text, "Madrid, Spain") == "€55,000 - €65,000 per year"
