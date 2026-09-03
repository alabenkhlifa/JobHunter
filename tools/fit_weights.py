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

BIASED_LABEL_SET_NOTE = (
    "WARNING -- looks biased: every rated job is one the old scorer already "
    "sent or skipped, which is the 44-row set eval_scoring measures and the "
    "very sample this refit exists to escape. A weighting fitted here is "
    "fitted to what the old scorer chose to show him. Check the export."
)

NOT_APPLIED_NOTE = (
    "not applied: changing job_scoring.WEIGHTS is the user's decision. A "
    "test_auc well below train_auc means the search overfit and the hand-set "
    "weights stand."
)

# One split of ~20 held-out rows is high-variance: a different shuffle gives a
# different test_auc, and a conclusion drawn from whichever seed the tool
# happened to use is a conclusion drawn from the shuffle. Every seed is
# reported, so a wide range is visible as the finding it is.
SEEDS = (11, 23, 37, 53, 71)


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


def _require_both_classes(counts):
    """Refuse a split that cannot produce a measurement.

    `eval_scoring.auc` returns 0.5 for an empty side. That is the honest value
    for "undefined", but printed next to a fitted weighting it reads exactly
    like "the search overfit, so the hand-set weights stand" when the truth is
    that nothing was measured at all. At n=60 this is not a corner case: if
    roughly one job in twenty is worth sending, there is about a one-in-five
    chance the whole sample holds no more than a single good one, and the
    stratified cut then keeps that one for training. The number decides
    whether stack fit keeps 35 of 100 points, so it must not be allowed to
    lie -- it refuses instead.
    """
    for side, (positives, negatives) in counts.items():
        if not positives or not negatives:
            raise ValueError(
                f"the {side} split holds {positives} rated good/excellent and "
                f"{negatives} rated bad/normal, so its AUC is undefined and "
                "would print as 0.5. Rate more jobs -- and more of the scarce "
                "class -- rather than reading a number that was never "
                "measured."
            )


def fit(labels, *, step=5, holdout=0.33, seed=11, freshness_neutral=True):
    """Search the grid on a training split, report on a held-out split.

    Raises ValueError when either split lacks a class; see
    `_require_both_classes`.
    """
    train, test = _split(labels, holdout=holdout, seed=seed)
    train_pos, train_neg = _by_class(train, freshness_neutral=freshness_neutral)
    test_pos, test_neg = _by_class(test, freshness_neutral=freshness_neutral)
    _require_both_classes({
        "training": (len(train_pos), len(train_neg)),
        "held-out": (len(test_pos), len(test_neg)),
    })

    best, best_auc = _search(train_pos, train_neg, step=step)
    held = _auc(test_pos, test_neg, best)
    return {"weights": best, "train_auc": round(best_auc, 3), "test_auc": round(held, 3)}


def _label_set_note(labels):
    """Which label set this looks like, from the rows themselves.

    Asserting "unbiased" unconditionally would print "unbiased" over an export
    of the old interested/skipped rows. The give-away is that every rated job
    is one the old scorer already acted on; a sample drawn across the corpus
    is overwhelmingly rows it never showed him.
    """
    biased = {"interested", "skipped"}
    if labels and all((job.get("status") in biased) for job, _ in labels):
        return BIASED_LABEL_SET_NOTE
    return LABEL_SET_NOTE


def _on_grid(step):
    """Whether the hand-set weighting is itself a candidate at this step.

    It is not, at the default step: it carries 12 and 8, which are not
    multiples of 5. So the search can never return "your guess was right"
    verbatim, and a reader must not infer agreement or disagreement from the
    fitted row alone -- the side-by-side rows are the comparison.
    """
    return all(job_scoring.WEIGHTS[name] % step == 0 for name in DIMENSIONS)


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return round((ordered[middle - 1] + ordered[middle]) / 2, 3)


def report(labels, *, step=5, holdout=0.33, seeds=SEEDS, freshness_neutral=True, source=""):
    """The fit, plus what is needed to judge whether to believe it.

    The fitted weighting alone says nothing. It has to be read against the
    hand-set weighting on the same splits (did the search beat the guess?),
    against each dimension measured on its own (which dimension carries the
    ranking, and which is dead weight?), and across several shuffles (is the
    held-out figure a property of the labels or of the seed?).
    """
    seeds = tuple(seeds)
    per_seed = []
    for seed in seeds:
        train, test = _split(labels, holdout=holdout, seed=seed)
        train_pos, train_neg = _by_class(train, freshness_neutral=freshness_neutral)
        test_pos, test_neg = _by_class(test, freshness_neutral=freshness_neutral)
        fitted = fit(labels, step=step, holdout=holdout, seed=seed,
                     freshness_neutral=freshness_neutral)
        per_seed.append({
            "seed": seed,
            "weights": fitted["weights"],
            "train_auc": fitted["train_auc"],
            "test_auc": fitted["test_auc"],
            "hand_set_train_auc": round(_auc(train_pos, train_neg, job_scoring.WEIGHTS), 3),
            "hand_set_test_auc": round(_auc(test_pos, test_neg, job_scoring.WEIGHTS), 3),
            # Per class, not just the totals: a held-out AUC of 1.0 on one
            # positive against nineteen negatives is not the same claim as one
            # on ten against ten, and the totals alone cannot tell them apart.
            "held_out": (len(test_pos), len(test_neg)),
            "trained_on": (len(train_pos), len(train_neg)),
        })

    first = per_seed[0]
    train, _ = _split(labels, holdout=holdout, seed=seeds[0])
    train_pos, train_neg = _by_class(train, freshness_neutral=freshness_neutral)
    solo = {
        name: round(eval_scoring.auc(
            [0.0 if parts is None else parts[name] for parts in train_pos],
            [0.0 if parts is None else parts[name] for parts in train_neg],
        ), 3)
        for name in DIMENSIONS
    }
    every = _by_class(labels, freshness_neutral=freshness_neutral)

    def across(key):
        values = [row[key] for row in per_seed]
        return {"median": _median(values), "range": (min(values), max(values))}

    return {
        "source": str(source),
        "label_set": _label_set_note(labels),
        "note": NOT_APPLIED_NOTE,
        "n_labels": len(labels),
        "n_positive": len(every[0]),
        "n_negative": len(every[1]),
        "n_knocked_out": sum(parts is None for parts in every[0] + every[1]),
        "freshness": ("held at the undated value, as in production -- so its "
                      "weight only trades the knockout split against the other "
                      "dimensions" if freshness_neutral
                      else "read from the stored posting dates"),
        "step": step,
        "candidates": len(grid(step=step)),
        "seeds": seeds,
        "per_seed": per_seed,
        "fitted_test_auc": across("test_auc"),
        "hand_set_test_auc": across("hand_set_test_auc"),
        "weight_range": {
            name: (min(row["weights"][name] for row in per_seed),
                   max(row["weights"][name] for row in per_seed))
            for name in DIMENSIONS
        },
        "hand_set_on_grid": _on_grid(step),
        "hand_set": {
            "weights": dict(job_scoring.WEIGHTS),
            "train_auc": first["hand_set_train_auc"],
            "test_auc": first["hand_set_test_auc"],
        },
        "fitted": {
            "weights": first["weights"],
            "train_auc": first["train_auc"],
            "test_auc": first["test_auc"],
        },
        "dimension_auc": solo,
    }


def load_labels(path=None, db_path=None):
    """His ratings, joined to the job rows the scorer reads.

    The export carries the rating and the job's identity, not the fields the
    rubric scores, so every `job_id` is looked up in data/jobs.db. A rating it
    cannot place, an unknown rating and a job rated twice are all refusals
    rather than silent drops: each would quietly change what is measured.
    """
    # Resolved here, not in the signature: a default bound at definition time
    # cannot be pointed anywhere else, including at a copy of the database.
    path = Path(DEFAULT_RATINGS if path is None else path)
    db_path = DEFAULT_DB if db_path is None else db_path
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
        labels, seen, missing, fixtures = [], set(), [], []
        for position, doc in enumerate(raw):
            if not isinstance(doc, dict):
                raise ValueError(
                    f"{path}: entry {position} is {type(doc).__name__}, not a "
                    "rating document. Each entry must be an object with a "
                    "`job_id` and a `label`."
                )
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
            job = dict(row)
            # eval_scoring's exclusion, called rather than restated. A row the
            # test harness wrote is not a posting anyone published, and one of
            # them carries a hand-set score of 99; if the sampler drew one, his
            # rating of it says nothing about ranking real jobs.
            if eval_scoring.is_fixture(job):
                fixtures.append(job_id)
                continue
            # Blanked the way eval_scoring blanks them: the scraper fills these
            # after scoring, so a stored row carries fields production never
            # sees at the moment it scores.
            labels.append((eval_scoring.as_scored_live(job), rating))
    finally:
        conn.close()

    if missing:
        raise ValueError(
            f"{len(missing)} rated job(s) are not in {db_path}: "
            f"{', '.join(map(repr, missing[:5]))}"
            f"{' ...' if len(missing) > 5 else ''}"
        )
    if fixtures:
        print(
            f"fit_weights: dropped {len(fixtures)} test-harness row(s) the "
            f"sampler drew: {', '.join(map(repr, fixtures))}",
            file=sys.stderr,
        )
    if not labels:
        raise ValueError(f"{path}: nothing left to fit once the fixtures are dropped.")
    return labels


def _row(label, weights, train_auc, test_auc):
    cells = "  ".join(f"{weights[name]:>9}" for name in DIMENSIONS)
    return f"{label:<16}{cells}  {train_auc:>9}  {test_auc:>9}"


def _format(summary):
    first = summary["per_seed"][0]
    lines = [
        f"{'ratings':22} {summary['n_labels']}  from {summary['source']}",
        f"{'label_set':22} {summary['label_set']}",
        f"{'positive':22} {summary['n_positive']}  (good, excellent)",
        f"{'negative':22} {summary['n_negative']}  (bad, normal)",
        f"{'knocked out':22} {summary['n_knocked_out']}  (scored 0, as in production)",
        f"{'freshness':22} {summary['freshness']}",
        f"{'candidates':22} {summary['candidates']}  weightings at step {summary['step']}",
        "",
        _row("", {name: name[:9] for name in DIMENSIONS}, "train_auc", "test_auc"),
        _row(f"hand-set", summary["hand_set"]["weights"],
             summary["hand_set"]["train_auc"], summary["hand_set"]["test_auc"]),
        _row(f"fitted seed {first['seed']}", summary["fitted"]["weights"],
             summary["fitted"]["train_auc"], summary["fitted"]["test_auc"]),
    ]
    if not summary["hand_set_on_grid"]:
        lines.append(
            f"  the hand-set weighting is not on the step-{summary['step']} grid, so "
            "the search cannot return it; read the two rows side by side."
        )
    lines += [
        "",
        f"every split, across {len(summary['seeds'])} seeds "
        "(good/bad counts are the held-out side):",
        f"  {'seed':>6}  {'held out':>10}  {'fitted test':>11}  "
        f"{'hand-set test':>13}  {'fitted stack':>12}",
    ]
    for row in summary["per_seed"]:
        held = f"{row['held_out'][0]}g / {row['held_out'][1]}b"
        lines.append(
            f"  {row['seed']:>6}  {held:>10}  {row['test_auc']:>11}  "
            f"{row['hand_set_test_auc']:>13}  {row['weights']['stack']:>12}"
        )
    fitted, hand = summary["fitted_test_auc"], summary["hand_set_test_auc"]

    def span(pair):
        return f"{pair[0]}-{pair[1]}"

    lines += [
        f"  {'median':>6}  {'':>10}  {fitted['median']:>11}  {hand['median']:>13}",
        f"  {'range':>6}  {'':>10}  {span(fitted['range']):>11}  "
        f"{span(hand['range']):>13}  {span(summary['weight_range']['stack']):>12}",
        "",
        "a wide range across seeds means the labels cannot settle the question",
        "and no single split's number should be quoted as the answer.",
        "",
        "each dimension alone, on the training split:",
    ]
    for name, value in sorted(summary["dimension_auc"].items(), key=lambda kv: -kv[1]):
        lines.append(f"  {name:<12} {value}")
    lines += ["", summary["note"]]
    return "\n".join(lines)


def main(argv):
    """Load the ratings named on the command line and print the fit.

    Usage: fit_weights.py [ratings.json] [jobs.db]
    """
    path = argv[0] if argv else DEFAULT_RATINGS
    db_path = argv[1] if len(argv) > 1 else None
    try:
        labels = load_labels(path, db_path=db_path)
        summary = report(labels, source=path)
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"fit_weights: {exc}", file=sys.stderr)
        return 2
    print(_format(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
