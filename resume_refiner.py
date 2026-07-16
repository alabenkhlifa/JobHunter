"""Truthful profile evidence helpers for the Resume Refiner onboarding flow.

The refiner stores candidate-provided facts separately from rendered resume
fields.  Selection is deterministic: only exact, explicitly confirmed public
text can become resume or cover-letter evidence.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


CONFIRMATION_VALUES = frozenset({"candidate-confirmed", "draft", "unconfirmed"})
CONFIDENTIALITY_VALUES = frozenset({"public", "private"})
VISIBILITY_VALUES = frozenset({"resume", "cover-letter", "interview-only"})

_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_RESUME_FIELDS = (
    "name",
    "headline",
    "email",
    "phone",
    "linkedin",
    "location",
    "summary",
    "certifications",
    "skills",
    "experience",
    "education",
    "additional",
)
_EXPERIENCE_FIELDS = ("title", "company", "subtitle", "location", "dates", "bullets", "tech")
_EDUCATION_FIELDS = ("degree", "school", "location", "dates")
_ADDITIONAL_FIELDS = ("teaching", "languages", "interests")
_V2_REQUIRED_EXPERIENCE_FIELDS = ("title", "company", "dates", "bullets")


class ProfileValidationError(ValueError):
    """Raised when a profile cannot safely participate in refinement."""


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_ID_PATTERN.fullmatch(value))


def _visibility_values(value: Any, *, index: int) -> tuple[str, ...]:
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, list) and value:
        values = tuple(value)
    else:
        raise ProfileValidationError(f"evidence_bank[{index}].visibility must be a non-empty string or list")
    if any(not isinstance(item, str) or item not in VISIBILITY_VALUES for item in values):
        raise ProfileValidationError(f"evidence_bank[{index}].visibility contains an unsupported value")
    if len(set(values)) != len(values):
        raise ProfileValidationError(f"evidence_bank[{index}].visibility contains duplicates")
    if "interview-only" in values and len(values) > 1:
        raise ProfileValidationError(
            f"evidence_bank[{index}].visibility cannot combine interview-only with public documents"
        )
    return values


def validate_profile(profile: Any) -> None:
    """Validate legacy profiles plus the additive Resume Refiner v2 contract.

    A legacy profile needs only the same candidate identity required by the
    existing package flow.  Once ``evidence_bank`` is present, each experience
    receives a stable ID and keeps the renderer's required legacy fields.
    Error messages identify structural locations but never echo candidate text.
    """
    if not isinstance(profile, dict):
        raise ProfileValidationError("Candidate profile must be a JSON object")
    if not isinstance(profile.get("name"), str) or not profile["name"].strip():
        raise ProfileValidationError("Candidate profile is missing the required name field")

    experiences = profile.get("experience", [])
    if not isinstance(experiences, list):
        raise ProfileValidationError("Candidate profile experience must be a list")
    for index, experience in enumerate(experiences):
        if not isinstance(experience, dict):
            raise ProfileValidationError(f"experience[{index}] must be an object")

    if "evidence_bank" not in profile:
        return

    evidence_bank = profile["evidence_bank"]
    if not isinstance(evidence_bank, list):
        raise ProfileValidationError("evidence_bank must be a list")

    experience_ids: set[str] = set()
    for index, experience in enumerate(experiences):
        experience_id = experience.get("id")
        if not _valid_id(experience_id):
            raise ProfileValidationError(f"experience[{index}].id is missing or malformed")
        if experience_id in experience_ids:
            raise ProfileValidationError(f"experience[{index}].id is duplicated")
        experience_ids.add(experience_id)
        for field in _V2_REQUIRED_EXPERIENCE_FIELDS:
            value = experience.get(field)
            if field == "bullets":
                valid = isinstance(value, list) and all(
                    isinstance(bullet, str) and bool(bullet.strip()) for bullet in value
                )
            else:
                valid = isinstance(value, str) and bool(value.strip())
            if not valid:
                raise ProfileValidationError(f"experience[{index}].{field} is required by the v2 profile")

    evidence_ids: set[str] = set()
    for index, item in enumerate(evidence_bank):
        if not isinstance(item, dict):
            raise ProfileValidationError(f"evidence_bank[{index}] must be an object")
        evidence_id = item.get("id")
        if not _valid_id(evidence_id):
            raise ProfileValidationError(f"evidence_bank[{index}].id is missing or malformed")
        if evidence_id in evidence_ids:
            raise ProfileValidationError(f"evidence_bank[{index}].id is duplicated")
        evidence_ids.add(evidence_id)

        experience_id = item.get("experience_id")
        if not _valid_id(experience_id) or experience_id not in experience_ids:
            raise ProfileValidationError(f"evidence_bank[{index}].experience_id does not reference an experience")
        public_text = item.get("public_text")
        if not isinstance(public_text, str) or not public_text.strip():
            raise ProfileValidationError(f"evidence_bank[{index}].public_text must be a non-empty string")
        if item.get("confirmation") not in CONFIRMATION_VALUES:
            raise ProfileValidationError(f"evidence_bank[{index}].confirmation contains an unsupported value")
        if item.get("confidentiality") not in CONFIDENTIALITY_VALUES:
            raise ProfileValidationError(f"evidence_bank[{index}].confidentiality contains an unsupported value")
        _visibility_values(item.get("visibility"), index=index)


def usable_evidence(profile: dict[str, Any], visibility: str) -> list[dict[str, Any]]:
    """Return exact candidate-confirmed public evidence for one output target."""
    if visibility not in {"resume", "cover-letter"}:
        raise ValueError("visibility must be resume or cover-letter")
    validate_profile(profile)
    usable: list[dict[str, Any]] = []
    for index, item in enumerate(profile.get("evidence_bank") or []):
        if (
            item["confirmation"] == "candidate-confirmed"
            and item["confidentiality"] == "public"
            and visibility in _visibility_values(item["visibility"], index=index)
        ):
            usable.append(copy.deepcopy(item))
    return usable


def _copy_selected(source: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    return {field: copy.deepcopy(source[field]) for field in fields if field in source}


def project_public_resume(profile: dict[str, Any]) -> dict[str, Any]:
    """Create the renderer-safe public view of a profile.

    This is deliberately an allowlist. Evidence banks, application defaults,
    refiner sessions, and unknown private metadata never enter an application
    package merely because they were stored in the master profile.
    """
    projection = _copy_selected(profile, _RESUME_FIELDS)
    if "experience" in projection:
        projection["experience"] = [
            _copy_selected(item, _EXPERIENCE_FIELDS)
            for item in profile.get("experience", [])
            if isinstance(item, dict)
        ]
    if "education" in projection:
        projection["education"] = [
            _copy_selected(item, _EDUCATION_FIELDS)
            for item in profile.get("education", [])
            if isinstance(item, dict)
        ]
    if isinstance(projection.get("additional"), dict):
        projection["additional"] = _copy_selected(profile["additional"], _ADDITIONAL_FIELDS)
    return projection


def _merge_dict(current: dict[str, Any], updates: dict[str, Any], *, path: tuple[str, ...] = ()) -> dict[str, Any]:
    merged = copy.deepcopy(current)
    for key, value in updates.items():
        existing = merged.get(key)
        next_path = (*path, key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_dict(existing, value, path=next_path)
        elif key == "visibility" and isinstance(existing, (str, list)) and isinstance(value, (str, list)):
            current_visibility = existing if isinstance(existing, list) else [existing]
            updated_visibility = value if isinstance(value, list) else [value]
            if "interview-only" in current_visibility or "interview-only" in updated_visibility:
                merged[key] = copy.deepcopy(updated_visibility)
            else:
                merged[key] = _merge_unique_values(current_visibility, updated_visibility)
        elif isinstance(existing, list) and isinstance(value, list):
            if key in {"experience", "evidence_bank"}:
                merged[key] = _merge_records(existing, value, path=next_path)
            else:
                merged[key] = _merge_unique_values(existing, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _merge_unique_values(current: list[Any], updates: list[Any]) -> list[Any]:
    """Append only values not already present while retaining original order."""
    merged = copy.deepcopy(current)
    for value in updates:
        if value not in merged:
            merged.append(copy.deepcopy(value))
    return merged


def _merge_records(current: list[Any], updates: list[Any], *, path: tuple[str, ...]) -> list[Any]:
    """Additively merge record lists while retaining unknown record fields."""
    merged = copy.deepcopy(current)
    indexes = {
        item.get("id"): index
        for index, item in enumerate(merged)
        if isinstance(item, dict) and _valid_id(item.get("id"))
    }
    for update_index, item in enumerate(updates):
        if not isinstance(item, dict):
            merged.append(copy.deepcopy(item))
            continue
        item_id = item.get("id")
        if _valid_id(item_id) and item_id in indexes and isinstance(merged[indexes[item_id]], dict):
            record_index = indexes[item_id]
            merged[record_index] = _merge_dict(merged[record_index], item, path=(*path, str(record_index)))
        elif (
            _valid_id(item_id)
            and path[-1] == "experience"
            and update_index < len(merged)
            and isinstance(merged[update_index], dict)
            and not _valid_id(merged[update_index].get("id"))
        ):
            merged[update_index] = _merge_dict(merged[update_index], item, path=(*path, str(update_index)))
            indexes[item_id] = update_index
        elif (
            not _valid_id(item_id)
            and path[-1] == "experience"
            and update_index < len(merged)
            and isinstance(merged[update_index], dict)
        ):
            merged[update_index] = _merge_dict(merged[update_index], item, path=(*path, str(update_index)))
        else:
            indexes[item_id] = len(merged)
            merged.append(copy.deepcopy(item))
    return merged


def atomic_update_profile(
    profile_path: Path | str,
    updates: dict[str, Any],
    *,
    candidate_confirmed: bool,
    now: datetime | None = None,
) -> Path:
    """Merge a confirmed refinement into a profile, with backup and atomic replace.

    ``candidate_confirmed`` is intentionally mandatory. Without it, callers may
    store draft/unconfirmed notes but cannot label new evidence as confirmed.
    The returned path is the timestamped backup of the previous profile.
    """
    path = Path(profile_path)
    if not path.exists():
        raise FileNotFoundError(f"Candidate profile not found: {path}")
    if not isinstance(updates, dict):
        raise ProfileValidationError("Profile updates must be a JSON object")

    current = json.loads(path.read_text(encoding="utf-8"))
    validate_profile(current)
    if not candidate_confirmed and any(field in updates for field in _RESUME_FIELDS):
        raise ProfileValidationError("Public resume fields require explicit candidate confirmation")
    for index, item in enumerate(updates.get("evidence_bank") or []):
        if (
            isinstance(item, dict)
            and item.get("confirmation") == "candidate-confirmed"
            and not candidate_confirmed
        ):
            raise ProfileValidationError(
                f"evidence_bank[{index}] cannot be candidate-confirmed without explicit candidate confirmation"
            )

    merged = _merge_dict(current, updates)
    validate_profile(merged)
    if not candidate_confirmed:
        current_usable = {
            (
                item["id"],
                item["experience_id"],
                item["public_text"],
                tuple(_visibility_values(item["visibility"], index=index)),
            )
            for visibility in ("resume", "cover-letter")
            for index, item in enumerate(usable_evidence(current, visibility))
        }
        merged_usable = {
            (
                item["id"],
                item["experience_id"],
                item["public_text"],
                tuple(_visibility_values(item["visibility"], index=index)),
            )
            for visibility in ("resume", "cover-letter")
            for index, item in enumerate(usable_evidence(merged, visibility))
        }
        if not merged_usable <= current_usable:
            raise ProfileValidationError("Usable public evidence requires explicit candidate confirmation")

    timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = path.with_name(f"{path.name}.backup-{timestamp}")
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.backup-{timestamp}-{suffix}")
        suffix += 1
    shutil.copy2(path, backup)

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".new",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(merged, temporary, indent=2, ensure_ascii=False)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, path.stat().st_mode)
        os.replace(temporary_name, path)
    except Exception:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise
    return backup


def main(argv: list[str] | None = None) -> int:
    """Run privacy-safe structural profile validation from the command line."""
    parser = argparse.ArgumentParser(description="Validate a JobHunter candidate profile")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a local profile JSON file")
    validate_parser.add_argument("profile_path")
    args = parser.parse_args(argv)

    if args.command == "validate":
        try:
            profile = json.loads(Path(args.profile_path).read_text(encoding="utf-8"))
            validate_profile(profile)
        except json.JSONDecodeError as exc:
            print(
                f"Profile validation failed: invalid JSON at line {exc.lineno}, column {exc.colno}",
                file=sys.stderr,
            )
            return 2
        except ProfileValidationError as exc:
            print(f"Profile validation failed: {exc}", file=sys.stderr)
            return 2
        except FileNotFoundError:
            print("Profile validation failed: profile file not found", file=sys.stderr)
            return 2
        except OSError:
            print("Profile validation failed: profile file could not be read", file=sys.stderr)
            return 2
        print("Profile is valid.")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
