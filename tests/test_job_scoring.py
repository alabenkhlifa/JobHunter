import job_scoring


def test_normalise_title_strips_seniority_words():
    assert job_scoring.normalise_title("Senior DevOps Manager") == "devops"
    assert job_scoring.normalise_title("Lead DevOps Engineer") == "devops engineer"
    assert job_scoring.normalise_title("Head of QA") == "qa"


def test_blocked_title_catches_every_devops_wording():
    for title in ("DevOps Manager", "Lead DevOps", "Senior DevOps Engineer", "DevOps"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_keeps_the_titles_he_wants():
    for title in ("Software Architect", "Cloud Architect", "Tech Lead",
                  "Senior Backend Engineer", "Lead Software Engineer",
                  "Senior Software Architect", "Solutions Architect"):
        assert job_scoring.blocked_title({"title": title}) is None, title


def test_blocked_title_honours_the_four_literal_blocks():
    for title in ("Staff Engineer", "Senior Architect", "Senior Cloud Architect",
                  "Senior Lead Software Engineer"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_is_not_fooled_by_a_hyphen():
    assert job_scoring.blocked_title({"title": "Front-End Developer"})
    assert job_scoring.blocked_title({"title": "Frontend Developer"})


def test_normalise_title_strips_abbreviated_seniority():
    assert job_scoring.normalise_title("Sr. Software Architect") == "software architect"


def test_blocked_title_catches_every_frontend_spelling():
    for title in ("Frontend Developer", "Front-End Developer", "Front End Developer"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_catches_suffixed_forms_of_blocked_families():
    for title in ("Manual Tester", "Manual Testing Specialist", "Networking Engineer"):
        assert job_scoring.blocked_title({"title": title}), title


def test_blocked_title_says_which_rule_rejected_it():
    assert job_scoring.blocked_title({"title": "DevOps Manager"}) == "blocked role family: devops"
    assert job_scoring.blocked_title({"title": "Staff Engineer"}) == "blocked title: staff engineer"


UAE = ("dubai", "abu dhabi", "jeddah", "switzerland")


def job(**over):
    base = {
        "title": "Software Architect", "company": "Acme",
        "location": "Dubai, United Arab Emirates", "description": "Java and Spring Boot.",
        "min_experience": 6, "tech_required": "java, spring boot", "tech_nice_to_have": "",
        "date_posted": "", "company_website": "https://acme.example", "recruiter_company": "",
    }
    base.update(over)
    return base


def test_knockout_rejects_a_location_outside_the_markets():
    assert job_scoring.knockout(job(location="Cairo, Egypt"), allowed_locations=UAE)


def test_knockout_rejects_junior_titles_outright():
    assert job_scoring.knockout(job(title="Junior Software Architect"), allowed_locations=UAE)
    assert job_scoring.knockout(job(title="Graduate Software Engineer"), allowed_locations=UAE)


def test_knockout_matches_junior_words_whole_not_as_substrings():
    assert job_scoring.knockout(job(title="Entry-Level Software Engineer"), allowed_locations=UAE)
    assert job_scoring.knockout(job(title="International Solutions Architect"), allowed_locations=UAE) is None
    assert job_scoring.knockout(job(title="Internal Tools Architect"), allowed_locations=UAE) is None


def test_knockout_rejects_more_experience_than_he_has():
    assert job_scoring.knockout(job(min_experience=12), allowed_locations=UAE)
    assert job_scoring.knockout(job(min_experience=8), allowed_locations=UAE) is None


def test_knockout_rejects_an_explicit_refusal_to_sponsor():
    text = "We will not sponsor visas for this role."
    assert job_scoring.knockout(job(description=text), allowed_locations=UAE)


def test_knockout_rejects_a_duplicate_of_a_job_already_seen():
    first = job(title="Senior Technical Architect", company="Inception")
    key = job_scoring.duplicate_key(first)
    assert job_scoring.knockout(first, allowed_locations=UAE, seen_keys=frozenset({key}))


def test_duplicate_key_ignores_seniority_and_case():
    a = job_scoring.duplicate_key(job(title="Senior Technical Architect", company="Inception"))
    b = job_scoring.duplicate_key(job(title="  technical   architect ", company="INCEPTION"))
    assert a == b


def test_knockout_passes_a_job_he_wants():
    assert job_scoring.knockout(job(), allowed_locations=UAE) is None


def test_duplicate_key_ignores_the_german_gender_marker():
    plain = job_scoring.duplicate_key(job(title="Software Architect", company="Acme"))
    for title in ("Software Architect (m/w/d)", "Software Architect (w/m/d)",
                  "Software Architect (m/f/x)", "Software Architect m/w/d"):
        assert job_scoring.duplicate_key(job(title=title, company="Acme")) == plain, title
