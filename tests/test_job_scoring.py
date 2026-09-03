from datetime import datetime, timedelta, timezone

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


def test_knockout_reads_a_curly_apostrophe_as_a_refusal():
    straight = job(description="We won't sponsor visas for this role.")
    curly = job(description="We won’t sponsor visas for this role.")
    assert job_scoring.knockout(straight, allowed_locations=UAE)
    assert job_scoring.knockout(curly, allowed_locations=UAE)


def test_knockout_rejects_interns_in_the_plural_too():
    assert job_scoring.knockout(job(title="Software Engineering Intern"), allowed_locations=UAE)
    assert job_scoring.knockout(job(title="Software Engineering Interns"), allowed_locations=UAE)


def test_stack_fit_rewards_his_core_stack_being_required():
    core = job(tech_required="java, kotlin, spring boot, microservices, aws")
    thin = job(tech_required="php, wordpress")
    assert job_scoring.stack_fit(core) > 0.8
    assert job_scoring.stack_fit(thin) < 0.2


def test_stack_fit_counts_nice_to_have_for_less_than_required():
    required = job(tech_required="java, spring boot", tech_nice_to_have="")
    optional = job(tech_required="", tech_nice_to_have="java, spring boot")
    assert job_scoring.stack_fit(required) > job_scoring.stack_fit(optional) > 0


def test_stack_fit_is_never_above_one():
    everything = job(tech_required="java, kotlin, spring boot, spring, microservices, "
                                   "aws, azure, terraform, kubernetes, docker, "
                                   "postgresql, mongodb, redis, kafka, rest")
    assert job_scoring.stack_fit(everything) <= 1.0


def test_role_fit_ranks_the_families_the_way_he_does():
    architect = job(title="Software Architect")
    senior_backend = job(title="Senior Backend Engineer")
    manager = job(title="Engineering Manager")
    fullstack = job(title="Full Stack Architect")
    generic = job(title="software engineer")
    assert job_scoring.role_fit(architect) == 1.0
    assert job_scoring.role_fit(senior_backend) == 0.8
    assert job_scoring.role_fit(manager) == 0.5
    assert job_scoring.role_fit(fullstack) == 0.4
    assert job_scoring.role_fit(generic) == 0.3


def test_role_fit_reads_full_stack_before_architect():
    # "Full Stack Architect" contains "architect" but half the job is frontend.
    assert job_scoring.role_fit(job(title="Full Stack Architect")) == 0.4


def test_role_fit_does_not_find_cto_inside_another_word():
    assert job_scoring.role_fit(job(title="Customer Success Director")) < 0.5
    assert job_scoring.role_fit(job(title="CTO")) == 0.5


def test_role_fit_still_reads_the_titles_he_wants():
    assert job_scoring.role_fit(job(title="Software Architect")) == 1.0
    assert job_scoring.role_fit(job(title="Platform Architect")) == 1.0
    assert job_scoring.role_fit(job(title="Full Stack Architect")) == 0.4


def test_role_fit_reads_architecture_ownership_as_architect_work():
    # 16 of the 24 corpus titles that say "architecture" without "architect"
    # own the architecture, so the word alone earns the architect rung.
    assert job_scoring.role_fit(job(title="Head of Architecture")) == 1.0
    assert job_scoring.role_fit(job(title="Cloud Solution Architecture")) == 1.0
    assert job_scoring.role_fit(job(title="Estimation Engineer - Architectural Opening Solutions")) == 0.3


def test_role_fit_reads_every_backend_spelling():
    for title in ("Backend Engineer", "Back-End Engineer", "Back End Developer"):
        assert job_scoring.role_fit(job(title=title)) == 0.8, title


def test_stack_fit_treats_k8s_as_kubernetes():
    spelled = job(tech_required="java, spring boot, kubernetes")
    short = job(tech_required="java, spring boot, k8s")
    assert job_scoring.stack_fit(short) == job_scoring.stack_fit(spelled)


def test_seniority_fit_peaks_in_his_band():
    assert job_scoring.seniority_fit(job(min_experience=6)) == 1.0
    assert job_scoring.seniority_fit(job(min_experience=3)) == 0.6
    assert job_scoring.seniority_fit(job(min_experience=-1)) == 0.6


def test_employer_fit_prefers_a_direct_employer_over_an_agency():
    direct = job(company="Acme", company_website="https://acme.example", recruiter_company="")
    agency = job(company="Acme", recruiter_company="Dicetek LLC")
    # No scraper fills company_website today, so its absence says nothing.
    no_website = job(company="Acme", company_website="", recruiter_company="")
    assert job_scoring.employer_fit(direct) == 1.0
    assert job_scoring.employer_fit(agency) == 0.3
    assert job_scoring.employer_fit(no_website) == 1.0


def test_employer_fit_reads_a_corpus_agency_name_as_an_agency():
    # credibility_notes is empty on 466 of the 502 agency-named corpus rows,
    # so the company name itself has to carry the signal.
    for name in ("TALENTMATE", "Jobgether", "Dicetek LLC", "Dautom",
                 "Halian | Managed Services, Recruitment Agency & Contract Staffing",
                 "North Star Staffing", "Penta Consulting", "MCG Talent",
                 "Talents Tide", "TASC Outsourcing", "Jobs Ai"):
        assert job_scoring.employer_fit(job(company=name, credibility_notes="")) == 0.3, name
    for name in ("Acme", "Emirates NBD", "Cognizant Consulting",
                 "Tata Consultancy Services", "NAFFCO Careers"):
        assert job_scoring.employer_fit(job(company=name, credibility_notes="")) == 1.0, name


def test_employer_fit_matches_agency_words_whole_not_as_substrings():
    assert job_scoring.employer_fit(job(company="Talentica Software")) == 1.0
    assert job_scoring.employer_fit(job(company="Fox Talent")) == 0.3


def test_employer_fit_does_not_read_a_government_agency_as_a_recruiter():
    for name in ("UAE Space Agency", "Environment Agency - Abu Dhabi", "Emirates News Agency"):
        assert job_scoring.employer_fit(job(company=name, recruiter_company="")) == 1.0, name


def test_employer_fit_still_catches_the_recruiters_it_caught_before():
    for name in ("TALENTMATE", "Halian", "Inspire Selection | Recruitment Agency"):
        assert job_scoring.employer_fit(job(company=name, recruiter_company="")) == 0.3, name


def test_employer_fit_reads_every_confidential_placeholder_the_same_way():
    # 60 corpus rows hide the employer behind "Confidential ..."; their
    # descriptions speak as the employer, so none is an agency.
    for name in ("Confidential", "Confidential Jobs", "Confidential Careers",
                 "Confidential Government", "Confidential Startup"):
        assert job_scoring.employer_fit(job(company=name, recruiter_company="")) == 1.0, name
    # The job boards that "jobs" used to catch are still caught, by name.
    for name in ("Jobs Ai", "Women First Jobs", "Senior IT Jobs UK", "Jobs via eFinancialCareers"):
        assert job_scoring.employer_fit(job(company=name, recruiter_company="")) == 0.3, name


def test_employer_fit_reads_an_aggregator_note_as_an_agency():
    reposted = job(credibility_notes="posted via TalentPool aggregator")
    assert job_scoring.employer_fit(reposted) == 0.3


def test_freshness_decays_over_the_seven_day_window():
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)

    def posted(days):
        return job(date_posted=(now - timedelta(days=days)).isoformat())

    assert job_scoring.freshness(posted(1), now=now) == 1.0
    assert job_scoring.freshness(posted(3), now=now) == 0.7
    assert job_scoring.freshness(posted(6), now=now) == 0.4


def test_freshness_of_an_undated_posting_is_the_middle_band():
    assert job_scoring.freshness(job(date_posted="")) == 0.7


def test_freshness_reads_the_literal_today_as_posted_today():
    # 3 corpus rows carry the word instead of a date.
    assert job_scoring.freshness(job(date_posted="today")) == 1.0


def test_freshness_of_a_future_dated_posting_is_fresh_not_stale():
    now = datetime(2026, 9, 3, tzinfo=timezone.utc)
    tomorrow = job(date_posted=(now + timedelta(days=1)).isoformat())
    assert job_scoring.freshness(tomorrow, now=now) == 1.0


def test_freshness_of_an_unparseable_date_is_the_middle_band():
    assert job_scoring.freshness(job(date_posted="2026-13-45")) == 0.7
    assert job_scoring.freshness(job(date_posted=None)) == 0.7


def test_weights_sum_to_one_hundred():
    assert sum(job_scoring.WEIGHTS.values()) == 100


def test_evaluate_returns_a_knocked_out_job_with_its_reason_and_no_score():
    result = job_scoring.evaluate(job(title="DevOps Manager"), allowed_locations=UAE)
    assert result["passed"] is False
    assert "devops" in result["reason"]
    assert result["total"] == 0


def test_evaluate_scores_a_strong_match_into_the_excellent_band():
    strong = job(
        title="Backend Lead - Microservices Architect",
        tech_required="kotlin, spring boot, microservices, kubernetes, aws",
        min_experience=6, company_website="https://purecs.example",
        date_posted=datetime.now(timezone.utc).isoformat(),
    )
    result = job_scoring.evaluate(strong, allowed_locations=UAE)
    assert result["passed"] is True
    assert result["total"] >= 75
    assert result["band"] == "excellent"


def test_evaluate_puts_a_generic_title_below_the_send_cutoff():
    weak = job(title="software engineer", tech_required="php", min_experience=2,
               company_website="", recruiter_company="Kanz Recruitment")
    result = job_scoring.evaluate(weak, allowed_locations=UAE)
    assert result["total"] < 45


def test_evaluate_reports_every_dimension_so_a_score_can_be_explained():
    result = job_scoring.evaluate(job(), allowed_locations=UAE)
    assert set(result["parts"]) == set(job_scoring.WEIGHTS)


def test_band_boundaries():
    assert job_scoring.band(75) == "excellent"
    assert job_scoring.band(74) == "good"
    assert job_scoring.band(60) == "good"
    assert job_scoring.band(59) == "normal"
    assert job_scoring.band(45) == "normal"
    assert job_scoring.band(44) == "below"


def test_send_cutoff_is_the_floor_of_the_lowest_band():
    assert job_scoring.SEND_CUTOFF == 45
    assert min(floor for floor, _ in job_scoring.BANDS) == job_scoring.SEND_CUTOFF


def test_evaluate_zeroes_every_part_of_a_knocked_out_job():
    result = job_scoring.evaluate(job(title="DevOps Manager"), allowed_locations=UAE)
    assert result["parts"] == {name: 0.0 for name in job_scoring.WEIGHTS}
    assert result["reason"] == job_scoring.knockout(job(title="DevOps Manager"), allowed_locations=UAE)


def test_evaluate_total_is_the_rounded_weighted_sum_of_its_parts():
    result = job_scoring.evaluate(job(), allowed_locations=UAE)
    expected = round(sum(result["parts"][n] * w for n, w in job_scoring.WEIGHTS.items()))
    assert result["total"] == expected
    assert 0 <= result["total"] <= 100


def test_evaluate_never_raises_on_a_malformed_row():
    # Missing keys, None values and an unparseable date all reach evaluate
    # from real scrapes; none of them may take the whole run down.
    for row in (
        {},
        {"title": None, "company": None, "location": "Dubai", "description": None,
         "min_experience": None, "tech_required": None, "tech_nice_to_have": None,
         "date_posted": "not a date", "recruiter_company": None},
        job(min_experience="six", date_posted="2026-13-45"),
    ):
        result = job_scoring.evaluate(row, allowed_locations=UAE)
        assert set(result) == {"passed", "reason", "total", "band", "parts"}
        assert isinstance(result["total"], int)


def test_sendable_is_false_for_a_knocked_out_job():
    result = job_scoring.evaluate(job(title="DevOps Manager"), allowed_locations=UAE)
    assert result["passed"] is False
    assert job_scoring.sendable(result) is False


def test_sendable_is_false_for_a_job_that_survives_but_scores_too_low():
    weak = job(title="software engineer", tech_required="php", min_experience=2,
               recruiter_company="Kanz Recruitment")
    result = job_scoring.evaluate(weak, allowed_locations=UAE)
    assert result["passed"] is True
    assert result["total"] < job_scoring.SEND_CUTOFF
    assert job_scoring.sendable(result) is False


def test_sendable_is_true_only_at_or_above_the_cutoff():
    strong = job(title="Backend Lead - Microservices Architect",
                 tech_required="kotlin, spring boot, microservices, kubernetes, aws",
                 min_experience=6)
    assert job_scoring.sendable(job_scoring.evaluate(strong, allowed_locations=UAE)) is True


import scraper


def test_scraper_score_job_delegates_to_the_rubric():
    strong = {
        "title": "Backend Lead - Microservices Architect", "company": "PureCS",
        "location": "Dubai, United Arab Emirates",
        "tech_required": "kotlin, spring boot, microservices, kubernetes, aws",
        "tech_nice_to_have": "", "min_experience": 6, "description": "",
        "company_website": "https://purecs.example", "recruiter_company": "",
        "date_posted": "",
    }
    score, breakdown = scraper.score_job(strong)
    assert score >= 75
    assert any("stack" in line for line in breakdown)


def test_scraper_score_job_returns_zero_for_a_knocked_out_job():
    score, breakdown = scraper.score_job({
        "title": "DevOps Manager", "company": "MODSOFT",
        "location": "Dubai, United Arab Emirates", "tech_required": "aws, azure",
        "tech_nice_to_have": "", "min_experience": 2, "description": "",
    })
    assert score == 0
    assert any("devops" in line for line in breakdown)


def test_the_scraper_reads_its_markets_from_job_scoring():
    # One list, three readers: the rubric, the scraper's location filter, and
    # the measurement tool. It holds the five markets he chose and nothing
    # else: the wider Gulf was an evaluation convenience, never a choice.
    assert list(scraper.CONFIG["allowed_locations"]) == list(job_scoring.DEFAULT_MARKETS)
    for market in ("dubai", "abu dhabi", "jeddah", "riyadh", "switzerland", "zurich", "lausanne"):
        assert market in job_scoring.DEFAULT_MARKETS, market
    for market in ("sharjah", "saudi", "saudi arabia", "united arab emirates"):
        assert market not in job_scoring.DEFAULT_MARKETS, market


def test_the_shipped_threshold_is_the_rubrics_send_cutoff():
    # A profile config may still override score_threshold; the default it
    # starts from must be the one number the rubric defines.
    assert scraper.DEFAULT_CONFIG["score_threshold"] == job_scoring.SEND_CUTOFF
    assert scraper.CONFIG["score_threshold"] == job_scoring.SEND_CUTOFF


def test_allowed_locations_cover_the_board_spellings_of_the_chosen_markets():
    for location in ("Jiddah, Makkah, Saudi Arabia", "Jeddah, Saudi Arabia",
                     "Dubai, United Arab Emirates", "Abu Dhabi", "Zurich, Switzerland",
                     "Riyadh, Saudi Arabia", "Saudi Arabia, Riyadh", "Riyadh Region"):
        assert job_scoring.knockout({"title": "Software Architect", "location": location,
                                     "description": "", "min_experience": 6},
                                    allowed_locations=job_scoring.DEFAULT_MARKETS) is None, location


def test_allowed_locations_still_exclude_the_markets_he_declined():
    # Jeddah and Riyadh are the two Saudi cities he chose; the country is not
    # a market, so a bare "Saudi Arabia" or any other Saudi city stays out.
    for location in ("Sharjah, United Arab Emirates", "United Arab Emirates",
                     "Saudi Arabia", "Dammam, Eastern, Saudi Arabia",
                     "Riyad Qana, Al Qasim, Saudi Arabia", "Cairo, Egypt"):
        assert job_scoring.knockout({"title": "Software Architect", "location": location,
                                     "description": "", "min_experience": 6},
                                    allowed_locations=job_scoring.DEFAULT_MARKETS), location


def test_duplicate_key_collapses_a_repost_of_the_same_role():
    a = {"title": "Senior Technical Architect", "company": "Inception"}
    b = {"title": "Technical Architect", "company": "Inception"}
    c = {"title": "Technical Architect", "company": "Different Co"}
    assert job_scoring.duplicate_key(a) == job_scoring.duplicate_key(b)
    assert job_scoring.duplicate_key(a) != job_scoring.duplicate_key(c)


def test_duplicate_key_separates_the_same_role_in_two_countries():
    dubai = job(title="Technical Architect", company="Inception", location="Dubai, United Arab Emirates")
    riyadh = job(title="Technical Architect", company="Inception", location="Riyadh, Saudi Arabia")
    abu_dhabi = job(title="Technical Architect", company="Inception", location="Abu Dhabi")
    assert job_scoring.duplicate_key(dubai) != job_scoring.duplicate_key(riyadh)
    assert job_scoring.duplicate_key(dubai) == job_scoring.duplicate_key(abu_dhabi)


# MARKET_COUNTRIES does double duty: it defines the allowed markets and it
# classifies the country for duplicate_key. A term added to stop a key
# fragmenting across an unchosen city (Khobar, Al Ain, Makkah) would widen
# DEFAULT_MARKETS by construction and enrol him in a market he never chose.
# The literal is written out, not derived, so this test is what stops a
# dedup fix from widening the search.
def test_default_markets_is_exactly_the_markets_he_chose():
    assert job_scoring.DEFAULT_MARKETS == (
        "dubai", "abu dhabi", "jeddah", "jiddah", "riyadh",
        "switzerland", "schweiz", "suisse", "svizzera",
        "zurich", "zürich", "geneva", "genève", "genf",
        "basel", "bern", "lausanne", "zug", "lucerne", "luzern",
    )



# ── Final review fixes ───────────────────────────────────────────────────────


def test_the_abbreviated_spelling_of_junior_is_the_same_verdict_as_the_word():
    # "jr" was in SENIORITY_WORDS and missing from JUNIOR_WORDS, so
    # "Jr Backend Engineer" scored 84 and was sent while "Junior Backend
    # Engineer" was knocked out. One spelling of one word decided it.
    spelled = job_scoring.evaluate(job(title="Junior Backend Engineer"), allowed_locations=UAE)
    short = job_scoring.evaluate(job(title="Jr Backend Engineer"), allowed_locations=UAE)
    dotted = job_scoring.evaluate(job(title="Jr. Backend Engineer"), allowed_locations=UAE)
    for name, result in (("junior", spelled), ("jr", short), ("jr.", dotted)):
        assert result["reason"].startswith("too junior"), name
        assert result["total"] == 0, name
        assert not job_scoring.sendable(result), name


def test_knockout_catches_the_junior_spellings_the_corpus_actually_carries():
    # Every one of these is a real title from data/jobs.db that reached a
    # score. "Junior-Level" escaped because the token matcher keeps the
    # hyphen while normalise_title strips it.
    for title in ("Jr. Architect- Modeler ( UAE National Only )",
                  "Jr. Architect - Saudi Talent",
                  "Software Engineer (Fresh Graduates)",
                  "Fresher Software Engineer",
                  "Fresh -Technical architect",
                  "Junior-Level Developer",
                  "Graduate-Level Engineer",
                  "Software Engineer Apprenticeship"):
        assert job_scoring.knockout(job(title=title), allowed_locations=UAE), title


def test_the_junior_knockout_still_leaves_the_words_that_only_look_junior():
    for title in ("International Solutions Architect", "Internal Tools Architect",
                  "Senior Freshservice Solution Architect"):
        assert job_scoring.knockout(job(title=title), allowed_locations=UAE) is None, title


def test_a_family_word_does_not_veto_a_role_he_searches_for():
    # "cloud architect" is one of his own search keywords (scraper CONFIG),
    # so a bare "infrastructure" block vetoed his own search. A Java
    # microservices role must not die because its title lists front end as
    # one of four technologies.
    for title in ("Cloud Infrastructure Architect",
                  "Azure Cloud & Infrastructure Solution Architect",
                  "Senior Software & Infrastructure Architect",
                  "Java Software Engineer(.Net + Microservices + front end + Cloud)",
                  "Software Developer (Java / Backend / Frontend)"):
        assert job_scoring.blocked_title({"title": title}) is None, title


def test_a_title_that_is_the_family_role_is_still_blocked():
    # The rescue is adjacency, not co-occurrence: a qualifier has to sit in
    # front of the family word, or the family word names the job.
    for title in ("Infrastructure Architect", "Senior Infrastructure Engineer",
                  "Virtualization Infrastructure Architect",
                  "IT Infrastructure Administrator", "Infrastructure & Cloud Engineer",
                  "Frontend Developer", "Senior Frontend Engineer",
                  "Software Engineer - Frontend", "Front End Developer",
                  "Network Engineer", "Senior Network Specialist",
                  "Lead Architect - Network & Infrastructure"):
        assert job_scoring.blocked_title({"title": title}), title


def test_only_the_three_widened_families_can_be_rescued():
    # devops, qa and the rest were never widened, so no qualifier saves them.
    for title in ("Cloud DevOps Engineer", "Software QA Engineer",
                  "Backend Data Engineer", "Cloud Security Architect"):
        assert job_scoring.blocked_title({"title": title}), title


def test_the_duplicate_guard_is_seeded_from_the_database(tmp_path):
    # The spec's rule is "inside the freshness window", not "inside this
    # process". Built empty in main() it caught only the reposts that arrive
    # in one run; "cloud architect remote @ joveo ai" came back on nine
    # consecutive nights, each with a fresh job id, and each night sent.
    import sqlite3

    db = tmp_path / "jobs.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE jobs (title TEXT, company TEXT, location TEXT, date_scraped TEXT)")
    now = datetime.now(timezone.utc)
    conn.executemany(
        "INSERT INTO jobs VALUES (?, ?, ?, ?)",
        [
            ("Senior Cloud Architect Remote", "Joveo AI", "Dubai, UAE", (now - timedelta(days=3)).isoformat()),
            ("Technical Architect", "Inception", "Dubai, UAE", (now - timedelta(days=30)).isoformat()),
        ],
    )
    conn.commit()

    keys = scraper.load_recent_duplicate_keys(conn, 7)

    repost = {"title": "Cloud Architect Remote", "company": "joveo ai", "location": "Abu Dhabi"}
    assert job_scoring.duplicate_key(repost) in keys
    # Outside the window it stops counting, so a role can be re-offered.
    stale = {"title": "Technical Architect", "company": "Inception", "location": "Dubai, UAE"}
    assert job_scoring.duplicate_key(stale) not in keys
    assert job_scoring.knockout(
        dict(repost, description="", min_experience=6),
        allowed_locations=job_scoring.DEFAULT_MARKETS,
        seen_keys=keys,
    ) == "duplicate of a posting already seen"
