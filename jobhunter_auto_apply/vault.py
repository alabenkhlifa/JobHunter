"""Encrypted credential vault for ATS account passwords.

Secrets are stored outside git by default. This is intentionally small and local:
- metadata can live in SQLite application notes;
- generated passwords/tokens belong in this encrypted vault;
- the master key must stay outside the repository.
"""

from __future__ import annotations

import json
import os
import secrets
import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # optional dependency, declared in requirements.txt for normal installs
    from cryptography.fernet import Fernet
except Exception:  # pragma: no cover - exercised only when dependency is missing
    Fernet = None  # type: ignore[assignment]


DEFAULT_VAULT_DIR = Path.home() / ".jobhunter" / "secrets"
DEFAULT_KEY_FILE = DEFAULT_VAULT_DIR / "vault.key"
DEFAULT_VAULT_FILE = DEFAULT_VAULT_DIR / "ats_credentials.json.enc"


class VaultError(RuntimeError):
    """Raised when encrypted credential storage is unavailable."""


def generate_password(length: int = 24) -> str:
    """Generate a strong ATS password with mixed character classes."""

    alphabet = string.ascii_letters + string.digits + "!@#$%^&*_-+"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(c in "!@#$%^&*_-+" for c in password)
        ):
            return password


@dataclass
class CredentialVault:
    key_file: Path = DEFAULT_KEY_FILE
    vault_file: Path = DEFAULT_VAULT_FILE

    def __post_init__(self) -> None:
        self.key_file = Path(self.key_file)
        self.vault_file = Path(self.vault_file)
        self.key_file.parent.mkdir(parents=True, exist_ok=True)
        self.vault_file.parent.mkdir(parents=True, exist_ok=True)

    def _fernet(self):
        if Fernet is None:
            raise VaultError("cryptography is required for CredentialVault")
        if not self.key_file.exists():
            key = Fernet.generate_key()
            self.key_file.write_bytes(key)
            os.chmod(self.key_file, 0o600)
        return Fernet(self.key_file.read_bytes())

    def _read_all(self) -> dict[str, Any]:
        if not self.vault_file.exists():
            return {}
        data = self._fernet().decrypt(self.vault_file.read_bytes())
        return json.loads(data.decode("utf-8"))

    def _write_all(self, payload: dict[str, Any]) -> None:
        encrypted = self._fernet().encrypt(json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))
        self.vault_file.write_bytes(encrypted)
        os.chmod(self.vault_file, 0o600)

    def put(self, key: str, value: dict[str, Any]) -> None:
        payload = self._read_all()
        payload[key] = value
        self._write_all(payload)

    def get(self, key: str) -> dict[str, Any] | None:
        value = self._read_all().get(key)
        return value if isinstance(value, dict) else None

    def put_generated_ats_password(self, ats_key: str, *, username: str | None = None) -> str:
        password = generate_password()
        self.put(ats_key, {"username": username, "password": password})
        return password
