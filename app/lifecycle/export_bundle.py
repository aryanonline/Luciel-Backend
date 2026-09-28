"""Data export bundle (Architecture §5.10). Open ZIP, no proprietary lock-in.

Bundle layout:
  README.txt, conversations/{session}.json + conversations.csv, leads.json/.csv,
  knowledge/{file} + manifest.json, instance.json (provider + non-secret config +
  status; NEVER secret material), audit_log.jsonl.

Secret material is never included (§3.8.3): instance.json carries provider,
non_secret_config, status only. The bundle is built in-memory here; in production
it is uploaded to vm-export-bundles S3 with a 7-day presigned URL (§3.6.6).
"""

from __future__ import annotations

import csv
import io
import json
import uuid
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AdminAuditLog
from app.models.connection import InstanceConnection
from app.models.conversation import Session, SessionSummary, Transcript
from app.models.instance import Instance
from app.models.lead import Lead

README = """VantageMind data export
========================
This bundle is in open, non-proprietary formats (JSON, JSONL, CSV, and your
original uploaded files). Nothing here requires VantageMind software to open.

Layout:
  conversations/      one JSON per session (transcript + summary) + conversations.csv
  leads.json/.csv     captured lead records
  knowledge/          originals/ (your uploaded files as you sent them),
                      text/ (pasted, crawled and synced sources as ingested text),
                      manifest.json (one entry per source, with the file it maps to)
  instance.json       your Luciel config: provider + NON-secret config + status.
                      Secret material (OAuth tokens, API keys) is NEVER included.
  audit_log.jsonl     your audit log within the retention window.
"""


def lead_export_rows(leads: Sequence[Lead]) -> list[dict[str, object]]:
    """The wire shape for an exported lead record, shared by the full account bundle and
    the standalone lead export (§3.4.10a "exportable"), so the two cannot drift apart.

    Lead records are Customer Content the Admin owns (Legal §A7) and carry no secret
    material, so every column is exportable — the "config, not secrets" rule that strips
    connection credentials from ``instance.json`` has nothing to strip here.
    """
    return [
        {
            "leadId": str(le.id),
            "name": le.name,
            "contactIdentifier": le.contact_identifier,
            "email": le.email,
            "intent": le.intent,
            "state": le.state.value,
            "outcome": le.outcome,
            "lastActivityAt": le.last_activity_at.isoformat(),
        }
        for le in leads
    ]


def transcripts_stmt(admin_id: uuid.UUID, session_id: uuid.UUID) -> Select[tuple[Transcript]]:
    """A session's transcript rows, time-ordered — with the tenant predicate stated
    (round 7 WP-5, item 4). RLS scopes the read already; the explicit ``admin_id`` is
    the defence in depth every other tenant read in this module carries, so a session
    id from anywhere can never widen the bundle."""
    return (
        select(Transcript)
        .where(Transcript.admin_id == admin_id, Transcript.session_id == session_id)
        .order_by(Transcript.at.asc())
    )


def summary_stmt(admin_id: uuid.UUID, session_id: uuid.UUID) -> Select[tuple[SessionSummary]]:
    """A session's summary row, tenant predicate stated (see ``transcripts_stmt``)."""
    return select(SessionSummary).where(
        SessionSummary.admin_id == admin_id, SessionSummary.session_id == session_id
    )


async def build_export_zip(session: AsyncSession, admin_id: uuid.UUID) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", README)

        # conversations/
        sessions = (
            (await session.execute(select(Session).where(Session.admin_id == admin_id)))
            .scalars()
            .all()
        )
        conv_rows: list[dict[str, object]] = []
        for sess in sessions:
            transcripts = (
                (await session.execute(transcripts_stmt(admin_id, sess.id))).scalars().all()
            )
            messages = [
                {"role": t.role, "text": t.text, "at": t.at.isoformat()} for t in transcripts
            ]
            if not messages and sess.archive_object_key:
                # Commitment #38 read-through: a session past the 90-day hot boundary
                # keeps its transcript in S3 cold storage — the export must include it,
                # or "Download all my data" silently drops every older conversation.
                from app.lifecycle.transcript_archive import load_archived_transcripts

                # A cold-storage miss raises ArchivedTranscriptUnavailable and is
                # deliberately NOT caught: failing the export is honest, whereas
                # completing it drops those conversations from a bundle the customer
                # was told is all of their data — the exact silent loss this
                # read-through exists to prevent.
                archived = await load_archived_transcripts(
                    session, admin_id=admin_id, session_id=sess.id
                )
                messages = [
                    {"role": str(r["role"]), "text": str(r["text"]), "at": str(r["at"])}
                    for r in (archived or [])
                ]
            summary = (await session.execute(summary_stmt(admin_id, sess.id))).scalar_one_or_none()
            conv = {
                "sessionId": str(sess.id),
                "channel": sess.channel,
                "mode": sess.mode,
                "startedAt": sess.started_at.isoformat(),
                "summary": summary.summary if summary else sess.summary,
                "messages": messages,
            }
            z.writestr(f"conversations/{sess.id}.json", json.dumps(conv, indent=2))
            conv_rows.append(
                {
                    "sessionId": str(sess.id),
                    "channel": sess.channel,
                    "startedAt": sess.started_at.isoformat(),
                    "summary": conv["summary"] or "",
                }
            )
        z.writestr("conversations/conversations.csv", to_csv(conv_rows))

        # leads
        leads = (
            (await session.execute(select(Lead).where(Lead.admin_id == admin_id))).scalars().all()
        )
        lead_dicts = lead_export_rows(leads)
        z.writestr("leads.json", json.dumps(lead_dicts, indent=2))
        z.writestr("leads.csv", to_csv(lead_dicts))

        # knowledge/: the original uploaded files (originals store, F016) and, for
        # sources with no file behind them (pasted text, crawled pages, synced
        # documents), the ingested text reassembled from their chunks — so every
        # source the owner gave Luciel comes back in a form they can read.
        from app.knowledge.original_store import get_original_store, safe_filename
        from app.models.knowledge import KnowledgeChunk, KnowledgeSource

        sources = (
            (
                await session.execute(
                    select(KnowledgeSource)
                    .where(KnowledgeSource.admin_id == admin_id)
                    .order_by(KnowledgeSource.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        originals = get_original_store()
        manifest: list[dict[str, object]] = []
        used_names: set[str] = set()
        for src in sources:
            entry: dict[str, object] = {
                "name": src.name,
                "origin": src.origin,
                "sizeBytes": src.size_bytes,
                "ingestedAt": src.created_at.isoformat(),
                "deletedAt": src.soft_deleted_at.isoformat() if src.soft_deleted_at else None,
                "file": None,
                "note": None,
            }
            base = safe_filename(src.name) or str(src.id)
            unique = base if base not in used_names else f"{src.id.hex[:8]}-{base}"
            used_names.add(unique)
            stored = None
            if src.original_key:
                stored = await originals.get(key=src.original_key)
            if stored is not None:
                path = f"knowledge/originals/{unique}"
                z.writestr(path, stored.data)
                entry["file"] = path
            else:
                chunks = (
                    (
                        await session.execute(
                            select(KnowledgeChunk.text)
                            .where(
                                KnowledgeChunk.admin_id == admin_id,
                                KnowledgeChunk.source_id == src.id,
                            )
                            .order_by(KnowledgeChunk.ordinal.asc())
                        )
                    )
                    .scalars()
                    .all()
                )
                if chunks:
                    path = f"knowledge/text/{unique}.txt"
                    z.writestr(path, "\n\n".join(chunks))
                    entry["file"] = path
                    entry["note"] = (
                        "original file not on record; this is the ingested text"
                        if src.original_key
                        else "no uploaded file for this source; this is the ingested text"
                    )
                else:
                    entry["note"] = "no content on record for this source"
            manifest.append(entry)
        z.writestr("knowledge/manifest.json", json.dumps(manifest, indent=2))

        # instance.json — NON-secret config only
        inst = (
            await session.execute(select(Instance).where(Instance.admin_id == admin_id))
        ).scalar_one_or_none()
        conns = (
            (
                await session.execute(
                    select(InstanceConnection).where(InstanceConnection.admin_id == admin_id)
                )
            )
            .scalars()
            .all()
        )
        instance_json: dict[str, object] = {
            "instanceId": str(inst.id) if inst else None,
            "name": inst.name if inst else None,
            "websiteUrl": inst.website_url if inst else None,
            "channels": inst.channels if inst else [],
            "tools": inst.tools if inst else [],
            "escalation": inst.escalation if inst else {},
            "personality": inst.personality if inst else {},
            "connections": [
                {
                    "connectionType": c.connection_type,
                    "provider": c.provider,
                    "status": c.status,
                    "nonSecretConfig": c.non_secret_config,
                    # secret_ref is deliberately omitted — never export secret pointers' material.
                }
                for c in conns
            ],
        }
        z.writestr("instance.json", json.dumps(instance_json, indent=2))

        # audit_log.jsonl
        audit_rows = (
            (
                await session.execute(
                    select(AdminAuditLog)
                    .where(AdminAuditLog.admin_id == admin_id)
                    .order_by(AdminAuditLog.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        lines = "\n".join(
            json.dumps(
                {
                    "eventId": str(r.event_id),
                    "eventType": r.event_type,
                    "at": r.created_at.isoformat(),
                    "payload": r.event_payload,
                }
            )
            for r in audit_rows
        )
        z.writestr("audit_log.jsonl", lines)
        z.writestr("_exported_at.txt", datetime.now(UTC).isoformat())

    return buf.getvalue()


def to_csv(rows: list[dict[str, object]] | list[dict[str, str | None]]) -> str:
    if not rows:
        return ""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return out.getvalue()
