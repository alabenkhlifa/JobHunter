from datetime import datetime, timezone

import pytest

import scraper
from jobhunter_autopilot import gate, policy, store
from test_autopilot_helpers import NOW, RUN_ID, add_job, enabled_policy, log_status, make_db, write_policy

PAGE = "https://boards.greenhouse.io/acme/jobs/1"


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "jobs.db"
    conn = make_db(db)
    store.ensure_schema(conn)
    path = write_policy(tmp_path / "policy.json", enabled_policy(mode="live"))
    yield conn, db, path
    conn.close()


def authorize(db, path, job_id, action="inspect", now=NOW, page_url=PAGE):
    return gate.authorize(db, job_id, action, page_url=page_url, now=now, policy_path=path)


def test_moves_packaged_to_applying_and_allows_submit(env):
    conn, db, path = env
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    assert authorize(db, path, job["id"]) == {"run_id": RUN_ID, "mode": "live"}
    assert log_status(conn, job["id"]) == "applying"
    assert authorize(db, path, job["id"], "upload")["mode"] == "live"
    assert authorize(db, path, job["id"], "submit")["run_id"] == RUN_ID


def test_refuses_when_disabled_paused_or_unplanned(env, tmp_path):
    conn, db, path = env
    job = add_job(conn)
    with pytest.raises(gate.PolicyRefused, match="not planned"):
        authorize(db, path, job["id"])
    store.upsert_log(conn, RUN_ID, job["id"], "blocked")
    with pytest.raises(gate.PolicyRefused, match="not planned"):
        authorize(db, path, job["id"])
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    off = write_policy(tmp_path / "off.json", policy.defaults())
    with pytest.raises(gate.PolicyRefused, match="disabled"):
        authorize(db, off, job["id"])
    paused = write_policy(tmp_path / "paused.json", enabled_policy(paused=True))
    with pytest.raises(gate.PolicyRefused, match="paused"):
        authorize(db, paused, job["id"])
    with pytest.raises(gate.PolicyRefused):
        authorize(db, path, job["id"], "delete-everything")
    with pytest.raises(gate.PolicyRefused, match="http"):
        authorize(db, path, job["id"], page_url="file:///etc/passwd")
    assert isinstance(gate.PolicyRefused("x"), PermissionError)
    assert log_status(conn, job["id"]) == "packaged"


def test_rehearsal_refuses_submit_but_allows_filling(env, tmp_path):
    conn, db, _ = env
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    rehearsal = write_policy(tmp_path / "r.json", enabled_policy())
    assert authorize(db, rehearsal, job["id"], "upload")["mode"] == "rehearsal"
    with pytest.raises(gate.PolicyRefused, match="rehearsal"):
        authorize(db, rehearsal, job["id"], "submit")


def test_refuses_resubmission(env):
    conn, db, path = env
    attempted = add_job(conn)
    submitted = add_job(conn)
    for job, stage in ((attempted, "submission_attempted"), (submitted, "submitted")):
        store.upsert_log(conn, RUN_ID, job["id"], "applying")
        scraper.record_application_stage(conn, job["id"], stage, sync=False)
        with pytest.raises(gate.PolicyRefused, match="already submitted"):
            authorize(db, path, job["id"], "submit")
    # verify-email after the click is still allowed.
    assert authorize(db, path, attempted["id"], "verify-email")["mode"] == "live"


def test_linkedin_pages_only_inspect(env):
    conn, db, path = env
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    linkedin = "https://www.linkedin.com/jobs/view/1"
    assert authorize(db, path, job["id"], "inspect", page_url=linkedin)
    with pytest.raises(gate.PolicyRefused) as refused:
        authorize(db, path, job["id"], "upload", page_url=linkedin)
    assert refused.value.code == "linkedin_easy_apply"


@pytest.mark.parametrize("url", [
    "https://jobs.smartrecruiters.com/oneclick-ui/company/Acme/publication/1",
    "https://www.smartr.me/oneclick-ui/company/Acme/publication/1",
])
def test_anti_bot_application_sites_are_refused_even_for_inspect(env, url):
    conn, db, path = env
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    for action in ("inspect", "upload", "submit"):
        with pytest.raises(gate.PolicyRefused) as refused:
            authorize(db, path, job["id"], action, page_url=url)
        assert refused.value.code == "anti_bot_ats"
    assert log_status(conn, job["id"]) == "packaged"


def test_cap_counts_today_only_in_policy_timezone(env, tmp_path):
    conn, db, _ = env
    path = write_policy(tmp_path / "cap.json", enabled_policy(mode="live", max_per_day=1))
    late = datetime(2026, 10, 15, 22, 59, tzinfo=timezone.utc)   # 23:59 Tunis
    early = datetime(2026, 10, 15, 23, 1, tzinfo=timezone.utc)   # 00:01 Tunis, next day
    done, waiting = add_job(conn), add_job(conn)
    store.upsert_log(conn, "2026-10-15", done["id"], "submitted")
    store.upsert_log(conn, "2026-10-15", waiting["id"], "packaged")
    with pytest.raises(gate.PolicyRefused, match="daily cap"):
        authorize(db, path, waiting["id"], now=late)
    store.upsert_log(conn, "2026-10-16", waiting["id"], "packaged")
    assert authorize(db, path, waiting["id"], now=early)["run_id"] == "2026-10-16"
    assert log_status(conn, waiting["id"], "2026-10-16") == "applying"
    assert log_status(conn, waiting["id"], "2026-10-15") == "packaged"


def test_missing_page_url_is_allowed_but_bad_urls_are_not(env):
    conn, db, path = env
    job = add_job(conn)
    store.upsert_log(conn, RUN_ID, job["id"], "packaged")
    assert authorize(db, path, job["id"], "upload", page_url=None)["mode"] == "live"
    with pytest.raises(gate.PolicyRefused):
        authorize(db, path, job["id"], "upload", page_url="not a url")
