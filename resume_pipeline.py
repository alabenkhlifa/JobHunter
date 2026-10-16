"""Blueprint-driven resume generation: select confirmed evidence, write the summary, fit, score.

The pipeline replaces the frozen variant snapshots and their hard gates. It
never composes bullet wording; the only generated text is the summary, which
is validated against the selected evidence and falls back to the blueprint's
confirmed summary when the model is unavailable or its draft fails validation.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field

import resume_blueprints as blueprints
import resume_evidence as evidence
import resume_layout as layout
import resume_quality
import resume_scorecard as scorecard

SUMMARY_GUIDANCE = (
    " summary_focus lists the kinds of work this role family leads with; draw the second and third sentences from the "
    "selected evidence of those kinds (focus_evidence_ids). Use at most two figures in the whole summary: the bullets "
    "already carry the numbers, so describe other scope in words."
)
BULLET_ORDERING = "evidence_facets_v1"


class BlueprintError(blueprints.BlueprintError):
    """Re-exported for callers that only import the pipeline."""


@dataclass
class Plan:
    blueprint: dict
    needs: evidence.PostingNeeds
    selection: evidence.Selection
    payload: dict
    scores: dict = field(default_factory=dict)
    openings: set = field(default_factory=set)


def enabled(profile):
    return blueprints.enabled(profile)


def plan(profile, job):
    chosen = blueprints.choose(profile, job)
    if chosen is None:
        raise blueprints.BlueprintError("No confirmed resume blueprint matches this posting and none is marked default")
    selection = evidence.select(profile, job, chosen)
    payload = blueprints.build(profile, job, chosen, selection)
    scores = {b.text: selection.scores[b.id] for p in selection.projects for b in p.bullets}
    openings = {p.bullets[0].text for p in selection.projects if p.bullets}
    return Plan(blueprint=chosen, needs=selection.needs, selection=selection, payload=payload,
                scores=scores, openings=openings)


def summary_context(profile, job, plan_, payload=None):
    """The legacy summary context plus the blueprint's focus, with its own digest."""
    view = copy.deepcopy(profile)
    (view.get("resume_quality") or {}).pop("summary_priorities", None)
    context = resume_quality.summary_context(view, job, payload or plan_.payload)
    context.pop("context_sha256", None)
    focus = list(plan_.blueprint.get("summary_focus") or [])
    context["summary_focus"] = focus
    context["focus_evidence_ids"] = [b.id for p in plan_.selection.projects for b in p.bullets if b.kind in focus]
    context["context_sha256"] = hashlib.sha256(json.dumps(context, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return context


def _trim_flags(draft):
    draft = dict(draft)
    flags = draft.get("review_flags")
    if isinstance(flags, list):
        draft["review_flags"] = [str(f)[:300] for f in flags[:10]]
    return draft


def summarize(profile, job, plan_, *, planner=None, draft=None):
    """Return (summary text, info). A supplied draft must validate; a model failure falls back to the blueprint."""
    context = summary_context(profile, job, plan_)
    info = {"source": "fallback", "review_verdict": None, "flags": [], "evidence_ids": [],
            "context_sha256": context["context_sha256"], "reason": ""}
    if draft is not None:
        validated = resume_quality.validate_summary(_trim_flags(draft), context)
        info.update(source="provided_reviewed_draft", evidence_ids=list(validated["evidence_ids"]),
                    flags=list(validated["review_flags"]))
        return validated["text"], info
    try:
        model = planner if planner is not None else resume_quality.owner_planner()
        raw = model([{"role": "system", "content": resume_quality.SUMMARY_PROMPT + SUMMARY_GUIDANCE},
                     {"role": "user", "content": json.dumps(context, ensure_ascii=False)}], resume_quality.SUMMARY_SCHEMA)
        validated = resume_quality.validate_summary(_trim_flags(raw) if isinstance(raw, dict) else raw, context)
    except Exception as exc:  # noqa: BLE001 - any failure here means the confirmed fallback wording
        info["reason"] = str(exc) if isinstance(exc, resume_quality.ResumeQualityError) else type(exc).__name__
        return plan_.blueprint["summary"], info
    info.update(source="model", evidence_ids=list(validated["evidence_ids"]), flags=list(validated["review_flags"]))
    try:
        review = model([{"role": "system", "content": resume_quality.REVIEW_PROMPT},
                        {"role": "user", "content": json.dumps({"context": context, "draft": validated}, ensure_ascii=False)}],
                       resume_quality.REVIEW_SCHEMA)
        verdict = review.get("verdict") if isinstance(review, dict) else None
        issues = [str(i)[:300] for i in (review.get("blocking_evidence_issues") or [])][:10] if isinstance(review, dict) else []
        if verdict == "ready":
            revised = resume_quality.validate_summary(_trim_flags(review["revised_draft"]), context)
            validated = revised
            info.update(review_verdict="ready", evidence_ids=list(revised["evidence_ids"]), flags=list(revised["review_flags"]))
        elif verdict == "blocked":
            info.update(review_verdict="blocked", flags=issues)
        else:
            info["review_verdict"] = "invalid"
    except Exception as exc:  # noqa: BLE001 - the validated draft stands; the review is advisory
        info["review_verdict"] = "unavailable"
        info["flags"].append(f"review unavailable: {type(exc).__name__}")
    return validated["text"], info


def _irrelevant_certifications(profile, job, payload):
    policy = profile.get("resume_quality") or {}
    text = evidence._text(" ".join(str(job.get(k) or "") for k in ("title", "description", "tech_required")))
    result = []
    for cert in policy.get("omittable_certifications", []):
        if cert not in (payload.get("certifications") or []) or cert in policy.get("protected_certifications", []):
            continue
        tags = [cert, *policy.get("certification_tags", {}).get(cert, [])]
        if not any(evidence._contains(text, evidence._text(tag)) for tag in tags if evidence._text(tag)):
            result.append(cert)
    return result


def fit(plan_, payload, profile, job, render):
    """Fit the payload to the blueprint's pages; returns (payload, layout report)."""
    return layout.fit_pages(payload, render, plan_.scores, pages=int(plan_.blueprint.get("pages") or 1),
                            openings=plan_.openings, omittable_certifications=_irrelevant_certifications(profile, job, payload))


def assess(plan_, payload, profile, *, layout_report, summary_info, audit):
    return scorecard.score_package(payload, profile, plan_.needs, plan_.blueprint,
                                   layout=layout_report, summary=summary_info, audit=audit)


def manifest_fields(plan_, payload, *, layout_report, summary_info, card):
    """The private manifest entries a blueprint package records next to the legacy keys."""
    return {
        "tailoring_mode": "blueprint", "bullet_ordering": BULLET_ORDERING,
        "selected_variant_id": None, "selected_blueprint_id": plan_.blueprint["id"],
        "resume_score": card["score"], "resume_needs_review": card["needs_review"], "score_flags": card["flags"],
        "scorecard": {"dimensions": card["dimensions"], "critical": card["critical"], "threshold": card["threshold"]},
        "selection": {"family": plan_.needs.family, "seniority": plan_.needs.seniority,
                      "stack": sorted(plan_.needs.stack), "domains": sorted(plan_.needs.domains),
                      "projects": [{"name": p.name, "company": p.company, "evidence_ids": [b.id for b in p.bullets]}
                                   for p in plan_.selection.projects]},
        "layout": layout_report,
        "summary_review": {"text": payload.get("summary", ""), **summary_info},
        "evidence_proposal_applied": False, "proposed_evidence_updates": [], "proposed_evidence_omissions": [],
        "content_review_revision_rounds": 0,
    }
