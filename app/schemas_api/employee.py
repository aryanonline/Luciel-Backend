"""Employee status wire shapes (round 6 WP-F) — mirror schemas/employee.ts exactly."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.schemas_api.common import WireModel


class CapabilityOut(WireModel):
    id: str
    kind: str  # channel | tool | knowledge | escalation | budget
    label: str
    state: str  # ready | attention | off | unavailable
    detail: str | None = None


class NeedOut(WireModel):
    code: str
    severity: str  # attention | info
    title: str
    detail: str
    href: str


class DayCountsOut(WireModel):
    date: str
    conversations: int = 0
    by_channel: dict[str, int] = Field(default_factory=dict)
    leads: int = 0
    leads_to_crm: int = 0
    escalations: int = 0
    escalations_reached: int = 0
    bookings: int = 0
    callbacks_scheduled: int = 0
    human_takeovers: int = 0


class EmployeeStatusOut(WireModel):
    """GET /admin/luciel/status — what Luciel can do, needs, and did.

    ``onDuty`` is always "around the clock": Luciel has no hours. ``teamReachableNow``
    is null until the owner describes their team's availability (WP-E) and then says
    whether a PERSON is reachable; it never describes Luciel."""

    assistant_name: str
    business_short_name: str | None = None
    on_duty: str
    conversations_used: int
    free_allowance: int
    free_remaining: int
    billed_blocks: int
    billable: bool
    period_resets_at: datetime
    timezone: str | None = None
    team_reachable_now: bool | None = None
    next_reachable: str | None = None
    capabilities: list[CapabilityOut]
    needs: list[NeedOut]
    yesterday: DayCountsOut
    today: DayCountsOut
    daily_brief_enabled: bool
