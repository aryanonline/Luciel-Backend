"""Providers read what the owner connected, all of it (2026-09-05 audit F094/F097/F117/F123).

Drive listed the first page of file NAMES and ingested empty text for every one;
HubSpot and Salesforce read their first page; the website crawl read only the
seed URLs. Now Drive walks every listing page and reads each file — native docs
exported as text, binaries downloaded and parsed — the CRM readers page to the end,
and the crawl follows same-origin links within bounds while honouring robots.txt.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from app.config import get_settings
from app.knowledge.providers import (
    GoogleDriveProvider,
    HubSpotProvider,
    SalesforceProvider,
    WebsiteCrawlProvider,
    _same_origin_links,
)

ADMIN, INSTANCE = uuid.uuid4(), uuid.uuid4()


class Store:
    def __init__(self, token: str = "tok") -> None:
        self._token = token

    async def get(self, ref):
        return {"access_token": self._token, "instance_url": "https://acme.my.salesforce.com"}


class RoutedTransport:
    """Answers by (url, params) so a test can script a whole provider conversation."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.routes: list[tuple[callable, object]] = []

    def on(self, predicate, response) -> None:
        self.routes.append((predicate, response))

    async def get(self, url, *, headers=None, params=None):
        params = dict(params or {})
        self.calls.append((url, params))
        for predicate, response in self.routes:
            if predicate(url, params):
                if isinstance(response, Exception):
                    raise response
                if isinstance(response, dict):
                    return httpx.Response(200, json=response, request=httpx.Request("GET", url))
                if isinstance(response, bytes):
                    return httpx.Response(200, content=response, request=httpx.Request("GET", url))
                return httpx.Response(200, text=str(response), request=httpx.Request("GET", url))
        raise AssertionError(f"unexpected call {url} {params}")

    async def post(self, url, *, data=None, headers=None, json=None):
        raise AssertionError("unexpected POST")


# --- Google Drive ------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_drive_reads_every_page_and_the_content_of_each_file():
    t = RoutedTransport()
    listing = "https://www.googleapis.com/drive/v3/files"
    t.on(
        lambda u, p: u == listing and "pageToken" not in p,
        {
            "nextPageToken": "p2",
            "files": [
                {
                    "id": "doc1",
                    "name": "Pricing",
                    "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "t1",
                },
                {
                    "id": "form1",
                    "name": "Survey",
                    "mimeType": "application/vnd.google-apps.form",
                    "modifiedTime": "t1",
                },
            ],
        },
    )
    t.on(
        lambda u, p: u == listing and p.get("pageToken") == "p2",
        {
            "files": [
                {
                    "id": "txt1",
                    "name": "hours.txt",
                    "mimeType": "text/plain",
                    "modifiedTime": "t2",
                    "size": "20",
                },
                {
                    "id": "img1",
                    "name": "logo.png",
                    "mimeType": "image/png",
                    "modifiedTime": "t2",
                    "size": "20",
                },
                {
                    "id": "gone",
                    "name": "old.txt",
                    "mimeType": "text/plain",
                    "modifiedTime": "t2",
                    "trashed": True,
                },
            ]
        },
    )
    t.on(
        lambda u, p: u.endswith("/files/doc1/export") and p.get("mimeType") == "text/plain",
        "Decks from $40 per square foot",
    )
    t.on(lambda u, p: u.endswith("/files/txt1") and p.get("alt") == "media", b"Open 9-5 weekdays")

    snap = await GoogleDriveProvider(
        admin_id=ADMIN, instance_id=INSTANCE, store=Store(), transport=t
    ).snapshot(uuid.uuid4())

    by_id = {i.external_id: i for i in snap.items}
    assert set(by_id) == {"doc1", "txt1"}  # form + image + trashed: nothing to ingest
    assert by_id["doc1"].text == "Decks from $40 per square foot"
    assert by_id["txt1"].text == "Open 9-5 weekdays"
    assert snap.failed_ids == []
    # Two listing pages were read; the export and the download happened once each.
    assert sum(1 for u, _ in t.calls if u == listing) == 2


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_drive_reports_a_file_it_could_not_read_instead_of_dropping_it():
    from app.knowledge.providers import ProviderUnavailable

    t = RoutedTransport()
    t.on(
        lambda u, p: u.endswith("/v3/files"),
        {
            "files": [
                {"id": "doc1", "name": "A", "mimeType": "application/vnd.google-apps.document"}
            ]
        },
    )
    t.on(lambda u, p: u.endswith("/files/doc1/export"), ProviderUnavailable("503 from Google"))
    snap = await GoogleDriveProvider(
        admin_id=ADMIN, instance_id=INSTANCE, store=Store(), transport=t
    ).snapshot(uuid.uuid4())
    assert snap.items == []
    assert snap.failed_ids == ["doc1"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_drive_honours_text_carried_on_a_listing_entry():
    """Provider fakes in the existing suite put the body on the listing row."""
    t = RoutedTransport()
    t.on(
        lambda u, p: u.endswith("/v3/files"),
        {"files": [{"id": "x", "name": "X", "text": "inline body"}]},
    )
    snap = await GoogleDriveProvider(
        admin_id=ADMIN, instance_id=INSTANCE, store=Store(), transport=t
    ).snapshot(uuid.uuid4())
    assert [i.text for i in snap.items] == ["inline body"]


# --- CRM knowledge bases -------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_hubspot_pages_to_the_end():
    t = RoutedTransport()
    url = "https://api.hubapi.com/cms/v3/knowledge-base/articles"
    t.on(
        lambda u, p: u == url and "after" not in p,
        {
            "results": [{"id": 1, "title": "A", "postBody": "a"}],
            "paging": {"next": {"after": "c2"}},
        },
    )
    t.on(
        lambda u, p: u == url and p.get("after") == "c2",
        {"results": [{"id": 2, "title": "B", "postBody": "b"}]},
    )
    snap = await HubSpotProvider(
        admin_id=ADMIN, instance_id=INSTANCE, store=Store(), transport=t
    ).snapshot(uuid.uuid4())
    assert [i.external_id for i in snap.items] == ["1", "2"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_salesforce_follows_next_records_url():
    t = RoutedTransport()
    base = "https://acme.my.salesforce.com"
    t.on(
        lambda u, p: u == f"{base}/services/data/v58.0/query",
        {
            "records": [{"Id": "k1", "Title": "One", "Summary": "s1"}],
            "done": False,
            "nextRecordsUrl": "/services/data/v58.0/query/01g-2000",
        },
    )
    t.on(
        lambda u, p: u == f"{base}/services/data/v58.0/query/01g-2000",
        {"records": [{"Id": "k2", "Title": "Two", "Summary": "s2"}], "done": True},
    )
    snap = await SalesforceProvider(
        admin_id=ADMIN, instance_id=INSTANCE, store=Store(), transport=t
    ).snapshot(uuid.uuid4())
    assert [i.external_id for i in snap.items] == ["k1", "k2"]


# --- website crawl -------------------------------------------------------------------- #
def test_same_origin_links_stay_on_the_site_and_skip_assets():
    html = """
      <a href="/pricing">Pricing</a> <a href='https://acme.example/about#team'>About</a>
      <a href="https://other.example/x">Other</a> <a href="mailto:hi@acme.example">Mail</a>
      <a href="/brochure.pdf">PDF</a> <a href="/pricing">Pricing again</a>
    """
    assert _same_origin_links("https://acme.example/", html) == [
        "https://acme.example/pricing",
        "https://acme.example/about",
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_crawl_discovers_same_origin_pages_within_bounds_and_honours_robots(monkeypatch):
    monkeypatch.setattr(get_settings(), "website_crawl_max_pages", 10, raising=False)
    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 1, raising=False)
    t = RoutedTransport()
    t.on(lambda u, p: u == "https://acme.example/robots.txt", "User-agent: *\nDisallow: /private\n")
    t.on(
        lambda u, p: u == "https://acme.example/",
        '<a href="/pricing">p</a><a href="/private/notes">n</a><a href="/deep">d</a>',
    )
    t.on(lambda u, p: u == "https://acme.example/pricing", "<p>Decks from $40</p>")
    t.on(lambda u, p: u == "https://acme.example/deep", '<a href="/deeper">x</a><p>deep page</p>')
    # /deeper would be depth 2 — beyond max_depth=1, never fetched.
    snap = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    ids = [i.external_id for i in snap.items]
    assert ids == [
        "https://acme.example/",
        "https://acme.example/pricing",
        "https://acme.example/deep",
    ]
    assert "https://acme.example/private/notes" not in ids
    assert not any(u.endswith("/deeper") for u, _ in t.calls)
    assert snap.failed_ids == []
    # /deeper exists but the depth cap ended the walk before it: the snapshot names it,
    # so the sync never reads its absence as "removed at the site" (knowledge #3). Every
    # page below the cap was read, so nothing else is unknown: the walk is complete.
    assert snap.unreached_ids == ["https://acme.example/deeper"]
    assert snap.complete is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_crawl_reports_a_page_it_could_not_fetch_and_stops_at_the_page_cap(monkeypatch):
    monkeypatch.setattr(get_settings(), "website_crawl_max_pages", 2, raising=False)
    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 3, raising=False)
    t = RoutedTransport()
    t.on(lambda u, p: u.endswith("/robots.txt"), httpx.ConnectError("no robots"))
    t.on(
        lambda u, p: u == "https://acme.example/",
        '<a href="/a">a</a><a href="/b">b</a><a href="/c">c</a>',
    )
    t.on(lambda u, p: u == "https://acme.example/a", httpx.ReadTimeout("slow"))
    t.on(lambda u, p: u == "https://acme.example/b", "<p>b</p>")
    snap = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    assert [i.external_id for i in snap.items] == ["https://acme.example/"]
    assert snap.failed_ids == [
        "https://acme.example/a"
    ]  # kept + flagged by the sync, never retired
    assert not any(u.endswith("/b") or u.endswith("/c") for u, _ in t.calls)  # the cap held
    # /b and /c were still queued when the page cap tripped: an incomplete snapshot.
    assert snap.complete is False
    assert snap.unreached_ids == []


# --- Round 7 Phase 2 review, knowledge #3: the budget is not the site ------------------- #
# A page that merely fell outside this run's page/depth budget (which pages fit depends
# on link order) used to be absent from a snapshot that looked complete, so the sync
# tombstoned it "removed at origin" and the retention pass purged it that night.


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_a_crawl_that_walked_the_whole_site_is_complete(monkeypatch):
    monkeypatch.setattr(get_settings(), "website_crawl_max_pages", 10, raising=False)
    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 2, raising=False)
    t = RoutedTransport()
    t.on(lambda u, p: u.endswith("/robots.txt"), httpx.ConnectError("no robots"))
    t.on(
        lambda u, p: u == "https://acme.example/",
        '<a href="/a">a</a><a href="/b">b</a><a href="/">home</a>',
    )
    t.on(lambda u, p: u == "https://acme.example/a", '<a href="/b">b</a><p>a</p>')
    t.on(lambda u, p: u == "https://acme.example/b", '<a href="/">home</a><p>b</p>')
    snap = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    assert len(snap.items) == 3
    # Links back to pages already visited (or queued twice) are not "unreached".
    assert snap.complete is True
    assert snap.unreached_ids == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_a_page_at_the_depth_cap_names_the_pages_it_links_onward_to(monkeypatch):
    monkeypatch.setattr(get_settings(), "website_crawl_max_pages", 10, raising=False)
    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 0, raising=False)
    t = RoutedTransport()
    t.on(lambda u, p: u.endswith("/robots.txt"), httpx.ConnectError("no robots"))
    t.on(lambda u, p: u == "https://acme.example/", '<a href="/pricing">p</a><p>home</p>')
    snap = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    assert [i.external_id for i in snap.items] == ["https://acme.example/"]
    assert snap.unreached_ids == ["https://acme.example/pricing"]  # kept by the sync
    # The same page with no onward link is the whole site.
    t2 = RoutedTransport()
    t2.on(lambda u, p: u.endswith("/robots.txt"), httpx.ConnectError("no robots"))
    t2.on(lambda u, p: u == "https://acme.example/", '<a href="/">home</a><p>home</p>')
    snap2 = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t2).snapshot(
        uuid.uuid4()
    )
    assert snap2.complete is True and snap2.unreached_ids == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("db_engine_reset")
async def test_a_failed_page_below_the_depth_cap_makes_the_crawl_incomplete(monkeypatch):
    """/blog timed out, so the posts it links to were never even discovered: their
    absence says nothing about the site, and the snapshot must not claim to be whole.
    The same failure AT the depth cap hides nothing (its links would not be followed)."""
    monkeypatch.setattr(get_settings(), "website_crawl_max_pages", 10, raising=False)
    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 2, raising=False)
    t = RoutedTransport()
    t.on(lambda u, p: u.endswith("/robots.txt"), httpx.ConnectError("no robots"))
    t.on(lambda u, p: u == "https://acme.example/", '<a href="/blog">b</a><p>home</p>')
    t.on(lambda u, p: u == "https://acme.example/blog", httpx.ReadTimeout("slow"))
    snap = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    assert snap.failed_ids == ["https://acme.example/blog"]
    assert snap.complete is False

    monkeypatch.setattr(get_settings(), "website_crawl_max_depth", 1, raising=False)
    snap2 = await WebsiteCrawlProvider(urls=["https://acme.example/"], transport=t).snapshot(
        uuid.uuid4()
    )
    assert snap2.failed_ids == ["https://acme.example/blog"]
    assert snap2.complete is True
