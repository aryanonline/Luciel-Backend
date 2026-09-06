"""The two sources of in-dashboard notices (Legal §A5 / §A10 / §B6).

§A5 owes notice for a REDUCTION only, with the effective date the Admin needs in order to
act on it. Much of this is about not crying wolf: no announcement means no banner, an
increase means no banner, and a change that has already landed is the current terms rather
than news.

The second half covers the operator PUBLISH path, which is what makes §A10 price changes
and §B6 sub-processor additions deliverable at all — their copy is operator-authored, so
nothing could derive it. Its two hard requirements are that a bad entry can never break
the login payload, and that noticeId stays stable (it is the client's dismissal key). The
wire shape on ``/api/v1/auth/me`` is covered in tests/contract/test_account_notices.py.
"""

from __future__ import annotations

import json
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


# --- operator-PUBLISHED notices (Legal §A5 / §A10 / §B6) --------------------- #


def _publish(monkeypatch, entries) -> None:
    monkeypatch.setattr(get_settings(), "platform_notices", json.dumps(entries))


def _entry(**over) -> dict:
    base = {
        "noticeId": "price:2026-09-01",
        "kind": notices.PRICE_CHANGE,
        "title": "Pay-as-you-go pricing is changing",
        "body": "From 1 September the PAYG block is $39 per 100 conversations.",
        "effectiveAt": (NOW + timedelta(days=45)).isoformat(),
    }
    base.update(over)
    return base


def test_a_published_notice_surfaces_with_its_full_shape(monkeypatch):
    """§A10 price changes and §B6 sub-processor additions have operator-authored copy, so
    nothing could derive them — a publish path is the only way they reach an Admin."""
    _publish(monkeypatch, [_entry(learnMoreHref="/billing")])

    (notice,) = notices.account_notices(now=NOW)
    assert notice.notice_id == "price:2026-09-01"
    assert notice.kind == notices.PRICE_CHANGE
    assert notice.title.startswith("Pay-as-you-go")
    assert "$39" in notice.body
    assert notice.effective_at == NOW + timedelta(days=45)
    assert notice.learn_more_href == "/billing"


def test_a_published_notice_id_is_stable_across_renders(monkeypatch):
    """The noticeId is the dismissal key, so the same announcement re-read on a later
    request must not resurrect a banner the Admin already dismissed this visit."""
    _publish(monkeypatch, [_entry()])
    first = notices.account_notices(now=NOW)[0].notice_id
    later = notices.account_notices(now=NOW + timedelta(days=3))[0].notice_id
    assert later == first == "price:2026-09-01"


def test_a_published_notice_with_no_date_runs_until_unpublished(monkeypatch):
    _publish(monkeypatch, [_entry(kind=notices.TERMS_CHANGE, effectiveAt=None)])
    (notice,) = notices.account_notices(now=NOW)
    assert notice.effective_at is None


def test_a_published_notice_stops_once_it_has_taken_effect(monkeypatch):
    """Same rule as the derived notice: a change that already landed is the current terms."""
    _publish(monkeypatch, [_entry()])
    assert notices.account_notices(now=NOW + timedelta(days=46)) == []


def test_an_unknown_kind_is_dropped_rather_than_500ing_the_session(monkeypatch):
    """AccountNoticeOut.kind is a closed Literal, so letting an unknown kind through would
    fail response validation and take the whole login payload down with it."""
    _publish(monkeypatch, [_entry(kind="marketing_blast")])
    assert notices.account_notices(now=NOW) == []


def test_an_incomplete_or_malformed_entry_is_skipped_not_raised(monkeypatch):
    """Fail-open per entry: this is read on the login path, so a typo in one announcement
    must not lock every Admin out of their dashboard."""
    _publish(monkeypatch, [_entry(noticeId=""), "not-an-object", _entry(body="")])
    assert notices.account_notices(now=NOW) == []

    monkeypatch.setattr(get_settings(), "platform_notices", "{not json")
    assert notices.account_notices(now=NOW) == []

    monkeypatch.setattr(get_settings(), "platform_notices", json.dumps({"noticeId": "x"}))
    assert notices.account_notices(now=NOW) == []


def test_a_good_entry_still_publishes_alongside_a_bad_one(monkeypatch):
    _publish(monkeypatch, ["junk", _entry()])
    (notice,) = notices.account_notices(now=NOW)
    assert notice.notice_id == "price:2026-09-01"


def test_a_published_allowance_notice_is_the_one_that_surfaces(monkeypatch):
    """§A5 can be satisfied by hand-written copy. Both sources describe the SAME
    announcement, so surfacing both would double-banner the reduction."""
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=40)).isoformat())
    _publish(
        monkeypatch,
        [_entry(noticeId="allowance:2026-07-11", kind=notices.ALLOWANCE_REDUCTION)],
    )

    (notice,) = notices.account_notices(now=NOW)
    assert notice.notice_id == "allowance:2026-07-11"


def test_a_published_price_notice_does_not_suppress_the_derived_allowance_one(monkeypatch):
    """Only an allowance notice supersedes the derived allowance notice — an unrelated
    §A10 announcement must not swallow the §A5 obligation."""
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=40)).isoformat())
    _publish(monkeypatch, [_entry()])

    kinds = {n.kind for n in notices.account_notices(now=NOW)}
    assert kinds == {notices.PRICE_CHANGE, notices.ALLOWANCE_REDUCTION}


# --- 2026-09-05 audit F114: the auto-swap 'model updated' notice ------------------- #


def _swap(
    monkeypatch, *, at: datetime, old: str = "claude-sonnet-4-6", new: str = "claude-sonnet-4-8"
):
    record = {
        "at": at.isoformat(),
        "setting": "llm_anthropic_primary_model",
        "from": old,
        "to": new,
    }
    monkeypatch.setattr(get_settings(), "llm_model_updated", json.dumps(record))


def test_an_auto_swap_shows_the_model_updated_notice_for_two_weeks(monkeypatch):
    swapped_at = NOW - timedelta(days=3)
    _swap(monkeypatch, at=swapped_at)
    (notice,) = notices.account_notices(now=NOW)
    assert notice.kind == notices.MODEL_UPDATED
    assert notice.notice_id == f"model_updated:{swapped_at.date().isoformat()}:claude-sonnet-4-8"
    assert "claude-sonnet-4-6" in notice.body and "claude-sonnet-4-8" in notice.body
    assert "nothing is needed" in notice.body and "test turn" in notice.body
    assert notice.effective_at is None
    # It is news for a fortnight, then it is simply the current model.
    assert notices.account_notices(now=NOW + timedelta(days=20)) == []
    # A record from the future (clock skew) is not shown early either.
    assert notices.account_notices(now=swapped_at - timedelta(days=1)) == []


def test_the_model_updated_notice_rides_alongside_a_pending_reduction(monkeypatch):
    _announce(monkeypatch, to=25, effective_at=(NOW + timedelta(days=40)).isoformat())
    _swap(monkeypatch, at=NOW - timedelta(days=1))
    kinds = [n.kind for n in notices.account_notices(now=NOW)]
    assert kinds == [notices.ALLOWANCE_REDUCTION, notices.MODEL_UPDATED]


def test_a_malformed_swap_record_never_breaks_login(monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_model_updated", "{not json")
    assert notices.account_notices(now=NOW) == []
    monkeypatch.setattr(get_settings(), "llm_model_updated", json.dumps({"at": "yesterday"}))
    assert notices.account_notices(now=NOW) == []
