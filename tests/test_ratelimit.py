"""The rate limiter has to actually delay, not merely exist.

Timing assertions are measured against a monotonic clock with generous slack,
so the test is about the floor being enforced rather than about precise sleeps.
"""

from __future__ import annotations

import asyncio
import time

from clients.http_polite import PoliteFetcher, senate_menu_url


class _RecordingTransport:
    """Captures request times and returns a canned body, without a network."""

    def __init__(self) -> None:
        self.times: list[float] = []
        self.headers_seen: list[dict] = []

    async def handler(self, request):
        import httpx

        self.times.append(time.monotonic())
        self.headers_seen.append(dict(request.headers))
        return httpx.Response(
            200,
            text="<vote_summary><votes></votes></vote_summary>",
            headers={"ETag": '"abc"', "Last-Modified": "Sat, 08 Aug 2026 09:15:29 GMT"},
        )


def _fetcher_with(transport_handler, interval: float) -> PoliteFetcher:
    import httpx

    f = PoliteFetcher(version="0.0.0", min_interval=interval)
    f._client = httpx.AsyncClient(
        transport=httpx.MockTransport(transport_handler),
        headers={"User-Agent": f._ua},
    )
    return f


def test_consecutive_requests_are_spaced_by_at_least_the_interval() -> None:
    rec = _RecordingTransport()
    interval = 0.25
    fetcher = _fetcher_with(rec.handler, interval)
    url_a = senate_menu_url(119, 1)
    url_b = senate_menu_url(119, 2)
    url_c = senate_menu_url(118, 1)

    async def run() -> None:
        await fetcher.get_text(url_a)
        await fetcher.get_text(url_b)
        await fetcher.get_text(url_c)
        await fetcher.close()

    start = time.monotonic()
    asyncio.run(run())
    elapsed = time.monotonic() - start

    assert len(rec.times) == 3
    gaps = [rec.times[i + 1] - rec.times[i] for i in range(len(rec.times) - 1)]
    for gap in gaps:
        assert gap >= interval * 0.9, f"requests were {gap:.3f}s apart, floor is {interval}s"
    # Two enforced gaps means the whole run cannot finish faster than 2 intervals.
    assert elapsed >= interval * 1.8


def test_zero_interval_does_not_deadlock() -> None:
    """The floor is configurable down to nothing; it must still terminate."""
    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)

    async def run() -> None:
        await fetcher.get_text(senate_menu_url(119, 1))
        await fetcher.get_text(senate_menu_url(119, 2))
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.times) == 2


def test_concurrent_callers_are_serialized() -> None:
    """Parallel tool calls must not bypass the floor by racing each other."""
    rec = _RecordingTransport()
    interval = 0.2
    fetcher = _fetcher_with(rec.handler, interval)

    async def run() -> None:
        await asyncio.gather(
            fetcher.get_text(senate_menu_url(119, 1)),
            fetcher.get_text(senate_menu_url(119, 2)),
            fetcher.get_text(senate_menu_url(118, 1)),
        )
        await fetcher.close()

    asyncio.run(run())
    gaps = [rec.times[i + 1] - rec.times[i] for i in range(len(rec.times) - 1)]
    assert len(rec.times) == 3
    for gap in gaps:
        assert gap >= interval * 0.9, "concurrency bypassed the rate limit"


def test_requests_carry_an_identifying_user_agent() -> None:
    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)

    async def run() -> None:
        await fetcher.get_text(senate_menu_url(119, 2))
        await fetcher.close()

    asyncio.run(run())
    ua = rec.headers_seen[0]["user-agent"]
    assert ua.startswith("mcp-cloakroom/")
    assert "+http" in ua, "the UA must carry a contact URL"


def test_second_fetch_sends_conditional_headers() -> None:
    """A stored validator must turn the next request into a conditional GET."""

    class Cache:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        def get(self, url):
            return self.rows.get(url)

        def put(self, **kw):
            self.rows[kw["url"]] = kw

    rec = _RecordingTransport()
    cache = Cache()
    fetcher = _fetcher_with(rec.handler, 0.0)
    fetcher._cache = cache
    url = senate_menu_url(119, 2)

    async def run() -> None:
        await fetcher.get_text(url)
        await fetcher.get_text(url)
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.headers_seen) == 2
    assert rec.headers_seen[0].get("if-none-match") is None
    assert rec.headers_seen[1].get("if-none-match") == '"abc"'
    assert rec.headers_seen[1].get("if-modified-since")


def test_immutable_cache_entry_prevents_any_second_request() -> None:
    """A closed session is fetched at most once, ever."""

    class Cache:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        def get(self, url):
            return self.rows.get(url)

        def put(self, **kw):
            self.rows[kw["url"]] = kw

    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)
    fetcher._cache = Cache()
    url = senate_menu_url(118, 1)

    async def run() -> None:
        await fetcher.get_text(url, immutable=True)
        await fetcher.get_text(url, immutable=True)
        await fetcher.get_text(url, immutable=True)
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.times) == 1, "an immutable entry was re-requested"


# ------------------------------------------------------- refresh window


class _Cache:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def get(self, url):
        return self.rows.get(url)

    def put(self, **kw):
        self.rows[kw["url"]] = kw


def test_refresh_window_serves_cache_without_any_request() -> None:
    """CLOAKROOM_REFRESH_HOURS was documented in three places and read nowhere.

    The setting existed, the docstring promised a refresh window, and
    get_text never consulted fetched_at, so raising the value did nothing.
    """
    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)
    fetcher._cache = _Cache()
    fetcher._refresh_seconds = 3600.0
    url = senate_menu_url(119, 2)

    async def run() -> None:
        await fetcher.get_text(url)
        await fetcher.get_text(url)
        await fetcher.get_text(url)
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.times) == 1, "the refresh window did not suppress repeat requests"


def test_expired_refresh_window_revalidates_conditionally() -> None:
    """Past the window it must ask again, and ask cheaply."""
    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)
    fetcher._cache = _Cache()
    fetcher._refresh_seconds = 0.001
    url = senate_menu_url(119, 2)

    async def run() -> None:
        await fetcher.get_text(url)
        await asyncio.sleep(0.05)
        await fetcher.get_text(url)
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.times) == 2, "an expired window should have revalidated"
    assert rec.headers_seen[1].get("if-none-match") == '"abc"', "revalidation was not conditional"


def test_zero_refresh_window_always_revalidates() -> None:
    """The default (0) must preserve the original always-revalidate behaviour."""
    rec = _RecordingTransport()
    fetcher = _fetcher_with(rec.handler, 0.0)
    fetcher._cache = _Cache()
    fetcher._refresh_seconds = 0.0
    url = senate_menu_url(119, 2)

    async def run() -> None:
        await fetcher.get_text(url)
        await fetcher.get_text(url)
        await fetcher.close()

    asyncio.run(run())
    assert len(rec.times) == 2


def test_refresh_window_setting_reaches_the_fetcher() -> None:
    """Guards the wiring, not just the mechanism.

    The bug was never in the window logic; it was that nothing connected the
    setting to the code. Asserting the mechanism alone would have stayed green.
    """
    import inspect

    import server
    from clients.config import CloakroomSettings

    source = inspect.getsource(server.build_context)
    assert "cloakroom_refresh_hours" in source, "the setting is not wired to the fetcher"
    assert "3600" in source, "hours must be converted to seconds"

    s = CloakroomSettings(_env_file=None, CLOAKROOM_REFRESH_HOURS="2")
    assert s.cloakroom_refresh_hours == 2.0
