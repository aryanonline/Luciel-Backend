# app/billing — CLAUDE.md

## Purpose
Owns PAYG billing accounts, metering, Stripe interaction, dunning, and billing notices.

## Key files
`metering.py` — session counting, delta blocks, watermark, and reconciliation.
`stripe_gateway.py` — Stripe posture and meter-event gateway.
`webhooks.py` — signature/replay handling.
`dunning.py`, `notices.py`, and `notice_delivery.py` — account follow-up, in-dashboard
notices, and the once-only notice email sweep worker.

## Contracts this module owns
- Price is $0/first 50 conversations then $39 CAD per 100-conversation PAYG block; no tiers, seats, or model picker.
- Use Stripe Billing Meter event `luciel_payg_blocks`, idempotent delta reporting, persisted watermark, and replay-gated events.
- Flush residual blocks on card detach and reset after the renewal's `invoice.paid` (next line).
- A renewal is the renewal invoice's `invoice.paid` (`billing_reason=subscription_cycle`); Stripe has no `customer.subscription.renewed`. The counter rolls at the cycle end by time (`budget.current_period`, F030), before Stripe even attempts that invoice. Its `invoice.paid` moves the stored anchor to the invoice's `period_end`, takes the new period's end from Stripe, flushes the settled period's residual, resets the watermarks unless a counted turn already rolled them, and clears dunning. Any other paid invoice (one-off, update, threshold, creation, or the final invoice of a subscription the account no longer holds) only settles: dunning clears and dormant tools return, the period and the watermarks stay. An invoice without a `billing_reason` is still read as the renewal.
- Period ends keep Stripe's anchor day (`stripe_gateway.next_period_end`: Jan 31 → Feb 28 → Mar 31); never chain `add_months` on a clamped date.
- Dispatch Stripe events only through `webhooks._HANDLERS` (keys: `HANDLED_EVENT_TYPES`), and key only event types Stripe sends: `tests/unit/test_stripe_event_types.py` checks the keys against stripe-python's list. The Dashboard endpoint subscribes to exactly those types; a change to them is a change there and in `docs/operations/teardown-and-restore.md`.

## Testing pointers
- `tests/integration/test_metering.py` and `test_usage_reporting.py`.
- `tests/contract/test_billing*.py`, `test_payg_webhook.py`, and unit Stripe/metering tests.

## Gotchas from the 2026-08-06 truth delta
- A billed conversation is a session counted once per Stripe billing period; use calendar-month fallback before card attachment.
- Legacy Stripe usage-records API is banned by regression coverage; prod remains mock until owner-gated live cutover.

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
