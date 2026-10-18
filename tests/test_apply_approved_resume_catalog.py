import copy
import hashlib
import json

import pytest

from test_resume_blocks import profile  # noqa: F401
from tools.apply_approved_resume_catalog import CatalogActivationError, apply_catalog


@pytest.fixture
def inputs(profile, tmp_path):
    approved = copy.deepcopy(profile['resume_blocks'])
    approved['flavors'] = approved['flavors'][:1]
    catalog_path = tmp_path / 'approved.json'
    catalog_path.write_text(json.dumps(approved))
    profile_path = tmp_path / 'profile.json'
    profile_path.write_text(json.dumps({'name': 'Example Candidate', 'private_notes': 'Keep unchanged',
                                        'resume_blocks': {'old': True}, 'other_settings': {'enabled': True}}))
    return profile_path, catalog_path, hashlib.sha256(catalog_path.read_bytes()).hexdigest()


def test_dry_run_and_apply_preserve_profile_and_private_backup(inputs):
    path, catalog_path, digest = inputs
    original = path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    assert apply_catalog(path, catalog_path, digest)['applied'] is False
    assert path.read_bytes() == original
    assert not list(path.parent.glob('profile.before-resume-catalog.*'))
    result = apply_catalog(path, catalog_path, digest, expected_profile_sha256=expected, apply=True)
    assert result['applied'] is True
    updated = json.loads(path.read_text())
    before = json.loads(original)
    assert {k: v for k, v in updated.items() if k != 'resume_blocks'} == {k: v for k, v in before.items() if k != 'resume_blocks'}
    assert updated['resume_blocks'] == json.loads(catalog_path.read_text())
    from pathlib import Path
    backup = Path(result['backup'])
    assert backup.read_bytes() == original
    assert backup.stat().st_mode & 0o777 == path.stat().st_mode & 0o777 == 0o600
    assert apply_catalog(path, catalog_path, digest, apply=True)['changed'] is False


@pytest.mark.parametrize('failure', ['catalog_digest', 'profile_digest', 'missing_profile_digest', 'unapproved_variant'])
def test_activation_refuses_changed_or_unapproved_inputs_without_writing(inputs, failure):
    path, catalog_path, digest = inputs
    original = path.read_bytes()
    expected = hashlib.sha256(original).hexdigest()
    if failure == 'catalog_digest': digest = '0' * 64
    if failure == 'profile_digest': expected = '0' * 64
    if failure == 'missing_profile_digest': expected = None
    if failure == 'unapproved_variant':
        value = json.loads(catalog_path.read_text())
        value['variants'][0]['confirmation'] = 'draft'
        catalog_path.write_text(json.dumps(value))
        digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
    with pytest.raises(CatalogActivationError):
        apply_catalog(path, catalog_path, digest, expected_profile_sha256=expected, apply=True)
    assert path.read_bytes() == original


def test_concurrent_profile_change_is_not_overwritten(inputs, monkeypatch):
    from tools import apply_approved_resume_catalog as activation
    path, catalog_path, digest = inputs
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    make_temp = activation.tempfile.mkstemp
    concurrent = b'{"name":"Concurrent profile change"}'
    def change_profile(*args, **kwargs):
        path.write_bytes(concurrent)
        return make_temp(*args, **kwargs)
    monkeypatch.setattr(activation.tempfile, 'mkstemp', change_profile)
    with pytest.raises(CatalogActivationError, match='changed during activation'):
        apply_catalog(path, catalog_path, digest, expected_profile_sha256=expected, apply=True)
    assert path.read_bytes() == concurrent
