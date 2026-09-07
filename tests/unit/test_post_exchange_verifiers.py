"""Round 6 WP-C: every OAuth provider proves its grant before the row flips.

A redeemable code is not a working grant. Each verifier asks the provider who
authorized (and, where the provider reports scopes, whether the grant can do the
job), stamps the expiry the refresh sweep keys on, and writes the owner-facing
``connectedAs`` line.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.connections import registry, verify
from app.connections.verify import POST_EXCHANGE_VERIFIERS, PostExchangeVerifyFailed

pytestmark = [pytest.mark.asyncio]


class _Resp:
    def __init__(self, status: int, payload: Any) -> None:
        self.status_code = status
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _Transport:
    """Canned replies keyed by URL prefix; records every call."""

    def __init__(self, replies: dict[str, _Resp]) -> None:
        self._replies = replies
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _Resp:
        self.calls.append((url, dict(headers or {})))
        for prefix, reply in self._replies.items():
            if url.startswith(prefix):
                return reply
        return _Resp(404, {})


SF = "https://acme.my.salesforce.com"


async def test_salesforce_names_the_org_and_stamps_an_expiry():
    transport = _Transport(
        {
            f"{SF}/services/oauth2/userinfo": _Resp(
                200, {"organization_id": "00D000000000001", "preferred_username": "ops@acme.com"}
            )
        }
    )
    updates = await verify.verify_salesforce_org(
        material={"access_token": "sf-tok", "instance_url": SF}, transport=transport
    )
    assert updates["instanceUrl"] == SF
    assert updates["orgId"] == "00D000000000001"
    assert updates["username"] == "ops@acme.com"
    assert "acme.my.salesforce.com" in str(updates["connectedAs"])
    assert "00D000000000001" in str(updates["connectedAs"])
    assert updates["token_expires_at"]  # the refresh sweep's cue — never stamped before
    assert transport.calls[0][1]["Authorization"] == "Bearer sf-tok"


@pytest.mark.parametrize(
    "material",
    [
        {"access_token": "sf-tok"},  # no org URL at all
        {"access_token": "sf-tok", "instance_url": "https://evil.example"},  # off-domain
        {"access_token": "sf-tok", "instance_url": "http://acme.my.salesforce.com"},  # plain
    ],
)
async def test_salesforce_refuses_a_grant_with_no_usable_org_url(material):
    transport = _Transport({})
    with pytest.raises(PostExchangeVerifyFailed):
        await verify.verify_salesforce_org(material=material, transport=transport)
    assert transport.calls == []  # never asked anything on a URL it will not use


async def test_salesforce_401_is_a_refusal_not_a_connection():
    transport = _Transport({f"{SF}/services/oauth2/userinfo": _Resp(401, {})})
    with pytest.raises(PostExchangeVerifyFailed) as exc:
        await verify.verify_salesforce_org(
            material={"access_token": "sf-tok", "instance_url": SF}, transport=transport
        )
    assert "rejected" in str(exc.value)


def _hubspot_info(scopes: list[str]) -> _Resp:
    return _Resp(
        200,
        {
            "hub_domain": "acme.hubspot.com",
            "hub_id": 123456,
            "user": "ops@acme.com",
            "scopes": scopes,
            "expires_in": 1800,
        },
    )


async def test_hubspot_refuses_a_token_without_the_contacts_write_scope():
    transport = _Transport(
        {verify.HUBSPOT_TOKEN_INFO_ENDPOINT: _hubspot_info(["crm.objects.contacts.read"])}
    )
    with pytest.raises(PostExchangeVerifyFailed) as exc:
        await verify.verify_hubspot_grant(material={"access_token": "hs-tok"}, transport=transport)
    assert "crm.objects.contacts.write" in str(exc.value)
    assert "reconnect" in str(exc.value)


async def test_hubspot_names_the_portal_when_the_write_scope_is_granted():
    transport = _Transport(
        {
            verify.HUBSPOT_TOKEN_INFO_ENDPOINT: _hubspot_info(
                ["crm.objects.contacts.read", "crm.objects.contacts.write"]
            )
        }
    )
    updates = await verify.verify_hubspot_grant(
        material={"access_token": "hs-tok"}, transport=transport
    )
    assert updates["hubDomain"] == "acme.hubspot.com"
    assert updates["hubId"] == "123456"
    assert "acme.hubspot.com (portal 123456)" in str(updates["connectedAs"])
    assert "ops@acme.com" in str(updates["connectedAs"])
    assert updates["token_expires_at"]
    # The token rides the path, never a header the provider would ignore.
    assert transport.calls[0][0].endswith("/hs-tok")


async def test_the_registry_requests_the_scopes_the_verifier_requires():
    spec = registry.get_provider("hubspot")
    assert spec is not None and spec.authorize is not None
    requested = set(spec.authorize.scopes.split())
    assert set(spec.authorize.required_scopes) <= requested
    assert "crm.objects.contacts.write" in requested


async def test_google_refuses_a_token_missing_the_scope_this_surface_needs():
    transport = _Transport(
        {
            verify.GOOGLE_TOKENINFO_ENDPOINT: _Resp(
                200,
                {
                    "scope": verify.GOOGLE_DRIVE_SCOPE,
                    "expires_in": "3599",
                    "email": "owner@acme.com",
                },
            )
        }
    )
    with pytest.raises(PostExchangeVerifyFailed) as exc:
        await verify.verify_google_calendar(material={"access_token": "g-tok"}, transport=transport)
    assert "calendar" in str(exc.value)
    updates = await verify.verify_google_drive(
        material={"access_token": "g-tok"}, transport=transport
    )
    assert updates["googleAccount"] == "owner@acme.com"
    assert "Drive read" in str(updates["connectedAs"])
    assert updates["token_expires_at"]


async def test_notion_names_the_workspace_and_stamps_no_expiry():
    transport = _Transport(
        {
            verify.NOTION_ME_ENDPOINT: _Resp(
                200, {"type": "bot", "name": "Luciel", "bot": {"workspace_name": "Acme Co"}}
            )
        }
    )
    updates = await verify.verify_notion_workspace(
        material={"access_token": "n-tok"}, transport=transport
    )
    assert updates["workspaceName"] == "Acme Co"
    assert updates["connectedAs"] == "Notion workspace Acme Co"
    assert "token_expires_at" not in updates
    assert transport.calls[0][1]["Notion-Version"] == verify.NOTION_VERSION


def _calendly_transport(event_types: list[dict[str, Any]]) -> _Transport:
    return _Transport(
        {
            verify.CALENDLY_ME_ENDPOINT: _Resp(
                200,
                {
                    "resource": {
                        "uri": "https://api.calendly.com/users/U1",
                        "name": "Aryan",
                        "scheduling_url": "https://calendly.com/acme",
                        "current_organization": "https://api.calendly.com/organizations/O1",
                    }
                },
            ),
            verify.CALENDLY_EVENT_TYPES_ENDPOINT: _Resp(200, {"collection": event_types}),
        }
    )


async def test_calendly_records_the_user_and_the_first_active_event_type():
    transport = _calendly_transport(
        [
            {"uri": "https://api.calendly.com/event_types/E0", "name": "Old", "active": False},
            {
                "uri": "https://api.calendly.com/event_types/E1",
                "name": "Intro call",
                "active": True,
            },
        ]
    )
    updates = await verify.verify_calendly_user(
        material={"access_token": "c-tok", "expires_in": "7200"}, transport=transport
    )
    assert updates["userUri"] == "https://api.calendly.com/users/U1"
    assert updates["eventTypeUri"] == "https://api.calendly.com/event_types/E1"
    assert updates["eventTypeName"] == "Intro call"
    assert updates["eventTypeCount"] == 1
    assert "booking: Intro call" in str(updates["connectedAs"])
    assert "users%2FU1" in transport.calls[1][0]  # the user uri rides URL-encoded


async def test_calendly_with_no_active_event_type_connects_and_says_so():
    updates = await verify.verify_calendly_user(
        material={"access_token": "c-tok"}, transport=_calendly_transport([])
    )
    assert updates["eventTypeCount"] == 0 and updates["eventTypeUri"] == ""
    assert "no active event type yet" in str(updates["connectedAs"])


async def test_meta_and_instagram_name_the_account():
    meta = _Transport({verify.META_ME_ENDPOINT: _Resp(200, {"id": "1001", "name": "Aryan Singh"})})
    updates = await verify.verify_meta_grant(material={"access_token": "m-tok"}, transport=meta)
    assert updates["metaUserId"] == "1001"
    assert updates["connectedAs"] == "Meta · Aryan Singh"
    assert updates["token_expires_at"]

    ig = _Transport({verify.INSTAGRAM_ME_ENDPOINT: _Resp(200, {"id": "1789", "username": "acme"})})
    updates = await verify.verify_instagram_grant(
        material={"access_token": "i-tok", "user_id": "1789"}, transport=ig
    )
    assert updates["instagramUsername"] == "acme"
    assert updates["connectedAs"] == "@acme"


async def test_a_transport_error_or_a_refusal_never_becomes_a_connection():
    class _Down:
        async def get(self, url: str, *, headers: dict[str, str] | None = None) -> _Resp:
            raise OSError("no route")

    for verifier in (
        verify.verify_hubspot_grant,
        verify.verify_google_calendar,
        verify.verify_notion_workspace,
        verify.verify_calendly_user,
        verify.verify_meta_grant,
        verify.verify_instagram_grant,
    ):
        with pytest.raises(PostExchangeVerifyFailed):
            await verifier(material={"access_token": "tok", "instance_url": SF}, transport=_Down())


def test_every_oauth_provider_in_the_registry_has_a_verifier():
    oauth = {p for p, s in registry.PROVIDERS.items() if s.is_oauth}
    assert oauth <= set(POST_EXCHANGE_VERIFIERS), oauth - set(POST_EXCHANGE_VERIFIERS)
