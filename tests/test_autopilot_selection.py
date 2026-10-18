import pytest

import scraper
from jobhunter_autopilot import selection, store
from test_autopilot_helpers import NOW, RUBRIC, RUN_ID, add_job, enabled_policy, make_db


@pytest.fixture
def conn(tmp_path):
    connection = make_db(tmp_path / "jobs.db")
    yield connection
    connection.close()


def ids(result):
    return [job["id"] for job in result["selected"]]


@pytest.mark.parametrize("title, families", [
    ("Senior Backend Engineer", ["senior_software_engineer", "backend_engineer"]),
    ("Sr. Java Developer (m/f/d)", ["senior_software_engineer", "backend_engineer"]),
    ("Tech Lead", ["lead_software_engineer"]),
    ("Technical Lead - Java", ["lead_software_engineer"]),
    ("Lead Software Engineer", ["lead_software_engineer"]),
    ("Líder Técnico Backend", ["lead_software_engineer"]),
    ("Ingeniero/a de Software Senior", ["senior_software_engineer"]),
    ("Desarrollador/a Backend Senior", ["senior_software_engineer", "backend_engineer"]),
    ("Programador Java Sénior", ["senior_software_engineer", "backend_engineer"]),
    ("Senior Software Architect", ["software_architect"]),
    ("Engineering Manager", ["engineering_manager"]),
    ("Principal Software Engineer", []),
    ("Staff Engineer", []),
    ("Director of Engineering", []),
    ("Junior Java Developer", []),
    ("Semi Senior Developer", []),
    ("Software Engineer", []),
    ("Lead Generation Specialist", []),
])
def test_role_families(title, families):
    assert selection.role_families(title) == families


def test_selects_send_verdicts_on_current_rubric_and_ranks_by_score(conn):
    low = add_job(conn, score=80, tech_required="python, aws", title="Senior Backend Engineer")
    high = add_job(conn, score=90, tech_required="golang, aws")
    boosted = add_job(conn, score=86, tech_required="kotlin")
    result = selection.select(conn, enabled_policy(), now=NOW)
    # +5 for a preferred-stack term lifts 86 (91) over 90; python/aws gets none.
    assert ids(result) == [boosted["id"], high["id"], low["id"]]
    job = result["selected"][0]
    assert job["criteria"][:4] == ["score 86 ≥ 75", "reviewer: send", "role: Senior Software Engineer",
                                   "location: Madrid, Spain"]
    assert job["verdict"]["verdict"] == "send" and job["score_basis"] == "stored"


def test_status_gate(conn):
    allowed = [add_job(conn, status=s)["id"] for s in ("new", "archived")]
    for status in ("interested", "skipped", "rejected", "unavailable", "delivery_pending"):
        add_job(conn, status=status)
    assert sorted(ids(selection.select(conn, enabled_policy(), now=NOW))) == sorted(allowed)
    only_new = enabled_policy(include_statuses=["new"])
    assert ids(selection.select(conn, only_new, now=NOW)) == [allowed[0]]


def test_application_history_and_todays_log_exclude(conn):
    submitted = add_job(conn)
    blocked = add_job(conn)
    attempted = add_job(conn)
    packaged = add_job(conn)
    in_log = add_job(conn)
    scraper.record_application_stage(conn, submitted["id"], "submitted", sync=False)
    scraper.record_application_stage(conn, blocked["id"], "blocked_site_challenge", sync=False)
    scraper.record_application_stage(conn, attempted["id"], "submission_attempted", sync=False)
    scraper.record_application_stage(conn, packaged["id"], "package_generated", sync=False)
    result = selection.select(conn, enabled_policy(), now=NOW, exclude_ids={in_log["id"]})
    assert ids(result) == [packaged["id"]]
    assert result["filtered"]["already_applied_or_blocked"] == 3
    assert result["filtered"]["already_in_todays_run"] == 1


def test_legacy_breakdown_is_rescored_in_memory(conn):
    legacy = add_job(conn, score=12, score_breakdown="tech: java (+10), location +5")
    inflated = add_job(conn, score=99, score_breakdown="legacy", description="Short.", tech_required="php",
                       title="Senior PHP Developer", ai_verdict="send")
    result = selection.select(conn, enabled_policy(), now=NOW)
    assert ids(result) == [legacy["id"]]
    assert result["selected"][0]["score_basis"] == "rescored"
    assert result["selected"][0]["effective_score"] >= 75
    stored = conn.execute("SELECT score, score_breakdown FROM jobs WHERE id=?", (legacy["id"],)).fetchone()
    assert stored == (12, "tech: java (+10), location +5")
    assert inflated["id"] not in ids(result)


def test_hard_filters_still_apply_but_freshness_does_not(conn):
    old = add_job(conn, date_posted="2026-06-01", date_scraped="2026-06-01T20:00:00+00:00")
    excluded = add_job(conn, title="Senior QA Engineer")
    local = add_job(conn, description=scraper.CONFIG["local_presence_phrases"][0] + " Spain. " + "x" * 10
                    + " Java Spring Boot microservices.")
    result = selection.select(conn, enabled_policy(), now=NOW)
    assert ids(result) == [old["id"]]
    assert excluded["id"] not in ids(result) and local["id"] not in ids(result)


def test_country_city_and_company_filters(conn):
    madrid = add_job(conn, company="Globex Payments")
    valencia = add_job(conn, location="Valencia, Spain")
    dubai = add_job(conn, location="Dubai, United Arab Emirates")
    acme = add_job(conn, company="ACME corp", location="Barcelona, Spain")
    base = selection.select(conn, enabled_policy(), now=NOW)
    assert dubai["id"] not in ids(base) and base["filtered"]["country"] == 1
    cities = enabled_policy(locations={"countries": ["es"], "cities": ["valencia"]})
    assert ids(selection.select(conn, cities, now=NOW)) == [valencia["id"]]
    deny = enabled_policy(companies={"allow": [], "deny": ["acme"]})
    assert acme["id"] not in ids(selection.select(conn, deny, now=NOW))
    allow = enabled_policy(companies={"allow": ["globex"], "deny": []})
    assert ids(selection.select(conn, allow, now=NOW)) == [madrid["id"]]


def test_roles_and_min_score(conn):
    lead = add_job(conn, title="Tech Lead Java")
    architect = add_job(conn, title="Software Architect")
    result = selection.select(conn, enabled_policy(), now=NOW)
    assert lead["id"] in ids(result) and architect["id"] not in ids(result)
    assert architect["id"] in ids(selection.select(conn, enabled_policy(roles=["software_architect"]), now=NOW))
    assert ids(selection.select(conn, enabled_policy(min_score=95), now=NOW)) == []


def test_verdict_gate_and_review_needs(conn):
    unreviewed = add_job(conn, ai_verdict="", ai_rubric="", ai_reviewed_at="")
    stale_send = add_job(conn, ai_rubric="2026-01-01")
    fresh_hold = add_job(conn, ai_verdict="hold", ai_reviewed_at="2026-10-15T10:00:00+00:00")
    old_hold = add_job(conn, ai_verdict="hold", ai_reviewed_at="2026-10-10T10:00:00+00:00")
    excluded = add_job(conn, ai_sponsorship="excluded")
    rejected = add_job(conn, ai_verdict="reject")
    good = add_job(conn)
    result = selection.select(conn, enabled_policy(), now=NOW)
    assert ids(result) == [good["id"]]
    assert result["needs_review"] == {unreviewed["id"]: "unreviewed", stale_send["id"]: "stale_rubric",
                                      old_hold["id"]: "hold_expired"}
    assert {job["id"] for job in result["review_pool"]} == set(result["needs_review"])
    assert result["filtered"]["sponsorship_excluded"] == 1
    assert result["filtered"]["reviewer_hold"] == 1
    assert result["filtered"]["reviewer_reject"] == 1
    assert fresh_hold["id"] not in result["needs_review"] and excluded["id"] not in ids(result)
    assert rejected["id"] not in ids(result)
    assert result["rubric"] == RUBRIC


def test_autopilot_owned_interested_job_competes_again(conn):
    owned = add_job(conn, status="interested")
    manual = add_job(conn, status="interested")
    store.ensure_schema(conn)
    store.upsert_log(conn, "2026-10-14", owned["id"], "rehearsed")
    store.upsert_log(conn, "2026-10-14", manual["id"], "packaged")
    scraper.record_job_feedback(conn, manual["id"], "interested")
    assert ids(selection.select(conn, enabled_policy(), now=NOW)) == [owned["id"]]
    store.upsert_log(conn, RUN_ID, owned["id"], "handed_off")
    assert ids(selection.select(conn, enabled_policy(), now=NOW)) == []


def test_same_role_on_two_boards_is_selected_once_preferring_the_employer_posting(conn):
    linkedin = add_job(conn, title="Senior Java Developer", company="Miratech",
                       location="Barcelona, Catalonia, Spain", score=87)
    board = add_job(conn, job_id="sj-1", title="Senior Java Developer", company="Miratech S.L.",
                    location="València, Spain (Remote)", source="SpainJobs.io", score=87,
                    url="https://spainjobs.io/jobs/1", apply_url="https://careers.miratech.com/1")
    other = add_job(conn, title="Senior Java Developer (Payments)", company="Miratech", score=85)
    held = add_job(conn, title="Senior Java Developer", company="Miratech", score=86, ai_verdict="hold",
                   ai_reviewed_at="2026-10-15T18:00:00+00:00")
    result = selection.select(conn, enabled_policy(), now=NOW)
    # The parenthetical names a different role; the plain copies collapse to the employer posting.
    assert ids(result) == [board["id"], other["id"]]
    assert result["filtered"]["duplicate_role"] == 1
    assert held["id"] not in result["needs_review"] and linkedin["id"] not in ids(result)


def test_role_already_applied_to_is_never_selected_again_from_another_copy(conn):
    applied = add_job(conn, title="Senior Java Developer", company="Miratech", status="interested")
    scraper.record_application_stage(conn, applied["id"], "submitted", sync=False)
    copy = add_job(conn, job_id="sj-2", title="Senior Java Developer", company="MIRATECH S.L.",
                   source="SpainJobs.io", url="https://spainjobs.io/jobs/2")
    other = add_job(conn, title="Lead Software Engineer", company="Miratech")
    result = selection.select(conn, enabled_policy(), now=NOW)
    assert ids(result) == [other["id"]]
    assert result["filtered"]["duplicate_of_applied"] == 1
    assert copy["id"] not in ids(result)
