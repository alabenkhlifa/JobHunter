"""Grid-search the dimension weights against his own ratings.

The weights in job_scoring are hand-set. With enough labelled jobs a search
beats a guess, but only if it is measured on labels it never trained on --
five weights fitted on sixty examples overfit easily. So the search sees a
training split and the figure that matters is the held-out one.

The search is exhaustive over the simplex, not a nudge around the hand-set
weighting. That is deliberate: the spec's blocking question is whether stack
fit deserves 35 of 100 points at all, given that it is the weakest single
predictor (AUC 0.585) and rises with how MANY technologies a posting lists
rather than with how central his stack is. A grid that cannot reach stack 0
cannot answer that question, so this one can.

Imports job_scoring and eval_scoring, never scraper.py. The AUC is
eval_scoring's, not a second implementation that could disagree with it.
"""

import itertools
import json
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_scoring
import job_scoring

# Production sums the parts in the order WEIGHTS declares them. Float addition
# is not associative, so the order is part of the arithmetic and this tuple
# must stay production's; a test pins that.
DIMENSIONS = ("stack", "role", "seniority", "employer", "freshness")

# His four ratings, split into the two classes the AUC ranks. "good" and
# "excellent" are jobs he wants to be shown; "bad" and "normal" are jobs he
# does not. "normal" sits on the negative side on purpose: the digest has room
# for a handful of jobs a night, so a job worth nothing more than a shrug is
# one the scorer should rank below the ones he would open.
GOOD_LABELS = ("good", "excellent")
BAD_LABELS = ("bad", "normal")
RATINGS = GOOD_LABELS + BAD_LABELS

MARKETS = eval_scoring.MARKETS
ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = eval_scoring.DEFAULT_DB
# Exported from the labelling artifact's `labels` collection. Under data/, so
# .gitignore keeps his ratings out of the repository.
DEFAULT_RATINGS = ROOT / "data" / "labels-unbiased.json"

LABEL_SET_NOTE = (
    "unbiased: his ratings of a sample drawn across the whole corpus, read "
    "only from the ratings file. Not the 44 interested/skipped rows "
    "eval_scoring measures -- those cover only jobs the old scorer chose to "
    "show him, and mixing the two would fit the old scorer's bias back in."
)

NOT_APPLIED_NOTE = (
    "not applied: changing job_scoring.WEIGHTS is the user's decision. A "
    "test_auc well below train_auc means the search overfit and the hand-set "
    "weights stand."
)


def grid(step=5):
    """Every weighting on `step` boundaries that sums to 100."""
    values = range(0, 101, step)
    out = []
    for stack, role, seniority, employer in itertools.product(values, repeat=4):
        freshness = 100 - (stack + role + seniority + employer)
        if 0 <= freshness <= 100:
            out.append({
                "stack": stack, "role": role, "seniority": seniority,
                "employer": employer, "freshness": freshness,
            })
    return out


def _scored(job, *, freshness_neutral):
    """The parts production would total for this job, or None if knocked out.

    A knocked-out job totals 0 in production, and `reason` is set exactly when
    the knockouts fired, so None here means "floor it" rather than "score the
    parts". (`passed` and `band` are not gates; nothing reads them.)

    `freshness_neutral` pins freshness at its undated value, the same
    correction eval_scoring applies and for the same reason: the rated rows
    are stored ones carrying old posting dates, while in production every job
    is fresh when it is scored. Fitting on the stored dates would buy AUC from
    an artifact that does not transfer.

    Held constant, freshness stops being a dimension and becomes a lever: it
    is the same for every job that passed and 0 for every job knocked out, so
    a freshness-heavy fit is the search saying the knockouts alone rank better
    than the four dimensions do. Read the fitted freshness weight that way,
    not as a claim about posting dates.
    """
    result = job_scoring.evaluate(job, allowed_locations=MARKETS)
    if result["reason"]:
        return None
    parts = result["parts"]
    if freshness_neutral:
        parts = dict(parts, freshness=job_scoring.UNDATED_FRESHNESS)
    return parts


def _total(parts, weights):
    """The weighted sum, rounded exactly as job_scoring.evaluate rounds it.

    round() is banker's rounding: 44.5 goes down, 45.5 goes up. Fitting on
    unrounded totals would measure a scorer that does not ship, because the
    ties rounding creates each count half a win in the AUC.
    """
    if parts is None:
        return 0
    return round(sum(parts[name] * weights[name] for name in DIMENSIONS))


def _by_class(rows, *, freshness_neutral):
    """(positives, negatives) as scored parts, ready to weight repeatedly.

    Every job is put through `evaluate` once here rather than once per
    candidate weighting: the grid is ten thousand candidates wide.
    """
    def parts(wanted):
        return [_scored(job, freshness_neutral=freshness_neutral)
                for job, label in rows if (label in GOOD_LABELS) == wanted]

    return parts(True), parts(False)


def _split(labels, *, holdout, seed):
    """Shuffle and cut, keeping the class balance on both sides.

    Stratified because the sample is small: an unlucky cut that put every
    positive in the training half would leave a held-out AUC of 0.5 by
    definition, which is not a measurement of anything.
    """
    rng = random.Random(seed)
    train, test = [], []
    for wanted in (True, False):
        rows = [row for row in labels if (row[1] in GOOD_LABELS) == wanted]
        rng.shuffle(rows)
        cut = max(1, int(len(rows) * (1 - holdout)))
        train.extend(rows[:cut])
        test.extend(rows[cut:])
    return train, test


def _auc(positives, negatives, weights):
    return eval_scoring.auc(
        [_total(parts, weights) for parts in positives],
        [_total(parts, weights) for parts in negatives],
    )


def _distance(weights):
    """How far a candidate moves the hand-set weighting."""
    return sum(abs(weights[name] - job_scoring.WEIGHTS[name]) for name in DIMENSIONS)


def _search(positives, negatives, *, step):
    """The best-scoring weighting on this split, and its AUC.

    Ties are common -- sixty labels cannot separate ten thousand candidates --
    so a tie goes to the weighting nearest the hand-set one. Without that the
    winner is whichever the iteration order reached first, and the tool would
    report a wild weighting as if the labels had chosen it.
    """
    best, best_auc = dict(job_scoring.WEIGHTS), -1.0
    for weights in grid(step=step):
        score = _auc(positives, negatives, weights)
        if score > best_auc or (score == best_auc and _distance(weights) < _distance(best)):
            best, best_auc = weights, score
    return best, best_auc


def fit(labels, *, step=5, holdout=0.33, seed=11, freshness_neutral=True):
    """Search the grid on a training split, report on a held-out split."""
    train, test = _split(labels, holdout=holdout, seed=seed)
    train_pos, train_neg = _by_class(train, freshness_neutral=freshness_neutral)
    test_pos, test_neg = _by_class(test, freshness_neutral=freshness_neutral)

    best, best_auc = _search(train_pos, train_neg, step=step)
    held = _auc(test_pos, test_neg, best)
    return {"weights": best, "train_auc": round(best_auc, 3), "test_auc": round(held, 3)}


def report(labels, *, step=5, holdout=0.33, seed=11, freshness_neutral=True, source=""):
    """The fit, plus what is needed to judge whether to believe it.

    The fitted weighting alone says nothing: it has to be read against the
    hand-set weighting on the same splits (did the search beat the guess?) and
    against each dimension measured on its own (which dimension is carrying
    the ranking, and which is dead weight?).
    """
    train, test = _split(labels, holdout=holdout, seed=seed)
    train_pos, train_neg = _by_class(train, freshness_neutral=freshness_neutral)
    test_pos, test_neg = _by_class(test, freshness_neutral=freshness_neutral)

    hand_set = dict(job_scoring.WEIGHTS)
    solo = {
        name: round(eval_scoring.auc(
            [0.0 if parts is None else parts[name] for parts in train_pos],
            [0.0 if parts is None else parts[name] for parts in train_neg],
        ), 3)
        for name in DIMENSIONS
    }

    return {
        "source": str(source),
        "label_set": LABEL_SET_NOTE,
        "note": NOT_APPLIED_NOTE,
        "n_labels": len(labels),
        "n_positive": len(train_pos) + len(test_pos),
        "n_negative": len(train_neg) + len(test_neg),
        "n_train": len(train),
        "n_test": len(test),
        "n_knocked_out": sum(p is None for p in train_pos + train_neg + test_pos + test_neg),
        "freshness": ("held at the undated value, as in production -- so its "
                      "weight only trades the knockout split against the other "
                      "dimensions" if freshness_neutral
                      else "read from the stored posting dates"),
        "step": step,
        "candidates": len(grid(step=step)),
        "hand_set": {
            "weights": hand_set,
            "train_auc": round(_auc(train_pos, train_neg, hand_set), 3),
            "test_auc": round(_auc(test_pos, test_neg, hand_set), 3),
        },
        "fitted": fit(labels, step=step, holdout=holdout, seed=seed,
                      freshness_neutral=freshness_neutral),
        "dimension_auc": solo,
    }


def load_labels(path=DEFAULT_RATINGS, db_path=DEFAULT_DB):
    """His ratings, joined to the job rows the scorer reads.

    The export carries the rating and the job's identity, not the fields the
    rubric scores, so every `job_id` is looked up in data/jobs.db. A rating it
    cannot place, an unknown rating and a job rated twice are all refusals
    rather than silent drops: each would quietly change what is measured.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"no ratings at {path}. Export the labelling artifact's `labels` "
            "collection to that path -- a JSON list of documents, each with a "
            "`job_id` and a `label` of bad / normal / good / excellent -- or "
            "pass the file as the first argument."
        )
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        if "labels" not in raw:
            raise ValueError(
                f"{path} is an object without a `labels` key. Expected the "
                "collection itself: a JSON list of rating documents, or an "
                "object holding that list under `labels`."
            )
        raw = raw["labels"]
    if not raw:
        raise ValueError(f"{path} holds no ratings.")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        labels, seen, missing = [], set(), []
        for doc in raw:
            job_id, rating = doc.get("job_id"), doc.get("label")
            if rating not in RATINGS:
                raise ValueError(
                    f"{path}: job {job_id!r} carries the rating {rating!r}, "
                    f"which is not one of {', '.join(RATINGS)}."
                )
            if job_id in seen:
                raise ValueError(f"{path}: job {job_id!r} is rated more than once.")
            seen.add(job_id)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                missing.append(job_id)
                continue
            # Blanked the way eval_scoring blanks them: the scraper fills these
            # after scoring, so a stored row carries fields production never
            # sees at the moment it scores.
            labels.append((eval_scoring.as_scored_live(dict(row)), rating))
    finally:
        conn.close()

    if missing:
        raise ValueError(
            f"{len(missing)} rated job(s) are not in {db_path}: "
            f"{', '.join(map(repr, missing[:5]))}"
            f"{' ...' if len(missing) > 5 else ''}"
        )
    return labels


def _row(label, weights, train_auc, test_auc):
    cells = "  ".join(f"{weights[name]:>9}" for name in DIMENSIONS)
    return f"{label:<16}{cells}  {train_auc:>9}  {test_auc:>9}"


def _format(summary):
    lines = [
        f"{'ratings':22} {summary['n_labels']}  from {summary['source']}",
        f"{'label_set':22} {summary['label_set']}",
        f"{'positive':22} {summary['n_positive']}  (good, excellent)",
        f"{'negative':22} {summary['n_negative']}  (bad, normal)",
        f"{'split':22} {summary['n_train']} trained on / {summary['n_test']} held out",
        f"{'knocked out':22} {summary['n_knocked_out']}  (scored 0, as in production)",
        f"{'freshness':22} {summary['freshness']}",
        f"{'candidates':22} {summary['candidates']}  weightings at step {summary['step']}",
        "",
        _row("", {name: name[:9] for name in DIMENSIONS}, "train_auc", "test_auc"),
        _row("hand-set", summary["hand_set"]["weights"],
             summary["hand_set"]["train_auc"], summary["hand_set"]["test_auc"]),
        _row("fitted", summary["fitted"]["weights"],
             summary["fitted"]["train_auc"], summary["fitted"]["test_auc"]),
        "",
        "each dimension alone, on the training split:",
    ]
    for name, value in sorted(summary["dimension_auc"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:<12} {value}")
    lines += ["", summary["note"]]
    return "\n".join(lines)


def main(argv):
    """Load the ratings named on the command line and print the fit."""
    path = argv[0] if argv else DEFAULT_RATINGS
    try:
        labels = load_labels(path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"fit_weights: {exc}", file=sys.stderr)
        return 2
    print(_format(report(labels, source=path)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
