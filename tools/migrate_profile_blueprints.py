#!/usr/bin/env python3
"""Add evidence facets, indexed confirmed wording, engagement roles and role blueprints to a private profile.

The updates file is private JSON::

    {"facets": {"<evidence id>": {"project": ..., "kind": ..., "stack": [...], "domains": [...]}},
     "new_evidence": [<evidence_bank items whose public_text already exists as confirmed wording>],
     "engagement_roles": {"<company>": {"<engagement name>": "<confirmed role title>"}},
     "resume_blueprints": [<blueprints>],
     "hide_from": {"<evidence id>": ["resume"]}}

``hide_from`` removes the listed values from an evidence item's ``visibility``,
so a confirmed bullet stops appearing on resumes without being deleted.

Nothing new is worded here: an indexed item is refused unless its text already
appears verbatim in the candidate's confirmed experience, employment groups,
confirmed variants or evidence bank. Dry run by default; ``--apply`` writes a
timestamped backup and replaces the profile atomically.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import resume_blueprints  # noqa: E402
from resume_inline import normalized_text  # noqa: E402
from resume_refiner import ProfileValidationError, validate_profile  # noqa: E402


def confirmed_wording(profile):
    approved = {normalized_text(e.get("public_text", "")) for e in profile.get("evidence_bank") or []
                if isinstance(e, dict) and e.get("confirmation") == "candidate-confirmed"}
    approved.update(normalized_text(b) for r in profile.get("experience") or [] for b in r.get("bullets") or [])
    approved.update(normalized_text(b) for g in profile.get("employment_groups") or []
                    if g.get("confirmation") == "candidate-reviewed"
                    for e in g.get("engagements") or [] for b in e.get("bullets") or [])
    approved.update(normalized_text(b) for v in profile.get("resume_variants") or []
                    if v.get("confirmation") == "candidate-confirmed"
                    for r in v.get("resume", {}).get("experience") or [] for b in r.get("bullets") or [])
    return approved


def merge(profile, updates):
    """Return (merged profile, report). Raises ValueError for anything that is not already confirmed."""
    merged = copy.deepcopy(profile)
    report = {"facets": 0, "new_evidence": 0, "skipped_evidence": 0, "engagement_roles": 0, "blueprints": 0,
              "hidden": 0}
    bank = {e.get("id"): e for e in merged.get("evidence_bank") or [] if isinstance(e, dict)}
    for evidence_id, facets in (updates.get("facets") or {}).items():
        if evidence_id not in bank:
            raise ValueError(f"facets reference unknown evidence {evidence_id}")
        if bank[evidence_id].get("facets") != facets:
            bank[evidence_id]["facets"] = copy.deepcopy(facets)
            report["facets"] += 1
    approved = confirmed_wording(merged)
    experience_ids = {e.get("id") for e in merged.get("experience") or []}
    for item in updates.get("new_evidence") or []:
        if item.get("id") in bank:
            report["skipped_evidence"] += 1
            continue
        if normalized_text(item.get("public_text", "")) not in approved:
            raise ValueError(f"new evidence {item.get('id')} is not already confirmed wording")
        if item.get("experience_id") not in experience_ids:
            raise ValueError(f"new evidence {item.get('id')} references an unknown experience")
        merged.setdefault("evidence_bank", []).append(copy.deepcopy(item))
        bank[item["id"]] = merged["evidence_bank"][-1]
        report["new_evidence"] += 1
    for evidence_id, values in (updates.get("hide_from") or {}).items():
        if evidence_id not in bank:
            raise ValueError(f"hide_from references unknown evidence {evidence_id}")
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            raise ValueError(f"hide_from values for {evidence_id} must be a list of strings")
        record = bank[evidence_id]
        current = record.get("visibility")
        current = [current] if isinstance(current, str) else list(current or [])
        kept = [v for v in current if v not in values]
        if kept != current:
            record["visibility"] = kept
            report["hidden"] += 1
    for company, roles in (updates.get("engagement_roles") or {}).items():
        group = next((g for g in merged.get("employment_groups") or [] if g.get("company") == company), None)
        if group is None:
            raise ValueError(f"engagement roles reference unknown employer {company}")
        for name, role in roles.items():
            engagement = next((e for e in group.get("engagements") or [] if e.get("name") == name), None)
            if engagement is None:
                raise ValueError(f"engagement roles reference unknown project {name}")
            if engagement.get("role") != role:
                engagement["role"] = role
                report["engagement_roles"] += 1
    if updates.get("resume_blueprints"):
        existing = merged.get("resume_blueprints") or []
        by_id = {b.get("id"): index for index, b in enumerate(existing)}
        for blueprint in updates["resume_blueprints"]:
            if blueprint.get("id") in by_id:
                if existing[by_id[blueprint["id"]]] != blueprint:
                    existing[by_id[blueprint["id"]]] = copy.deepcopy(blueprint)
                    report["blueprints"] += 1
            else:
                existing.append(copy.deepcopy(blueprint))
                by_id[blueprint["id"]] = len(existing) - 1
                report["blueprints"] += 1
        merged["resume_blueprints"] = existing
    validate_profile(merged)
    resume_blueprints.validate(merged)
    return merged, report


def write_atomically(path, profile):
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.",
                                         suffix=".new", delete=False)
    with handle:
        json.dump(profile, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(handle.name, path.stat().st_mode)
    os.replace(handle.name, path)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--updates", required=True)
    parser.add_argument("--apply", action="store_true", help="write the merged profile (default: dry run)")
    args = parser.parse_args(argv)
    path = Path(args.profile)
    profile = json.loads(path.read_text(encoding="utf-8"))
    updates = json.loads(Path(args.updates).read_text(encoding="utf-8"))
    try:
        merged, report = merge(profile, updates)
    except (ValueError, ProfileValidationError) as exc:
        print(f"migration refused: {exc}", file=sys.stderr)
        return 2
    print("changes: " + ", ".join(f"{k}={v}" for k, v in report.items()))
    if not args.apply:
        print("dry run; pass --apply to write")
        return 0
    if merged == profile:
        print("profile already migrated; nothing written")
        return 0
    backup = write_atomically(path, merged)
    print(f"written; backup at {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
