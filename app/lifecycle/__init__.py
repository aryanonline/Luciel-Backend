"""Luciel/account state machine + hard-delete cascade + export (DOCTRINE ANCHOR — §3.6).

Pause (non-destructive), delete Luciel (fused quiesce → 30-day grace → cascade,
account shell survives), restore → active (not paused), close account (requires
Luciel deleted first; export-first). Lands in Arc 10.
"""
