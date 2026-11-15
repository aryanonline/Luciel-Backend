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


## Round 7 M10 — a sync in flight at a pause or delete
- `sync.sync_connection` reads the Luciel's serving state (a column select — never the
  identity-mapped entity) before `provider.snapshot()` and again after it returns, BEFORE
  any write. Serving at the start and not serving now (paused or deleted meanwhile) ends
  the run with NO writes — no chunks, tombstones, status flags or audit row — and
  `SyncResult.halted = "instance_not_serving"`. Nothing is marked failed; the first pass
  after resume/restore reconciles. A run STARTED on an already-paused Luciel (the owner's
  manual Re-sync) proceeds as before; restore's grace-window de-duplication remains the
  safety net for a manual run on a deleted one.
  Tests: `tests/integration/test_knowledge_sync_halts_when_not_serving.py`.

## Round 7 walk run 8 (2026-10-25) — the manual sync is a JOB
- Gotcha: `POST /admin/knowledge/sync-connections/{id}/sync` and, for a connection-backed
  source, `POST /admin/knowledge/sources/{id}/resync` no longer run the whole sync inside the
  request (an 8-page crawl with one LLM graph call per page outlived the ALB's 60 s idle
  timeout; its CORS-less 504 read as "Network request failed."). `sync_job.py`: the route
  waits `knowledge_sync_inline_wait_seconds` (20 s). Finished → **200** (`SyncResultOut`; the
  refreshed source on /resync) or the same 409s (shrink, unreachable / dead credential; other
  failures 503 with the owner message, a real failure). Still running → **202** `{connectionId,
  state: "syncing", startedAt, finishedAt: null, result: null, error: null, shrink: null,
  trigger: "manual"}`. Every job ends in a terminal state on `instance_connections.sync_job`
  (migration 0077), served as `syncJob` on BOTH GET /sync-connections and GET /connections
  (states syncing | synced | failed | shrink_pending).
- LIVENESS IS A DATABASE LOCK, NOT AN AGE. `sync.sync_connection` takes
  `pg_try_advisory_xact_lock(SYNC_LOCK_K1, sync_lock_k2(connection))` as its first statement
  and raises `SyncAlreadyRunning` on refusal, so no two reconciles of one connection overlap,
  whichever of the callers started them (`knowledge_sources` has no unique (connection_id,
  external_id): overlap duplicates sources). A `syncing` record is live when the connection is
  in `_RUNNING`, its key is in `pg_locks` (`sync.live_sync_lock_keys`: mind signed int4 vs
  unsigned oid), or it is younger than `knowledge_sync_job_start_grace_seconds` (60 s, the gap
  between the stamp's commit and the job taking the lock). Otherwise it READS as failed
  ("interrupted"); the row is not rewritten. A job past any age reads syncing and a second start
  is a 409; an orphan past the grace reads interrupted and admits a start at once. The start, the
  sweep's skip and both lists use this one rule; a list reads `pg_locks` at most once, only when
  a row says syncing (`live_keys_for`). A lock first taken inside a SAVEPOINT is released when
  it rolls back: a caller that must keep it past a failed sync takes it first (the job, the sweep).
- THE TERMINAL STAMP COMMITS WITH THE OUTCOME. Token refresh is its own short transaction (the
  reconcile holds no row lock on the connection; a disconnect answers mid-sync). Then: outer
  transaction, lock, SAVEPOINT around `sync_connection`, evidence writers, the stamp in its own
  savepoint, commit: data, stamp and lock release become visible together. Evidence writers
  (`flag_unreachable_site`, `flag_dead_credential`) run AFTER the savepoint rollback released
  the sync's row locks and BEFORE the stamp, so they cannot deadlock against the outer
  transaction; the shrink refusal writes its evidence before any write of the sync. The stamp is
  conditional on `sync_job->>'started_at'`: a cleared or superseded record is never overwritten.
  A failed stamp rolls back its savepoint, the data commits, and it retries in short
  transactions with backoff for up to 60 s (`knowledge_sync_job_stamp_lost` at ERROR after).
- REMOVING A SOURCE STOPS ITS SYNC. `sync_connection` re-reads the status (a column select) after
  the snapshot, before each item's ingest and before returning; not_connected / revoked raises
  `SyncStoppedByOwner`, the savepoint unwinds, nothing is kept, outcome `stopped`, inline 409
  "This source was removed while it was syncing — nothing from that sync was kept." A sync
  already past its last look also unwinds if its stamp finds the record gone on a removed row.
  Every path to not_connected / revoked clears `sync_job` (`connections.service.
  clear_sync_job_on_removal`); every path back to connected clears a terminal record unless a job
  is live (`clear_job_on_reconnect`); `effective_job` serves None on removed rows; a start on
  not_connected is a 409 "This source was removed — connect it again to sync it." Known limit: a
  disconnect AND a reconnect between two of the job's looks lets the running job finish.
- The sweep stamps the same record (trigger `scheduled`) and commits EACH CONNECTION ON ITS OWN
  (`sweep._sync_one_connection`, through the job's `reconcile_in_transaction`): fresh status and
  job read (not `connected`: skip; the pass's list is stale by then); skip if live; lock OUTSIDE
  the savepoint (refused: skip, no stamp); a conditional `syncing` stamp in its own transaction,
  refused on a removed row (no row or a failed write: skip); refresh in its own transaction;
  SAVEPOINT + sync; evidence; stamp; commit. The first design held every connection's data until
  the tenant's whole pass committed, so a crawl removed while the pass worked on the next
  connection (or on the maintenance passes) got its pages minutes after "no longer crawled". The
  tenant's own transaction now holds only the sweep's lock and runs the maintenance passes, each
  under a SAVEPOINT: they swallow their failures, and a database error there would otherwise turn
  the tenant's COMMIT into a silent ROLLBACK. If a connection's transaction is lost at its commit,
  the row is stamped `failed` afterwards (nothing kept, and that is what it says).
- A removal is reported as a removal, whatever the sync raised: a disconnect deletes the secret,
  so a snapshot after one raises ProviderUnavailable before the first status look.
  `reconcile_in_transaction` checks `connection_removed` before settling a failure, and
  `flag_dead_credential` re-reads the row locked: no status flip, no pause, no D10 clock on a
  removed row. A dead-credential mark is its own short transaction on both paths, so its row lock
  never outlives the flip. `ensure_fresh_access_token` re-reads the row locked after its HTTP call
  and discards the rotation on a removed row (no live token behind `secret_deleted`).
- A start the lock refuses (the record was NULL, so the start was admitted) answers 409 and CLEARS
  its own record: the holder's stamp is keyed on its own `started_at` and could never replace a
  failure written here. A run halted by a pause (`result.halted = "instance_not_serving"`) has
  outcome `halted`, not `synced`, and carries `halted` on `SyncResultOut` (omitted when None) and on
  `syncJob.result` (null when it ran): its empty lists mean "not compared".
- Shutdown (`app/main.py`): ECS `stopTimeout` 60 s = uvicorn `--timeout-graceful-shutdown` 22 +
  `drain_sync_jobs` 5 + 6, FIRST + poller 10 + background 15. A SIGKILL still leaves no orphan
  lock: the row reads interrupted past the grace.
- Graph extractor: the graph call passes `max_output_tokens=GRAPH_MAX_OUTPUT_TOKENS`
  (4096) through `LLMRouter.complete` (None = unchanged for every other caller). An unusable
  answer, `truncated` (stop_reason max_tokens / finish_reason length) or simply unparseable, keeps
  the grammar-only graph AND bumps the revision: re-extracting would cost an LLM call and a
  re-embed per pass and report the page "updated" with nothing changed. WARNING
  `graph_extract.llm_truncated` / `llm_unparseable` (extra `reason`). `llm_skipped` is only the
  budget and cap skips.
- Tests: `tests/contract/test_knowledge_sync_job.py`, `tests/integration/test_knowledge_sync_lock.py`,
  `tests/integration/test_knowledge_sync_sweep.py` (sweep stamps / skip / commit order),
  `tests/integration/test_migration_0077_connection_sync_job.py`,
  `tests/unit/test_llm_output_ceiling.py`, `tests/unit/test_graph_extract_llm.py`,
  `tests/unit/test_proxy_headers_launch.py` (the shutdown budget).

## Unreadable files and pypdf 7 (2026-11-14)
- Drive: a file the parsers refuse (`ParseError`: no text layer, an open password, a PDF pypdf
  refuses) goes into `failed_ids` from `GoogleDriveProvider.snapshot`, so it is kept and flagged
  (F117) while the other files sync. It used to escape and fail the whole connection's sync, on
  every pass: each pass downloads and parses every in-scope file, and only the ingest is gated by
  revision. Test: `tests/unit/test_knowledge_provider_reads.py`.
- `parsers._read_abbreviated_filter_names` replaces pypdf's private
  `pypdf.filters._deprecate_inline_image_filters` with a no-op. pypdf 7.0 refuses the abbreviated
  filter names (/Fl, /A85, /AHx, ...) on ordinary streams with DeprecationError
  (py-pdf/pypdf#4306), rejecting the whole document over one font, though pypdf 6 read them.
  Remove the shim only once a pypdf release reads those names itself (py-pdf/pypdf#4371, open);
  `tests/unit/test_knowledge_parsers.py` fails while pypdf refuses them without the shim.
- Accepted, not worked around: pypdf 7.0 refuses an object stream whose offsets do not strictly
  increase (security fix py-pdf/pypdf#4364), even when every pair is correct but listed out of
  order, which pypdf 6 read. Such a file is `unparseable_pdf`; a test pins that.
