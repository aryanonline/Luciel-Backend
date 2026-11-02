"""The WP-8 pagination audit, read from the schema.

Covers GETs under /api/v1 whose 200 response is an array, or an object with an array as a
top-level property: each takes the shared ``limit`` with a ceiling, or joins a table below
with its reason. A list nested deeper than that, or under a response the schema does not
describe, is not walked. So a new route of those two shapes cannot be added without a
decision, but this test is not a proof that every list anywhere is bounded.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from app.api.paging import PAGE_MAX, SOURCES_MAX, TOTAL_COUNT_HEADER
from app.api.v1 import dashboard
from app.api.v1.admin import knowledge as knowledge_routes
from app.knowledge import service
from app.main import create_app
from app.schemas_api.knowledge import SCOPE_SELECTIONS_MAX, SaveScopeRequest
from fastapi import Response
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from starlette.testclient import TestClient

_API = "/api/v1"
_SCOPE = f"{_API}/admin/knowledge/sync-connections/{{connection_id}}/scope"
_SOURCES = f"{_API}/admin/knowledge/sources"
_TRANSCRIPT = f"{_API}/dashboard/conversations/{{session_id}}/messages"

# Array routes that take no limit because what they return cannot grow without limit.
_BOUNDED_BY_OTHER_MEANS = {
    f"{_API}/admin/connections/providers": "the static provider registry",
    f"{_API}/admin/luciel/capabilities": "the static capability catalog",
    f"{_SCOPE}/candidates": "one provider call: Drive's default page (100) or Notion's 100",
}

# Array routes that CAN grow without limit and were left that way on purpose.
_UNBOUNDED_BY_DECISION = {
    f"{_API}/chat-widget/sessions/{{session_id}}": (
        "visitor-facing, polled for new replies; a limit on this oldest-first list would "
        "hide them. Its length is one session's, so the same long-session question as "
        "the transcript's, and open."
    ),
}

# A ceiling above PAGE_MAX: the dashboard reads this one whole and has no pager for it.
_OWN_CEILING = {_SOURCES: SOURCES_MAX}

# Objects with a top-level array, and what bounds each array.
_OBJECT_ARRAYS = {
    f"{_API}/auth/me": {"notices": "the operator's few published and derived notices"},
    f"{_API}/admin/luciel": {
        "allowedOrigins": "MAX_ALLOWED_ORIGINS on save",
        "channels": "one row per channel",
        "escalationContactHealth": "one row per contact slot",
        "tools": "the static tool catalog",
    },
    f"{_API}/admin/luciel/status": {
        "capabilities": "a fixed set of checks",
        "needs": "a fixed set of checks",
    },
    f"{_API}/admin/connections/email": {"dnsRecords": "derived from one address"},
    f"{_API}/admin/connections/{{connection_id}}/calendly/event-types": {
        "eventTypes": "one provider call with count=50",
    },
    f"{_API}/admin/connections/{{connection_id}}/twilio/numbers": {
        "numbers": "one provider call with limit=50",
    },
    f"{_SOURCES}/{{source_id}}/chunks": {"chunks": "service.CHUNK_PREVIEW_PAGE"},
    _SCOPE: {"selections": "SaveScopeRequest caps what can be saved (SCOPE_SELECTIONS_MAX)"},
    f"{_TRANSCRIPT}/{{message_id}}/evidence": {"sourceChunks": "the retriever's k chunks"},
    f"{_API}/admin/usage/overview": {
        "escalationsBySignal": "one row per signal",
        "channelMix": "one row per channel",
        "busiestTimes": "fixed time buckets",
        "topKnowledgeSources": "LIMIT 10",
        "conversionByChannel": "one row per channel",
        "conversionBySource": "one row per first-contact channel",
    },
}


def _ok_schema(get: dict[str, Any]) -> dict[str, Any]:
    return (
        get["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
        or {}
    )


def _is_array(schema: dict[str, Any]) -> bool:
    return schema.get("type") == "array" or any(
        option.get("type") == "array" for option in schema.get("anyOf", [])
    )


def _object_properties(schema: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    for option in [schema, *schema.get("anyOf", [])]:
        if "$ref" in option:
            name = option["$ref"].rsplit("/", 1)[-1]
            properties.update(spec["components"]["schemas"][name].get("properties", {}))
    return properties


def _gets(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {path: item["get"] for path, item in spec["paths"].items() if "get" in item}


def test_every_array_route_takes_a_ceilinged_limit_or_says_why_not() -> None:
    spec = create_app().openapi()
    routes = {p: g for p, g in _gets(spec).items() if _is_array(_ok_schema(g))}
    excused = set(_BOUNDED_BY_OTHER_MEANS) | set(_UNBOUNDED_BY_DECISION)
    assert excused <= set(routes), "a table entry names no array route"
    for path, get in routes.items():
        if path in excused:
            continue
        by_name = {p["name"]: p["schema"] for p in get.get("parameters", [])}
        assert "limit" in by_name, f"{path} returns an array with no limit"
        assert by_name["limit"]["maximum"] <= _OWN_CEILING.get(path, PAGE_MAX), path
        assert by_name["limit"]["minimum"] == 1, path
        assert by_name["offset"]["minimum"] == 0, path


def test_every_top_level_array_in_an_object_response_is_accounted_for() -> None:
    spec = create_app().openapi()
    found: dict[str, set[str]] = {}
    for path, get in _gets(spec).items():
        arrays = {
            name
            for name, schema in _object_properties(_ok_schema(get), spec).items()
            if _is_array(schema)
        }
        if arrays:
            found[path] = arrays
    assert found == {path: set(arrays) for path, arrays in _OBJECT_ARRAYS.items()}


def test_a_saved_scope_cannot_outgrow_its_cap() -> None:
    pick = {"id": "folder", "name": "Folder"}
    SaveScopeRequest.model_validate({"selections": [pick] * SCOPE_SELECTIONS_MAX})
    with pytest.raises(ValidationError):
        SaveScopeRequest.model_validate({"selections": [pick] * (SCOPE_SELECTIONS_MAX + 1)})
    SaveScopeRequest.model_validate({"selections": []})  # empty clears: the whole account


def test_a_browser_on_the_dashboard_can_read_the_total() -> None:
    allowed = TestClient(create_app()).get("/healthz", headers={"Origin": "http://localhost:3000"})
    exposed = allowed.headers.get("access-control-expose-headers", "")
    assert TOTAL_COUNT_HEADER.lower() in exposed.lower()


def _sql(statement: Any) -> str:
    return str(
        statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


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
    return _sql(session.statement)


def test_the_owners_source_page_covers_both_kinds_and_is_bounded() -> None:
    sql = asyncio.run(_source_sql(include_removed_at_origin=True, limit=7, offset=14))
    assert "knowledge_sources.deleted_by = 'origin'" in sql
    assert "LIMIT 7" in sql
    assert "OFFSET 14" in sql


def test_the_internal_source_list_stays_whole_and_active_only() -> None:
    sql = asyncio.run(_source_sql())
    assert "deleted_by" not in sql.split("ORDER BY")[0].split("WHERE")[1]
    assert "LIMIT" not in sql


class _Rows:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _Rows:
        return self

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        return self._rows[0]


class _Transcript:
    """Answers the route's statements over ``total`` messages numbered oldest first, by
    applying the statement's own direction, OFFSET and LIMIT."""

    def __init__(self, total: int) -> None:
        self.total = total

    async def execute(self, statement: Any) -> _Rows:
        sql = _sql(statement)
        if "count(" in sql:
            return _Rows([self.total])
        assert "transcripts.at DESC, transcripts.id DESC" in sql
        match = re.search(r"LIMIT (\d+) OFFSET (\d+)", sql)
        assert match is not None, sql
        limit, offset = int(match[1]), int(match[2])
        newest_first = list(range(self.total, 0, -1))
        return _Rows(newest_first[offset : offset + limit])


def _read_transcript(
    monkeypatch: pytest.MonkeyPatch,
    session: Any,
    *,
    limit: int = PAGE_MAX,
    offset: int = 0,
    archive_object_key: str | None = None,
) -> tuple[list[Any], Response]:
    @asynccontextmanager
    async def tenant_session(_admin_id: uuid.UUID) -> Any:
        yield session

    async def require_session(*_a: Any, **_k: Any) -> Any:
        return SimpleNamespace(archive_object_key=archive_object_key)

    monkeypatch.setattr(dashboard, "tenant_session", tenant_session)
    monkeypatch.setattr(dashboard, "_require_session", require_session)
    monkeypatch.setattr(dashboard, "_message_out", lambda row: row)
    monkeypatch.setattr(dashboard, "_archived_message_out", lambda row: row)
    response = Response()
    page = asyncio.run(
        dashboard.get_messages(
            uuid.uuid4(),
            response,
            limit=limit,
            offset=offset,
            subject=SimpleNamespace(admin_id=uuid.uuid4()),  # type: ignore[arg-type]
        )
    )
    return page, response


@pytest.mark.parametrize("total", [1, 499, 500, 501, 750])
def test_a_no_parameter_transcript_read_ends_at_the_newest_message(
    monkeypatch: pytest.MonkeyPatch, total: int
) -> None:
    """What the takeover poll sends. A window on the oldest rows froze it past 500."""
    page, response = _read_transcript(monkeypatch, _Transcript(total))
    assert page == list(range(max(1, total - PAGE_MAX + 1), total + 1))  # oldest first
    assert response.headers[TOTAL_COUNT_HEADER] == str(total)


def test_the_poll_sees_a_new_message_past_the_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    before, _ = _read_transcript(monkeypatch, _Transcript(500))
    after, _ = _read_transcript(monkeypatch, _Transcript(501))
    assert after != before
    assert after[-1] == 501


def test_offset_counts_back_from_the_newest(monkeypatch: pytest.MonkeyPatch) -> None:
    page, _ = _read_transcript(monkeypatch, _Transcript(750), offset=500)
    assert page == list(range(1, 251))


class _NoHotRows:
    async def execute(self, _statement: Any) -> _Rows:
        return _Rows([])


@pytest.mark.parametrize(
    ("limit", "offset", "expected"),
    [
        (PAGE_MAX, 0, list(range(101, 601))),
        (PAGE_MAX, 500, list(range(1, 101))),
        (10, 595, [1, 2, 3, 4, 5]),
        (10, 600, []),
    ],
)
def test_an_archived_transcript_pages_the_same_way(
    monkeypatch: pytest.MonkeyPatch, limit: int, offset: int, expected: list[int]
) -> None:
    async def archived_rows(*_a: Any, **_k: Any) -> list[int]:
        return list(range(1, 601))

    monkeypatch.setattr(dashboard, "_archived_rows", archived_rows)
    page, response = _read_transcript(
        monkeypatch, _NoHotRows(), limit=limit, offset=offset, archive_object_key="key"
    )
    assert page == expected
    assert response.headers[TOTAL_COUNT_HEADER] == "600"


def test_the_source_list_reports_its_whole_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 600-file Drive sync: the list reads all of them, and says how many there are."""
    seen: dict[str, Any] = {}

    @asynccontextmanager
    async def tenant_session(_admin_id: uuid.UUID) -> Any:
        yield object()

    async def list_sources(_s: Any, _a: Any, **kwargs: Any) -> list[Any]:
        seen.update(kwargs)
        return [SimpleNamespace(id=n) for n in range(600)]

    async def count(_s: Any, _a: Any) -> int:
        return 600

    async def used(_s: Any, _a: Any) -> dict[uuid.UUID, int]:
        return {}

    monkeypatch.setattr(knowledge_routes, "tenant_session", tenant_session)
    monkeypatch.setattr(service, "list_sources", list_sources)
    monkeypatch.setattr(service, "count_listed_sources", count)
    monkeypatch.setattr(knowledge_routes, "_used_by_questions_7d", used)
    monkeypatch.setattr(knowledge_routes, "_source_out", lambda row, _used: row)
    response = Response()
    page = asyncio.run(
        knowledge_routes.list_sources(
            response,
            limit=SOURCES_MAX,
            offset=0,
            subject=SimpleNamespace(admin_id=uuid.uuid4()),  # type: ignore[arg-type]
        )
    )
    assert len(page) == 600
    assert seen["include_removed_at_origin"] is True
    assert response.headers[TOTAL_COUNT_HEADER] == "600"


def test_the_source_total_counts_what_the_list_shows() -> None:
    class _Count(_Recording):
        async def execute(self, statement: Any) -> Any:
            self.statement = statement
            return _Rows([3])

    session = _Count()
    assert asyncio.run(service.count_listed_sources(session, uuid.uuid4())) == 3  # type: ignore[arg-type]
    where = _sql(session.statement).split("WHERE")[1]
    assert "soft_deleted_at IS NULL" in where
    assert "deleted_by = 'origin'" in where
