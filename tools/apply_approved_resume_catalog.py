#!/usr/bin/env python3
"""Apply an approved fixed resume catalog without replacing other profile data."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resume_blocks import build, catalog as validate_catalog


class CatalogActivationError(ValueError):
    pass


def apply_catalog(profile_path: Path, catalog_path: Path, expected_catalog_sha256: str,
                  *, expected_profile_sha256: str | None = None, apply: bool = False) -> dict:
    profile_path, catalog_path = Path(profile_path), Path(catalog_path)
    catalog_bytes = catalog_path.read_bytes()
    if hashlib.sha256(catalog_bytes).hexdigest() != expected_catalog_sha256:
        raise CatalogActivationError("The approved catalog checksum changed")
    approved = json.loads(catalog_bytes)
    validate_catalog({"resume_blocks": approved})
    if any(item["confirmation"] != "candidate-confirmed" for kind in ("flavors", "variants") for item in approved[kind]):
        raise CatalogActivationError("Every published flavor and variant must be candidate-confirmed")
    for variant in approved["variants"]:
        build({"resume_blocks": approved}, {"flavor_id": variant["flavor_id"], "variant_id": variant["id"],
                                            "project_ids": variant["default_project_ids"]})
    initial = profile_path.read_bytes()
    profile = json.loads(initial)
    if not isinstance(profile, dict):
        raise CatalogActivationError("The profile must be an object")
    if profile.get("resume_blocks") == approved:
        return {"changed": False, "applied": False, "backup": None}
    if expected_profile_sha256 is not None and hashlib.sha256(initial).hexdigest() != expected_profile_sha256:
        raise CatalogActivationError("The profile changed since inspection")
    if not apply:
        return {"changed": True, "applied": False, "backup": None}
    if expected_profile_sha256 is None:
        raise CatalogActivationError("Applying requires the inspected profile checksum")
    updated = copy.deepcopy(profile)
    updated["resume_blocks"] = approved
    backup = profile_path.with_name(profile_path.stem + ".before-resume-catalog." + uuid.uuid4().hex + ".json")
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as stream:
        stream.write(initial)
        stream.flush()
        os.fsync(stream.fileno())
    fd, name = tempfile.mkstemp(prefix=".resume-catalog-", suffix=".tmp", dir=profile_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(updated, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if profile_path.read_bytes() != initial:
            raise CatalogActivationError("The profile changed during activation; no changes were applied")
        os.replace(name, profile_path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    return {"changed": True, "applied": True, "backup": str(backup)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--expected-catalog-sha256", required=True)
    parser.add_argument("--expected-profile-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = apply_catalog(args.profile, args.catalog, args.expected_catalog_sha256,
                               expected_profile_sha256=args.expected_profile_sha256, apply=args.apply)
    except (OSError, ValueError, KeyError, TypeError):
        print("Catalog activation refused: inspect approval, checksums and profile state.", file=sys.stderr)
        return 2
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
