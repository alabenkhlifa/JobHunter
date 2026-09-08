import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import rescore_pending_backlog as tool


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "jobs.db"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("""CREATE TABLE jobs (
            id TEXT PRIMARY KEY, title TEXT, company TEXT, location TEXT,
            description TEXT, tech_required TEXT, tech_nice_to_have TEXT,
            min_experience INTEGER, date_posted TEXT, date_scraped TEXT,
            recruiter_company TEXT, credibility_notes TEXT,
            score INTEGER, score_breakdown TEXT, notified INTEGER, status TEXT,
            ai_verdict TEXT
        )""")
        conn.commit()
    return path


def add_jobs(db, *overrides, conn=None):
    own_connection = conn is None
    conn = conn or sqlite3.connect(db)
    try:
        for values in overrides:
            row = dict(
                id="sample", title="Software Architect", company="Example",
                location="Dubai", description="Java and Spring Boot backend services.",
                tech_required="java, spring boot, aws", tech_nice_to_have="",
                min_experience=6, date_posted="2026-07-10T12:00:00+00:00",
                date_scraped="2026-07-12T12:00:00+00:00",
                recruiter_company="Enriched later", credibility_notes="Enriched later",
                score=1, score_breakdown="old", notified=0, status="new", ai_verdict="hold",
            )
            row.update(values)
            conn.execute(
                f"INSERT INTO jobs ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                tuple(row.values()),
            )
        conn.commit()
    finally:
        if own_connection:
            conn.close()


def rows(db):
    with closing(sqlite3.connect(db)) as conn:
        conn.row_factory = sqlite3.Row
        return {row["id"]: dict(row) for row in conn.execute("SELECT * FROM jobs")}


def test_only_pending_scores_change_and_backup_preserves_original(db, monkeypatch):
    add_jobs(db, {"id": "pending"}, {"id": "sent", "notified": 1},
             {"id": "archived", "status": "archived"},
             {"id": "interested", "status": "interested"},
             {"id": "skipped", "status": "skipped"})
    before = rows(db)
    monkeypatch.setattr(tool, "rescore", lambda job, now: (70, ["updated"]))

    result = tool.run_rescore(db, apply=True)

    expected = {**before, "pending": {**before["pending"], "score": 70, "score_breakdown": "updated"}}
    assert rows(db) == expected
    assert rows(result["backup_path"]) == before
    assert result["affected_rows"] == result["score_changes"] == 1


@pytest.mark.parametrize("extra_args", [[], ["--dry-run"]])
def test_cli_defaults_to_read_only_preview(db, extra_args, capsys):
    add_jobs(db, {})
    original = db.read_bytes()

    assert tool.main(["--db", str(db), *extra_args]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False
    assert report["affected_rows"] == 1
    assert report["backup_path"] is None
    assert db.read_bytes() == original
    assert not (db.parent / "backups").exists()


def test_freshness_uses_collection_time_and_enrichment_is_excluded(db, monkeypatch):
    add_jobs(db, {"date_scraped": "2026-07-12T12:00:00"})
    calls = []

    def rescore(job, now):
        calls.append((job, now))
        return 65, ["fixed"]

    monkeypatch.setattr(tool, "rescore", rescore)
    tool.run_rescore(db)

    job, now = calls[0]
    assert now == datetime(2026, 7, 12, 12, tzinfo=timezone.utc)
    assert job["recruiter_company"] == job["credibility_notes"] == ""


def test_reports_score_and_breakdown_changes_and_threshold_crossings(db, monkeypatch):
    cutoff = tool.job_scoring.SEND_CUTOFF
    add_jobs(db, {"id": "up", "score": cutoff - 1},
             {"id": "down", "score": cutoff}, {"id": "missing", "score": None},
             {"id": "breakdown", "score": cutoff})
    scores = {"up": cutoff, "down": cutoff - 1, "missing": cutoff, "breakdown": cutoff}
    monkeypatch.setattr(tool, "rescore", lambda job, now: (scores[job["id"]], ["new"]))

    report = tool.run_rescore(db)

    assert report["pending_rows"] == report["affected_rows"] == 4
    assert report["score_changes"] == 3
    assert report["breakdown_only_changes"] == 1
    assert report["crossed_above"] == 2
    assert report["crossed_below"] == 1


def test_apply_is_idempotent_and_keeps_original_backup(db):
    add_jobs(db, {})
    first = tool.run_rescore(db, apply=True)
    original_backup = Path(first["backup_path"]).read_bytes()
    after = rows(db)

    second = tool.run_rescore(db, apply=True)

    assert rows(db) == after
    assert second["affected_rows"] == 0
    assert second["backup_path"] is None
    assert Path(first["backup_path"]).read_bytes() == original_backup


def test_backups_are_unique_private_and_never_overwritten(db, tmp_path):
    add_jobs(db, {})
    backup_dir = tmp_path / "backups"
    first = tool.create_backup(db, backup_dir)
    original = first.read_bytes()
    add_jobs(db, {"id": "later"})

    second = tool.create_backup(db, backup_dir)

    assert first != second
    assert first.read_bytes() == original
    assert len(rows(first)) == 1 and len(rows(second)) == 2
    assert first.stat().st_mode & 0o777 == 0o600
    assert backup_dir.stat().st_mode & 0o777 == 0o700


def test_backup_includes_committed_wal_rows(db):
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        add_jobs(db, {"id": "committed-in-wal"}, conn=writer)
        before = rows(db)

        result = tool.run_rescore(db, apply=True)

        assert rows(result["backup_path"]) == before
        assert result["affected_rows"] == 1


def test_backup_failure_prevents_updates(db, monkeypatch):
    add_jobs(db, {})
    before = rows(db)

    def fail_backup(*args):
        raise OSError("Backup unavailable")

    monkeypatch.setattr(tool, "create_backup", fail_backup)
    with pytest.raises(OSError, match="Backup unavailable"):
        tool.run_rescore(db, apply=True)

    assert rows(db) == before


def test_sql_failure_rolls_back_entire_batch(db):
    add_jobs(db, {"id": "first"}, {"id": "second"})
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("""CREATE TRIGGER fail_second BEFORE UPDATE ON jobs
            WHEN OLD.id = 'second' BEGIN SELECT RAISE(ABORT, 'test failure'); END""")
        conn.commit()
    before = rows(db)

    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        tool.run_rescore(db, apply=True)

    assert rows(db) == before
    backups = list((db.parent / "backups").glob("*.sqlite3"))
    assert len(backups) == 1 and rows(backups[0]) == before


@pytest.mark.parametrize("date", [None, "", "not a date"])
def test_invalid_dates_abort_without_updates_or_backup(db, date):
    add_jobs(db, {"id": "valid"}, {"id": "invalid", "date_scraped": date})
    before = rows(db)

    with pytest.raises(ValueError, match="no scores were changed"):
        tool.run_rescore(db, apply=True)

    assert rows(db) == before
    assert not (db.parent / "backups").exists()


def test_missing_source_is_not_created(tmp_path):
    missing = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        tool.run_rescore(missing, apply=True)

    assert not missing.exists()


def test_competing_writer_is_blocked_until_rescore_finishes(db, monkeypatch):
    add_jobs(db, {})

    def rescore(job, now):
        with closing(sqlite3.connect(db, timeout=0)) as competitor:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                competitor.execute("UPDATE jobs SET status='interested'")
        return 70, ["updated"]

    monkeypatch.setattr(tool, "rescore", rescore)
    tool.run_rescore(db, apply=True)
    assert rows(db)["sample"]["status"] == "new"


def test_knocked_out_job_gets_zero():
    score, breakdown = tool.rescore(
        {"title": "Junior Developer", "location": "Dubai"}, datetime.now(timezone.utc)
    )
    assert score == 0
    assert breakdown[0].startswith("knocked out:")
