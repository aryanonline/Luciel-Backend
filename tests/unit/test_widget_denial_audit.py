"""Adversarial review R-WP1-1: a widget flood must not become one audit INSERT per
denied request. Denials are folded per (instance, reason) per window; the row that
is written says how many it stands for."""

from __future__ import annotations

import uuid

from app.api.v1 import chat_widget as cw


def test_first_denial_writes_and_the_window_folds_the_rest():
    iid = uuid.uuid4()
    assert cw._denial_audit_folded(iid, "rate_limited", now=1000.0) == 0
    assert cw._denial_audit_folded(iid, "rate_limited", now=1001.0) is None
    assert cw._denial_audit_folded(iid, "rate_limited", now=1030.0) is None
    assert cw._denial_audit_folded(iid, "rate_limited", now=1059.9) is None
    # The window closes: one row, carrying the three denials it stands for.
    assert cw._denial_audit_folded(iid, "rate_limited", now=1061.0) == 3
    assert cw._denial_audit_folded(iid, "rate_limited", now=1062.0) is None


def test_reasons_and_instances_fold_independently():
    a, b = uuid.uuid4(), uuid.uuid4()
    assert cw._denial_audit_folded(a, "rate_limited", now=5.0) == 0
    assert cw._denial_audit_folded(a, "daily_cap", now=5.0) == 0
    assert cw._denial_audit_folded(b, "rate_limited", now=5.0) == 0
    assert cw._denial_audit_folded(a, "rate_limited", now=6.0) is None
    assert cw._denial_audit_folded(b, "daily_cap", now=6.0) == 0


def test_the_slot_table_is_bounded(monkeypatch):
    monkeypatch.setattr(cw, "_DENIAL_AUDIT_MAX_KEYS", 8)
    cw._denial_audit_slots.clear()
    for i in range(8):
        assert cw._denial_audit_folded(uuid.uuid4(), "rate_limited", now=float(i)) == 0
    # Past the ceiling, keys whose window has closed are dropped before a new one lands.
    assert cw._denial_audit_folded(uuid.uuid4(), "rate_limited", now=500.0) == 0
    assert len(cw._denial_audit_slots) == 1
    cw._denial_audit_slots.clear()
