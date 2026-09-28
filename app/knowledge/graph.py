"""Graph retrieval over the tenant's entities/edges (Architecture §3.2.1 — Path Locked).

PostgreSQL recursive CTEs ONLY — no Neo4j/Memgraph/new vendor (§3.2.1, Locked Decision
§7 #4). Every query runs UNDER the per-request ``app.admin_id`` GUC inside a tenant_session,
so RLS scopes the walk to the calling tenant (the CTE itself also filters by admin_id +
instance_id as defense-in-depth — RLS is the backstop, §3.7.2b). Depth is capped
(MAX_HOPS) so a cyclic graph cannot run away (§3.2.1 ~1M-edge performance envelope).

Three retrieval shapes the §3.2.1 retrieval router needs:
  * relationship traversal — walk outward from seed entities up to N hops,
  * multi-attribute intersection / membership — find entities by canonical name,
and a convenience that turns a structured query string into traversal seeds + results.
"""

from __future__ import annotations

import uuid

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.knowledge.graph_extract import canonicalize
from app.knowledge.service import paused_serving_cutoff

# A source's graph is not walked once the source has stopped serving: tombstoned for
# its undo window (§3.2.2) or paused by a revoked credential past PAUSED_SERVING_DAYS
# (owner decision D10, ``:paused_cutoff``). The same rule service.retrieve applies to
# the chunks. The NOT EXISTS clause is spelled out in both queries below.

# Hop cap for the recursive CTE (§3.2.1). Bounds traversal cost and prevents runaway
# cycles; 3 hops covers the relationship-traversal patterns in the §3.2.1 examples
# (e.g. Department HAS Role REQUIRES Skill) without unbounded fan-out.
MAX_HOPS = 3


async def find_entities_by_names(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    names: list[str],
) -> list[uuid.UUID]:
    """Membership lookup: resolve canonical names to entity ids (§3.2.1).

    Tenant-scoped (admin_id + instance_id); RLS is the backstop. Returns the entity ids
    whose canonical name matches any of the (canonicalized) input names."""
    canon = sorted({canonicalize(n) for n in names if n.strip()})
    if not canon:
        return []
    # A tombstoned source's graph stays for its undo window but must not be
    # traversed: the runtime stops answering from deleted knowledge at once (§3.2.2).
    # A source paused past PAUSED_SERVING_DAYS is not traversed either (D10).
    stmt = text(
        "SELECT e.id FROM knowledge_entities e "
        "WHERE e.admin_id = :admin_id AND e.instance_id = :instance_id "
        "AND e.canonical = ANY(:canon) "
        "AND NOT EXISTS (SELECT 1 FROM knowledge_sources ks "
        "  WHERE ks.id = e.source_id "
        "    AND (ks.soft_deleted_at IS NOT NULL OR ks.paused_since <= :paused_cutoff))"
    ).bindparams(
        bindparam("admin_id", type_=PGUUID(as_uuid=True)),
        bindparam("instance_id", type_=PGUUID(as_uuid=True)),
        bindparam("canon", expanding=False),
        bindparam("paused_cutoff", type_=DateTime(timezone=True)),
    )
    rows = await session.execute(
        stmt,
        {
            "admin_id": admin_id,
            "instance_id": instance_id,
            "canon": canon,
            "paused_cutoff": paused_serving_cutoff(),
        },
    )
    return [r[0] for r in rows]


async def traverse(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    seed_entity_ids: list[uuid.UUID],
    max_hops: int = MAX_HOPS,
) -> list[str]:
    """Recursive-CTE forward traversal from seeds, returning rendered fact strings.

    Walks ``knowledge_edges`` src→dst up to ``max_hops`` hops. Each reached edge is
    rendered as a deterministic "Src RELATIONSHIP Dst" fact (with the dst entity_type),
    ordered by (depth, src name, relationship, dst name) so fusion ordering is stable.
    A ``visited`` array on each path prevents revisiting a node within that path → cycle
    safety on top of the depth cap (§3.2.1)."""
    if not seed_entity_ids:
        return []
    hops = max(1, min(max_hops, MAX_HOPS))
    # Recursive CTE (§3.2.1 Path-Locked: this IS the graph engine — Postgres, not Neo4j).
    # depth starts at 1 on the seed rows; each step appends the dst to `visited` and stops
    # when the dst is already on the path or the depth cap is reached. The seed set is
    # matched on BOTH endpoints: a relationship-traversal query like "which roles require
    # Python" names the *target* (Python), so an edge incident to the seed in EITHER
    # direction is a relevant hop-1 fact; subsequent hops then walk forward src→dst.
    sql = text(
        """
        WITH RECURSIVE live_edges AS (
            -- Edges of tombstoned sources exist for the undo window but are never
            -- walked (§3.2.2: deleted knowledge stops answering immediately); nor
            -- are the edges of a source paused past PAUSED_SERVING_DAYS (D10).
            SELECT e.* FROM knowledge_edges e
            WHERE e.admin_id = :admin_id AND e.instance_id = :instance_id
              AND NOT EXISTS (SELECT 1 FROM knowledge_sources ks
                              WHERE ks.id = e.source_id
                                AND (ks.soft_deleted_at IS NOT NULL
                                     OR ks.paused_since <= :paused_cutoff))
        ), walk AS (
            SELECT e.src_entity_id, e.dst_entity_id, e.relationship_type,
                   1 AS depth, ARRAY[e.src_entity_id, e.dst_entity_id] AS visited
            FROM live_edges e
            WHERE (e.src_entity_id = ANY(:seeds) OR e.dst_entity_id = ANY(:seeds))
            UNION ALL
            SELECT e.src_entity_id, e.dst_entity_id, e.relationship_type,
                   w.depth + 1, w.visited || e.dst_entity_id
            FROM live_edges e
            JOIN walk w ON e.src_entity_id = w.dst_entity_id
            WHERE w.depth < :max_hops
              AND NOT (e.dst_entity_id = ANY(w.visited))
        )
        SELECT DISTINCT w.depth,
               s.name AS src_name, w.relationship_type,
               d.name AS dst_name, d.entity_type AS dst_type
        FROM walk w
        JOIN knowledge_entities s ON s.id = w.src_entity_id
        JOIN knowledge_entities d ON d.id = w.dst_entity_id
        WHERE s.admin_id = :admin_id AND d.admin_id = :admin_id
        ORDER BY w.depth, src_name, w.relationship_type, dst_name
        """
    ).bindparams(
        bindparam("admin_id", type_=PGUUID(as_uuid=True)),
        bindparam("instance_id", type_=PGUUID(as_uuid=True)),
        bindparam("seeds", expanding=False),
        bindparam("paused_cutoff", type_=DateTime(timezone=True)),
    )
    rows = await session.execute(
        sql,
        {
            "admin_id": admin_id,
            "instance_id": instance_id,
            "seeds": list(seed_entity_ids),
            "max_hops": hops,
            "paused_cutoff": paused_serving_cutoff(),
        },
    )
    facts: list[str] = []
    for row in rows:
        rel = row.relationship_type.replace("_", " ").lower()
        facts.append(f"{row.src_name} {rel} {row.dst_name} ({row.dst_type}).")
    return facts


async def graph_facts_for_terms(
    session: AsyncSession,
    *,
    admin_id: uuid.UUID,
    instance_id: uuid.UUID,
    terms: list[str],
    max_hops: int = MAX_HOPS,
) -> list[str]:
    """Resolve query terms → seed entities → traversed facts (§3.2.1 relationship traversal).

    Tenant-scoped; RLS backstop. Empty when no term matches an ingested entity — the graph
    NEVER fabricates a node that the admin did not ingest (§3.2 correctness boundary)."""
    seeds = await find_entities_by_names(
        session, admin_id=admin_id, instance_id=instance_id, names=terms
    )
    return await traverse(
        session,
        admin_id=admin_id,
        instance_id=instance_id,
        seed_entity_ids=seeds,
        max_hops=max_hops,
    )
