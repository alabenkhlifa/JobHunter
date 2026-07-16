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
_VARIANT_RESUME_FIELDS = (
    "headline",
    "summary",
    "certifications",
    "skills",
    "experience",
    "education",
    "additional",
)
_VARIANT_IDENTITY_FIELDS = ("name", "email", "phone", "linkedin", "location")
_OMITTABLE_SECTIONS = frozenset(
    {"summary", "skills", "certifications", "experience", "education", "additional"}
)


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


def _normalized_match_text(value: str) -> str:
    """Normalize words and phrases while retaining common language markers."""
    return " ".join(re.sub(r"[^a-z0-9+#]+", " ", value.lower()).split())


def _validate_string_list(value: Any, path: str, *, non_empty: bool = False) -> None:
    if not isinstance(value, list) or (non_empty and not value):
        requirement = "a non-empty list" if non_empty else "a list"
        raise ProfileValidationError(f"{path} must be {requirement} of non-empty strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ProfileValidationError(f"{path} must contain only non-empty strings")


def _validate_variant_resume(resume: Any, *, index: int) -> None:
    path = f"resume_variants[{index}].resume"
    if not isinstance(resume, dict):
        raise ProfileValidationError(f"{path} must be an object")
    if set(resume) - set(_VARIANT_RESUME_FIELDS):
        raise ProfileValidationError(f"{path} contains unsupported, identity, or private fields")

    for field in ("headline", "summary"):
        if field in resume and not isinstance(resume[field], str):
            raise ProfileValidationError(f"{path}.{field} must be a string")
    if "certifications" in resume:
        _validate_string_list(resume["certifications"], f"{path}.certifications")
    if "skills" in resume:
        skills = resume["skills"]
        if not isinstance(skills, dict):
            raise ProfileValidationError(f"{path}.skills must be an object")
        for category, values in skills.items():
            if not isinstance(category, str) or not category.strip():
                raise ProfileValidationError(f"{path}.skills contains an invalid category")
            if isinstance(values, str):
                if not values.strip():
                    raise ProfileValidationError(f"{path}.skills contains an empty value")
            else:
                _validate_string_list(values, f"{path}.skills category")

    if "experience" in resume:
        experiences = resume["experience"]
        if not isinstance(experiences, list):
            raise ProfileValidationError(f"{path}.experience must be a list")
        for experience_index, experience in enumerate(experiences):
            item_path = f"{path}.experience[{experience_index}]"
            if not isinstance(experience, dict):
                raise ProfileValidationError(f"{item_path} must be an object")
            if set(experience) - set(_EXPERIENCE_FIELDS):
                raise ProfileValidationError(f"{item_path} contains unsupported or private fields")
            if not isinstance(experience.get("title"), str) or not experience["title"].strip():
                raise ProfileValidationError(f"{item_path}.title is required")
            for field in ("company", "subtitle", "location", "dates", "tech"):
                if field in experience and not isinstance(experience[field], str):
                    raise ProfileValidationError(f"{item_path}.{field} must be a string")
            if "bullets" in experience:
                _validate_string_list(experience["bullets"], f"{item_path}.bullets")

    if "education" in resume:
        education = resume["education"]
        if not isinstance(education, list):
            raise ProfileValidationError(f"{path}.education must be a list")
        for education_index, item in enumerate(education):
            item_path = f"{path}.education[{education_index}]"
            if not isinstance(item, dict):
                raise ProfileValidationError(f"{item_path} must be an object")
            if set(item) - set(_EDUCATION_FIELDS):
                raise ProfileValidationError(f"{item_path} contains unsupported or private fields")
            if any(not isinstance(value, str) for value in item.values()):
                raise ProfileValidationError(f"{item_path} fields must be strings")

    if "additional" in resume:
        additional = resume["additional"]
        if not isinstance(additional, dict):
            raise ProfileValidationError(f"{path}.additional must be an object")
        if set(additional) - set(_ADDITIONAL_FIELDS):
            raise ProfileValidationError(f"{path}.additional contains unsupported or private fields")
        if any(not isinstance(value, str) for value in additional.values()):
            raise ProfileValidationError(f"{path}.additional fields must be strings")


def _validate_resume_variant(variant: Any, *, index: int) -> None:
    path = f"resume_variants[{index}]"
    if not isinstance(variant, dict):
        raise ProfileValidationError(f"{path} must be an object")
    allowed_fields = {
        "id",
        "confirmation",
        "match_terms",
        "priority",
        "max_pages",
        "omit_sections",
        "resume",
    }
    if set(variant) - allowed_fields:
        raise ProfileValidationError(f"{path} contains unsupported or private metadata")
    if not _valid_id(variant.get("id")):
        raise ProfileValidationError(f"{path}.id is missing or malformed")
    if variant.get("confirmation") not in CONFIRMATION_VALUES:
        raise ProfileValidationError(f"{path}.confirmation contains an unsupported value")

    match_terms = variant.get("match_terms")
    _validate_string_list(match_terms, f"{path}.match_terms", non_empty=True)
    normalized_terms = [_normalized_match_text(term) for term in match_terms]
    if any(not term for term in normalized_terms) or len(set(normalized_terms)) != len(normalized_terms):
        raise ProfileValidationError(f"{path}.match_terms must contain unique matchable terms")

    if "priority" in variant and (
        isinstance(variant["priority"], bool) or not isinstance(variant["priority"], int)
    ):
        raise ProfileValidationError(f"{path}.priority must be an integer")
    if "max_pages" in variant and (
        isinstance(variant["max_pages"], bool)
        or not isinstance(variant["max_pages"], int)
        or variant["max_pages"] <= 0
    ):
        raise ProfileValidationError(f"{path}.max_pages must be a positive integer")
    if "omit_sections" in variant:
        omit_sections = variant["omit_sections"]
        if not isinstance(omit_sections, list):
            raise ProfileValidationError(f"{path}.omit_sections must be a list")
        if any(not isinstance(section, str) or section not in _OMITTABLE_SECTIONS for section in omit_sections):
            raise ProfileValidationError(f"{path}.omit_sections contains an unsupported section")
        if len(set(omit_sections)) != len(omit_sections):
            raise ProfileValidationError(f"{path}.omit_sections contains duplicates")
    _validate_variant_resume(variant.get("resume"), index=index)


def _validate_resume_variants(profile: dict[str, Any]) -> None:
    if "resume_variants" not in profile:
        return
    variants = profile["resume_variants"]
    if not isinstance(variants, list):
        raise ProfileValidationError("resume_variants must be a list")
    seen_ids: set[str] = set()
    for index, variant in enumerate(variants):
        _validate_resume_variant(variant, index=index)
        variant_id = variant["id"]
        if variant_id in seen_ids:
            raise ProfileValidationError(f"resume_variants[{index}].id is duplicated")
        seen_ids.add(variant_id)


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

    _validate_resume_variants(profile)

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


def _sanitized_variant_resume(resume: dict[str, Any]) -> dict[str, Any]:
    """Copy only renderer-safe variant fields after structural validation."""
    sanitized = _copy_selected(resume, _VARIANT_RESUME_FIELDS)
    if "experience" in sanitized:
        sanitized["experience"] = [
            _copy_selected(item, _EXPERIENCE_FIELDS) for item in resume["experience"]
        ]
    if "education" in sanitized:
        sanitized["education"] = [
            _copy_selected(item, _EDUCATION_FIELDS) for item in resume["education"]
        ]
    if "additional" in sanitized:
        sanitized["additional"] = _copy_selected(resume["additional"], _ADDITIONAL_FIELDS)
    return sanitized


def select_resume_variant(profile: dict[str, Any], job_text: str) -> dict[str, Any] | None:
    """Select a confirmed variant for normalized whole-term/phrase matches.

    Eligible variants are ranked by descending matched-term count, then
    descending ``priority`` (default zero), then their stable source order.
    The selected stored variant is returned as a deep copy; no profile-level
    metadata is included.
    """
    validate_profile(profile)
    normalized_job = _normalized_match_text(str(job_text or ""))
    padded_job = f" {normalized_job} "
    best_variant: dict[str, Any] | None = None
    best_score: tuple[int, int, int] | None = None
    for index, variant in enumerate(profile.get("resume_variants") or []):
        if variant["confirmation"] != "candidate-confirmed":
            continue
        matched_count = sum(
            1
            for term in variant["match_terms"]
            if f" {_normalized_match_text(term)} " in padded_job
        )
        if not matched_count:
            continue
        score = (matched_count, variant.get("priority", 0), -index)
        if best_score is None or score > best_score:
            best_score = score
            best_variant = variant
    return copy.deepcopy(best_variant) if best_variant is not None else None


def apply_resume_variant(profile: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """Build an approved snapshot plus current identity, without master-section inheritance."""
    validate_profile(profile)
    _validate_resume_variant(variant, index=0)
    if variant["confirmation"] != "candidate-confirmed":
        raise ProfileValidationError("Only a candidate-confirmed resume variant can be applied")
    stored_variant = next(
        (item for item in profile.get("resume_variants") or [] if item.get("id") == variant["id"]),
        None,
    )
    if stored_variant != variant:
        raise ProfileValidationError("Resume variant must match the confirmed variant stored in the profile")

    result = _copy_selected(profile, _VARIANT_IDENTITY_FIELDS)
    result.update(_sanitized_variant_resume(variant["resume"]))
    for section in variant.get("omit_sections") or []:
        result.pop(section, None)
    return result


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
            if key in {"experience", "evidence_bank", "resume_variants"}:
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
        if (
            path[-1] == "resume_variants"
            and _valid_id(item_id)
            and item_id in indexes
        ):
            merged[indexes[item_id]] = copy.deepcopy(item)
        elif _valid_id(item_id) and item_id in indexes and isinstance(merged[indexes[item_id]], dict):
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
    for index, item in enumerate(updates.get("resume_variants") or []):
        _validate_resume_variant(item, index=index)
        if (
            isinstance(item, dict)
            and item.get("confirmation") == "candidate-confirmed"
            and not candidate_confirmed
        ):
            raise ProfileValidationError(
                f"resume_variants[{index}] cannot be candidate-confirmed without explicit candidate confirmation"
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
        if merged_usable != current_usable:
            raise ProfileValidationError("Usable public evidence requires explicit candidate confirmation")
        current_variants = {
            json.dumps(variant, ensure_ascii=False, sort_keys=True)
            for variant in current.get("resume_variants") or []
            if variant["confirmation"] == "candidate-confirmed"
        }
        merged_variants = {
            json.dumps(variant, ensure_ascii=False, sort_keys=True)
            for variant in merged.get("resume_variants") or []
            if variant["confirmation"] == "candidate-confirmed"
        }
        if merged_variants != current_variants:
            raise ProfileValidationError("Usable resume variants require explicit candidate confirmation")

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
