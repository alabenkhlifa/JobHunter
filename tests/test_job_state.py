import sqlite3
from datetime import datetime, timezone, timedelta

import scraper


def make_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE jobs (
            id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            location TEXT,
            url TEXT,
            source TEXT,
            score INTEGER,
            date_posted TEXT,
            date_scraped TEXT,
            notified INTEGER DEFAULT 0,
            description TEXT DEFAULT '',
            tech_required TEXT DEFAULT '',
            tech_nice_to_have TEXT DEFAULT '',
            min_experience INTEGER DEFAULT -1,
            salary TEXT DEFAULT '',
            work_model TEXT DEFAULT 'on-site',
            score_breakdown TEXT DEFAULT '',
            status TEXT DEFAULT 'new'
        )
        """
    )
    return conn


def insert_job(conn, job_id, *, status="new", notified=0, source="LinkedIn", scraped_at=None):
    scraped_at = scraped_at or datetime(2026, 7, 2, tzinfo=timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO jobs (
            id, title, company, location, url, source, score,
            date_posted, date_scraped, notified, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            job_id,
            f"Job {job_id}",
            "ExampleCo",
            "Dubai",
            f"https://example.com/{job_id}",
            source,
            20,
            scraped_at,
            scraped_at,
            notified,
            status,
        ),
    )
    conn.commit()


def test_get_job_status_summary_groups_by_status_notification_and_source():
    conn = make_conn()
    insert_job(conn, "fresh-1", status="new", notified=0, source="LinkedIn")
    insert_job(conn, "fresh-2", status="new", notified=0, source="Foundit")
    insert_job(conn, "sent-1", status="new", notified=1, source="LinkedIn")
    insert_job(conn, "skip-1", status="skipped", notified=1, source="LinkedIn")
    insert_job(conn, "int-1", status="interested", notified=1, source="Foundit")

    summary = scraper.get_job_status_summary(conn)

    assert summary["total"] == 5
    assert summary["unreviewed"] == 2
    assert summary["by_status"] == {"interested": 1, "new": 3, "skipped": 1}
    assert summary["by_source"] == {"Foundit": 2, "LinkedIn": 3}
    assert summary["by_status_and_notified"] == {
        "interested:notified": 1,
        "new:notified": 1,
        "new:unnotified": 2,
        "skipped:notified": 1,
    }


def test_archive_stale_unreviewed_jobs_only_archives_old_unnotified_new_jobs():
    conn = make_conn()
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    old = (now - timedelta(days=15)).isoformat()
    fresh = (now - timedelta(days=2)).isoformat()

    insert_job(conn, "old-new", status="new", notified=0, scraped_at=old)
    insert_job(conn, "fresh-new", status="new", notified=0, scraped_at=fresh)
    insert_job(conn, "old-notified", status="new", notified=1, scraped_at=old)
    insert_job(conn, "old-interested", status="interested", notified=0, scraped_at=old)
    insert_job(conn, "old-skipped", status="skipped", notified=0, scraped_at=old)

    dry_run_count = scraper.archive_stale_unreviewed_jobs(conn, older_than_days=7, now=now, dry_run=True)
    assert dry_run_count == 1
    assert conn.execute("SELECT status FROM jobs WHERE id = 'old-new'").fetchone()[0] == "new"

    archived_count = scraper.archive_stale_unreviewed_jobs(conn, older_than_days=7, now=now)

    assert archived_count == 1
    statuses = dict(conn.execute("SELECT id, status FROM jobs"))
    assert statuses == {
        "old-new": "archived",
        "fresh-new": "new",
        "old-notified": "new",
        "old-interested": "interested",
        "old-skipped": "skipped",
    }
