import json
import sqlite3

import pytest

from jobhunter_auto_apply import cli, credentials
from jobhunter_auto_apply.credentials import CredentialError, fill_credentials, vault_key
from jobhunter_auto_apply.vault import CredentialVault


URL = "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Senior-Backend-Engineer/apply"
HOST = "acme.wd3.myworkdayjobs.com"
MAILBOX = "jobs@example.com"
SECRET = "Sup3r-Secret-Value!"


class FormClient:
    """Fake CDP page: tracks focus and typed text, refuses non-password targets for secrets."""

    def __init__(self, url=URL, fields=None):
        self.url = url
        self.fields = fields or {"#email": "email", "#password": "password", "#confirm": "password"}
        self.focused = None
        self.typed = {}
        self.calls = []

    def evaluate(self, expression, **kwargs):
        if expression.strip() == "location.href":
            return self.url
        if f'location.href !== {json.dumps(self.url)}' not in expression:
            return False
        for selector, kind in self.fields.items():
            if f"document.querySelector({json.dumps(selector)})" in expression:
                wants_password = "el.type === 'password'" in expression
                if wants_password != (kind == "password"):
                    return False
                self.focused = selector
                return True
        return False

    def call(self, method, params=None):
        self.calls.append(method)
        assert method == "Input.insertText"
        self.typed[self.focused] = params["text"]
        return {}


@pytest.fixture
def vault(tmp_path):
    return CredentialVault(key_file=tmp_path / "vault.key", vault_file=tmp_path / "vault.enc")


def test_create_generates_stores_and_fills_without_returning_secret(vault):
    client = FormClient()
    result = fill_credentials(client, page_url=URL, ats_key=HOST, email_selector="#email",
                              password_selector="#password", confirm_selector="#confirm", mailbox=MAILBOX,
                              create=True, vault=vault, password_factory=lambda: SECRET)
    assert result == {"ok": True, "ats_key": HOST, "username": MAILBOX, "created": True,
                      "filled": ["email", "password", "confirm"]}
    assert SECRET not in json.dumps(result)
    assert client.typed == {"#email": MAILBOX, "#password": SECRET, "#confirm": SECRET}
    stored = vault.get(vault_key(HOST, MAILBOX))
    assert stored["password"] == SECRET and stored["username"] == MAILBOX


def test_existing_credentials_are_reused_not_regenerated(vault):
    vault.put(vault_key(HOST, MAILBOX), {"username": MAILBOX, "password": SECRET})
    client = FormClient()
    result = fill_credentials(client, page_url=URL, ats_key=HOST, email_selector="#email",
                              password_selector="#password", mailbox=MAILBOX, create=True, vault=vault,
                              password_factory=lambda: pytest.fail("must reuse"))
    assert result["created"] is False and client.typed["#password"] == SECRET


def test_missing_credentials_without_create_refuses(vault):
    with pytest.raises(CredentialError, match="--create"):
        fill_credentials(FormClient(), page_url=URL, ats_key=HOST, email_selector="#email",
                         password_selector="#password", mailbox=MAILBOX, vault=vault)


@pytest.mark.parametrize("kwargs,match", [
    ({"ats_key": "evil.example"}, "does not match"),
    ({"page_url": "http://acme.wd3.myworkdayjobs.com/apply"}, "https"),
    ({"mailbox": ""}, "mailbox"),
])
def test_host_scheme_and_mailbox_guards(vault, kwargs, match):
    params = {"page_url": URL, "ats_key": HOST, "email_selector": "#email", "password_selector": "#password",
              "mailbox": MAILBOX, "create": True, "vault": vault, **kwargs}
    with pytest.raises(CredentialError, match=match):
        fill_credentials(FormClient(url=params["page_url"]), **params)


def test_secret_is_never_typed_into_a_non_password_field(vault):
    vault.put(vault_key(HOST, MAILBOX), {"username": MAILBOX, "password": SECRET})
    client = FormClient(fields={"#email": "email", "#password": "text"})
    with pytest.raises(CredentialError) as raised:
        fill_credentials(client, page_url=URL, ats_key=HOST, email_selector="#email",
                         password_selector="#password", mailbox=MAILBOX, vault=vault)
    assert SECRET not in str(raised.value)
    assert SECRET not in client.typed.values()


def test_cdp_error_text_is_not_chained(vault):
    vault.put(vault_key(HOST, MAILBOX), {"username": MAILBOX, "password": SECRET})
    client = FormClient()

    def leaky(method, params=None):
        if params["text"] == SECRET:
            raise RuntimeError(f"failed with {params['text']}")
        return {}

    client.call = leaky
    with pytest.raises(CredentialError) as raised:
        fill_credentials(client, page_url=URL, ats_key=HOST, email_selector="#email",
                         password_selector="#password", mailbox=MAILBOX, vault=vault)
    assert SECRET not in str(raised.value) and raised.value.__cause__ is None and raised.value.__suppress_context__


def _db(tmp_path):
    db = tmp_path / "jobs.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE jobs(id TEXT PRIMARY KEY, title TEXT, company TEXT)")
        conn.execute("INSERT INTO jobs VALUES('job-1', 'Senior Backend Engineer', 'Acme')")
    return db


@pytest.mark.parametrize("flags,code", [(["--policy-run"], 0), (["--approved"], 0), ([], 2)])
def test_credential_cli_output_never_contains_secret(tmp_path, monkeypatch, capsys, vault, flags, code):
    db = _db(tmp_path)
    client = FormClient()
    monkeypatch.setattr("jobhunter_auto_apply.engine.connect_first_page", lambda *a, **k: client)
    monkeypatch.setattr("jobhunter_auto_apply.engine._default_gate_authorizer",
                        lambda *a, **k: {"run_id": "2026-10-15", "mode": "live"})
    monkeypatch.setattr(credentials, "CredentialVault", lambda: vault)
    monkeypatch.setattr(credentials, "generate_password", lambda: SECRET)
    rc = cli.main(["--db", str(db), "--output-dir", str(tmp_path / "out"), "credential", "--job-id", "job-1",
                   "--page-url", URL, "--ats-key", HOST, "--email-selector", "#email",
                   "--password-selector", "#password", "--confirm-selector", "#confirm", "--create",
                   "--mailbox", MAILBOX, *flags])
    out, err = capsys.readouterr()
    assert rc == code
    assert SECRET not in out and SECRET not in err
    if code == 0:
        assert json.loads(out)["created"] is True and client.typed["#password"] == SECRET
    else:
        assert client.typed == {}


def test_credential_cli_gate_refusal_exits_4(tmp_path, monkeypatch, capsys):
    db = _db(tmp_path)

    def refuse(*args, **kwargs):
        raise PermissionError("autopilot is paused")

    monkeypatch.setattr("jobhunter_auto_apply.engine._default_gate_authorizer", refuse)
    rc = cli.main(["--db", str(db), "credential", "--job-id", "job-1", "--page-url", URL, "--ats-key", HOST,
                   "--email-selector", "#email", "--password-selector", "#password", "--policy-run",
                   "--mailbox", MAILBOX])
    assert rc == 4 and "paused" in capsys.readouterr().err
