import scraper


def test_en_dash_range_with_a_spaced_plus():
    text = "YEARS OF EXPERIENCE\n\n12 – 18 + years of hands-on experience."

    assert scraper.extract_min_experience(text) == 12


def test_em_dash_range():
    assert scraper.extract_min_experience("3 — 7 years of experience in banking") == 3


def test_space_before_the_plus():
    assert scraper.extract_min_experience("18 + years of relevant experience") == 18


def test_years_attached_to_a_field_without_the_word_experience():
    assert scraper.extract_min_experience("8+ years in solution architecture") == 8


def test_experience_label_before_the_figure():
    assert scraper.extract_min_experience("Experience: 9+ Years Job Location: Riyadh, KSA") == 9


def test_curly_apostrophe_before_experience():
    assert scraper.extract_min_experience("10+ years’ experience in architectural design") == 10


def test_company_boast_is_not_a_requirement():
    text = "Learn about us here. 10+ Years of Impact: Started with a simple idea."

    assert scraper.extract_min_experience(text) == -1


def test_employer_tenure_is_not_a_requirement():
    assert scraper.extract_min_experience("Our average employee tenure is over 6 years.") == -1


def test_company_history_is_not_a_requirement():
    text = "We have been in this business for over 26 years and still deliver daily."

    assert scraper.extract_min_experience(text) == -1


def test_years_of_service_benefit_is_not_a_requirement():
    text = "20 days of paid time off, rising by 2 days after 3 years of service."

    assert scraper.extract_min_experience(text) == -1


def test_contract_length_is_not_a_requirement():
    text = "Duration : 1 year of contract extendable. Experience: 6+ years"

    assert scraper.extract_min_experience(text) == 6


def test_takes_the_lowest_requirement_stated():
    text = "8+ years of total experience, with 4+ years in AI/ML Engineering roles"

    assert scraper.extract_min_experience(text) == 4


def test_unstated_stays_unstated():
    assert scraper.extract_min_experience("We are hiring a backend engineer in Dubai.") == -1
