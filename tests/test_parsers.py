"""Parser tests against checked-in samples of the real upstream files."""

from __future__ import annotations

import asyncio

import pytest

from clients.senate_gov import SenateGovClient
from tests.conftest import read_text


class _FixtureFetcher:
    """Stands in for PoliteFetcher, serving fixture text by URL suffix."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def get_text(self, url: str, *, immutable: bool = False) -> str:
        self.calls.append(url)
        for suffix, fixture in self._mapping.items():
            if url.endswith(suffix) or suffix in url:
                return read_text(fixture)
        raise AssertionError(f"unexpected URL in test: {url}")


def _client(mapping: dict[str, str], conn=None) -> tuple[SenateGovClient, _FixtureFetcher]:
    f = _FixtureFetcher(mapping)
    return SenateGovClient(f, conn, current_congress=119, current_session=2), f


def test_vote_menu_parses_real_index() -> None:
    client, _ = _client({"vote_menu_": "vote_menu_sample.xml"})
    votes = asyncio.run(client.vote_menu(119, 2))
    assert len(votes) == 5
    top = votes[0]
    assert top["vote_number"] == 231
    assert top["issue"] == "S. 5271"
    assert top["result"] == "Rejected"
    assert top["yeas"] == 52
    assert top["nays"] == 46
    # The year is not on the vote element; it is carried down from the feed.
    assert top["congress_year"] == 2026
    # Whitespace inside <question> must be collapsed, not preserved verbatim.
    assert top["question"] == "On Cloture on the Motion to Proceed"
    assert "\n" not in top["question"]


def test_vote_detail_parses_full_record_and_positions() -> None:
    client, _ = _client({"roll_call_votes": "vote_119_2_00231.xml"})
    d = asyncio.run(client.vote_detail(119, 2, 231))
    assert d["congress"] == 119
    assert d["session"] == 2
    assert d["vote_number"] == 231
    assert d["majority_requirement"] == "3/5"
    assert d["count_yeas"] == 52
    assert d["count_nays"] == 46
    assert d["count_absent"] == 2
    assert d["document_name"] == "S. 5271"
    assert "photo identification" in d["vote_document_text"]
    assert len(d["positions"]) == 100
    first = d["positions"][0]
    assert first["lis_member_id"].startswith("S")
    assert first["vote_cast"] in {"Yea", "Nay", "Present", "Not Voting"}
    # Empty elements must become None rather than empty strings.
    assert d["tie_breaker_by"] is None


def test_vote_detail_is_cached_and_not_refetched(conn) -> None:
    """Second call must be served from the store, with no upstream request."""
    client, fetcher = _client({"roll_call_votes": "vote_119_2_00231.xml"}, conn)
    asyncio.run(client.vote_detail(119, 2, 231))
    assert len(fetcher.calls) == 1
    again = asyncio.run(client.vote_detail(119, 2, 231))
    assert len(fetcher.calls) == 1, "a cached vote was fetched a second time"
    assert again["vote_number"] == 231
    assert again["count_yeas"] == 52


def test_hearings_flags_status_rows_without_dropping_them() -> None:
    client, _ = _client({"hearings.xml": "hearings_sample.xml"})
    rows = asyncio.run(client.hearings())
    assert rows, "hearings feed produced nothing"
    placeholders = [r for r in rows if r["placeholder"]]
    real = [r for r in rows if not r["placeholder"]]
    assert placeholders, "status row should be preserved, flagged"
    assert real, "real meetings should parse"
    assert real[0]["committee"] == "Armed Services"
    assert real[0]["date"] == "2026-09-17"
    assert real[0]["committee_code"] == "SSAS00"


def test_floor_schedule_reads_attributes_not_elements() -> None:
    """This feed keys its data on XML attributes; a child-element parser reads nothing."""
    client, _ = _client({"floor_schedule.xml": "floor_schedule_sample.xml"})
    sched = asyncio.run(client.floor_schedule())
    assert sched["congress"] == 119
    assert sched["session"] == 2
    assert len(sched["days"]) == 4
    day = sched["days"][0]
    assert day["legislative_day"].startswith("2026-01-03")
    assert day["convene"].startswith("2026-01-03T12:00")
    assert day["adjourn_type"] == "Adjourn"


def test_senators_feed_carries_both_identifiers() -> None:
    client, _ = _client({"cvc_member_data.xml": "cvc_member_sample.xml"})
    sens = asyncio.run(client.senators())
    assert len(sens) == 3
    s = sens[0]
    assert s["lis_member_id"] == "S428"
    assert s["bioguide_id"] == "A000382"
    assert s["state"] == "MD"
    assert s["committees"], "committee assignments should parse"
    assert all(c["code"] for c in s["committees"])


def test_closed_sessions_are_marked_immutable() -> None:
    """A past session must be requested with the permanent-cache flag."""
    seen = {}

    class Recorder(_FixtureFetcher):
        async def get_text(self, url: str, *, immutable: bool = False) -> str:
            seen[url] = immutable
            return await super().get_text(url, immutable=immutable)

    f = Recorder({"vote_menu_": "vote_menu_sample.xml"})
    client = SenateGovClient(f, None, current_congress=119, current_session=2)
    asyncio.run(client.vote_menu(118, 1))
    assert all(seen.values()), "a closed session was not cached permanently"

    seen.clear()
    asyncio.run(client.vote_menu(119, 2))
    assert not any(seen.values()), "the in-progress session must stay revalidated"


def test_malformed_xml_raises_the_error_contract() -> None:
    from clients.http_polite import PoliteError

    class Broken:
        async def get_text(self, url, *, immutable=False):
            return "<vote_summary><unclosed>"

    client = SenateGovClient(Broken(), None)
    with pytest.raises(PoliteError) as exc:
        asyncio.run(client.vote_menu(119, 2))
    assert exc.value.code == "INTERNAL"
