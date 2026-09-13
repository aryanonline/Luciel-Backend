# app/knowledge — CLAUDE.md

## Purpose
Ingests and syncs knowledge, stores chunks/embeddings/graph data, applies scope, and supplies retrieval inputs.

## Key files
`service.py` and `sync.py` — ingestion and provider sync.
`scope.py` — persisted provider scope selection.
`chunking.py`, `embeddings.py`, `graph.py`, and `graph_extract.py` — retrieval structures.
`parsers.py`, `providers.py`, and `intent.py` — source intake and query support.

## Contracts this module owns
- Apply Drive folder and Notion page/database scope stored on the connection; no selection means whole connected account.
- Retire out-of-scope material on the next sync.
- Provide retrieval evidence accurately; do not cause the UI to claim no source unless retrieval explicitly found none.

## Testing pointers
- `tests/integration/test_knowledge*.py`, graph, hybrid-runtime, and provider sync tests.
- `tests/contract/test_knowledge*.py` and parser/embedding unit tests.

## Gotchas from the 2026-08-06 truth delta
- Grounding scores belong to runtime answers but source/evidence inputs must preserve explicit availability states.
- Knowledge-backed proof is distinct from a generic model answer; no retrieved KB should lead to honest refusal/escalation.

## Change checklist
- Read the root `CLAUDE.md` and trace callers and dependents before editing.
- Add or update a test that reproduces the real condition being changed.
- Keep tenant, live/mock, and externally visible status behavior explicit.
- If persistence, routes, or deployment posture changes, update the related migration, registry, or infrastructure test deliberately.
- Do not replace an error/unknown state with fabricated success.

## Review prompt
Before merge, ask whether this change could hide an unconfigured, failed,
or tenant-crossing condition behind a successful-looking result. If so,
make that state explicit and test it.

## Round 6 WP-G (2026-09-08)
- F122: `graph_extract.LlmGraphExtractor` (LUCIEL_GRAPH_EXTRACTOR=llm, live mode only) infers
  typed nodes/edges from prose via the fast model under strict, capped JSON and merges the
  deterministic line grammar in; any failure falls back to deterministic. Ingest calls
  `extract_async` — the sync Protocol stays deterministic.
- F044: `service.retrieve` runs `pgvector.search_settings_sql` (SET LOCAL hnsw.ef_search =
  max(40, 4k); iterative_scan = relaxed_order on 0.8+) before the ANN query so a small tenant
  beside a large one still gets k. Docker/CI pin `pgvector/pgvector:0.8.0-pg17`; `/healthz`
  reports `pgvector_too_old:<ver>` below the pin.
- F121b (2026-09-13 dev walk): the walk tenant's clinic-hours chunk carried a pre-cutover hash
  vector stamped `text-embedding-3-small` by migration 0058's backfill — cosine distance 0.978
  from every query, dropped by the live relevance floor on every turn, invisible to the
  label-based re-embed. `service.verify_chunk_embeddings` (called by the knowledge_sync
  sweep, 20 chunks per tenant per pass, `embedding_verified_at`) re-embeds any vector that
  disagrees with a fresh embedding of its own text; migration 0065 also makes every tenant
  with chunks a knowledge_sync candidate, not only tenants with a connected sync source.

