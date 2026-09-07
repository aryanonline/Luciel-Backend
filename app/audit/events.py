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
        # An owner delete undone inside its 30-day window (§3.2.2).
        "knowledge_source_restored",
        # Which folders/pages of a connected source Luciel may read (Decision #9).
        # Widening the scope is as consequential as narrowing it, so both are audited.
        "knowledge_source_scope_changed",
        "escalation_fired",
        # The fallback model answered a turn (2026-09-05 audit F132): recorded so an
        # outage on the primary is visible to the owner, never a silent quality drop.
        "llm_fallback_used",
        "escalation_notification_sent",
        "escalation_delivery_failed",
        "escalation_acked",
        "escalation_owner_fallback",
        # The team was unreachable and the owner's after-hours contact took the page (WP-E).
        "escalation_after_hours_routing",
        "budget_exhausted",
        "budget_overage_threshold",
        # Billing / metering lifecycle (Architecture §3.4.1b, CJ §6, Legal §A3)
        "billing_checkout_started",
        "payment_method_added",
        "payment_method_removed",
        "invoice_paid",
        "payment_failed",
        "dunning_state_changed",
        # Legal §A4: at closure the metered subscription is ended with a final invoice
        # for the partial period (payload: subscription id + whether usage was owed).
        "billing_closure_settled",
        # §3.6.6 / Legal §B5: the Stripe customer object is destroyed at account purge,
        # so the processor holds no billing identity for a tenant that no longer exists.
        "stripe_customer_deleted",
        # §3.4.1b: the free-allowance anchor moved from the calendar month to the Stripe
        # billing period the moment billing began (payload: old + new anchor dates).
        "billing_period_anchored",
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
        # The owner opened their export link (a fresh short presign was minted).
        "data_export_downloaded",
        "account_closure_initiated",
        "llm_unavailable_escalation",
        "widget_abuse_blocked",
        # A page outside the owner's allowed origins tried to load the chat (WP-I).
        "widget_origin_blocked",
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
        # C11 capability-URL inbound verification (tenants whose Twilio auth
        # token the platform never holds): provisioned = a per-connection cap
        # token was minted onto the registered webhook URLs; rejected = an
        # inbound presented a cap parameter that did not verify for the number's
        # tenant (forgery signal, number suffix only — never the token).
        "webhook_capability_provisioned",
        "inbound_capability_rejected",
        # Round 5B item 10: the owner's audited attestation that their A2P 10DLC
        # carrier registration is approved (or inapplicable) — the honest exit
        # from pending_carrier_registration, since the A2P status API is
        # unreachable with the deployment's auth. Payload: connection_id, the
        # attested number, and who attested — never credential material.
        "carrier_registration_attested",
        # §3.8.5 (Phase 6.6): the health worker emailed the owner because a
        # connection transitioned into expired/error. Payload carries the
        # delivery outcome, never credential material.
        "connection_attention_emailed",
        # c21: an attempt to claim a destination another tenant holds was refused.
        # Recorded under the CLAIMANT (never the holder — an attacker must not be
        # able to spam a victim's audit chain), in its own transaction because the
        # 409 rolls the request's transaction back.
        "destination_claim_rejected",
        "lead_pruned",
        "lead_archived",
        "lead_reactivated",
        # Round 5C item 8: an inbound STOP/START keyword changed the lead's
        # recorded consent state (Lead.opted_out — the callback dispatcher's
        # gate). Payload: channel + keyword + the new state; no PII.
        "lead_opt_out_recorded",
        # Round 5A item 9: a CRM disconnect cleared the instance's crm_lead_links
        # rows (stale record ids from a possibly-different account would silently
        # 404-heal into duplicates on reconnect). Payload: connection, provider,
        # count — never lead PII.
        "crm_links_cleared",
        # 2026-09-05 audit F134: a captured lead could not be pushed to the CRM and
        # the owner was told (once per lead). Payload: delivery metadata only.
        "crm_push_failure_emailed",
        # Round 5A item 7: two lead rows proved to be the same person (exact
        # identifier match) and were unified. Hashed ids only (§3.4.11).
        "lead_merged",
        # Round 5B item 17: the owner minted a fresh widget embed key (a leaked
        # key's remedy used to be delete-and-recreate). Keys are public
        # identifiers, so both ride the payload.
        "embed_key_rotated",
        # Round 5B item 13 — escalation-contact reliability. bounced: an SES
        # bounce/complaint event was recorded against a configured contact;
        # verification_sent/verified: the confirm-loop's two ends;
        # deferred_abandon_emailed: the owner was emailed about an abandoned
        # deferred item (the audit-only gap the module docstring flagged).
        "email_contact_bounced",
        "escalation_contact_verification_sent",
        "escalation_contact_verified",
        "deferred_abandon_emailed",
        # Admin marked a lead's business outcome (Vision §7 conversion; C14).
        "lead_outcome_marked",
        "lifecycle_notice_sent",
        # A billing/legal notice email left the platform (§A5 allowance
        # reduction, §A10 price change, the 80% pay-as-you-go heads-up).
        # Payload: notice_id + kind + delivery status. The once-only proof is
        # the notice_deliveries row; this is the audit trail of the send.
        "billing_notice_sent",
    }
)
