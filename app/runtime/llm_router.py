"""LLM provider + model routing (DOCTRINE ANCHOR — Architecture §3.4.3).

Anthropic primary, OpenAI fallback (immediate failover, no retry on primary);
fast/cheap intra-session routing under the stated conditions; prompt caching on
the system-prompt prefix; model selection is platform-controlled, never an admin
field. Lands in Arc 14.
"""

from __future__ import annotations
