"""The website crawl follows redirects — re-pinned on every hop — identifies itself, and
reads a 401/403 robots.txt as "keep out" (round 7 WP-4c, item 1).

Live shape that motivated this: a small-business site 301s its apex to www. The crawl
client followed nothing and ``raise_for_status`` read the 301 as a failed page, so a
crawl of ``https://acme.example`` fetched no pages at all ("Sync unavailable: crawl
fetched no pages"). Following redirects must not reopen the SSRF window F090/R-WP4-1
closed: every hop's target is resolved and validated exactly like the seed URL, and the
socket connects to the validated address with the hostname as Host + SNI.

These run the REAL fetch path over ``httpx.MockTransport`` — the same client build,
pin, header and redirect handling production uses, with no network.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from app.connections import sandbox
from app.knowledge.providers import (
    CRAWL_MAX_REDIRECTS,
    CRAWL_USER_AGENT,
    ProviderUnavailable,
    WebsiteCrawlProvider,
)

PUBLIC_BY_HOST = {
    "acme.example": "93.184.216.34",
    "www.acme.example": "93.184.216.35",
    "internal.acme.example": "10.0.0.5",  # a name that answers with a private address
}


def _resolver(host: str) -> list[str]:
    """DNS stand-in: names resolve per the table; a literal address resolves to itself
    (so a redirect straight to 169.254.169.254 is validated as that address)."""
    try:
        import ipaddress

        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    if host in PUBLIC_BY_HOST:
        return [PUBLIC_BY_HOST[host]]
    raise OSError("nxdomain")


class _Site:
    """Records every request the crawler makes and answers by (Host header, path)."""

    def __init__(self, routes: dict[tuple[str, str], Callable[[], httpx.Response]]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get((request.headers.get("host", ""), request.url.path))
        if route is None:
            return httpx.Response(404, text="not here")
        return route()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _redirect(to: str, status: int = 301) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(status, headers={"Location": to})


def _page(html: str) -> Callable[[], httpx.Response]:
    return lambda: httpx.Response(200, text=html)


@pytest.fixture(autouse=True)
def _pin_dns(monkeypatch):
    monkeypatch.setattr(sandbox, "_resolve_ips", _resolver)


async def test_an_apex_to_www_redirect_reaches_the_page(monkeypatch):
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://www.acme.example/"),
            ("www.acme.example", "/"): _page("<html><body>Welcome to Acme</body></html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    body, final_url = await provider._fetch_page("https://acme.example/")
    assert "Welcome to Acme" in body
    assert final_url == "https://www.acme.example/"
    # Both hops were pinned: the socket target is the validated ADDRESS, the name rides
    # as Host — and the bot identified itself on each.
    hosts = [(str(r.url.host), r.headers["host"], r.headers["user-agent"]) for r in site.requests]
    assert hosts == [
        ("93.184.216.34", "acme.example", CRAWL_USER_AGENT),
        ("93.184.216.35", "www.acme.example", CRAWL_USER_AGENT),
    ]


async def test_a_redirect_to_a_private_address_is_refused_before_any_connect():
    """The redirect target is validated like the seed URL: the metadata endpoint named
    in a Location header never gets a request."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("http://169.254.169.254/latest/meta-data/"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    with pytest.raises(ProviderUnavailable):
        await provider._fetch_page("https://acme.example/")
    assert [r.headers["host"] for r in site.requests] == ["acme.example"]


async def test_a_redirect_that_downgrades_https_to_http_is_refused():
    """Round 7 Phase 2 review, knowledge #6: the owner registered an https site and a
    misconfigured edge answered ``Location: http://...``. The follower accepted the hop
    and every page was fetched — and ingested as readable chunks — over cleartext. The
    hop is refused before any cleartext connect."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("http://acme.example/plain"),
            ("acme.example", "/plain"): _page("<html>cleartext</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    with pytest.raises(ProviderUnavailable, match="downgraded https to http"):
        await provider._fetch_page("https://acme.example/")
    assert [r.url.scheme for r in site.requests] == ["https"]  # the http hop never connected


async def test_a_downgrading_seed_is_a_failed_page_never_an_ingested_one():
    """At the snapshot level the refused hop is a failed page (kept + flagged by the
    sync, F117), never cleartext content — and a site that only downgrades reached no
    page at all, which is the unreachable-site failure, not an empty site."""
    from app.knowledge.providers import CrawlUnreachable

    site = _Site(
        {
            ("acme.example", "/"): _page('<html>home <a href="/pricing">p</a></html>'),
            ("acme.example", "/pricing"): _redirect("http://acme.example/pricing"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert [i.external_id for i in snapshot.items] == ["https://acme.example/"]
    assert snapshot.failed_ids == ["https://acme.example/pricing"]
    assert all(r.url.scheme == "https" for r in site.requests)

    only = _Site({("acme.example", "/"): _redirect("http://acme.example/")})
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=only.transport()
    )
    with pytest.raises(CrawlUnreachable):
        await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert all(r.url.scheme == "https" for r in only.requests)


async def test_an_http_seed_may_upgrade_to_https():
    """The guard is one-directional: an owner may register an http URL, and its
    redirect to https is followed."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://www.acme.example/"),
            ("www.acme.example", "/"): _page("<html><body>Welcome</body></html>"),
        }
    )
    provider = WebsiteCrawlProvider(urls=["http://acme.example/"], httpx_transport=site.transport())
    body, final_url = await provider._fetch_page("http://acme.example/")
    assert "Welcome" in body and final_url == "https://www.acme.example/"
    assert [r.url.scheme for r in site.requests] == ["http", "https"]


async def test_a_redirect_to_a_name_that_resolves_privately_is_refused():
    """Rebinding through the redirect: a public first hop, then a Location naming a host
    whose DNS answer is RFC 1918. Re-pinning on the hop catches it."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://internal.acme.example/admin"),
            ("internal.acme.example", "/admin"): _page("<html>secrets</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    with pytest.raises(ProviderUnavailable):
        await provider._fetch_page("https://acme.example/")
    assert len(site.requests) == 1


async def test_a_redirect_loop_stops_at_the_hop_cap():
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://www.acme.example/"),
            ("www.acme.example", "/"): _redirect("https://acme.example/"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    with pytest.raises(ProviderUnavailable, match="redirected more than"):
        await provider._fetch_page("https://acme.example/")
    assert len(site.requests) == CRAWL_MAX_REDIRECTS + 1


async def test_a_relative_location_resolves_against_the_current_hop():
    site = _Site(
        {
            ("acme.example", "/docs"): _redirect("/docs/", status=308),
            ("acme.example", "/docs/"): _page("<html>Docs home</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/docs"], httpx_transport=site.transport()
    )
    body, final_url = await provider._fetch_page("https://acme.example/docs")
    assert "Docs home" in body and final_url == "https://acme.example/docs/"


async def test_the_whole_crawl_survives_an_apex_redirect_and_follows_www_links(monkeypatch):
    """The end-to-end shape of the live failure: seed = apex, site lives on www. The
    seed page is fetched through the redirect, and its relative links resolve against
    www (the page they came from), not the apex they would only redirect from again."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://www.acme.example/"),
            ("www.acme.example", "/"): _page(
                '<html><body>Acme home <a href="/pricing">Pricing</a></body></html>'
            ),
            ("www.acme.example", "/pricing"): _page("<html><body>From $10 a month</body></html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert snapshot.failed_ids == []
    by_id = {item.external_id: item.text for item in snapshot.items}
    assert by_id == {
        "https://acme.example/": "Acme home Pricing",
        "https://www.acme.example/pricing": "From $10 a month",
    }
    # robots.txt was consulted once per origin (both 404 → no rule).
    robots = [r.headers["host"] for r in site.requests if r.url.path == "/robots.txt"]
    assert sorted(robots) == ["acme.example", "www.acme.example"]


async def test_an_apex_redirect_does_not_ingest_the_home_page_twice():
    """Round 7 Phase 2 review, knowledge #5: the seed (apex) redirects to www, and the
    www home page links to itself (the logo, a "Home" nav item). The www home URL was
    never recorded as visited, so it was fetched and ingested again — two sources, the
    same text, two embedding and LLM passes, duplicate hits in retrieval."""
    site = _Site(
        {
            ("acme.example", "/"): _redirect("https://www.acme.example/"),
            ("www.acme.example", "/"): _page(
                '<html><body>Acme home <a href="/">Home</a> <a href="/pricing">Pricing</a>'
                "</body></html>"
            ),
            ("www.acme.example", "/pricing"): _page(
                '<html><body>From $10 a month <a href="/">Home</a></body></html>'
            ),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert {i.external_id: i.text for i in snapshot.items} == {
        "https://acme.example/": "Acme home Home Pricing",
        "https://www.acme.example/pricing": "From $10 a month Home",
    }
    assert len({i.revision for i in snapshot.items}) == len(snapshot.items)
    www_home = [
        r for r in site.requests if r.headers["host"] == "www.acme.example" and r.url.path == "/"
    ]
    assert len(www_home) == 1  # the www home body was fetched once
    assert snapshot.complete is True and snapshot.unreached_ids == []


async def test_a_403_robots_txt_keeps_the_crawler_out(monkeypatch):
    """RFC 9309: an unauthorized/forbidden robots.txt means the whole origin is off
    limits. It used to read as 'no rule', and the pages were crawled anyway."""
    site = _Site(
        {
            ("acme.example", "/robots.txt"): lambda: httpx.Response(403, text="nope"),
            ("acme.example", "/"): _page("<html>should not be read</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert snapshot.items == [] and snapshot.failed_ids == []
    assert [r.url.path for r in site.requests] == ["/robots.txt"]


async def test_a_401_robots_txt_keeps_the_crawler_out_too():
    site = _Site(
        {
            ("acme.example", "/robots.txt"): lambda: httpx.Response(401, text="auth"),
            ("acme.example", "/"): _page("<html>should not be read</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert snapshot.items == []
    assert [r.url.path for r in site.requests] == ["/robots.txt"]


async def test_a_missing_or_broken_robots_txt_is_no_rule():
    """404 and 5xx on robots.txt stay 'the site made no rule' (RFC 9309 §2.3.1.3/.4)."""
    for status in (404, 500):
        site = _Site(
            {
                ("acme.example", "/robots.txt"): lambda s=status: httpx.Response(s, text="x"),
                ("acme.example", "/"): _page("<html>readable</html>"),
            }
        )
        provider = WebsiteCrawlProvider(
            urls=["https://acme.example/"], httpx_transport=site.transport()
        )
        snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
        assert [i.text for i in snapshot.items] == ["readable"], status


async def test_a_fetched_robots_txt_is_honoured_for_our_user_agent():
    site = _Site(
        {
            ("acme.example", "/robots.txt"): lambda: httpx.Response(
                200, text="User-agent: LucielBot\nDisallow: /private\n"
            ),
            ("acme.example", "/"): _page(
                '<html><a href="/private">p</a><a href="/public">q</a></html>'
            ),
            ("acme.example", "/private"): _page("<html>keep out</html>"),
            ("acme.example", "/public"): _page("<html>fine</html>"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    snapshot = await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert {i.external_id for i in snapshot.items} == {
        "https://acme.example/",
        "https://acme.example/public",
    }
    assert "/private" not in [r.url.path for r in site.requests]


async def test_a_crawl_that_reaches_no_page_raises_crawl_unreachable_not_a_credential_error():
    """Round 7 Phase 2 review, knowledge #2: a maintenance page (503 everywhere) is an
    unreachable SITE. The typed failure lets the sweep and the manual routes keep it off
    the revoked-credential path; it stays a ProviderUnavailable for every other caller."""
    from app.knowledge.providers import CrawlUnreachable

    site = _Site(
        {
            ("acme.example", "/robots.txt"): lambda: httpx.Response(503, text="maintenance"),
            ("acme.example", "/"): lambda: httpx.Response(503, text="maintenance"),
        }
    )
    provider = WebsiteCrawlProvider(
        urls=["https://acme.example/"], httpx_transport=site.transport()
    )
    with pytest.raises(CrawlUnreachable, match="crawl fetched no pages") as caught:
        await provider.snapshot(connection_id=None)  # type: ignore[arg-type]
    assert isinstance(caught.value, ProviderUnavailable)
