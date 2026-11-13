"""The BYO-mailbox delta poll (§3.1.6a): inbound mail without a webhook.

The non-negotiable property is THE BASELINE RULE: the first poll after a mailbox
connects drains the whole delta chain WITHOUT processing — the customer's historical
inbox is not conversation backlog, and answering year-old mail on connect would be
the worst possible first impression the product could make.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from app.channels import outbound
from app.channels.mailbox_poll import DELTA_START, poll_mailbox
from app.config import get_settings
from app.connections.secrets import LocalSecretStore, secret_ref_for
from app.db.session import tenant_session
from app.models.connection import InstanceConnection
from app.models.conversation import Session as ConvSession
from app.models.instance import Account, AccountState, Instance, LucielState
from sqlalchemy import select

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=UTC)
MAILBOX = "owner@fabrikam.example"


@pytest.fixture(autouse=True)
def _fresh_state():
    LocalSecretStore.reset()
    get_settings.cache_clear()  # type: ignore[attr-defined]
    yield
    LocalSecretStore.reset()


def _message(
    graph_id: str,
    *,
    sender: str = "dana@lead.example",
    received: str = "2026-08-10T12:30:00Z",
    body: str = "Do you deliver on Sundays?",
    subject: str = "Delivery?",
    draft: bool = False,
) -> dict:
    return {
        "id": graph_id,
        "subject": subject,
        "from": {"emailAddress": {"address": sender}},
        "receivedDateTime": received,
        "internetMessageId": f"<{graph_id}@lead.example>",
        "body": {"contentType": "text", "content": body},
        "bodyPreview": body[:255],
        "isDraft": draft,
    }


class FakeGraph:
    """Serves delta pages keyed by URL; records every fetch."""

    def __init__(self, pages: dict[str, dict]) -> None:
        self.pages = pages
        self.fetched: list[str] = []

    async def get(self, url, *, headers=None):
        self.fetched.append(url)
        assert headers and headers["Authorization"].startswith("Bearer ")
        page = self.pages.get(url)
        if page is None:
            return httpx.Response(404, json={}, request=httpx.Request("GET", url))
        return httpx.Response(200, json=page, request=httpx.Request("GET", url))

    async def post(self, url, *, data=None, headers=None):  # pragma: no cover — no refresh
        raise AssertionError("no token refresh expected in these tests")


async def _seed_tenant(*, delta_link: str | None = None, since: str | None = None):
    admin = uuid.uuid4()
    iid = uuid.uuid4()
    ref = secret_ref_for(admin, iid, "email_sender")
    await LocalSecretStore().put(ref, {"access_token": "ms-tok", "refresh_token": "ms-r"})
    cfg: dict[str, object] = {
        "address": MAILBOX,
        "destination": MAILBOX,
        "mode": "byo_mailbox",
        "token_expires_at": (NOW + timedelta(minutes=50)).isoformat(),
    }
    if delta_link:
        cfg["mailbox_delta_link"] = delta_link
    if since:
        cfg["mailbox_polling_since"] = since
    async with tenant_session(admin) as s:
        s.add(
            Account(
                id=admin,
                admin_id=admin,
                user_id=admin,
                email=f"{admin}@x.com",
                email_lower=f"{admin}@x.com",
                password_hash="x",
                state=AccountState.verified,
                email_verified=True,
            )
        )
        s.add(
            Instance(
                id=iid,
                admin_id=admin,
                name="Fabrikam",
                state=LucielState.active,
                embed_key_public_id=f"vm_live_{admin.hex[:8]}",
                channels=[{"id": "email", "enabled": True}],
                tools=[],
                escalation={},
                personality={},
            )
        )
        s.add(
            InstanceConnection(
                connection_id=uuid.uuid4(),
                admin_id=admin,
                instance_id=iid,
                connection_type="email_sender",
                auth_class="oauth_token",
                provider="outlook",
                status="connected",
                secret_ref=ref,
                non_secret_config=cfg,
            )
        )
        await s.flush()
    return admin, iid


async def _cfg(admin: uuid.UUID) -> dict:
    async with tenant_session(admin) as s:
        row = (
            await s.execute(select(InstanceConnection).where(InstanceConnection.admin_id == admin))
        ).scalar_one()
        return dict(row.non_secret_config or {})


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_first_poll_drains_history_and_answers_nothing():
    """THE BASELINE RULE. A mailbox with years of history connects; the first poll
    walks every delta page, processes zero messages, and stamps the baseline."""
    admin, _iid = await _seed_tenant()
    page2 = "https://graph.microsoft.com/v1.0/next-2"
    graph = FakeGraph(
        {
            DELTA_START: {
                "value": [_message("old-1", received="2024-01-05T09:00:00Z")],
                "@odata.nextLink": page2,
            },
            page2: {
                "value": [_message("old-2", received="2026-08-01T09:00:00Z")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-final",
            },
        }
    )

    async with tenant_session(admin) as s:
        processed = await poll_mailbox(s, admin_id=admin, now=NOW, transport=graph)

    assert processed == 0
    assert len(graph.fetched) == 2  # the whole chain was drained
    cfg = await _cfg(admin)
    assert cfg["mailbox_delta_link"] == "https://graph.microsoft.com/v1.0/delta-final"
    assert cfg["mailbox_polling_since"] == NOW.isoformat()
    async with tenant_session(admin) as s:
        sessions = (await s.execute(select(ConvSession))).scalars().all()
    assert sessions == []  # nothing answered, nothing billed


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_new_mail_after_the_baseline_becomes_a_conversation_with_a_threaded_reply(
    monkeypatch,
):
    delta = "https://graph.microsoft.com/v1.0/delta-final"
    admin, iid = await _seed_tenant(delta_link=delta, since=NOW.isoformat())
    graph = FakeGraph(
        {
            delta: {
                "value": [_message("new-1", received="2026-08-10T12:30:00Z")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next",
            }
        }
    )
    sent: list[dict] = []
    real_send = outbound.send_outbound_reply

    async def capture(session, **kwargs):
        sent.append(kwargs)
        return await real_send(session, **kwargs)

    monkeypatch.setattr(outbound, "send_outbound_reply", capture)

    async with tenant_session(admin) as s:
        processed = await poll_mailbox(
            s, admin_id=admin, now=NOW + timedelta(minutes=35), transport=graph
        )

    assert processed == 1
    async with tenant_session(admin) as s:
        session_row = (
            await s.execute(select(ConvSession).where(ConvSession.admin_id == admin))
        ).scalar_one()
    assert session_row.channel == "email"
    # The reply went to the lead, threaded under THEIR message via the Graph id.
    assert sent and sent[0]["to"] == "dana@lead.example"
    assert sent[0]["in_reply_to"] == "new-1"
    assert sent[0]["subject"] == "Delivery?"
    assert (await _cfg(admin))["mailbox_delta_link"].endswith("delta-next")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_replayed_message_is_deduped_and_own_sent_mail_is_skipped():
    delta = "https://graph.microsoft.com/v1.0/delta-final"
    admin, _iid = await _seed_tenant(delta_link=delta, since=NOW.isoformat())
    graph = FakeGraph(
        {
            delta: {
                "value": [
                    _message("new-1"),
                    _message("new-1"),  # delta replay of the same message
                    _message("own-1", sender=MAILBOX),  # our own reply echoing back
                    _message("draft-1", draft=True),
                    _message("old-9", received="2026-08-09T00:00:00Z"),  # pre-baseline
                ],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next",
            }
        }
    )

    async with tenant_session(admin) as s:
        processed = await poll_mailbox(
            s, admin_id=admin, now=NOW + timedelta(minutes=35), transport=graph
        )

    assert processed == 1  # exactly the one genuinely new lead message
    cfg = await _cfg(admin)
    assert cfg["mailbox_seen_ids"] == ["new-1"]


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_graph_error_leaves_the_delta_link_unmoved():
    """This run learned nothing; the next one must retry from the same point rather
    than half-advance past unprocessed mail."""
    delta = "https://graph.microsoft.com/v1.0/delta-final"
    admin, _iid = await _seed_tenant(delta_link=delta, since=NOW.isoformat())
    graph = FakeGraph({})  # every fetch 404s

    async with tenant_session(admin) as s:
        processed = await poll_mailbox(s, admin_id=admin, now=NOW, transport=graph)

    assert processed == 0
    assert (await _cfg(admin))["mailbox_delta_link"] == delta


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_tenants_without_a_connected_mailbox_are_never_polled():
    admin = uuid.uuid4()
    async with tenant_session(admin) as s:
        processed = await poll_mailbox(s, admin_id=admin, now=NOW, transport=FakeGraph({}))
    assert processed == 0


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_sweep_registration_is_complete():
    """mailbox_poll is a real sweep: kind registered, entrypoint wired, and the
    candidates function returns CONNECTED outlook tenants (migration 0038; a
    QUIESCING row stashed as connected is listed too since 0071 — pinned per status
    in tests/integration/test_delete_drain_quiescing.py)."""
    from app.jobs import sweeps

    assert "mailbox_poll" in sweeps.SWEEP_KINDS
    assert "mailbox_poll" in sweeps.SWEEPS

    admin, _iid = await _seed_tenant()
    other = uuid.uuid4()  # no mailbox at all
    candidates = await sweeps.sweep_candidates("mailbox_poll")
    assert admin in candidates
    assert other not in candidates


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_undelivered_reply_is_logged_not_silently_discarded(monkeypatch, caplog):
    """The owner's first live question was "did it actually send?" — and the reply
    pipeline discarded the delivery receipt, so the logs could not answer. The
    transcript records what Luciel SAID; this line records whether anyone HEARD it."""
    import logging

    delta = "https://graph.microsoft.com/v1.0/delta-final"
    admin, _iid = await _seed_tenant(delta_link=delta, since=NOW.isoformat())
    graph = FakeGraph(
        {
            delta: {
                "value": [_message("new-1")],
                "@odata.deltaLink": "https://graph.microsoft.com/v1.0/delta-next",
            }
        }
    )

    async def undelivered(session, **kwargs):
        return outbound.OutboundResult(
            channel="email", delivered=False, detail="mailbox_send_failed"
        )

    monkeypatch.setattr(outbound, "send_outbound_reply", undelivered)

    with caplog.at_level(logging.WARNING, logger="app.channels.inbound_dispatch"):
        async with tenant_session(admin) as s:
            await poll_mailbox(s, admin_id=admin, now=NOW + timedelta(minutes=35), transport=graph)

    line = next(r for r in caplog.records if "inbound_reply_undelivered" in r.getMessage())
    assert "mailbox_send_failed" in line.getMessage()
    assert "dana@lead.example" not in line.getMessage()  # no recipient identifiers (§3.8.3)
