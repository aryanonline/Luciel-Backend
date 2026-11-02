"""Every array-returning GET under /api/v1 is bounded or says why not (WP-8 audit).

Read from the schema, so no route can be added without a decision: it takes the shared
``limit`` with a ceiling, or it joins the table below with the reason nothing it returns
can grow without limit.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from app.api.paging import PAGE_MAX
from app.knowledge import service
from app.main import create_app
from sqlalchemy.dialects import postgresql

_BOUNDED_BY_OTHER_MEANS = {
    "/api/v1/admin/connections/providers": "the static provider registry",
    "/api/v1/admin/luciel/capabilities": "the static capability catalog",
    "/api/v1/admin/knowledge/sync-connections/{connection_id}/scope/candidates": (
        "one provider call: Drive's default page (100) or Notion page_size 100"
    ),
    "/api/v1/chat-widget/sessions/{session_id}": "visitor-facing, not an owner list: "
    "see the audit's open question, the widget polls it for new replies",
}


def _array_gets(spec: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    for path, item in spec["paths"].items():
        get = item.get("get")
        if not get:
            continue
        schema = (
            get["responses"].get("200", {}).get("content", {}).get("application/json", {})
        ).get("schema", {})
        if schema.get("type") == "array":
            found[path] = get.get("parameters", [])
    return found


def test_every_array_route_takes_a_ceilinged_limit_or_says_why_not() -> None:
    routes = _array_gets(create_app().openapi())
    assert set(_BOUNDED_BY_OTHER_MEANS) <= set(routes), "a table entry names no array route"
    for path, parameters in routes.items():
        if path in _BOUNDED_BY_OTHER_MEANS:
            continue
        by_name = {p["name"]: p["schema"] for p in parameters}
        assert "limit" in by_name, f"{path} returns an array with no limit"
        assert by_name["limit"]["maximum"] <= PAGE_MAX, path
        assert by_name["limit"]["minimum"] == 1, path
        assert by_name["offset"]["minimum"] == 0, path


class _Recording:
    """Stands in for the session: keeps the statement, returns no rows."""

    statement: Any = None

    async def execute(self, statement: Any) -> Any:
        self.statement = statement

        class _Result:
            def scalars(self) -> Any:
                return self

            def all(self) -> list[object]:
                return []

        return _Result()


async def _source_sql(**kwargs: Any) -> str:

    session = _Recording()
    await service.list_sources(session, uuid.uuid4(), **kwargs)  # type: ignore[arg-type]
    return str(
        session.statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


def test_the_owners_source_page_covers_both_kinds_and_is_bounded() -> None:

    sql = asyncio.run(_source_sql(include_removed_at_origin=True, limit=7, offset=14))
    assert "knowledge_sources.deleted_by = 'origin'" in sql
    assert "LIMIT 7" in sql
    assert "OFFSET 14" in sql


def test_the_internal_source_list_stays_whole_and_active_only() -> None:

    sql = asyncio.run(_source_sql())
    assert "deleted_by" not in sql.split("ORDER BY")[0].split("WHERE")[1]
    assert "LIMIT" not in sql
