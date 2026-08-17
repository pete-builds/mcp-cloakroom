"""Service-level paths: the health route, the schedule wiring, and ingest.

These exercise the seams the unit tests leave alone: the plain HTTP route an
uptime monitor polls, the tool that fans out across three feed shapes, and the
CLI that a self-hosting user actually runs.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import ClassVar

import pytest
from fastmcp import FastMCP
from starlette.requests import Request

from clients import db, loaders
from clients.config import CloakroomSettings
from clients.senate_gov import SenateGovClient
from tests.conftest import read_csv, read_text
from tools.health import register_health_route
from tools.schedule import register_schedule_tools
from tools.votes import register_vote_tools


@dataclass
class Ctx:
    settings: CloakroomSettings
    conn: object
    senate: object | None = None
    fetcher: object | None = None


class FixtureFetcher:
    """Serves fixture bodies for the allowlisted feed URLs, with no network."""

    MAP: ClassVar[dict[str, str]] = {
        "hearings.xml": "hearings_sample.xml",
        "cvc_member_data.xml": "cvc_member_sample.xml",
        "floor_schedule.xml": "floor_schedule_sample.xml",
        "vote_menu_": "vote_menu_sample.xml",
        "roll_call_votes": "vote_119_2_00231.xml",
    }

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_text(self, url: str, *, immutable: bool = False) -> str:
        self.calls.append(url)
        for needle, fixture in self.MAP.items():
            if needle in url:
                return read_text(fixture)
        raise AssertionError(f"unexpected URL: {url}")


def _route_handler(mcp: FastMCP, path: str):
    for route in mcp.http_app().routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"route {path} was never registered")


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/healthz", "headers": []})


# ------------------------------------------------------------------- health


def test_healthz_reports_503_while_the_database_is_empty(conn) -> None:
    """During first-run ingest the honest answer is 'not ready yet'."""
    settings = CloakroomSettings(_env_file=None)
    mcp = FastMCP("t")
    register_health_route(mcp, Ctx(settings=settings, conn=conn), version="0.1.0")
    resp = asyncio.run(_route_handler(mcp, "/healthz")(_request()))
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["status"] == "loading"
    assert body["votes"] == 0


def test_healthz_reports_200_and_counts_once_loaded(loaded) -> None:
    settings = CloakroomSettings(_env_file=None)
    mcp = FastMCP("t")
    register_health_route(mcp, Ctx(settings=settings, conn=loaded), version="0.1.0")
    resp = asyncio.run(_route_handler(mcp, "/healthz")(_request()))
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["status"] == "ok"
    assert body["service"] == "mcp-cloakroom"
    assert body["votes"] == 200
    assert body["rollcalls"] == 8
    assert sorted(body["senate_feeds_enabled"]) == ["floor", "hearings", "members", "votes"]


def test_healthz_degrades_rather_than_raising_on_a_broken_store(loaded) -> None:
    loaded.execute("DROP TABLE votes")
    settings = CloakroomSettings(_env_file=None)
    mcp = FastMCP("t")
    register_health_route(mcp, Ctx(settings=settings, conn=loaded), version="0.1.0")
    resp = asyncio.run(_route_handler(mcp, "/healthz")(_request()))
    assert resp.status_code == 503
    assert json.loads(resp.body)["status"] == "degraded"


def test_healthz_never_touches_an_upstream(loaded) -> None:
    """A probe that reaches a third party turns their outage into a restart loop."""
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None)
    ctx = Ctx(settings=settings, conn=loaded, senate=SenateGovClient(fetcher, loaded))
    mcp = FastMCP("t")
    register_health_route(mcp, ctx, version="0.1.0")
    asyncio.run(_route_handler(mcp, "/healthz")(_request()))
    assert fetcher.calls == [], "the healthcheck made an upstream request"


# ----------------------------------------------------------------- schedule


@pytest.fixture
def schedule_ctx(loaded):
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None)
    ctx = Ctx(
        settings=settings,
        conn=loaded,
        senate=SenateGovClient(fetcher, loaded, current_congress=119, current_session=2),
        fetcher=fetcher,
    )
    mcp = FastMCP("t")
    register_schedule_tools(mcp, ctx)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["get_schedule"]
    return tool, fetcher


def test_get_schedule_normalizes_all_three_feed_shapes(schedule_ctx) -> None:
    tool, _ = schedule_ctx
    payload = json.loads(asyncio.run(tool.fn(feed="all")))
    data = payload["data"]
    assert set(data) == {"hearings", "floor", "members", "votes"}
    assert isinstance(data["hearings"], list)
    assert data["floor"]["congress"] == 119
    assert data["members"][0]["bioguide_id"] == "A000382"
    assert data["votes"][0]["vote_number"] == 231
    assert payload["provenance"]["sources"][-1]["name"] == "senate.gov"


@pytest.mark.parametrize("feed", ["hearings", "floor", "members", "votes"])
def test_get_schedule_can_request_one_feed_only(schedule_ctx, feed: str) -> None:
    """Asking for one feed must not fetch the other three."""
    tool, fetcher = schedule_ctx
    payload = json.loads(asyncio.run(tool.fn(feed=feed)))
    assert set(payload["data"]) == {feed}
    assert len(fetcher.calls) == 1


def test_get_schedule_marks_individually_disabled_feeds(loaded) -> None:
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None, CLOAKROOM_SENATE_FEEDS="floor")
    ctx = Ctx(settings=settings, conn=loaded, senate=SenateGovClient(fetcher, loaded))
    mcp = FastMCP("t")
    register_schedule_tools(mcp, ctx)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["get_schedule"]
    data = json.loads(asyncio.run(tool.fn(feed="all")))["data"]
    assert "unavailable" in data["hearings"]
    assert "unavailable" in data["votes"]
    assert data["floor"]["congress"] == 119


# -------------------------------------------------- senate detail on get_vote


def test_get_vote_enriches_from_senate_gov_only_when_asked(loaded) -> None:
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None)
    ctx = Ctx(
        settings=settings,
        conn=loaded,
        senate=SenateGovClient(fetcher, loaded, current_congress=119, current_session=2),
    )
    mcp = FastMCP("t")
    register_vote_tools(mcp, ctx)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["get_vote"]

    plain = json.loads(asyncio.run(tool.fn(congress=119, rollnumber=890)))
    assert "senate_detail" not in plain["data"]
    assert fetcher.calls == [], "the default path must make no request"

    rich = json.loads(
        asyncio.run(tool.fn(congress=119, rollnumber=890, include_senate_detail=True))
    )
    assert rich["data"]["senate_detail"]["majority_requirement"] == "3/5"
    assert len(fetcher.calls) == 1
    assert any(s["name"] == "senate.gov" for s in rich["provenance"]["sources"])


def test_get_vote_explains_when_senate_detail_predates_coverage(loaded) -> None:
    """Pre-101st votes have no senate.gov file; say so rather than erroring."""
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None)
    ctx = Ctx(settings=settings, conn=loaded, senate=SenateGovClient(fetcher, loaded))
    mcp = FastMCP("t")
    register_vote_tools(mcp, ctx)
    tool = {t.name: t for t in asyncio.run(mcp.list_tools())}["get_vote"]
    payload = json.loads(asyncio.run(tool.fn(congress=1, rollnumber=1, include_senate_detail=True)))
    assert "unavailable" in payload["data"]["senate_detail"]
    assert fetcher.calls == []


# ------------------------------------------------------------------- ingest


def test_run_ingest_records_completion_and_is_idempotent(conn, monkeypatch) -> None:
    """The full ingest path, with the network replaced by fixtures."""
    samples = {
        "rollcalls": read_csv("rollcalls_sample.csv"),
        "members": read_csv("members_sample.csv"),
        "votes": read_csv("votes_sample.csv"),
    }

    def fake_stream(url: str, ua: str, timeout: float = 300.0):
        for key, rows in samples.items():
            if key in url:
                return iter(rows)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(loaders, "_stream_csv", fake_stream)
    monkeypatch.setattr(loaders, "load_legislators", lambda conn, ua: 0)

    assert loaders.is_populated(conn) is False
    first = loaders.run_ingest(conn, version="0.1.0")
    assert first["counts"]["votes"] == 200
    assert loaders.is_populated(conn) is True
    assert loaders.get_meta(conn, "last_ingest_completed")

    second = loaders.run_ingest(conn, version="0.1.0")
    assert second["counts"] == first["counts"]
    assert conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"] == 200


def test_ingest_cli_status_makes_no_network_call(tmp_path, monkeypatch, capsys) -> None:
    import ingest

    monkeypatch.setenv("CLOAKROOM_DB_PATH", str(tmp_path / "s.db"))
    monkeypatch.setattr(
        loaders, "run_ingest", lambda *a, **k: pytest.fail("status must not ingest")
    )
    assert ingest.main(["--status"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["populated"] is False
    assert out["counts"]["votes"] == 0


def test_ingest_cli_if_needed_skips_a_populated_database(tmp_path, monkeypatch) -> None:
    import ingest

    path = tmp_path / "p.db"
    monkeypatch.setenv("CLOAKROOM_DB_PATH", str(path))
    conn = db.connect(path)
    db.init_schema(conn)
    loaders.insert_votes(conn, iter(read_csv("votes_sample.csv")))
    loaders.set_meta(conn, "last_ingest_completed", "2026-08-14T00:00:00+00:00")
    conn.close()

    monkeypatch.setattr(
        loaders, "run_ingest", lambda *a, **k: pytest.fail("--if-needed re-ingested")
    )
    assert ingest.main(["--if-needed"]) == 0


def test_http_cache_round_trips_validators(conn) -> None:
    from clients.http_polite import SqliteHttpCache

    cache = SqliteHttpCache(conn)
    assert cache.get("https://example.test/x") is None
    cache.put(
        url="https://example.test/x",
        etag='"v1"',
        last_modified="Sat, 08 Aug 2026 09:15:29 GMT",
        body="<xml/>",
        fetched_at="2026-08-14T00:00:00+00:00",
        immutable=True,
    )
    row = cache.get("https://example.test/x")
    assert row["etag"] == '"v1"'
    assert row["immutable"] == 1
    assert row["body"] == "<xml/>"


# ------------------------------------------------- feed toggle enforcement


def _vote_tool(loaded, feeds: str):
    """A get_vote tool wired with a specific CLOAKROOM_SENATE_FEEDS value."""
    fetcher = FixtureFetcher()
    settings = CloakroomSettings(_env_file=None, CLOAKROOM_SENATE_FEEDS=feeds)
    ctx = Ctx(
        settings=settings,
        conn=loaded,
        senate=SenateGovClient(
            fetcher,
            loaded,
            current_congress=119,
            current_session=2,
            enabled_feeds=settings.enabled_feeds,
        ),
    )
    mcp = FastMCP("t")
    register_vote_tools(mcp, ctx)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}["get_vote"], fetcher


def test_disabling_the_votes_feed_blocks_get_vote_enrichment(loaded) -> None:
    """Negative control: the toggle must actually stop the request.

    This was a real bug. get_vote checked only whether a senate client existed,
    not whether the votes feed was enabled, so an operator who disabled the feed
    specifically to stop senate.gov traffic still got live requests.
    """
    tool, fetcher = _vote_tool(loaded, "hearings,floor,members")
    payload = json.loads(
        asyncio.run(tool.fn(congress=119, rollnumber=890, include_senate_detail=True))
    )
    assert fetcher.calls == [], "a request was made for a disabled feed"
    assert "unavailable" in payload["data"]["senate_detail"]
    assert "disabled" in payload["data"]["senate_detail"]["unavailable"]


def test_enabling_the_votes_feed_still_permits_enrichment(loaded) -> None:
    """Positive control: the guard must not block everything unconditionally."""
    tool, fetcher = _vote_tool(loaded, "hearings,floor,members,votes")
    payload = json.loads(
        asyncio.run(tool.fn(congress=119, rollnumber=890, include_senate_detail=True))
    )
    assert len(fetcher.calls) == 1
    assert payload["data"]["senate_detail"]["majority_requirement"] == "3/5"


def test_the_client_enforces_the_toggle_even_if_a_caller_forgets(loaded) -> None:
    """The choke-point guarantee, tested directly against the client.

    A tool-level check alone would leave the next caller free to bypass it.
    """
    from clients.senate_gov import FeedDisabled

    fetcher = FixtureFetcher()
    client = SenateGovClient(fetcher, loaded, enabled_feeds={"hearings"})
    for coro in (
        client.vote_menu(119, 2),
        client.vote_detail(119, 2, 231),
        client.senators(),
        client.floor_schedule(),
    ):
        with pytest.raises(FeedDisabled):
            asyncio.run(coro)
    assert fetcher.calls == [], "a disabled feed still reached the network"
    # The one enabled feed must still work.
    assert asyncio.run(client.hearings())
    assert len(fetcher.calls) == 1
