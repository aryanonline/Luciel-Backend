"""split the combined instagram_messenger channel into messenger + instagram (§3.1.2)

Architecture §3.1.2 ratifies that the wire and config enums carry SEPARATE
``whatsapp``, ``messenger``, and ``instagram`` channels: one ``meta`` connection
powers WhatsApp + Messenger, Instagram rides its own Business Login connection,
and each channel binds its own destination. The config surface previously stored
one combined ``instagram_messenger`` entry in ``instances.channels`` (JSONB list),
which conflated two channels that do not even share a sign-in.

This migration rewrites every instance's channels list: the combined entry is
replaced IN PLACE (list order preserved) by two entries, ``messenger`` and
``instagram``, both inheriting the combined entry's ``enabled`` flag and any other
per-channel fields it carried. An owner who had the combined channel on keeps
both surfaces answering — turning either OFF afterward is a plain config act.

Deliberately NOT rewritten:
  * ``sessions.channel`` rows — a historic session does not say which platform it
    used, and inventing that would falsify the record. Read paths tolerate the
    legacy value (schemas_api/conversations.py, runtime maps).
  * ``config_snapshot`` blobs on old sessions — frozen history (§3.8.7 rule E).

Downgrade merges the two entries back into one combined entry whose ``enabled``
is the OR of the pair (the safe direction: a channel the owner had answering
keeps answering).

Revision ID: 0032_split_meta_channels
Revises: 0031_transcript_source_chunks
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0032_split_meta_channels"
down_revision: str | None = "0031_transcript_source_chunks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None

_LEGACY = "instagram_messenger"


def _split_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    base = {k: v for k, v in entry.items() if k != "id"}
    return [{"id": "messenger", **base}, {"id": "instagram", **base}]


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, channels FROM instances")).fetchall()
    for row in rows:
        channels = row.channels if isinstance(row.channels, list) else []
        if not any(isinstance(c, dict) and c.get("id") == _LEGACY for c in channels):
            continue  # idempotent: nothing to split
        rewritten: list[dict[str, Any]] = []
        for c in channels:
            if isinstance(c, dict) and c.get("id") == _LEGACY:
                rewritten.extend(_split_entry(c))
            else:
                rewritten.append(c)
        conn.execute(
            sa.text("UPDATE instances SET channels = CAST(:channels AS jsonb) WHERE id = :id"),
            {"channels": json.dumps(rewritten), "id": row.id},
        )


def downgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, channels FROM instances")).fetchall()
    for row in rows:
        channels = row.channels if isinstance(row.channels, list) else []
        split = {
            c.get("id"): c
            for c in channels
            if isinstance(c, dict) and c.get("id") in ("messenger", "instagram")
        }
        if not split:
            continue
        merged_enabled = any(bool(c.get("enabled")) for c in split.values())
        combined = {**next(iter(split.values())), "id": _LEGACY, "enabled": merged_enabled}
        rewritten: list[dict[str, Any]] = []
        emitted = False
        for c in channels:
            if isinstance(c, dict) and c.get("id") in ("messenger", "instagram"):
                if not emitted:
                    rewritten.append(combined)
                    emitted = True
                continue
            rewritten.append(c)
        conn.execute(
            sa.text("UPDATE instances SET channels = CAST(:channels AS jsonb) WHERE id = :id"),
            {"channels": json.dumps(rewritten), "id": row.id},
        )
