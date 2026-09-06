"""Both SES webhook routes refuse a message from any topic but their own (2026-09-05
audit F048).

An SNS signature proves the message came from SNS — not from OUR topic. Anyone with
an AWS account can create a topic, subscribe our endpoint, and publish signed
messages into it. The events route pinned its topic; the inbound-mail route did not,
so a stranger could feed a tenant inbound mail through a topic they control. In live
mode with a pinned ARN both routes now reject a foreign topic BEFORE looking at the
signature; unpinned (dev / mock) behaviour is unchanged.
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
