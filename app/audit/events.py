"""The exact admin_audit_log event_type enumeration (Architecture §5.2).

Using a frozen set lets the audit writer assert that callers only emit known
event types, keeping the enumeration honest as arcs are added.
"""

from __future__ import annotations

EVENT_TYPES: frozenset[str] = frozenset(
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
        # Billing / metering lifecycle (Architecture §3.4.1b, CJ §6, Legal §A3)
        "billing_checkout_started",
        "payment_method_added",
        "payment_method_removed",
        "invoice_paid",
        "payment_failed",
        "dunning_state_changed",
        "connection_status_changed",
        "connection_secret_cleanup",
        "admin_login",
        "admin_login_failed",
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
        # Credential-lifecycle events (Architecture §3.7.1a)
        "account_created",
        "email_verified",
        "verification_resent",
        "unverified_account_reaped",
        "login_succeeded",
        "login_failed",
        "logout",
        "password_reset_requested",
        "password_reset_completed",
        "voice_consent_ack",
        # Per-call proof the mandatory recording/AI disclosure was played (§3.1.2).
        "voice_disclosure_played",
        "lead_pruned",
        "lead_archived",
        "lead_reactivated",
        "lifecycle_notice_sent",
    }
)
