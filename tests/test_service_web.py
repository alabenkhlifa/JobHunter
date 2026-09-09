"""HTTP and WebSocket integration checks; all external account services are mocked."""

import asyncio
import json
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import Mock

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from jobhunter_integrations.web_oauth import VerifiedGoogleGrant
from jobhunter_service.service import JobHunterService
from jobhunter_service.web import create_app

ADMIN_TOKEN = "synthetic-admin-token-for-tests-only-123456"


class Google:
    def __init__(self):
        self.exchanges = []
        self.on_exchange = None

    def authorization_url(self, kind, state, account):
        return {"url": "https://accounts.google.com/o/oauth2/auth?" + urlencode({"state": state}),
                "code_verifier": "synthetic-verifier-" + "x" * 60}

    def exchange(self, kind, code, account, verifier):
        self.exchanges.append((kind, code, account, verifier))
        if self.on_exchange:
            return self.on_exchange(kind, code, account, verifier)
        return VerifiedGoogleGrant(kind, account, {"token": "synthetic-" + account})


@pytest.fixture
def service(tmp_path):
    browser = Mock()
    browser.start.side_effect = lambda profile: {"profile_id": profile, "status": "running", "viewer_port": 33000, "cdp_port": 33001, "expires_at": time.time() + 1800}
    browser.status.side_effect = browser.start.side_effect
    result = JobHunterService(tmp_path / "data", 1, public_url="https://jobs.example.test", telegram_client=Mock(), browser_manager=browser)
    for user_id in (11, 22):
        result.admin(1, "add", user_id)
        result.authorize(user_id)
        proposal = result.propose(user_id, {"accounts": {"gmail": {"account": f"jobs{user_id}@example.test"},
                                                          "tracker": {"account": f"jobs{user_id}@example.test", "viewer_email": f"personal{user_id}@example.test"}}})
        result.confirm(user_id, proposal["action_id"])
    return result


async def consent_state(client, service, user=11, kind="gmail"):
    link = service.connect(user, "google", kind)
    response = await client.get(urlsplit(link).path + "?" + urlsplit(link).query, allow_redirects=False)
    assert response.status == 302
    return parse_qs(urlsplit(response.headers["Location"]).query)["state"][0], link


def test_admin_http_binds_owner_from_bearer_and_rejects_claimed_actor(service):
    async def scenario():
        async with TestClient(TestServer(create_app(service, admin_token=ADMIN_TOKEN))) as client:
            denied = await client.post("/admin", json={"operation": "add", "user_id": 33})
            assert denied.status == 403
            forged = await client.post("/admin", headers={"Authorization": "Bearer " + ADMIN_TOKEN},
                                       json={"operation": "add", "user_id": 33, "actor_id": 1})
            assert forged.status == 400
            accepted = await client.post("/admin", headers={"Authorization": "Bearer " + ADMIN_TOKEN},
                                         json={"operation": "add", "user_id": 33})
            assert accepted.status == 200
            assert (await accepted.json())["status"] == "pending"
            assert service.store.member(33)["status"] == "pending"
            health = await client.get("/health")
            assert await health.json() == {"status": "ok"}
            assert health.headers["Referrer-Policy"] == "no-referrer"
            assert health.headers["Cache-Control"] == "no-store"
    asyncio.run(scenario())


def test_google_link_and_callback_are_one_use_and_store_only_own_token(service):
    google = Google()
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=google))) as client:
            state, link = await consent_state(client, service)
            replay_link = await client.get(urlsplit(link).path + "?" + urlsplit(link).query, allow_redirects=False)
            assert replay_link.status == 403
            complete = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic-code"})
            assert complete.status == 200
            replay_callback = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic-code"})
            assert replay_callback.status == 403
    asyncio.run(scenario())
    assert len(google.exchanges) == 1
    assert google.exchanges[0][2] == "jobs11@example.test"
    token = service.root / "u11" / "secrets" / "gmail_token.json"
    assert json.loads(token.read_text())["token"] == "synthetic-jobs11@example.test"
    assert token.stat().st_mode & 0o777 == 0o600
    assert not (service.root / "u22" / "secrets" / "gmail_token.json").exists()


def test_google_callback_ignores_claimed_candidate_id(service):
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=Google()))) as client:
            state, _ = await consent_state(client, service)
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic", "user_id": "22"})
            assert response.status == 200
    asyncio.run(scenario())
    assert (service.root / "u11" / "secrets" / "gmail_token.json").exists()
    assert not (service.root / "u22" / "secrets" / "gmail_token.json").exists()


def test_google_denied_consent_consumes_state_without_saving_credentials(service):
    google = Google()
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=google))) as client:
            state, _ = await consent_state(client, service)
            response = await client.get("/oauth/google/callback", params={"state": state, "error": "access_denied"})
            assert response.status == 200
            assert "not connected" in await response.text()
            replay = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert replay.status == 403
    asyncio.run(scenario())
    assert not google.exchanges
    assert not (service.root / "u11" / "secrets" / "gmail_token.json").exists()


@pytest.mark.parametrize("change", ["account", "revision", "revoke"])
def test_google_stale_or_revoked_configuration_rejects_callback(service, change):
    google = Google()
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=google))) as client:
            state, _ = await consent_state(client, service)
            if change == "revoke":
                service.admin(1, "revoke", 11)
            else:
                patch = {"accounts": {"gmail": {"account": "changed@example.test"}}} if change == "account" else {"schedule": {"timezone": "Africa/Tunis"}}
                proposal = service.propose(11, patch)
                service.confirm(11, proposal["action_id"])
            result = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert result.status == 403
    asyncio.run(scenario())
    assert not google.exchanges
    assert not (service.root / "u11" / "secrets" / "gmail_token.json").exists()


def test_google_revocation_during_exchange_prevents_token_persistence(service):
    google = Google()
    def exchange(kind, code, account, verifier):
        service.admin(1, "revoke", 11)
        return VerifiedGoogleGrant(kind, account, {"token": "synthetic"})
    google.on_exchange = exchange
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=google))) as client:
            state, _ = await consent_state(client, service)
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert response.status == 403
    asyncio.run(scenario())
    assert not (service.root / "u11" / "secrets" / "gmail_token.json").exists()


def test_google_wrong_provider_account_cannot_write_a_token(service):
    google = Google()
    def exchange(*args):
        raise PermissionError("Selected account does not match the configured account.")
    google.on_exchange = exchange
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=google))) as client:
            state, _ = await consent_state(client, service)
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert response.status == 403
    asyncio.run(scenario())
    assert not (service.root / "u11" / "secrets" / "gmail_token.json").exists()


def test_tracker_uses_own_grant_viewer_and_document_state(service, monkeypatch):
    provision = Mock(return_value={"spreadsheet_id": "synthetic-sheet-11", "spreadsheet_url": "https://docs.google.com/spreadsheets/d/synthetic-sheet-11", "sheet_id": 0})
    monkeypatch.setattr("jobhunter_integrations.google_tracker_setup.provision_tracker", provision)
    async def scenario():
        async with TestClient(TestServer(create_app(service, google_client=Google()))) as client:
            state, _ = await consent_state(client, service, kind="tracker")
            response = await client.get("/oauth/google/callback", params={"state": state, "code": "synthetic"})
            assert response.status == 200
    asyncio.run(scenario())
    args, kwargs = provision.call_args.args, provision.call_args.kwargs
    assert args[0] == service.root / "u11" / "secrets" / "tracker_token.json"
    assert args[1] == "jobs11@example.test"
    assert kwargs["viewer_email"] == "personal11@example.test"
    assert kwargs["drive_state_path"] == service.root / "u11" / "state" / "tracker_drive_files.json"
    assert not (service.root / "u22" / "state" / "tracker_connection.json").exists()


def test_browser_link_is_single_use_and_sets_private_cookie(service):
    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            link = urlsplit(service.connect(11, "linkedin", "login"))
            response = await client.get(link.path + "?" + link.query, allow_redirects=False)
            assert response.status == 302
            cookie = response.cookies["jobhunter_browser"]
            assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict"
            assert cookie["path"] == "/browser"
            assert service.store.read_token(cookie.value, "browser_session", consume=False)[0] == 11
            replay = await client.get(link.path + "?" + link.query, allow_redirects=False)
            assert replay.status == 403
            denied = await client.get("/browser/vnc.html")
            assert denied.status == 403
    asyncio.run(scenario())
    service.browser_manager.start.assert_called_once_with("u11")


def test_browser_start_rejects_mismatched_broker_candidate(service):
    service.browser_manager.start.side_effect = lambda _: {"profile_id": "u22", "status": "running", "viewer_port": 33000, "cdp_port": 33001, "expires_at": time.time() + 1800}
    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            link = urlsplit(service.connect(11, "linkedin", "login"))
            response = await client.get(link.path + "?" + link.query, allow_redirects=False)
            assert response.status == 403
            assert "jobhunter_browser" not in response.cookies
    asyncio.run(scenario())


@pytest.mark.parametrize("tail", ["app/..%2f..%2fsecrets", "app/%5csecrets", "etc/passwd", "json/version"])
def test_viewer_rejects_path_traversal_and_cdp_endpoints(service, tail):
    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            cookie = service.store.token(11, "browser_session", {"profile_id": "u11"})
            response = await client.get("/browser/" + tail, headers={"Cookie": "jobhunter_browser=" + cookie})
            assert response.status == 404
    asyncio.run(scenario())
    service.browser_manager.status.assert_not_called()


def test_viewer_rejects_wrong_candidate_broker_result(service):
    service.browser_manager.status.side_effect = lambda _: {"profile_id": "u22", "status": "running", "viewer_port": 33000}
    async def scenario():
        async with TestClient(TestServer(create_app(service))) as client:
            cookie = service.store.token(11, "browser_session", {"profile_id": "u11"})
            response = await client.get("/browser/vnc.html", headers={"Cookie": "jobhunter_browser=" + cookie})
            assert response.status == 403
    asyncio.run(scenario())


def test_real_local_viewer_http_and_websocket_close_after_revocation(service):
    async def scenario():
        upstream_app = web.Application()
        async def page(request):
            return web.Response(text="Synthetic candidate browser", content_type="text/html")
        async def socket(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("viewer-ready")
            async for message in ws:
                if message.type == WSMsgType.TEXT:
                    await ws.send_str(message.data)
            return ws
        upstream_app.router.add_get("/vnc.html", page)
        upstream_app.router.add_get("/websockify", socket)
        async with TestServer(upstream_app) as upstream:
            service.browser_manager.status.side_effect = lambda profile: {"profile_id": profile, "status": "running", "viewer_port": upstream.port}
            async with TestClient(TestServer(create_app(service))) as client:
                cookie = service.store.token(11, "browser_session", {"profile_id": "u11"})
                headers = {"Cookie": "jobhunter_browser=" + cookie}
                page_response = await client.get("/browser/vnc.html", headers=headers)
                assert page_response.status == 200
                assert await page_response.text() == "Synthetic candidate browser"
                async with client.ws_connect("/browser/websockify", headers={**headers, "Origin": service.public_url}) as ws:
                    assert (await ws.receive(timeout=2)).data == "viewer-ready"
                    await ws.send_str("synthetic-interaction")
                    assert (await ws.receive(timeout=2)).data == "synthetic-interaction"
                    service.admin(1, "revoke", 11)
                    assert (await ws.receive(timeout=5)).type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.CLOSING}
                denied = await client.get("/browser/vnc.html", headers=headers)
                assert denied.status == 403
    asyncio.run(scenario())
