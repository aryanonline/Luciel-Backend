"""Adversarial review R-WP2-1: Graph paging is followed only on Graph hosts. The
``paging.next`` URL carries the grant's access token in its query string, so a reply
naming another host must end the walk rather than be fetched."""

from __future__ import annotations

from typing import Any

import pytest
from app.connections import meta_assets

pytestmark = [pytest.mark.asyncio]


class _Resp:
    def __init__(self, data: dict[str, Any], status: int = 200) -> None:
        self._data = data
        self.status_code = status

    def json(self) -> dict[str, Any]:
        return self._data


class _Transport:
    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self.pages = pages
        self.calls: list[str] = []

    async def get(self, url: str, *, params: Any = None, headers: Any = None) -> _Resp:
        self.calls.append(url)
        return _Resp(self.pages.get(url, {"data": []}))


ACCOUNTS = f"{meta_assets.GRAPH_BASE}/me/accounts"
BUSINESSES = f"{meta_assets.GRAPH_BASE}/me/businesses"


async def test_an_off_graph_next_url_ends_the_walk_and_is_never_fetched():
    transport = _Transport(
        {
            ACCOUNTS: {
                "data": [{"id": "page-1"}],
                "paging": {"next": "https://evil.example/collect?access_token=tok"},
            },
            BUSINESSES: {"data": []},
        }
    )
    ids = await meta_assets.list_grant_assets(
        material={"access_token": "tok"}, connection_type="channel_auth", transport=transport
    )
    assert ids == {"page-1"}
    assert not any("evil.example" in url for url in transport.calls)


async def test_a_graph_next_url_is_followed():
    second = f"{ACCOUNTS}?after=abc"
    transport = _Transport(
        {
            ACCOUNTS: {"data": [{"id": "page-1"}], "paging": {"next": second}},
            second: {"data": [{"id": "page-2"}]},
            BUSINESSES: {"data": []},
        }
    )
    ids = await meta_assets.list_grant_assets(
        material={"access_token": "tok"}, connection_type="channel_auth", transport=transport
    )
    assert ids == {"page-1", "page-2"}
    assert transport.calls.count(second) == 1
