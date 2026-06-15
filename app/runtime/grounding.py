"""Answer grounding & anti-hallucination (DOCTRINE ANCHOR — Architecture §3.4.13).

Grounding floor is 0.50, uniform across all accounts (Architecture §9 #18).
Below floor with no resolving tool → cannot_answer escalation. Faithful-readback:
Luciel may only state what a tool/result actually contains. Lands in Arc 14.
"""

from __future__ import annotations

GROUNDING_FLOOR = 0.50
