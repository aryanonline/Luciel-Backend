# app/runtime — CLAUDE.md

## Purpose
Runs the tenant-scoped AI loop: sessions, safety, retrieval, grounding, handoff, provider selection, channels, and speech.

## Key files
`orchestrator.py` — main runtime loop.
`grounding.py` and `knowledge_retrieval.py` — evidence and score gate.
`sessions.py`, `memory.py`, and `budget.py` — continuity/counting controls (participant-key session resolution: conversation = ONE billed session per §3.4.8).
`session_close.py` — deterministic session END (inactivity sweep + explicit end signals) firing §3.4.7 summarization.
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

## Team availability (round 6 WP-E, 2026-09-07)
- Luciel has NO working hours; nothing may make it stop answering. `team_availability.py`
  describes when the HUMAN team is reachable (owner's IANA zone, weekly windows incl.
  overnight, closure dates) and is used for two things only: `_handoff_ack` says WHEN a
  person follows up ("tomorrow at 9:00 am (EDT)") instead of "shortly", and
  `escalation_dispatch.resolve_routing` pages the after-hours contact. Unset/off →
  byte-identical behaviour to before. Never promise a real-time human reply.

## Operating context (round 6 WP-F, 2026-09-07)
- `Orchestrator.operating_context` (built by `app.employee.context.build_operating_context`
  at every entry point) composes a platform-resolved stanza under the invariants: business
  short name, "available around the clock" (Luciel has no hours), the channels live right
  now, how the team is notified (never a real-time human), whether a PERSON is reachable
  (WP-E), and "an unlisted capability does not exist". None keeps the prompt byte-identical.
- `disclosure_business_name` / `_disclosure_business_name` now serve `instances.
  business_short_name` (PUT /admin/luciel/business-name) and still never the Luciel name.

## Channel switching (round 6 WP-H, F060, 2026-09-08)
- `channel_switch.py` + `Orchestrator._switch_or_ask` / `_settle_pending_outbound`: an
  "email me / text me" request sends the answer to a CONFIRMED identifier (transport-
  carried, confirmed at capture; or typed in the same message) as the tenant, with a
  same-channel note; otherwise it asks where and parks the answer on
  `sessions.pending_outbound` for the next turn (yes/an address sends; no drops; anything
  else drops and the loop runs). Never to an unconfirmed address; audited as
  `channel_switch_delivery`. The arbiter's own long-SMS switch still only OFFERS.

## Judgment quality (round 6 WP-G, 2026-09-08)
- F124: live providers parse a trailing `<confidence>X</confidence>` line the prompt asks
  for (`providers.parse_confidence_tag`; absent → 1.0 as before) so the grounding-OR-
  confidence gate finally has a real second input. `EscalationDecision.reasons` (owner
  phrases: "named a budget", "the model was 30% sure") persist on `escalation_events.reasons`.
  The complaint lexicon is ~80 terms and negation-aware; "refund"/"cancel"/"review" are
  requests, not complaints.
- F102: `personality.highValueSignals` (≤ 20 phrases ≤ 60 chars) is the ONE admin input to
  the fixed high_value_lead signal — `personality.custom_rules_hit` over the recent turns
  adds 0.3; it never adds a signal.

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
- R6-6 (2026-09-13 dev walk): "Could you email me your Saturday opening hours?" retrieved the
  clinic chunk and still handed off — the token-overlap proxy scored the draft 4/11 = 0.36
  because the delivery offer the operating-context stanza asks for ("send a copy … best
  address") and the customer's echoed words ("opening hours") have no chunk support.
  `score_grounding` now drops delivery/courtesy words from the answer side (like the
  pleasantry stopwords) and, given `question=`, drops unsupported words the customer used
  (a restatement, not a claim). A fabricated fact behind the same offer still scores ~0.

