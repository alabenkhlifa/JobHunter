import json
import os
import sqlite3
import stat
from datetime import datetime, timezone

import pytest

from jobhunter_autopilot import codes, policy, store


def test_missing_file_is_the_disabled_rehearsal_default(tmp_path):
    loaded = policy.load(tmp_path / "absent.json")
    assert loaded["enabled"] is False
    assert loaded["mode"] == "rehearsal"
    assert loaded["min_score"] == 75 and loaded["max_per_day"] == 3
    assert loaded["roles"] == ["senior_software_engineer", "lead_software_engineer"]
    assert loaded["locations"] == {"countries": ["es"], "cities": []}
    assert loaded["include_statuses"] == ["new", "archived"]
    assert loaded["timezone"] == "Africa/Tunis"


def test_env_override_and_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("JOBHUNTER_AUTOPILOT_POLICY", str(tmp_path / "p.json"))
    assert policy.default_path() == tmp_path / "p.json"
    monkeypatch.delenv("JOBHUNTER_AUTOPILOT_POLICY")
    monkeypatch.setenv("JOBHUNTER_STATE_DIR", str(tmp_path / "state"))
    assert policy.default_path() == tmp_path / "state" / "auto_apply.json"
    assert store.lock_path() == tmp_path / "state" / "autopilot.lock"


def test_save_is_private_atomic_and_round_trips(tmp_path):
    path = tmp_path / "state" / "auto_apply.json"
    saved = policy.save(policy.apply_update(policy.defaults(), "min_score=80"), path, via="telegram")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert saved["updated_via"] == "telegram" and saved["updated_at"]
    assert policy.load(path)["min_score"] == 80
    assert [p.name for p in path.parent.iterdir()] == ["auto_apply.json"]


@pytest.mark.parametrize("update, key, expected", [
    ("min_score=80", "min_score", 80),
    ("max_per_day=0", "max_per_day", 0),
    ("mode=live", "mode", "live"),
    ("enabled=yes", "enabled", True),
    ("roles=senior_software_engineer,software_architect", "roles",
     ["senior_software_engineer", "software_architect"]),
    ("roles=tech lead", "roles", ["lead_software_engineer"]),
    ("preferred_stack=Java, Kotlin", "preferred_stack", ["java", "kotlin"]),
])
def test_valid_updates(update, key, expected):
    assert policy.apply_update(policy.defaults(), update)[key] == expected


def test_nested_list_updates():
    p = policy.apply_update(policy.defaults(), "locations.cities=Madrid, Valencia")
    assert p["locations"]["cities"] == ["madrid", "valencia"]
    p = policy.apply_update(p, "companies.deny=Acme Corp")
    p = policy.apply_update(p, "companies.deny+=Globex,acme corp")
    assert p["companies"]["deny"] == ["Acme Corp", "Globex"]
    p = policy.apply_update(p, "companies.deny-=acme corp")
    assert p["companies"]["deny"] == ["Globex"]
    p = policy.apply_update(p, "companies.allow=")
    assert p["companies"]["allow"] == []
    p = policy.apply_update(p, "locations.countries=Spain")
    assert p["locations"]["countries"] == ["es"]


@pytest.mark.parametrize("update", [
    "min_score=101", "min_score=-1", "min_score=high", "max_per_day=11", "mode=yolo",
    "roles=principal_engineer", "roles=", "include_statuses=interested", "include_statuses=rejected",
    "unknown_key=1", "enabled=maybe", "timezone=Mars/Base", "min_score+=1", "nonsense",
    "salary_ask.quantile=2", "locations.countries=fr",
])
def test_invalid_updates_raise_policy_error(update):
    original = policy.defaults()
    with pytest.raises(policy.PolicyError):
        policy.apply_update(original, update)
    assert original == policy.defaults()


def test_corrupt_file_is_an_error(tmp_path):
    path = tmp_path / "p.json"
    path.write_text("{not json")
    with pytest.raises(policy.PolicyError):
        policy.load(path)
    path.write_text(json.dumps({"min_score": 500}))
    with pytest.raises(policy.PolicyError):
        policy.load(path)


def test_pause_until_and_activity():
    now = datetime(2026, 10, 15, 18, 0, tzinfo=timezone.utc)
    p = policy.apply_update(policy.defaults(), "enabled=true")
    assert policy.is_active(p, now)
    p["paused"] = True
    assert not policy.is_active(p, now)
    p["paused_until"] = policy.parse_until("2d", now=now)
    assert policy.is_paused(p, now)
    assert not policy.is_paused(p, datetime(2026, 10, 18, tzinfo=timezone.utc))
    assert policy.parse_until("2026-10-20", tz="Africa/Tunis").startswith("2026-10-20T23:59:59+01:00")
    with pytest.raises(policy.PolicyError):
        policy.parse_until("whenever")


def test_summary_text_is_short_and_states_mode():
    text = policy.summary_text(policy.defaults())
    assert text.startswith("Auto apply: OFF (rehearsal")
    assert "Min score 75" in text and "max 3/day" in text and "Spain (all cities)" in text
    live = policy.apply_update(policy.apply_update(policy.defaults(), "enabled=true"), "mode=live")
    assert "ON (live" in policy.summary_text(live)
    assert len(text.splitlines()) == 4


def test_codes():
    assert codes.is_valid("captcha")
    assert codes.is_valid("unanswerable:Notice period")
    assert not codes.is_valid("unanswerable:")
    assert not codes.is_valid("made_up")
    assert "Notice period" in codes.describe("unanswerable:Notice period")
    assert len(codes.HARD_STOPS) == 14


def test_store_schema_log_and_cap():
    conn = sqlite3.connect(":memory:")
    store.ensure_schema(conn)
    store.ensure_schema(conn)
    run = "2026-10-15"
    store.upsert_log(conn, run, "a", "selected", criteria=["score 80 ≥ 75"])
    store.upsert_log(conn, run, "a", "packaged")
    row = store.get_log(conn, run, "a")
    assert row["status"] == "packaged" and row["criteria"] == ["score 80 ≥ 75"]
    for job, status in [("b", "applying"), ("c", "submitted"), ("d", "unverified"),
                        ("e", "blocked"), ("f", "rehearsed")]:
        store.upsert_log(conn, run, job, status)
    store.upsert_log(conn, "2026-10-14", "z", "submitted")
    assert store.cap_used_today(conn, run) == 3
    assert store.cap_used_today(conn, run, include_rehearsed=True) == 4
    with pytest.raises(ValueError):
        store.upsert_log(conn, run, "a", "bogus")
    store.update_run(conn, run, plan_finished_at="x")
    store.update_run(conn, run, summary_sent_at="y")
    assert store.get_run(conn, run)["plan_finished_at"] == "x"


def test_run_id_follows_policy_timezone():
    # Africa/Tunis is UTC+1: 22:59Z is 23:59 local, 23:01Z is 00:01 the next day.
    assert store.run_id_for(datetime(2026, 10, 15, 22, 59, tzinfo=timezone.utc), "Africa/Tunis") == "2026-10-15"
    assert store.run_id_for(datetime(2026, 10, 15, 23, 1, tzinfo=timezone.utc), "Africa/Tunis") == "2026-10-16"


def test_lock_is_exclusive(tmp_path):
    path = tmp_path / "autopilot.lock"
    with store.autopilot_lock(path) as first:
        assert first
        with store.autopilot_lock(path) as second:
            assert not second
    with store.autopilot_lock(path) as again:
        assert again
