from jobhunter_auto_apply.vault import CredentialVault, generate_password


def test_generate_password_has_required_character_classes():
    password = generate_password(20)

    assert len(password) == 20
    assert any(c.islower() for c in password)
    assert any(c.isupper() for c in password)
    assert any(c.isdigit() for c in password)
    assert any(c in "!@#$%^&*_-+" for c in password)


def test_credential_vault_round_trip(tmp_path):
    vault = CredentialVault(key_file=tmp_path / "vault.key", vault_file=tmp_path / "vault.json.enc")

    vault.put("ats/example", {"username": "candidate@example.com", "password": "secret-value"})

    assert vault.get("ats/example") == {"username": "candidate@example.com", "password": "secret-value"}
    assert b"secret-value" not in (tmp_path / "vault.json.enc").read_bytes()
