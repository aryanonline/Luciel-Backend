"""Contract tests for the knowledge SCOPE endpoints (Decision #9).

The owner has to be able to answer "which part of my Drive may Luciel read?" from the
admin API alone: list what is selectable, save a choice, read the choice back. The
default matters as much as the feature — a connection with no scope saved must report
the WHOLE account, because that is what it has always synced and quietly narrowing it
to nothing would empty a working knowledge base.
"""

from __future__ import annotations

import uuid
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = [pytest.mark.contract]


class FakeDriveTransport:
    """Serves the token exchange (POST) and the folder listing (GET) from one object.

    The endpoint under test builds its transport with ``oauth_transport()``, so both the
    callback and the candidates call get whatever that factory returns."""

    def __init__(self, listing: dict) -> None:
        self._listing = listing
        self.get_params: list[dict] = []

    async def post(self, url, *, data=None, headers=None):
        return httpx.Response(
            200,
            json={"access_token": "gd-tok", "refresh_token": "gd-refresh", "expires_in": 3600},
            request=httpx.Request("POST", url),
        )

    async def get(self, url, *, headers=None, params=None):
        if url.startswith("https://oauth2.googleapis.com/tokeninfo"):
            # Round 6 WP-C: the callback verifies the Drive read scope before flipping.
            return httpx.Response(
                200,
                json={"scope": "https://www.googleapis.com/auth/drive.readonly"},
                request=httpx.Request("GET", url),
            )
        self.get_params.append(dict(params or {}))
        return httpx.Response(200, json=self._listing, request=httpx.Request("GET", url))

    async def aclose(self) -> None:
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


_FOLDERS = {
    "files": [
        {"id": "folder-public", "name": "Public FAQ"},
        {"id": "folder-hr", "name": "HR"},
    ]
}


def _state_from(conn_body: dict) -> str:
    url = (conn_body["nonSecretConfig"] or {})["authorize_url"]
    return parse_qs(urlparse(url).query)["state"][0]


async def _account(client) -> uuid.UUID:
    from app.auth import access

    email = f"kb-scope-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    r = await client.post(
        "/api/v1/admin/luciel", json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"}
    )
    assert r.status_code in (200, 201), r.text
    return acct.id


async def _connected_drive(client, monkeypatch) -> tuple[str, FakeDriveTransport]:
    """A google_drive knowledge connection that has completed OAuth, plus its transport."""
    import app.api.v1.admin.knowledge as knowledge_api
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    configured = Settings(
        google_client_id="gcid",
        google_client_secret="gcsecret",
        google_redirect_uri="https://api.example.com/api/v1/connections/google/callback",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: configured)
    monkeypatch.setattr(registry_module, "get_settings", lambda: configured)

    transport = FakeDriveTransport(_FOLDERS)
    monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: transport)

    await _account(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    assert rc.status_code == 201, rc.text
    conn_body = rc.json()
    connection_id = conn_body["connectionId"]
    rcb = await client.post(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/oauth-callback",
        json={"code": "auth-code", "state": _state_from(conn_body)},
    )
    assert rcb.status_code == 200, rcb.text
    return connection_id, transport


# --- reading the scope -------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_connection_with_no_saved_scope_reports_the_whole_account(client, monkeypatch):
    """The default is "everything", never "nothing" — existing connections must keep syncing."""
    connection_id, _ = await _connected_drive(client, monkeypatch)
    r = await client.get(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wholeAccount"] is True
    assert body["selections"] == []
    assert body["scopeKind"] == "drive_folders"
    assert body["provider"] == "google_drive"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_scope_of_an_unknown_connection_is_404_not_an_empty_scope(client, monkeypatch):
    await _connected_drive(client, monkeypatch)
    r = await client.get(f"/api/v1/admin/knowledge/sync-connections/{uuid.uuid4()}/scope")
    assert r.status_code == 404


# --- listing what is selectable ---------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_candidates_lists_only_folders_with_ids_and_names(client, monkeypatch):
    """Non-secret by construction (§3.8.3): ids and display names, nothing else."""
    connection_id, transport = await _connected_drive(client, monkeypatch)
    r = await client.get(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope/candidates"
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert [c["id"] for c in body] == ["folder-public", "folder-hr"]
    assert {c["kind"] for c in body} == {"folder"}
    assert all(set(c) == {"id", "name", "kind"} for c in body)
    # The listing asked Drive for folders only, so the owner is not shown files to pick.
    assert "application/vnd.google-apps.folder" in transport.get_params[-1]["q"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_candidates_for_a_provider_with_nothing_to_narrow_is_a_clean_409(client):
    """website_crawl has no external account: there is no folder tree to offer."""
    await _account(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://x.com"]},
    )
    assert rc.status_code == 201, rc.text
    connection_id = rc.json()["connectionId"]
    r = await client.get(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope/candidates"
    )
    assert r.status_code == 409, r.text


# --- saving a scope ----------------------------------------------------------- #


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_a_scope_narrows_the_connection_and_survives_a_reread(client, monkeypatch):
    connection_id, _ = await _connected_drive(client, monkeypatch)
    r = await client.put(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope",
        json={"selections": [{"id": "folder-public", "name": "Public FAQ"}]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["wholeAccount"] is False
    assert [s["id"] for s in body["selections"]] == ["folder-public"]

    again = await client.get(f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope")
    assert again.json()["selections"][0]["name"] == "Public FAQ"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_an_empty_selection_restores_the_whole_account(client, monkeypatch):
    """Clearing the scope must widen back to the account, not leave a scope of nothing."""
    connection_id, _ = await _connected_drive(client, monkeypatch)
    await client.put(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope",
        json={"selections": [{"id": "folder-public"}]},
    )
    r = await client.put(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope",
        json={"selections": []},
    )
    assert r.status_code == 200, r.text
    assert r.json()["wholeAccount"] is True
    assert r.json()["selections"] == []


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_a_scope_writes_an_audit_event(client, monkeypatch):
    """Narrowing or widening what Luciel may read is a lifecycle change, so it is audited."""
    from app.db.session import tenant_session
    from app.models.audit import AdminAuditLog
    from sqlalchemy import select

    admin_id = await _account(client)
    # _connected_drive makes its own account, so build the connection against THIS one.
    import app.api.v1.admin.knowledge as knowledge_api
    from app.config import Settings
    from app.connections import registry as registry_module
    from app.knowledge import providers as providers_module

    configured = Settings(
        google_client_id="gcid",
        google_client_secret="gcsecret",
        google_redirect_uri="https://api.example.com/api/v1/connections/google/callback",
    )
    monkeypatch.setattr(providers_module, "get_settings", lambda: configured)
    monkeypatch.setattr(registry_module, "get_settings", lambda: configured)
    monkeypatch.setattr(knowledge_api, "oauth_transport", lambda: FakeDriveTransport(_FOLDERS))

    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections", json={"provider": "google_drive"}
    )
    connection_id = rc.json()["connectionId"]
    r = await client.put(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope",
        json={"selections": [{"id": "folder-public", "name": "Public FAQ"}]},
    )
    assert r.status_code == 200, r.text

    async with tenant_session(admin_id) as s:
        rows = (
            (
                await s.execute(
                    select(AdminAuditLog).where(
                        AdminAuditLog.admin_id == admin_id,
                        AdminAuditLog.event_type == "knowledge_source_scope_changed",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].event_payload["selected_ids"] == ["folder-public"]
    assert rows[0].event_payload["scope_kind"] == "drive_folders"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_saving_a_scope_on_a_provider_without_one_is_422(client):
    await _account(client)
    rc = await client.post(
        "/api/v1/admin/knowledge/sync-connections",
        json={"provider": "website_crawl", "crawlUrls": ["https://x.com"]},
    )
    connection_id = rc.json()["connectionId"]
    r = await client.put(
        f"/api/v1/admin/knowledge/sync-connections/{connection_id}/scope",
        json={"selections": [{"id": "whatever"}]},
    )
    assert r.status_code == 422, r.text
