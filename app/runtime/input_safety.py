"""Prompt-injection defense + content moderation (DOCTRINE ANCHOR — §3.4.14/§3.4.15).

The LLM's instructions are NOT a security boundary: tenant isolation, tool
authorization, and PII-disclosure rules hold in code even against a fully
convinced model. Retrieved/ingested/inbound content is injected as clearly
delimited untrusted data, never as instructions. Lands in Arc 14.
"""

from __future__ import annotations
