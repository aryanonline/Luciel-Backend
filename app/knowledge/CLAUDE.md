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
