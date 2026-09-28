"""Architecture §5.2 names the audit event enumeration; ``EVENT_TYPES`` is the canonical
machine-checkable list the doc's table says it mirrors (round 7 WP-5, item 6).

The table is copied here as a LITERAL on purpose: the doc lives outside the repo, so
this is the one place the two can be compared in CI. The enumeration may grow beyond
the table (every addition is deliberate and commented in ``app/audit/events.py``); it
may never lose a name the architecture promises.
"""

from __future__ import annotations

from app.audit.events import EVENT_TYPES

# Architecture §5.2 "Complete event type enumeration", event names verbatim
# (VANTAGEMIND_ARCHITECTURE.md, 2026-08-06 canon).
ARCHITECTURE_5_2_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "instance_created",
        "instance_configured",
        "lifecycle_transition",
        "data_retention_hard_delete",
        "lead_erasure",
        "knowledge_source_viewed",
        "knowledge_source_edited",
        "knowledge_source_deleted",
        "escalation_fired",
        "escalation_notification_sent",
        "escalation_delivery_failed",
        "escalation_acked",
        "escalation_owner_fallback",
        "budget_exhausted",
        "budget_overage_threshold",
        "connection_status_changed",
        "connection_secret_cleanup",
        "login_succeeded",
        "login_failed",
        "data_export_requested",
        "data_export_ready",
        "data_export_self_serve",
        "account_closure_initiated",
        "llm_unavailable_escalation",
        "widget_abuse_blocked",
        "channel_abuse_blocked",
        "prompt_injection_detected",
        "content_moderation_event",
        "byo_webhook_circuit_open",
        "human_takeover_started",
        "human_takeover_ended",
        "answer_flagged_by_admin",
        "account_created",
        "email_verified",
        "verification_resent",
        "unverified_account_reaped",
        "logout",
        "password_reset_requested",
        "password_reset_completed",
        "billing_checkout_started",
        "payment_method_added",
        "payment_method_removed",
        "invoice_paid",
        "payment_failed",
        "dunning_state_changed",
        "lifecycle_notice_sent",
        "voice_consent_ack",
        "voice_disclosure_played",
        "lead_pruned",
        "lead_archived",
        "lead_reactivated",
    }
)


def test_every_event_type_architecture_5_2_names_is_in_the_enumeration() -> None:
    missing = ARCHITECTURE_5_2_EVENT_TYPES - EVENT_TYPES
    assert not missing, f"§5.2 names events the enumeration lost: {sorted(missing)}"


def test_the_table_is_the_size_the_architecture_prints() -> None:
    """A copy-paste that dropped a row would pass the subset check silently."""
    assert len(ARCHITECTURE_5_2_EVENT_TYPES) == 51
