import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import eval_scoring


def test_auc_is_one_when_every_positive_outranks_every_negative():
    assert eval_scoring.auc([90, 80, 70], [60, 50]) == 1.0


def test_auc_is_zero_when_the_ranking_is_exactly_backwards():
    assert eval_scoring.auc([10, 20], [80, 90]) == 0.0


def test_auc_counts_a_tie_as_half_a_win():
    assert eval_scoring.auc([50], [50]) == 0.5


def test_auc_of_an_empty_side_is_undefined_and_reported_as_a_half():
    assert eval_scoring.auc([], [70]) == 0.5


COLUMNS = (
    "title", "company", "location", "status", "description", "tech_required",
    "tech_nice_to_have", "min_experience", "date_posted", "recruiter_company",
    "credibility_notes", "score",
)


def _db(tmp_path, rows):
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.execute(f"CREATE TABLE jobs ({', '.join(COLUMNS)})")
    for row in rows:
        full = {c: None for c in COLUMNS}
        full.update(row)
        conn.execute(
            f"INSERT INTO jobs ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
            [full[c] for c in COLUMNS],
        )
    conn.commit()
    conn.close()
    return str(path)


def test_load_labels_leaves_out_the_test_fixtures(tmp_path):
    path = _db(tmp_path, [
        {"title": "Software Architect", "company": "Acme", "status": "interested"},
        {"title": "Backend Engineer", "company": "Globex", "status": "skipped"},
        {"title": "CTA Button Test — Lead Backend", "company": "JobHunter Test",
         "status": "skipped", "score": 99},
        {"title": "TEST RUN — Lead Backend Engineer", "company": "Example FinTech",
         "status": "skipped"},
        {"title": "TEST RUN — Solution Architect", "company": "Example Consulting",
         "status": "skipped"},
        {"title": "Real Architect", "company": "Example SaaS", "status": "interested"},
        {"title": "Untouched", "company": "Initech", "status": "new"},
    ])
    interested, skipped = eval_scoring.load_labels(path)
    assert [j["company"] for j in interested] == ["Acme"]
    assert [j["company"] for j in skipped] == ["Globex"]


def test_report_counts_the_interested_jobs_the_knockouts_removed(tmp_path):
    path = _db(tmp_path, [
        {"title": "Software Architect", "company": "Acme", "location": "Dubai",
         "status": "interested", "tech_required": "java, spring boot, aws"},
        {"title": "Software Architect", "company": "Acme", "location": "Berlin",
         "status": "interested", "tech_required": "java, spring boot, aws"},
        {"title": "Junior Developer", "company": "Acme", "location": "Dubai",
         "status": "interested"},
        {"title": "Office Manager", "company": "Globex", "location": "Dubai",
         "status": "skipped"},
    ])
    result = eval_scoring.report(path)
    assert result["n_positive"] == 3
    assert result["n_negative"] == 1
    assert result["baseline_auc"] == 0.565
    assert result["above_cutoff"] == "1/3"
    assert result["knocked_out_positives"] == {
        "outside the configured markets: Berlin": 1,
        "too junior: junior": 1,
    }
    # The one that survived outranks the skip; the two knocked out score 0 and tie
    # or lose against the skipped job's real total.
    assert 0.0 < result["auc"] < 1.0


def test_report_also_measures_auc_with_freshness_held_constant(tmp_path):
    # Same job twice: the interested copy is undated (0.7), the skipped copy
    # carries an old date (0.2). Raw AUC rewards "has no date"; the neutral
    # figure must not.
    job = {"title": "Software Architect", "company": "Acme", "location": "Dubai",
           "tech_required": "java, spring boot, aws"}
    path = _db(tmp_path, [
        {**job, "status": "interested", "date_posted": ""},
        {**job, "status": "skipped", "date_posted": "2020-01-01"},
    ])
    result = eval_scoring.report(path)
    assert result["auc"] == 1.0
    assert result["auc_freshness_neutral"] == 0.5


def test_report_says_it_is_an_in_sample_measurement(tmp_path, capsys):
    path = _db(tmp_path, [
        {"title": "Software Architect", "company": "Acme", "location": "Dubai",
         "status": "interested"},
        {"title": "Office Manager", "company": "Globex", "location": "Dubai",
         "status": "skipped"},
    ])
    result = eval_scoring.report(path)
    note = result["in_sample"]
    assert "same rows" in note and "Task 9" in note
    eval_scoring._print(result)
    out = capsys.readouterr().out
    assert note in out
    assert "auc_freshness_neutral" in out
