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
- R6-2 (2026-09-13 dev walk): "What are your opening hours?" handed off with 0% grounding
  although the clinic-hours chunk was ready. Cause: the live relevance floor. Measured with
  text-embedding-3-small, relevant questions land at cosine distance 0.52–0.66 against a
  real multi-topic chunk (0.619 for that question), unrelated ones at 0.71–0.91; the ceiling
  of 0.6 dropped the relevant hits. Now `knowledge_relevance_max_distance` = 0.8 plus
  `knowledge_relevance_band` = 0.15 relative to the best hit (`apply_relevance_floor`).
- F121b (same walk, safety net): `service.verify_chunk_embeddings` (knowledge_sync sweep, 20
  chunks per tenant per pass, `embedding_verified_at`) re-embeds a vector that disagrees with
  a fresh embedding of its own text — the case migration 0058's backfill could create (a
  pre-cutover hash vector stamped with the live name). Migration 0065 also makes every tenant
  with chunks a knowledge_sync candidate. NEVER run the knowledge sweep from a mock-mode
  container against live data: the Verify task is mock-mode and its hash embedder rewrote a
  live tenant's chunks on 2026-09-13 (the live sweep restored them); both passes now refuse
  to touch live-labelled rows when the mock embedder is active.

