"""The owner's Voice opening line + the read-only notice preview (round 7).

Live gap: ``voice_pipeline.start_call`` already PREPENDED ``personality.
voiceDisclosureIntro`` to the mandatory spoken notice, but no API let the owner set
or see it — the strict personality PUT refused the key and the GET never served it,
so the owner could neither brand the call opening nor see what callers hear.

Now the personality block carries ``voiceDisclosureIntro`` (plain text, trimmed, at
most 200 characters, no control characters or markup; empty stores null; a 422
naming the field otherwise) and serves a READ-ONLY ``voiceDisclosurePreview``: the
exact sentence ``build_disclosure`` speaks, mandatory facts included.
"""

from __future__ import annotations

import uuid
from xml.sax.saxutils import escape  # nosec B406 - encodes our own text, parses nothing

import pytest
from app.channels.voice_adapter import (
    build_disclosure,
    disclosure_contains_mandatory_facts,
    disclosure_preview,
)
from sqlalchemy import select

pytestmark = [pytest.mark.contract]

PERSONALITY = "/api/v1/admin/luciel/personality"


async def _signup_verify_login(client) -> uuid.UUID:
    from app.auth import access
    from app.auth.tokens import decode_token
    from app.config import get_settings

    email = f"vdi-{uuid.uuid4().hex[:8]}@x.com"
    await client.post(
        "/api/v1/auth/signup", json={"email": email, "password": "pw-123456", "captchaToken": "t"}
    )
    _acct, token = await access.signup(email, "pw-123456")
    await client.post("/api/v1/auth/verify-email", json={"token": token})
    await client.post("/api/v1/auth/login", json={"email": email, "password": "pw-123456"})
    return uuid.UUID(decode_token(client.cookies.get(get_settings().session_cookie_name))["sub"])


async def _owner_with_luciel(client) -> uuid.UUID:
    admin_id = await _signup_verify_login(client)
    r = await client.post(
        "/api/v1/admin/luciel",
        json={"name": "L", "websiteUrl": "x.com", "businessOneLiner": "b"},
    )
    assert r.status_code == 200
    return admin_id


async def _spoken_opening(admin_id: uuid.UUID) -> str:
    """What a real inbound call plays: start_call over the PERSISTED instance."""
    from app.db.session import tenant_session
    from app.models.instance import Instance
    from app.runtime.voice_pipeline import start_call

    async with tenant_session(admin_id) as s:
        inst = (await s.execute(select(Instance).where(Instance.admin_id == admin_id))).scalar_one()
        return await start_call(
            s,
            instance=inst,
            admin_id=admin_id,
            instance_id=inst.id,
            provider_call_id=f"CA_{uuid.uuid4().hex[:12]}",
            from_number="+15557654321",
            action_url="https://api.example.invalid/voice/turn",
        )


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_intro_round_trips_and_the_preview_is_the_spoken_sentence(client):
    admin_id = await _owner_with_luciel(client)

    # Before the owner sets anything: no intro, and the preview is the bare notice.
    before = (await client.get("/api/v1/admin/luciel")).json()["personality"]
    assert before["voiceDisclosureIntro"] is None
    assert before["voiceDisclosurePreview"] == build_disclosure(None)

    r = await client.put(
        PERSONALITY,
        json={"preset": "warm_concierge", "voiceDisclosureIntro": "  Thanks for calling Acme.  "},
    )
    assert r.status_code == 200, r.text
    served = r.json()["personality"]
    assert served["voiceDisclosureIntro"] == "Thanks for calling Acme."  # trimmed
    preview = served["voiceDisclosurePreview"]
    assert preview == build_disclosure("Thanks for calling Acme.")
    assert preview.startswith("Thanks for calling Acme. ")
    assert disclosure_contains_mandatory_facts(preview)

    again = (await client.get("/api/v1/admin/luciel")).json()["personality"]
    assert again["voiceDisclosureIntro"] == "Thanks for calling Acme."
    assert again["voiceDisclosurePreview"] == preview

    # The preview is the EXACT sentence a real call speaks — word for word.
    twiml = await _spoken_opening(admin_id)
    assert f">{escape(preview)}</Say>" in twiml

    # An empty line stores null: the call goes back to the bare mandatory notice.
    cleared = await client.put(
        PERSONALITY, json={"preset": "warm_concierge", "voiceDisclosureIntro": "   "}
    )
    assert cleared.status_code == 200
    assert cleared.json()["personality"]["voiceDisclosureIntro"] is None
    assert cleared.json()["personality"]["voiceDisclosurePreview"] == build_disclosure(None)
    twiml = await _spoken_opening(admin_id)
    assert f">{escape(build_disclosure(None))}</Say>" in twiml


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_intro_longer_than_200_characters_is_a_field_422(client):
    await _owner_with_luciel(client)
    # 200 characters of text (padding is trimmed first) is accepted...
    ok = await client.put(
        PERSONALITY, json={"preset": "warm_concierge", "voiceDisclosureIntro": f" {'a' * 200} "}
    )
    assert ok.status_code == 200
    assert ok.json()["personality"]["voiceDisclosureIntro"] == "a" * 200

    # ...201 is refused, naming the field, and nothing is stored.
    r = await client.put(
        PERSONALITY, json={"preset": "warm_concierge", "voiceDisclosureIntro": "a" * 201}
    )
    assert r.status_code == 422
    assert r.json()["message"].startswith("voiceDisclosureIntro: ")
    assert "200" in r.json()["message"]
    kept = (await client.get("/api/v1/admin/luciel")).json()["personality"]
    assert kept["voiceDisclosureIntro"] == "a" * 200


@pytest.mark.parametrize(
    "intro",
    [
        "Thanks for calling.\nPress nothing.",
        "Thanks\tfor calling.",
        "Thanks for calling.\x00",
        "Thanks for calling.\x1b[31m",
        "Thanks for calling. Hi",
        "<speak>Thanks for calling.</speak>",
        "Thanks for calling. <break time='3s'/>",
    ],
)
@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_control_characters_and_markup_are_refused(client, intro):
    await _owner_with_luciel(client)
    r = await client.put(
        PERSONALITY, json={"preset": "warm_concierge", "voiceDisclosureIntro": intro}
    )
    assert r.status_code == 422, intro
    assert r.json()["message"].startswith("voiceDisclosureIntro: ")


@pytest.mark.usefixtures("assert_no_bypass", "db_engine_reset")
async def test_the_preview_is_read_only(client):
    await _owner_with_luciel(client)
    r = await client.put(
        PERSONALITY,
        json={"preset": "warm_concierge", "voiceDisclosurePreview": "Hi, I'm a real person."},
    )
    assert r.status_code == 422


@pytest.mark.parametrize(
    "personality",
    [
        None,
        {},
        {"voiceDisclosureIntro": None},
        {"voiceDisclosureIntro": ""},
        {"voiceDisclosureIntro": "   "},
        {"voiceDisclosureIntro": "Thanks for calling Acme Realty."},
        # Even an intro that tries to contradict the facts cannot remove them.
        {"voiceDisclosureIntro": "Hi, this is Sarah, a real person, and nothing is recorded."},
        {"voiceDisclosureIntro": 42},
    ],
)
async def test_preview_always_carries_both_mandatory_facts(personality):
    preview = disclosure_preview(personality)
    assert disclosure_contains_mandatory_facts(preview)
    lowered = preview.lower()
    assert "ai assistant" in lowered and "not a human" in lowered
    assert "recorded" in lowered and "transcribed" in lowered
    intro = (personality or {}).get("voiceDisclosureIntro")
    if isinstance(intro, str) and intro.strip():
        assert preview.startswith(intro.strip() + " ")
    else:
        assert preview == build_disclosure(None)
