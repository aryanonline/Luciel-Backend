"""The employee coherence layer (round 6 WP-F, 2026-09-07).

Every feature and every changeable configuration adds up to one thing the owner
hired: an employee who is on duty around the clock. This package derives that
employee's status from rows that already exist — what it can do right now, what it
needs from the owner, and what it did — and speaks it in three places: the
dashboard's Today card (``GET /admin/luciel/status``), the morning brief email
(``daily_brief`` sweep), and the model's own operating-context prompt stanza.
Nothing here is narrated by a model; it is counts, states and links.
"""
