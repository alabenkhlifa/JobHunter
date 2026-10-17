import copy
import json
import sqlite3

import pytest

import resume_choices as choices
from test_resume_blocks import profile  # noqa: F401


def token(keyboard, label):
    return next(b['callback_data'].partition(':')[2] for row in keyboard['inline_keyboard'] for b in row
                if label in b['text'])


def test_picker_flavor_length_projects_and_one_use_generation_survive_reconnect(profile, tmp_path):
    path = tmp_path / 'choices.db'
    conn = sqlite3.connect(path)
    message, keyboard = choices.start(conn, 'job-one', profile)
    assert 'Architect: wording awaiting approval' in message
    assert len(keyboard['inline_keyboard']) == 1
    flavor = token(keyboard, 'Software Engineer')
    assert len(('resume_choice:' + flavor).encode()) <= 64
    assert choices.choose(conn, flavor, profile) == ('job-one', None)
    with pytest.raises(choices.ChoiceError, match='already used'):
        choices.choose(conn, flavor, profile)
    _, keyboard = choices.card(conn, 'job-one', profile)
    choices.choose(conn, token(keyboard, 'Core projects'), profile)
    _, keyboard = choices.card(conn, 'job-one', profile)
    stale_generate = token(keyboard, 'Generate')
    choices.choose(conn, token(keyboard, 'Farm'), profile)
    with pytest.raises(choices.ChoiceError):
        choices.choose(conn, stale_generate, profile)
    conn.close()
    conn = sqlite3.connect(path)
    _, keyboard = choices.card(conn, 'job-one', profile)
    generate = token(keyboard, 'Generate')
    job_id, selection = choices.choose(conn, generate, profile)
    assert job_id == 'job-one'
    assert selection == {'flavor_id': 'software-engineer', 'variant_id': 'software-engineer-core',
                         'project_ids': ['cars', 'farm']}
    with pytest.raises(choices.ChoiceError):
        choices.choose(conn, generate, profile)
    _, keyboard = choices.retry(conn, 'job-one', profile)
    assert token(keyboard, 'Generate') != generate


def test_empty_selection_has_no_generate_button_and_other_job_is_untouched(profile):
    conn = sqlite3.connect(':memory:')
    for job_id in ('a', 'b'):
        _, keyboard = choices.start(conn, job_id, profile)
        choices.choose(conn, token(keyboard, 'Software Engineer'), profile)
        _, keyboard = choices.card(conn, job_id, profile)
        choices.choose(conn, token(keyboard, 'Core projects'), profile)
    _, keyboard = choices.card(conn, 'a', profile)
    choices.choose(conn, token(keyboard, 'Cars'), profile)
    _, keyboard = choices.card(conn, 'a', profile)
    assert not any('Generate' in b['text'] for row in keyboard['inline_keyboard'] for b in row)
    _, keyboard = choices.card(conn, 'b', profile)
    assert token(keyboard, 'Generate')


def test_changed_catalog_and_expired_token_cannot_generate(profile):
    conn = sqlite3.connect(':memory:')
    _, keyboard = choices.start(conn, 'a', profile)
    selected = token(keyboard, 'Software Engineer')
    changed = copy.deepcopy(profile)
    changed['resume_blocks']['flavors'][0]['summary'] = 'Changed approved content.'
    with pytest.raises(choices.ChoiceError, match='content changed'):
        choices.choose(conn, selected, changed)
    conn.execute('UPDATE resume_choice_tokens SET expires=0')
    conn.commit()
    with pytest.raises(choices.ChoiceError, match='expired'):
        choices.choose(conn, selected, profile)


def test_callback_apply_opens_picker_then_only_generate_builds_package(profile, tmp_path, monkeypatch):
    from test_callback_handler import load_callback_handler
    handler = load_callback_handler(monkeypatch)
    path = tmp_path / 'db.sqlite'
    profile_path = tmp_path / 'profile.json'
    profile_path.write_text(json.dumps(profile))
    monkeypatch.setattr(handler, 'DB_PATH', path)
    monkeypatch.setattr(handler, 'PROFILE_PATH', profile_path)
    monkeypatch.setattr(handler, 'get_job', lambda job_id: {'id': job_id, 'title': 'Senior Engineer', 'company': 'Example'})
    monkeypatch.setattr(handler, 'has_delivered_research', lambda job_id: True)
    monkeypatch.setattr(handler, 'current_package_includes_cover_letter', lambda job_id: False)
    sent = []
    monkeypatch.setattr(handler, 'send_message', lambda message, reply_markup=None: sent.append((message, reply_markup)) or True)
    packages = []
    monkeypatch.setattr(handler.interest_flow, 'prepare_application_package', lambda *a, **kw: packages.append(kw))
    assert handler.handle_apply('one')
    assert not packages
    assert handler.dispatch_callback_data('resume_choice:' + token(sent[-1][1], 'Software Engineer')) == 0
    assert not packages
    assert handler.dispatch_callback_data('resume_choice:' + token(sent[-1][1], 'Core projects')) == 0
    assert not packages
    generated = []
    monkeypatch.setattr(handler, 'handle_apply', lambda job_id, **kw: generated.append((job_id, kw)) or True)
    data = 'resume_choice:' + token(sent[-1][1], 'Generate')
    assert handler.dispatch_callback_data(data) == 0
    assert generated[0][1]['resume_selection']['project_ids'] == ['cars']
    assert handler.dispatch_callback_data(data) == 3
    assert len(generated) == 1


def test_invalid_enabled_catalog_never_falls_back_to_automatic_generation(profile, tmp_path, monkeypatch):
    from test_callback_handler import load_callback_handler
    handler = load_callback_handler(monkeypatch)
    path = tmp_path / 'profile.json'
    profile['resume_blocks']['flavors'][0]['summary'] = ''
    path.write_text(json.dumps(profile))
    monkeypatch.setattr(handler, 'PROFILE_PATH', path)
    with pytest.raises(ValueError):
        handler.fixed_profile()


def test_interested_delivers_research_then_automatically_asks_flavor(profile, tmp_path, monkeypatch):
    from test_callback_handler import load_callback_handler
    handler = load_callback_handler(monkeypatch)
    monkeypatch.setattr(handler, 'DB_PATH', tmp_path / 'choices.db')
    monkeypatch.setattr(handler, 'fixed_profile', lambda: profile)
    monkeypatch.setattr(handler, 'get_job', lambda job_id: {'id': job_id, 'title': 'Backend Engineer', 'company': 'Example'})
    monkeypatch.setattr(handler, 'mark_interested', lambda job_id: None)
    monkeypatch.setattr(handler, 'record_research_delivery', lambda job_id: None)
    monkeypatch.setattr(handler.interest_flow, 'research_job', lambda job: object())
    monkeypatch.setattr(handler.interest_flow, 'build_research_brief_message', lambda job, research: 'Company research')
    sent = []
    monkeypatch.setattr(handler, 'send_message', lambda text, reply_markup=None: sent.append((text, reply_markup)) or True)
    assert handler.handle_interested('one')
    assert sent[0][0] == 'Company research'
    assert 'Choose your resume flavor' in sent[1][0]
    assert token(sent[1][1], 'Software Engineer')
