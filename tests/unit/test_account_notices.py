"""Derivation rules for the in-dashboard allowance-reduction notice (Legal §A5).

§A5 owes notice for a REDUCTION only, with the effective date the Admin needs in order to
act on it. Everything here is about not crying wolf: no announcement means no banner, an
increase means no banner, and a change that has already landed is the current allowance
rather than news. The wire shape on ``/api/v1/auth/me`` is covered in
tests/contract/test_account_notices.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.billing import notices
from app.config import get_settings

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def _announce(monkeypatch, *, to: int, effective_at: str) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "free_allowance_reduction_effective_at", effective_at)
    monkeypatch.setattr(settings, "free_allowance_reduction_to", to)


def test_no_notice_when_nothing_is_announced():
    assert notices.account_notices(now=NOW) == []


def test_pending_reduction_carries_the_lead_time_and_both_numbers(monkeypatch):
    effective = NOW + timedelta(days=40)
    _announce(monkeypatch, to=25, effective_at=effective.isoformat())

    (notice,) = notices.account_notices(now=NOW)
    assert notice.kind == notices.ALLOWANCE_REDUCTION
    assert notice.effective_at == effective
    assert "25" in notice.body
    assert str(get_settings().free_allowance) in notice.body
    assert "40 days" in notice.body


def test_notice_id_is_stable_for_the_same_announcement(monkeypatch):
    effective = NOW + timedelta(days=40)
    _announce(monkeypatch, to=25, effective_at=effective.isoformat())
    first = notices.account_notices(now=NOW)[0].notice_id
    # A later render of the SAME pending change must not resurrect a dismissed banner.
    assert notices.account_notices(now=NOW + timedelta(days=1))[0].notice_id == first


def test_moving_the_date_is_a_new_notice(monkeypatch):
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=40)).isoformat())
    first = notices.account_notices(now=NOW)[0].notice_id
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=60)).isoformat())
    assert notices.account_notices(now=NOW)[0].notice_id != first


def test_notice_disappears_once_the_reduction_has_taken_effect(monkeypatch):
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=40)).isoformat())
    assert len(notices.account_notices(now=NOW)) == 1
    assert notices.account_notices(now=NOW + timedelta(days=41)) == []


def test_an_increase_is_never_announced(monkeypatch):
    _announce(
        monkeypatch,
        to=get_settings().free_allowance + 50,
        effective_at=(NOW + timedelta(days=40)).isoformat(),
    )
    assert notices.account_notices(now=NOW) == []


def test_an_unparseable_announcement_yields_no_notice(monkeypatch):
    """A malformed date must not 500 the session payload — the dashboard would not load."""
    _announce(monkeypatch, to=25, effective_at="not-a-date")
    assert notices.account_notices(now=NOW) == []


def test_a_naive_announcement_instant_is_treated_as_utc(monkeypatch):
    _announce(monkeypatch, to=25, effective_at="2026-07-11T00:00:00")
    (notice,) = notices.account_notices(now=NOW)
    assert notice.effective_at == datetime(2026, 7, 11, tzinfo=UTC)
