import json
import re
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parent.parent / "jobhunter_research" / "salary_benchmarks.json"

CITIES = {"madrid", "barcelona", "valencia", "*"}
LEVELS = {"mid", "senior", "lead", "staff"}
FAMILY_LEVELS = {
    "senior_software_engineer": {"senior"},
    "lead_software_engineer": {"lead"},
    "software_architect": {"senior", "lead"},
    "backend_engineer": {"mid", "senior"},
    "engineering_manager": {"lead"},
}
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@pytest.fixture(scope="module")
def data():
    return json.loads(PATH.read_text(encoding="utf-8"))


def test_header(data):
    assert data["schema_version"] == 1
    assert data["currency"] == "EUR"
    assert data["period"] == "year"
    assert data["basis"] == "gross annual base"
    assert re.match(r"^\d{4}-\d{2}$", data["updated"])
    assert isinstance(data["notes"], list) and data["notes"]
    assert data["entries"]


def test_entries_are_valid(data):
    seen = set()
    for entry in data["entries"]:
        key = (entry["city"], entry["role_family"], entry["level"])
        assert key not in seen, f"duplicate entry {key}"
        seen.add(key)
        assert entry["country"] == "es"
        assert entry["city"] in CITIES
        assert entry["role_family"] in FAMILY_LEVELS
        assert entry["level"] in LEVELS
        assert entry["level"] in FAMILY_LEVELS[entry["role_family"]], key
        values = [entry["p25"], entry["p50"], entry["p75"]]
        assert all(type(v) is int and v > 0 for v in values), key
        assert entry["p25"] <= entry["p50"] <= entry["p75"], key
        assert entry["sources"], key
        for source in entry["sources"]:
            assert source["name"]
            assert re.match(r"^https?://", source["url"]), key
            assert DATE.match(source["retrieved"]), key


def test_spain_wide_row_for_every_family_level(data):
    present = {(e["role_family"], e["level"]) for e in data["entries"] if e["city"] == "*"}
    required = {(family, level) for family, levels in FAMILY_LEVELS.items() for level in levels}
    assert required <= present, sorted(required - present)
