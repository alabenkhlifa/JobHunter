"""Deterministic interleavings at confirmed configuration and account boundaries."""
import asyncio
import json
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock

from aiohttp.test_utils import TestClient, TestServer
import pytest

from jobhunter_integrations.web_oauth import VerifiedGoogleGrant
from jobhunter_service.scheduler import Scheduler
from jobhunter_service.service import JobHunterService
from jobhunter_service.state import private_json
from jobhunter_service.web import create_app


@pytest.fixture
def service(tmp_path):
    result = JobHunterService(tmp_path, 900, public_url="https://jobs.example.test", telegram_client=Mock())
    result.admin(900, "add", 123)
    result.authorize(123)
    apply_settings(result, {"accounts": {"gmail": {"account": "old@example.test", "enabled": True}},
                            "search": {"keywords": ["original-role"]}})
    return result


def apply_settings(service, patch):
    action = service.propose(123, patch)
    return service.confirm(123, action["action_id"])


def test_stale_member_cannot_roll_back_materialized_settings_or_archive_new_grant(service):
    stale = service._member(123)
    apply_settings(service, {"accounts": {"gmail": {"account": "new@example.test"}},
                             "search": {"keywords": ["new-role"]}})
    root = service.profile_dir(service._member(123))
    token = root / "secrets" / "gmail_token.json"
    private_json(token, {"fixture_account": "new@example.test"})
    # A scheduler can retain the old member while a Telegram confirmation wins.
    # Implementations may reject the stale object or reload the canonical one.
    try:
        service.materialize(stale)
    except (ValueError, PermissionError):
        pass
    current = json.loads(service._member(123)["settings"])
    assert json.loads((root / "config.json").read_text()) == current["search"]
    assert json.loads((root / "settings.json").read_text()) == current
    assert token.exists() and json.loads(token.read_text())["fixture_account"] == "new@example.test"


def test_materialized_resume_replaces_confirmed_lists_and_keeps_private_backup(service):
    from jobhunter_service.runner import _snapshot
    old = {"name": "Synthetic Candidate", "experience": [
        {"id": "exp-first", "company": "Example One", "title": "Developer", "location": "France",
         "dates": "2020 - 2022", "bullets": ["Built synthetic applications."]},
        {"id": "exp-second", "company": "Example Two", "title": "Developer", "location": "France",
         "dates": "2022 - 2024", "bullets": ["Maintained synthetic applications."]}],
        "evidence_bank": [
            {"id": "ev-first", "experience_id": "exp-first", "public_text": "Built synthetic applications.",
             "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]},
            {"id": "ev-second", "experience_id": "exp-second", "public_text": "Maintained synthetic applications.",
             "confirmation": "candidate-confirmed", "confidentiality": "public", "visibility": ["resume"]}]}
    apply_settings(service, {"resume": old, "search": {"markets": [
        {"name": "France", "locations": ["France"], "work_authorization": "authorized", "relocation_required": False}]}})
    root = service.profile_dir(service._member(123))
    prior = json.loads((root / "master-profile.json").read_text())
    action = service.propose(123, {"resume": {"experience": old["experience"][:1],
                                              "evidence_bank": old["evidence_bank"][:1]}},
                             replace_resume_lists=True)
    service.confirm(123, action['action_id'])
    member = service._member(123)
    current = json.loads(member["settings"])["resume"]
    assert json.loads((root / "master-profile.json").read_text()) == current
    assert len(current["experience"]) == len(current["evidence_bank"]) == 1
    backups = list(root.glob("master-profile.json.backup-*"))
    assert len(backups) == 1 and json.loads(backups[0].read_text()) == prior
    assert backups[0].stat().st_mode & 0o777 == 0o600
    assert _snapshot("u123", service.root, member["revision"])["master_profile"] == current
    service.materialize(member)
    assert len(list(root.glob("master-profile.json.backup-*"))) == 1


def test_focused_resume_interview_preserves_other_experiences_and_evidence(service):
    profile = {"name": "Synthetic Candidate", "experience": [
        {"id": "exp-first", "title": "Developer", "company": "Example One", "dates": "2020 - 2022",
         "location": "France", "bullets": ["Built synthetic applications."]},
        {"id": "exp-second", "title": "Developer", "company": "Example Two", "dates": "2022 - Present",
         "bullets": ["Maintained synthetic applications."]}],
        "evidence_bank": [
            {"id": "ev-first", "experience_id": "exp-first", "public_text": "Built synthetic applications.",
             "confirmation": "draft", "confidentiality": "public", "visibility": ["resume"]},
            {"id": "ev-second", "experience_id": "exp-second", "public_text": "Maintained synthetic applications.",
             "confirmation": "draft", "confidentiality": "public", "visibility": ["resume"]}]}
    before = apply_settings(service, {"resume": profile})['settings']['resume']
    # The planner reviews one employer. Its sparse lists are not deletions.
    action = service.propose(123, {'resume': {
        'experience': [{'id': 'exp-second', 'bullets': ['Improved synthetic applications.']}],
        'evidence_bank': [{'id': 'ev-third', 'experience_id': 'exp-second',
                          'public_text': 'Improved synthetic applications.', 'confirmation': 'draft',
                          'confidentiality': 'public', 'visibility': ['resume']}]}})
    assert service.snapshot(123)['settings']['resume'] == before
    assert [item['id'] for item in action['patch']['resume']['experience']] == ['exp-second']
    assert [item['id'] for item in action['patch']['resume']['evidence_bank']] == ['ev-third']
    after = service.confirm(123, action['action_id'])['settings']['resume']
    assert after['experience'][0] == before['experience'][0]
    assert after['experience'][1] == {**before['experience'][1], 'bullets': ['Improved synthetic applications.']}
    assert after['evidence_bank'][:2] == before['evidence_bank']
    assert after['evidence_bank'][2]['confirmation'] == 'candidate-confirmed'
    root = service.profile_dir(service._member(123))
    assert json.loads((root / 'master-profile.json').read_text()) == after
    unchanged = apply_settings(service, {'resume': {'experience': [], 'evidence_bank': []}})['settings']['resume']
    assert unchanged == after


def test_resume_updates_preserve_unconfirmed_evidence_not_in_the_preview(service):
    profile = {'name': 'Synthetic Candidate', 'experience': [
        {'id': 'exp-first', 'title': 'Developer', 'company': 'Example One',
         'dates': '2020 - Present', 'bullets': ['Built synthetic applications.']}],
        'evidence_bank': []}
    apply_settings(service, {'resume': profile})
    # A draft may exist in operator state; an unrelated review cannot confirm it.
    with service.store.connect() as db:
        settings = json.loads(service._member(123)['settings'])
        draft = {'id': 'ev-draft', 'experience_id': 'exp-first', 'public_text': 'Unreviewed synthetic fact.',
                 'confirmation': 'draft', 'confidentiality': 'public', 'visibility': ['resume']}
        settings['resume']['evidence_bank'] = [draft]
        db.execute('UPDATE members SET settings=? WHERE user_id=123', (json.dumps(settings),))
    after = apply_settings(service, {'resume': {'evidence_bank': [
        {'id': 'ev-new', 'experience_id': 'exp-first', 'public_text': 'Reviewed synthetic fact.',
         'confirmation': 'draft', 'confidentiality': 'public', 'visibility': ['resume']}]}})['settings']['resume']
    assert after['evidence_bank'][0] == draft
    assert after['evidence_bank'][1]['confirmation'] == 'candidate-confirmed'


def test_duplicate_resume_update_ids_are_rejected_without_a_pending_action(service):
    before = service.snapshot(123)
    with pytest.raises(ValueError, match='duplicate IDs'):
        service.propose(123, {'resume': {'experience': [
            {'id': 'exp-first', 'company': 'Example One'}, {'id': 'exp-first', 'company': 'Example Two'}]}})
    assert service.snapshot(123) == before


def test_pending_preview_from_before_sparse_merging_cannot_erase_an_experience(service):
    first = {'id': 'exp-first', 'title': 'Developer', 'company': 'Example One',
             'dates': '2020 - 2022', 'bullets': ['Built synthetic applications.']}
    second = {'id': 'exp-second', 'title': 'Developer', 'company': 'Example Two',
              'dates': '2022 - Present', 'bullets': ['Maintained synthetic applications.']}
    apply_settings(service, {'resume': {'name': 'Synthetic Candidate', 'experience': [first, second]}})
    action = service.propose(123, {'resume': {'experience': [second]}})
    with service.store.connect() as db:
        row = db.execute('SELECT payload FROM actions WHERE id=?', (action['action_id'],)).fetchone()
        payload = json.loads(row['payload'])
        # Reproduce the full-settings payload written by the older service.
        payload['settings']['resume']['experience'] = [second]
        db.execute('UPDATE actions SET payload=? WHERE id=?', (json.dumps(payload), action['action_id']))
    after = service.confirm(123, action['action_id'])['settings']['resume']
    assert after['experience'] == [first, second]


def test_focused_evidence_preview_does_not_repeat_the_entire_bank(service):
    experience = {'id': 'exp-first', 'title': 'Developer', 'company': 'Example One',
                  'dates': '2020 - Present', 'bullets': ['Built synthetic applications.']}
    apply_settings(service, {'resume': {'name': 'Synthetic Candidate', 'experience': [experience], 'evidence_bank': []}})
    with service.store.connect() as db:
        settings = json.loads(service._member(123)['settings'])
        settings['resume']['evidence_bank'] = [
            {'id': f'ev-{i}', 'experience_id': 'exp-first', 'public_text': 'Synthetic fact. ' * 80,
             'confirmation': 'candidate-confirmed', 'confidentiality': 'public', 'visibility': ['resume']}
            for i in range(30)]
        db.execute('UPDATE members SET settings=? WHERE user_id=123', (json.dumps(settings),))
    action = service.propose(123, {'resume': {'evidence_bank': [
        {'id': 'ev-new', 'experience_id': 'exp-first', 'public_text': 'Another synthetic fact.',
         'confirmation': 'draft', 'confidentiality': 'public', 'visibility': ['resume']}]}})
    assert len(action['patch']['resume']['evidence_bank']) == 1
    after = service.confirm(123, action['action_id'])['settings']['resume']
    assert len(after['evidence_bank']) == 31


class Google:
    def authorization_url(self, kind, state, account):
        return {"url": "https://accounts.google.com/auth?" + urlencode({"state": state}),
                "code_verifier": "synthetic-verifier"}

    def exchange(self, kind, code, account, verifier):
        return VerifiedGoogleGrant(kind, account, {"fixture_account": account})


def test_account_change_cannot_be_followed_by_old_callback_grant_persistence(service, monkeypatch):
    entered = threading.Event()
    errors = []
    original = VerifiedGoogleGrant.persist

    def persist(grant, path):
        entered.set()
        # Give the independent Telegram worker a chance to confirm while the
        # callback is between its final member check and its filesystem write.
        # A correct per-profile lock delays that confirmation until this ends.
        time.sleep(0.15)
        return original(grant, path)

    def change_account():
        try:
            assert entered.wait(3)
            apply_settings(service, {"accounts": {"gmail": {"account": "new@example.test"}}})
        except Exception as exc:
            errors.append(exc)

    monkeypatch.setattr(VerifiedGoogleGrant, "persist", persist)
    changer = threading.Thread(target=change_account)
    changer.start()

    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=Google()))) as client:
            link = urlsplit(service.connect(123, "google", "gmail"))
            start = await client.get(link.path + "?" + link.query, allow_redirects=False)
            state = parse_qs(urlsplit(start.headers["Location"]).query)["state"][0]
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert response.status in {200, 403}

    try:
        asyncio.run(scenario())
    finally:
        changer.join(timeout=3)
    assert not changer.is_alive()
    assert errors == []
    member = service._member(123)
    assert json.loads(member["settings"])["accounts"]["gmail"]["account"] == "new@example.test"
    token = service.profile_dir(member) / "secrets" / "gmail_token.json"
    assert not token.exists(), "The superseded account grant must not become the current token"


def test_mail_disabled_after_scheduling_snapshot_is_not_monitored(service, monkeypatch):
    root = service.profile_dir(service._member(123))
    private_json(root / "secrets" / "gmail_token.json", {"fixture_account": "old@example.test"})
    monitor = Mock()
    monkeypatch.setattr("jobhunter_integrations.gmail_monitor.run_candidate_monitor", monitor)
    scheduler = Scheduler(service, None, service.telegram_client)
    original = service._member
    changed = False

    def current_member(actor, **kwargs):
        nonlocal changed
        if not changed:
            changed = True
            apply_settings(service, {"accounts": {"gmail": {"enabled": False}}})
        return original(actor, **kwargs)

    monkeypatch.setattr(service, "_member", current_member)
    scheduler.monitor_due(datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc))
    assert json.loads(original(123)["settings"])["accounts"]["gmail"]["enabled"] is False
    monitor.assert_not_called()
