"""Round 6 WP-H (F062): the live record connector reads the owner's endpoint through
the egress seam and reports exactly what came back."""

from __future__ import annotations

import pytest
from app.tools import record_backend as rb

URL = "https://records.acme.example/lookup"


@pytest.fixture(autouse=True)
def _reset():
    rb.MockRecordTransport.reset()
    yield
    rb.MockRecordTransport.reset()


async def _query(query: str, **kw):
    return await rb.query_live_records(
        url=URL,
        query=query,
        api_key=kw.get("api_key", "k"),
        allowlist=kw.get("allowlist", frozenset({"records.acme.example"})),
        transport=rb.MockRecordTransport(),
    )


async def test_an_array_of_records_is_returned_as_is_and_capped_for_ambiguity():
    rb.MockRecordTransport.seed(URL, [{"package": "Starter", "price": "$899"}])
    assert await _query("starter") == [{"package": "Starter", "price": "$899"}]
    rb.MockRecordTransport.seed(URL, [{"n": i} for i in range(20)])
    assert len(await _query("anything")) == rb.AMBIGUOUS_CANDIDATE_CAP + 1
    rb.MockRecordTransport.seed(URL, {"records": [{"a": 1}]})
    assert await _query("wrapped") == [{"a": 1}]


async def test_the_query_and_key_travel_as_expected():
    class _Spy:
        url = ""
        headers: dict[str, str] = {}

        async def get(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
            _Spy.url, _Spy.headers = url, headers
            return 200, b"[]"

    await rb.query_live_records(
        url=f"{URL}?tenant=1",
        query="the starter package",
        api_key="secret-key",
        allowlist=frozenset(),
        transport=_Spy(),
    )
    assert _Spy.url == f"{URL}?tenant=1&q=the%20starter%20package"
    assert _Spy.headers["Authorization"] == "Bearer secret-key"


@pytest.mark.parametrize(
    ("seed", "reason"),
    [
        (b"<html>not json</html>", "record_source_not_json"),
        ({"not": "an array"}, "record_source_not_an_array"),
        (500, "record_source_http_500"),
        (401, "record_source_http_401"),
    ],
)
async def test_anything_but_a_json_array_is_the_endpoints_fault_said_so(seed, reason):
    rb.MockRecordTransport.seed(URL, seed)
    with pytest.raises(rb.RecordSourceUnavailable, match=reason):
        await _query("x")


async def test_a_host_outside_the_allowlist_is_refused_before_any_request():
    rb.MockRecordTransport.seed(URL, [{"a": 1}])
    with pytest.raises(rb.RecordSourceUnavailable, match="egress_refused"):
        await _query("x", allowlist=frozenset({"other.example"}))


async def test_the_size_cap_and_timeout_are_pinned():
    assert rb.LIVE_RECORD_MAX_BYTES == 256 * 1024
    assert rb.LIVE_RECORD_TIMEOUT_SECONDS == 10.0
