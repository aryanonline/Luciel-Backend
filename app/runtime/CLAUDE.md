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
- Phase-2 security review (round 7): a lead holding SEVERAL confirmed identifiers of a
  kind (a second phone, or one joined by a typed claim — WP-3c item 1 stays) sends
  without asking only to the one THIS conversation's transport carried
  (`confirmed_identifier(sess=…)`); otherwise it asks and offers the record's candidate
  masked (`mask_identifier`). `_absorb_transport` fills phone/email columns only while
  the lead has no other confirmed identifier of that kind. Who a conversation is WITH
  (takeover reply, voice→SMS bridge, owner page contact line) is
  `conversation_sender.py`: `sessions.transport_sender` (0074), else the participant-key
  match, never another identifier of the same kind.

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
- R6-8 (2026-09-13 dev walk, after R6-6): the same email-me turn cleared the floor and was
  still replaced - by the unfounded-claim guard (C4/F110). Told that the platform delivers
  a copy, the model wrote "I'll have a copy sent to your email once you confirm the
  address", and `_FIRST_PERSON_SUCCESS_CLAIM` read "I ... sent" as a completed action. A
  promise is not a claim: the regex gap now refuses to cross a future/conditional modal
  (a second subject after the modal - "I can confirm it has been sent" - is still
  caught), and the operating-context stanza says "never say it has already been sent".


## Judgment as an employee (round 7 WP-2, D2 2026-09-14; built 2026-09-20)
- Gate 2 judges the ANSWER and the OPPORTUNITY on every turn: `EscalationDecision.
  answer_ungrounded` is the floor/confidence verdict, independent of which signal names
  the row; the orchestrator replaces a below-floor draft whichever signal won (item 1).
- `hand_to_team(reason)` is always-on cognition (`app/tools/schemas.HAND_TO_TEAM_DEF`,
  never in the catalog): the loop dispatches it itself in `_dispatch_tools`, before the
  resolver/broker, and `_hand_to_team` records `luciel_requested_handoff` through
  `_record_escalation` (row + audit + owner page carrying the reason). The session stays
  in ai mode. `_HANDOFF_CLAIM` turns a reply that claims/promises a hand-off on a turn
  with no recorded escalation into that same real escalation; the words stay (item 2).
- D2 takeover scope: `_hand_off_with_ack` (→ `handoff.take_over`) is reached ONLY by
  explicit_human_request and the threats_violence / self_harm moderation categories.
  abuse_harassment / illegal_solicitation → `content_flagged` row at intake, loop runs;
  strong_negative_sentiment → row at intake, question answered, `_TEAM_TOLD_NOTE` once;
  the ask bound → cannot_answer with the missing field names (appended to the session's
  existing row when one exists), the hand-off line, ai mode kept (item 3).
- The per-tenant LLM cap is paid once per turn (`LLMRouter.turn()` around `_reason`'s
  loop); `LLMCapExceeded` is caught beside `ProviderError` and answers through the
  llm_unavailable path with its own reason and the `LLMCapExceeded` metric — never an
  exception to the transport (item 4). Ingest-time graph extraction passes `admin_id`
  per call so it is capped under the ingesting tenant.
- Owner notifications start after the turn's commit, in a background task (item 5; see
  app/notifications/CLAUDE.md). `ProviderError.retryable` (429/529) buys the FALLBACK
  leg one jittered retry; the primary still gets none (item 6).
- Known gap (not changed here): gate 2 records + pages `cannot_answer` for a bare
  clarifying question on the FIRST stall ("what's your email?") even though F100 lets
  the question go out; four established loop tests lean on that echo shape.

## Judgment as an employee — the remainder (round 7 WP-2, built 2026-09-27)
- One hand-off ack, one contact ask, per session (item 1): `_handoff_reply` sends the
  full `_handoff_ack` line only while `handoff_ack_sent(history)` is False, then one of
  `SHORT_HANDOFF_ACKS` (cycled by `short_handoff_ack`); `_with_contact_ask` appends
  `CONTACT_ASK_LINE` only when `contact_ask_due` says so — first escalating turn, then
  after `CONTACT_ASK_RESPITE_TURNS` Luciel turns with still nothing on file, at most
  `ASK_ATTEMPT_BOUND` times. All read from the session transcript (no new column), so
  the transcript order matters: entry points pass `received_at`. A DISTINCT uncovered
  question after the row exists (`escalation.is_new_question` over
  `unanswered_questions(history)`) appends `also asked: "…"` to the row's reasons and
  re-notifies the same row (`_renotify_new_question`, metric `EscalationRenotified`); a
  repeat pages nobody.
- Taking no for an answer (item 2, §3.3.7 rule 4 / stanza rule T5): `escalation.
  is_decline_turn` (deterministic, negation-aware, a supplied value always wins) is
  consulted ONLY while `sessions.input_ask_attempts > 0`. On a decline the counter
  resets and the ask stops in code: `pending_gather` reads the latest turn's durable
  tool traces — a `missing_input` there (or an unresolved `ambiguous`) means the action
  is blocked, so `declined_input_reply` goes out with a REAL cannot_answer row naming
  the field (`record_or_extend_escalation`: new row, or the session's existing row
  extended and re-paged) and no model call; a prose stall gets
  `DECLINED_INPUT_CONTINUE`; a decline carrying a question is answered by the loop,
  opened by `DECLINED_INPUT_LINE`. That line in a Luciel turn switches
  `contact_ask_due` off for the rest of the session (the contact ask names the very
  identifiers a gather asks for). `session_close._escalate_unfinished_gather`: a session
  sealed with `input_ask_attempts > 0` writes the same cannot_answer row so the owner
  learns a booking stalled. Edge kept: explicit_human_request at intake still runs
  before the decline check.
- Voice retrieval (item 5): `voice_pipeline.handle_turn` binds `retriever_bundle=
  make_retriever_bundle(...)` beside `retriever`/`chunk_attributor`, exactly as the
  widget and `runtime_entry` do, so a call gets the relevance floor and the
  EmbeddingError degraded marker (`tests/integration/test_round7_voice_retrieval.py`).
- Required inputs on file (item 10): `channel_switch.confirmed_inputs_on_file` gathers
  the lead's CONFIRMED email/phone (the sending bar: transport-carried or
  `confirm_identifier`'d) plus the transport identifier itself, keyed by tool input
  name; `run_turn` computes it once per tool-bearing turn, names the FIELDS (never
  values) in the stanza's `On file for this customer` line, and threads it
  `_reason` → `_dispatch_tools` → `ToolBroker.dispatch(gathered=)`, where a required
  input the model left out is filled before the missing_input check. An unconfirmed
  address fills nothing; T2 still has the model confirm before a write.
- The business's clock (item 11, 2026-10-04): the prompt carried no date and no zone.
  `OperatingContext.timezone` / `local_today` (`app/employee/context.py`, from
  `instances.timezone`) add one operating-context line — the zone and today's date
  there, "a time a customer gives is in that zone", tell the customer local time never
  UTC; with no zone on file the line gives UTC's date and says appointment times are
  UTC. A context constructed without the two fields adds no line. `_dispatch_tools`
  passes the same zone to `ToolBroker.dispatch(timezone=)`, so the model and the
  appointment tools always agree on the clock (see app/tools/CLAUDE.md).

## Judgment hardening (round 7 review:judgment, built 2026-10-11..18)
- Retrieval skip: `retrieval_skippable` / `routing_hints` skip the knowledge search only
  when the WHOLE message is a pleasantry or a contact detail
  (`escalation.substantive_remainder` empty, no question). When unsure, retrieve.
- Take-overs: every entry into human_controlled pages (`record_takeover_escalation`: the
  session's explicit_human_request row is written, or extended — a safety take-over's
  `moderation:<category>` first — and re-paged). A take-over always gets the full
  take-over wording (`_handoff_reply(take_over=True)`); if the row + page could not be
  written nothing claims the team was told and the session stays in ai mode
  (`SELF_HARM_CRISIS_LINES` / `TAKEOVER_UNRECORDED_REPLY`). A take-over or moderated
  message while an answer is parked on `pending_outbound` drops the parked answer and
  reaches the gates — it is never settled as "no".
- `sessions.input_ask_attempts` means consecutive stalls ON THE SAME detail: cleared on
  every served turn, re-counted only when this turn stalls on the same asks as the
  previous turn's durable traces (`PendingGather.asks`). The decline classifier runs only
  while it is > 0, and a named choice (weekday, time, ordinal) is never a decline.
- The lead record fills a tool input / receives a channel-switch send / is offered back
  only when the transport proves the identity or THIS session supplied the value
  (`channel_switch.record_readable`). A mentioned address ("my accountant at …") is the
  destination of that one send, never confirmed as the lead's own (`is_own_identifier`).
- One page per turn for one piece of news: hand_to_team recorded this turn + a
  below-floor draft → the answer reasons ride on the hand-off row (no cannot_answer row,
  no re-page). Every canned "passed to the team" line is backed by a page: the ask bound
  and the unfounded-claim replacement use `record_or_extend_escalation`.
- An empty draft is never sent as silence: a tool does not ground words never written
  (the honest hand-off goes out), except a confirmed booking/move/cancel, read back from
  its payload ("Here are the details: …").
