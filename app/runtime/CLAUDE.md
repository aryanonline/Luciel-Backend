# app/runtime — CLAUDE.md

## Purpose
Runs the tenant-scoped AI loop: sessions, safety, retrieval, grounding, handoff, provider selection, channels, and speech.

## Key files
`orchestrator.py` — main runtime loop.
`grounding.py` and `knowledge_retrieval.py` — evidence and score gate.
`sessions.py`, `memory.py`, and `budget.py` — continuity/counting controls.
`integration_status.py` and `provider_factory.py` — mode/binding truthfulness.

## Contracts this module owns
- Persist a real grounding score and enforce floor 0.50 with low-grounding OR low-confidence escalation/refusal.
- Honor the shared pause gate across all channel entries.
- Report integration mode and degraded live bindings via `/healthz`; never select a fake send/provider silently in live mode.

## Testing pointers
- `tests/integration/test_orchestrator_loop.py`, `test_hybrid_runtime_binding.py`, and config snapshot tests.
- `tests/unit/test_grounding_floor.py`, budget, handoff, provider, memory, and integration-mode tests.

## Gotchas from the 2026-08-06 truth delta
- AI disclosure must be present on every channel, and a greeting does not repeat the Luciel name.
- No knowledge evidence must not be recast as an attributed answer; offer honest refusal/escalation.

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
