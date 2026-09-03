import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import eval_scoring
import fit_weights
import job_scoring


JOB = {
    "title": "Software Architect", "company": "Acme", "location": "Dubai",
    "tech_required": "java, spring boot, aws", "min_experience": 6,
    "description": "", "company_website": "https://acme.example",
    "recruiter_company": "", "date_posted": "", "tech_nice_to_have": "",
}


def _labels(n=20):
    return [
        (dict(JOB, title=f"Architect {i}", company=f"Acme {i}"),
         "excellent" if i % 2 else "bad")
        for i in range(n)
    ]


def test_every_candidate_weighting_sums_to_one_hundred():
    for weights in fit_weights.grid(step=20):
        assert sum(weights.values()) == 100


def test_the_grid_is_exhaustive_at_its_step():
    coarse = fit_weights.grid(step=50)
    assert {"stack": 100, "role": 0, "seniority": 0, "employer": 0, "freshness": 0} in coarse
    assert {"stack": 50, "role": 50, "seniority": 0, "employer": 0, "freshness": 0} in coarse
    assert all(sum(w.values()) == 100 for w in coarse)


def test_fit_holds_out_part_of_the_labels_from_training():
    result = fit_weights.fit(_labels(), step=25)
    assert set(result) == {"weights", "train_auc", "test_auc"}
    assert sum(result["weights"].values()) == 100


def test_the_default_grid_can_drop_stack_fit_or_shrink_it_to_ten():
    # The blocking question is whether stack fit deserves 35 of 100 points.
    # A search that can only nudge the hand-set weighting cannot answer it,
    # so the default grid must reach both 0 and 10 for that dimension.
    weightings = fit_weights.grid()
    assert any(w["stack"] == 0 for w in weightings)
    assert any(w["stack"] == 10 for w in weightings)
    assert len(weightings) == 10626


def test_the_dimensions_are_productions_weights_in_productions_order():
    # The total is a float sum, so the order of the terms is part of the
    # arithmetic. Iterating a different order could round differently.
    assert fit_weights.DIMENSIONS == tuple(job_scoring.WEIGHTS)


def test_the_total_is_the_one_production_ships():
    production = job_scoring.evaluate(JOB, allowed_locations=fit_weights.MARKETS)
    parts = fit_weights._scored(JOB, freshness_neutral=False)
    assert fit_weights._total(parts, job_scoring.WEIGHTS) == production["total"]


def test_the_total_rounds_the_way_production_rounds():
    # round() is banker's rounding: 44.5 goes DOWN, 45.5 goes UP. Fitting on
    # unrounded totals would measure a different scorer, because the ties
    # rounding creates count half a win each in the AUC.
    half = {"stack": 0.5, "role": 0.0, "seniority": 0.0, "employer": 0.0, "freshness": 0.0}
    assert fit_weights._total(half, {"stack": 89, "role": 11, "seniority": 0,
                                     "employer": 0, "freshness": 0}) == 44
    assert fit_weights._total(half, {"stack": 91, "role": 9, "seniority": 0,
                                     "employer": 0, "freshness": 0}) == 46


def test_a_knocked_out_job_scores_zero_even_with_freshness_held_constant():
    # Production totals a knocked-out job at 0. Pinning freshness must not
    # lift it off the floor and turn a rejected job into a ranked one.
    outside = dict(JOB, location="Berlin")
    assert fit_weights._scored(outside, freshness_neutral=True) is None
    assert fit_weights._total(None, job_scoring.WEIGHTS) == 0


def test_good_and_excellent_are_positives_and_bad_and_normal_are_negatives():
    assert fit_weights.GOOD_LABELS == ("good", "excellent")
    assert fit_weights.BAD_LABELS == ("bad", "normal")
    rows = [(JOB, label) for label in ("bad", "normal", "good", "excellent")]
    positives, negatives = fit_weights._by_class(rows, freshness_neutral=True)
    assert len(positives) == 2 and len(negatives) == 2


def test_the_split_keeps_both_ratings_on_both_sides():
    # Five weights on sixty labels overfit trivially, and an AUC on a split
    # holding only one class is 0.5 by definition, not a measurement.
    train, test = fit_weights._split(_labels(), holdout=0.33, seed=11)
    assert len(train) + len(test) == 20
    assert not (set(id(j) for j, _ in train) & set(id(j) for j, _ in test))
    for part in (train, test):
        labels = {label for _, label in part}
        assert labels == {"excellent", "bad"}


def test_fit_refuses_a_held_out_split_with_only_one_class():
    # An AUC on an empty side is 0.5 by definition, and 0.5 printed next to a
    # fitted weighting reads exactly like "the search overfit, hand-set
    # stands" when in truth nothing was measured. It must refuse, not print.
    lopsided = [(JOB, "excellent")] + [(JOB, "bad")] * 19
    try:
        fit_weights.fit(lopsided, step=50)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "held-out" in str(exc)
        assert "0 rated good/excellent" in str(exc)


def test_fit_refuses_when_a_class_is_missing_altogether():
    try:
        fit_weights.fit([(JOB, "bad")] * 10, step=50)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "training" in str(exc)


def test_report_names_the_label_file_and_the_label_set_it_used():
    summary = fit_weights.report(_labels(), step=50, source="data/labels-unbiased.json")
    assert summary["source"] == "data/labels-unbiased.json"
    assert "unbiased" in summary["label_set"]
    assert "eval_scoring" in summary["label_set"]
    assert summary["n_positive"] == 10 and summary["n_negative"] == 10
    assert set(summary["hand_set"]) == {"weights", "train_auc", "test_auc"}
    assert set(summary["fitted"]) == {"weights", "train_auc", "test_auc"}
    assert set(summary["dimension_auc"]) == set(fit_weights.DIMENSIONS)


def test_report_warns_when_every_rated_job_is_one_the_old_scorer_acted_on():
    # An export of the biased 44 would otherwise print "unbiased" over it.
    biased = [(dict(job, status="skipped" if i % 2 else "interested"), label)
              for i, (job, label) in enumerate(_labels())]
    summary = fit_weights.report(biased, step=50, source="x.json")
    assert "WARNING" in summary["label_set"]
    assert "biased" in summary["label_set"]
    assert summary["label_set"] in fit_weights._format(summary)


def test_report_shows_the_held_out_counts_per_class_not_just_the_total():
    # A held-out AUC of 1.0 on 1 positive against 19 negatives is not the
    # same claim as one on 10 against 10, and a total cannot tell them apart.
    summary = fit_weights.report(_labels(), step=50, source="x.json")
    for row in summary["per_seed"]:
        good, bad = row["held_out"]
        assert good and bad
        assert good + bad + sum(row["trained_on"]) == 20
    assert "4g / 4b" in fit_weights._format(summary)


def test_report_spans_several_seeds_so_one_shuffle_cannot_decide():
    summary = fit_weights.report(_labels(), step=50, source="x.json")
    assert summary["seeds"] == fit_weights.SEEDS
    assert {row["seed"] for row in summary["per_seed"]} == set(fit_weights.SEEDS)
    low, high = summary["fitted_test_auc"]["range"]
    assert low <= summary["fitted_test_auc"]["median"] <= high
    assert set(summary["weight_range"]) == set(fit_weights.DIMENSIONS)


def test_report_says_the_hand_set_weighting_is_not_on_the_default_grid():
    # 12 and 8 are not multiples of 5, so the search can never return the
    # hand-set weighting and "it disagrees with your guess" cannot be read
    # off the fitted row alone.
    assert fit_weights._on_grid(5) is False
    assert fit_weights._on_grid(1) is True
    assert fit_weights.report(_labels(), step=50)["hand_set_on_grid"] is False
    printed = fit_weights._format(fit_weights.report(_labels(), step=50))
    assert "not on the step-50 grid" in printed


def test_printing_the_report_says_which_labels_it_read():
    summary = fit_weights.report(_labels(), step=50, source="somewhere/ratings.json")
    lines = fit_weights._format(summary)
    assert "somewhere/ratings.json" in lines
    assert summary["label_set"] in lines
    assert "train_auc" in lines and "test_auc" in lines
    assert "median" in lines and "range" in lines


def test_missing_ratings_are_a_message_not_a_traceback(tmp_path, capsys):
    absent = tmp_path / "labels-unbiased.json"
    code = fit_weights.main([str(absent)])
    assert code == 2
    err = capsys.readouterr().err
    assert str(absent) in err
    assert "Traceback" not in err


def _db(tmp_path, rows):
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id, title, company, location, tech_required)")
    for row in rows:
        conn.execute("INSERT INTO jobs VALUES (?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()
    return path


def test_load_labels_joins_the_ratings_to_their_job_rows(tmp_path):
    db = _db(tmp_path, [
        ("j1", "Software Architect", "Acme", "Dubai", "java, spring boot"),
        ("j2", "Office Manager", "Globex", "Dubai", ""),
    ])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([
        {"job_id": "j1", "label": "excellent"},
        {"job_id": "j2", "label": "bad"},
    ]))
    labels = fit_weights.load_labels(path, db_path=db)
    assert [(job["company"], label) for job, label in labels] == [
        ("Acme", "excellent"), ("Globex", "bad")]
    # Blanked the same way eval_scoring blanks them: the scraper fills these
    # after scoring, so a stored row carries fields production never sees.
    assert all(job["recruiter_company"] == "" for job, _ in labels)


def test_load_labels_accepts_the_collection_wrapped_in_its_name(tmp_path):
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps({"labels": [{"job_id": "j1", "label": "good"}]}))
    assert len(fit_weights.load_labels(path, db_path=db)) == 1


def test_load_labels_drops_the_rows_the_test_harness_wrote(tmp_path, capsys):
    # One of them carries a hand-set score of 99. Reuses eval_scoring's
    # exclusion rather than restating it, so there is one definition.
    db = _db(tmp_path, [
        ("real", "Software Architect", "Acme", "Dubai", "java"),
        ("fake-company", "Software Architect", "Example SaaS", "Dubai", "java"),
        ("fake-title", "TEST RUN — Lead Backend", "Initech", "Dubai", "java"),
        ("fake-cta", "CTA Button Test — Lead", "Initech", "Dubai", "java"),
    ])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([
        {"job_id": job_id, "label": "good"}
        for job_id in ("real", "fake-company", "fake-title", "fake-cta")
    ]))
    labels = fit_weights.load_labels(path, db_path=db)
    assert [job["id"] for job, _ in labels] == ["real"]
    assert "dropped 3 test-harness row(s)" in capsys.readouterr().err


def test_the_fixture_rule_is_eval_scorings_and_not_a_second_copy():
    assert eval_scoring.is_fixture({"title": "x", "company": "Example SaaS"})
    assert eval_scoring.is_fixture({"title": "TEST RUN — x", "company": "Acme"})
    assert eval_scoring.is_fixture({"title": "CTA Button Test", "company": "Acme"})
    assert not eval_scoring.is_fixture({"title": "Software Architect", "company": "Acme"})
    assert not eval_scoring.is_fixture({"title": None, "company": None})


def test_load_labels_refuses_an_entry_that_is_not_a_rating_document(tmp_path):
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([{"job_id": "j1", "label": "good"}, "j2"]))
    try:
        fit_weights.load_labels(path, db_path=db)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "entry 1" in str(exc) and "str" in str(exc)


def test_load_labels_refuses_a_rating_it_cannot_place(tmp_path):
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([{"job_id": "gone", "label": "good"}]))
    try:
        fit_weights.load_labels(path, db_path=db)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "gone" in str(exc)


def test_load_labels_refuses_an_unknown_rating(tmp_path):
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([{"job_id": "j1", "label": "maybe"}]))
    try:
        fit_weights.load_labels(path, db_path=db)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "maybe" in str(exc)


def test_load_labels_refuses_the_same_job_rated_twice(tmp_path):
    # A duplicated export would weight that job twice in the AUC.
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([
        {"job_id": "j1", "label": "good"},
        {"job_id": "j1", "label": "bad"},
    ]))
    try:
        fit_weights.load_labels(path, db_path=db)
        raise AssertionError("expected a refusal")
    except ValueError as exc:
        assert "j1" in str(exc)


def test_load_labels_ignores_the_biased_labels_eval_scoring_measures(tmp_path):
    # The 44 interested/skipped rows only cover jobs the old scorer chose to
    # show him. The ratings file is the only source of a label here: a row
    # eval_scoring would call a positive is absent unless it was rated, and a
    # rated row keeps its rating whatever the old scorer recorded.
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE jobs (id, title, company, location, tech_required, status)")
    conn.executemany("INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?)", [
        ("rated", "Software Architect", "Acme", "Dubai", "java", "skipped"),
        ("unrated", "Solution Architect", "Globex", "Dubai", "java", "interested"),
    ])
    conn.commit()
    conn.close()
    ratings = tmp_path / "labels.json"
    ratings.write_text(json.dumps([{"job_id": "rated", "label": "excellent"}]))
    labels = fit_weights.load_labels(ratings, db_path=path)
    assert [(job["id"], label) for job, label in labels] == [("rated", "excellent")]


def test_a_refusal_to_measure_is_also_a_message_not_a_traceback(tmp_path, capsys):
    # The refusal has to survive the command line too: a traceback here would
    # be read as a broken tool rather than as "these labels cannot answer it".
    db = _db(tmp_path, [("j1", "Software Architect", "Acme", "Dubai", "java")])
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([{"job_id": "j1", "label": "good"}]))
    assert fit_weights.main([str(path), str(db)]) == 2
    captured = capsys.readouterr()
    assert "AUC is undefined" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
