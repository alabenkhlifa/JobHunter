"""Which sources a profile calls: chosen from its configured locations, never always on."""

from copy import deepcopy
import json
import logging
from unittest import mock

import pytest

import scraper

BOARDS = ("Spain Dev Jobs", "SpainJobs.io")


@pytest.fixture
def config(monkeypatch):
    config = deepcopy(scraper.DEFAULT_CONFIG)
    monkeypatch.setattr(scraper, "CONFIG", config)
    return config


def buckets_for(caplog, regions=None):
    if regions is not None:
        scraper.CONFIG["regions"] = regions
    with caplog.at_level(logging.INFO, logger="scraper"):
        return scraper.build_collection_buckets(mock.Mock())


def logged(caplog):
    return [record.getMessage() for record in caplog.records]


def generators(buckets):
    return {name: len(bucket["generators"]) for name, bucket in buckets.items()}


def test_source_countries():
    assert scraper.source_countries("LinkedIn") is None
    assert scraper.source_countries("Foundit") == {"uae", "ksa"}
    assert scraper.source_countries("Nope") == frozenset()
    assert all(scraper.source_countries(name) == {"es"} for name in BOARDS)


def test_spain_profile_never_calls_foundit(config, caplog):
    buckets = buckets_for(caplog)
    keywords = len(config["keywords"])
    assert generators(buckets) == {
        f"{source}/{city}": keywords if source == "LinkedIn" else 1
        for source in (*BOARDS, "LinkedIn") for city in ("Valencia", "Madrid", "Barcelona")}
    assert "Foundit: no configured location in ksa, uae; skipped" in logged(caplog)


def test_gulf_profile_calls_linkedin_and_foundit_but_no_spain_board(config, caplog):
    buckets = buckets_for(caplog, {"Gulf": ["Dubai, United Arab Emirates", "Riyadh, Saudi Arabia"]})
    keywords = len(config["keywords"])
    assert generators(buckets) == {
        "LinkedIn/Gulf": keywords * 2,
        "Foundit/United Arab Emirates": keywords,
        "Foundit/Saudi Arabia": keywords,
    }
    for board in BOARDS:
        assert f"{board}: no configured location in es; skipped" in logged(caplog)


def test_foundit_is_asked_for_the_country_not_the_city(config, caplog, monkeypatch):
    calls = []
    monkeypatch.setattr(scraper, "SCRAPERS", [("Foundit", lambda *args: calls.append(args) or iter(()))])
    buckets_for(caplog, {"Gulf": ["Dubai, United Arab Emirates", "Abu Dhabi", "Jeddah"]})
    assert {location for _, _, location in calls} == {"United Arab Emirates", "Saudi Arabia"}
    assert len(calls) == 2 * len(config["keywords"])


def test_profile_outside_every_board_country_only_calls_linkedin(config, caplog):
    buckets = buckets_for(caplog, {"Berlin": ["Berlin, Germany"]})
    assert list(buckets) == ["LinkedIn/Berlin"]
    messages = logged(caplog)
    assert "Foundit: no configured location in ksa, uae; skipped" in messages
    for board in BOARDS:
        assert f"{board}: no configured location in es; skipped" in messages


def test_mixed_profile_gives_each_source_only_its_own_countries(config, caplog):
    buckets = buckets_for(caplog, {"Madrid": ["Madrid, Spain"], "Dubai": ["Dubai, United Arab Emirates"]})
    assert set(buckets) == {
        "Spain Dev Jobs/Madrid", "SpainJobs.io/Madrid",
        "LinkedIn/Madrid", "LinkedIn/Dubai",
        "Foundit/United Arab Emirates",
    }
    assert not any("skipped" in message for message in logged(caplog))


def test_spanish_location_without_a_city_listing_is_left_to_linkedin(config, caplog):
    buckets = buckets_for(caplog, {"Seville": ["Seville, Spain"], "Madrid": ["Madrid, Spain"]})
    assert set(buckets) == {"Spain Dev Jobs/Madrid", "SpainJobs.io/Madrid", "LinkedIn/Seville", "LinkedIn/Madrid"}
    for board in BOARDS:
        assert f"{board}: no city listing for 'Seville, Spain'; skipped" in logged(caplog)


def test_disabled_source_is_skipped_even_where_it_would_run(config, caplog):
    config["disabled_sources"] = ["Foundit"]
    buckets = buckets_for(caplog, {"Dubai": ["Dubai"]})
    assert list(buckets) == ["LinkedIn/Dubai"]
    assert "Foundit: disabled by config" in logged(caplog)


def write_profile(tmp_path, monkeypatch, name, market):
    monkeypatch.setenv("JOBHUNTER_DATA_ROOT", str(tmp_path))
    (tmp_path / name).mkdir()
    (tmp_path / name / "config.json").write_text(json.dumps({
        "matching": {"preset": "generic", "preferred_roles": ["backend"]},
        "keywords": ["java"],
        "markets": [{**market, "work_authorization": "authorized", "relocation_required": False}],
    }))
    config = scraper.load_profile_config(name)
    monkeypatch.setattr(scraper, "CONFIG", config)
    return config


def test_invited_gulf_profile_runs_linkedin_and_foundit_only(tmp_path, monkeypatch, caplog):
    write_profile(tmp_path, monkeypatch, "friend", {"name": "Gulf", "locations": ["Dubai, United Arab Emirates"]})
    buckets = buckets_for(caplog)
    assert generators(buckets) == {"LinkedIn/Gulf": 1, "Foundit/United Arab Emirates": 1}


def test_invited_spain_profile_runs_the_spain_boards_and_linkedin_only(tmp_path, monkeypatch, caplog):
    write_profile(tmp_path, monkeypatch, "amigo", {"name": "Spain", "locations": ["Madrid, Spain", "Valencia, Spain"]})
    buckets = buckets_for(caplog)
    assert generators(buckets) == {"Spain Dev Jobs/Spain": 2, "SpainJobs.io/Spain": 2, "LinkedIn/Spain": 2}
    assert "Foundit: no configured location in ksa, uae; skipped" in logged(caplog)
