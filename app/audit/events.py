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
        # Which folders/pages of a connected source Luciel may read (Decision #9).
        # Widening the scope is as consequential as narrowing it, so both are audited.
        "knowledge_source_scope_changed",
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
        # A push_to_crm upsert landed (§3.3.6 traced create-vs-update history).
        # Payload: provider + action + record id — never lead PII (§3.4.11).
        "crm_record_upserted",
        "connection_secret_cleanup",
        # Emitted at delete_luciel time (§3.6.3): the secret's destruction is
        # SCHEDULED for the day-30 cascade so a restore can revive the
        # connection. The real `connection_secret_cleanup` (with the deleted
        # outcome) fires only when a secret is actually destroyed — an audit
        # event must never assert a cleanup that has not happened.
        "connection_secret_cleanup_scheduled",
        # Hot→cold transcript offload (Commitment #38): a session's transcript moved
        # to the S3 archive (key + row count, never content), and the cascade's
        # prefix cleanup of a deleted tenant's archived objects.
        "transcript_archived",
        "transcript_archive_cleanup",
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
        # Admin-authored reply sent during takeover (§3.4.12) — carries the delivery
        # outcome, never the message text.
        "human_agent_message_sent",
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
        # c20 webhook auto-config on the designated number (customer's own creds):
        # applied = the platform pointed Voice/SMS/StatusCallback at itself;
        # failed = it could not, and the manual guidance was surfaced instead.
        "webhook_autoconfig_applied",
        "webhook_autoconfig_failed",
        "lead_pruned",
        "lead_archived",
        "lead_reactivated",
        "lifecycle_notice_sent",
        # A billing/legal notice email left the platform (§A5 allowance
        # reduction, §A10 price change, the 80% pay-as-you-go heads-up).
        # Payload: notice_id + kind + delivery status. The once-only proof is
        # the notice_deliveries row; this is the audit trail of the send.
        "billing_notice_sent",
    }
)
