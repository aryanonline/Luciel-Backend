"""The contact form has to actually receive something (ruling 1).

The marketing site shipped a form posting ``{name, email, message, captchaToken}`` to
``POST /api/v1/contact``, and no such route existed — so every enquiry a prospect wrote
was answered with a 404 the form did not surface, and lost.

The ruling is specific about the failure mode to avoid: "returns accepted only if store
succeeded". A contact form that answers "thanks!" on the strength of an email attempt is
the same class of lie as a Null sender fabricating a message id — the visitor is told
their message arrived when the only thing that happened was a call to a provider that may
have refused. So these tests pin the row, not the mail: accepted means stored.
"""

from __future__ import annotations

import pytest
from app.channels import rate_limit
from app.db.session import untenanted_session
from app.models.contact import ContactSubmission
from sqlalchemy import select

pytestmark = [pytest.mark.contract]

_BODY = {
    "name": "Dana Prospect",
    "email": "dana@example.com",
    "message": "Can Luciel answer questions about our pricing page?",
    "captchaToken": "t",
}


@pytest.fixture(autouse=True)
def _fresh_bucket():
    """The bucket is a process global and these tests deliberately exhaust it."""
    rate_limit._store = None
    yield
    rate_limit._store = None


async def _stored() -> list[ContactSubmission]:
    async with untenanted_session() as s:
        return list((await s.execute(select(ContactSubmission))).scalars().all())


async def test_a_submission_is_persisted_and_acknowledged(client) -> None:
    resp = await client.post("/api/v1/contact", json=_BODY)

    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True

    rows = await _stored()
    assert len(rows) == 1
    assert str(rows[0].id) == body["submissionId"]
    assert rows[0].name == "Dana Prospect"
    assert rows[0].email == "dana@example.com"
    assert rows[0].message == _BODY["message"]


async def test_mock_mode_records_that_nothing_was_emailed(client) -> None:
    """The Null sender is honest about not sending; the row must be too."""
    await client.post("/api/v1/contact", json=_BODY)

    rows = await _stored()
    assert rows[0].notified is False


async def test_a_failed_store_is_not_reported_as_accepted(client, monkeypatch) -> None:
    """The one failure the visitor is told about — retrying is the correct advice."""
    from app.api.v1 import contact as contact_mod

    def _boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(contact_mod, "untenanted_session", _boom)

    resp = await client.post("/api/v1/contact", json=_BODY)

    assert resp.status_code == 500
    assert await _stored() == []


async def test_a_flood_from_one_source_is_refused_rather_than_absorbed(client) -> None:
    codes = [(await client.post("/api/v1/contact", json=_BODY)).status_code for _ in range(6)]

    assert codes[0] == 202
    assert 429 in codes, "an unauthenticated form with no rate limit is a free mailbox"
    # Only the admitted ones were stored — a rejection must not write a row.
    assert len(await _stored()) == codes.count(202)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("email", "not-an-email"),
        ("message", ""),
        ("message", "x" * 5001),
    ],
)
async def test_malformed_submissions_are_rejected_before_anything_is_stored(
    client, field: str, value: str
) -> None:
    resp = await client.post("/api/v1/contact", json={**_BODY, field: value})

    assert resp.status_code == 422
    assert await _stored() == []


async def test_the_endpoint_needs_no_session(client) -> None:
    """A prospect has not signed up — requiring auth here would be the whole bug again."""
    resp = await client.post("/api/v1/contact", json=_BODY)

    assert resp.status_code not in (401, 403)
