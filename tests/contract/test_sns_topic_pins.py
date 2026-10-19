"""Both SES webhook routes refuse a message from any topic but their own (2026-09-05
audit F048).

An SNS signature proves the message came from SNS — not from OUR topic. Anyone with
an AWS account can create a topic, subscribe our endpoint, and publish signed
messages into it. The events route pinned its topic; the inbound-mail route did not,
so a stranger could feed a tenant inbound mail through a topic they control. In live
mode with a pinned ARN both routes now reject a foreign topic BEFORE looking at the
signature; unpinned (dev / mock) behaviour is unchanged.

Round 7 WP-7: ComputeStack never injected SES_INBOUND_TOPIC_ARN, so in every live
deploy the inbound pin was simply skipped. The CDK now injects it (the managed -v2
topic plus the console-created topic by name, comma-separated), and a LIVE inbound
route with no pin refuses everything rather than trusting any signed topic.
"""

from __future__ import annotations

import pytest
from app.config import get_settings

pytestmark = [pytest.mark.contract, pytest.mark.asyncio]

OURS = "arn:aws:sns:ca-central-1:111111111111:luciel-dev-ses-inbound"
THEIRS = "arn:aws:sns:us-east-1:222222222222:someone-elses-topic"


def _envelope(topic: str) -> dict:
    return {
        "Type": "Notification",
        "MessageId": "m-1",
        "TopicArn": topic,
        "Message": "{}",
        "Timestamp": "2026-09-05T00:00:00.000Z",
        "SignatureVersion": "1",
        "Signature": "not-a-real-signature",
        "SigningCertURL": "https://sns.ca-central-1.amazonaws.com/x.pem",
    }


@pytest.mark.parametrize(
    ("route", "setting"),
    [
        ("/api/v1/webhooks/ses/inbound", "ses_inbound_topic_arn"),
        ("/api/v1/webhooks/ses/events", "ses_events_topic_arn"),
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_foreign_topic_is_rejected_before_signature_verification(
    client, monkeypatch, route, setting
):
    monkeypatch.setattr(get_settings(), "integration_mode", "live", raising=False)
    monkeypatch.setattr(get_settings(), setting, OURS, raising=False)

    r = await client.post(route, json=_envelope(THEIRS))
    assert r.status_code == 200
    assert r.json() == {"status": "rejected", "reason": "topic_mismatch"}

    # Our own topic gets past the pin and on to the real checks (the signature here
    # is fake, so it is refused THERE — never as a topic mismatch).
    r = await client.post(route, json=_envelope(OURS))
    assert r.status_code == 200
    assert r.json().get("reason") != "topic_mismatch"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_an_unpinned_inbound_route_does_not_refuse_on_topic(client):
    # mock posture, nothing pinned: the topic is not consulted at all.
    r = await client.post("/api/v1/webhooks/ses/inbound", json=_envelope(THEIRS))
    assert r.status_code == 200
    assert r.json().get("reason") != "topic_mismatch"


MANAGED_V2 = "arn:aws:sns:ca-central-1:111111111111:luciel-dev-ses-inbound-v2"


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_a_live_inbound_route_without_a_pin_refuses_every_topic(client, monkeypatch):
    """The condition that shipped: live mode, SES_INBOUND_TOPIC_ARN never injected. The
    pin used to be skipped; now nothing is accepted — not even a subscription
    confirmation — until the deployment names its topic."""
    monkeypatch.setattr(get_settings(), "integration_mode", "live", raising=False)
    monkeypatch.setattr(get_settings(), "ses_inbound_topic_arn", "", raising=False)

    for envelope in (_envelope(OURS), _envelope(THEIRS)):
        r = await client.post("/api/v1/webhooks/ses/inbound", json=envelope)
        assert r.status_code == 200
        assert r.json() == {"status": "rejected", "reason": "topic_unpinned"}

    confirmation = {**_envelope(THEIRS), "Type": "SubscriptionConfirmation"}
    r = await client.post("/api/v1/webhooks/ses/inbound", json=confirmation)
    assert r.json() == {"status": "rejected", "reason": "topic_unpinned"}


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_inbound_pin_accepts_each_listed_topic_and_nothing_else(client, monkeypatch):
    """ComputeStack injects TWO topics (the managed -v2 and the console one): an
    environment before or after the receipt-rule cutover must both keep their mail."""
    monkeypatch.setattr(get_settings(), "integration_mode", "live", raising=False)
    monkeypatch.setattr(
        get_settings(), "ses_inbound_topic_arn", f"{MANAGED_V2}, {OURS}", raising=False
    )

    for ours in (OURS, MANAGED_V2):
        r = await client.post("/api/v1/webhooks/ses/inbound", json=_envelope(ours))
        assert r.json().get("reason") not in ("topic_mismatch", "topic_unpinned")

    r = await client.post("/api/v1/webhooks/ses/inbound", json=_envelope(THEIRS))
    assert r.json() == {"status": "rejected", "reason": "topic_mismatch"}


async def test_an_unpinned_live_inbound_route_is_a_degraded_binding(monkeypatch):
    from app.runtime.integration_status import degraded_bindings

    settings = get_settings()
    monkeypatch.setattr(settings, "ses_inbound_topic_arn", "", raising=False)
    assert "ses_inbound_topic:unpinned" not in degraded_bindings()  # mock: route is open

    monkeypatch.setattr(settings, "integration_mode", "live", raising=False)
    assert "ses_inbound_topic:unpinned" in degraded_bindings()

    monkeypatch.setattr(settings, "ses_inbound_topic_arn", OURS, raising=False)
    assert "ses_inbound_topic:unpinned" not in degraded_bindings()
