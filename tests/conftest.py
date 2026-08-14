"""Shared fixtures. No test in this suite makes a network request.

The database fixtures are built by feeding checked-in samples of the real
upstream files through the same insert functions the production ingest uses, so
the coercion behaviour under test is the behaviour that ships.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

import pytest

from clients import db, loaders

FIXTURES = Path(__file__).parent / "fixtures"


def read_csv(name: str) -> list[dict]:
    with (FIXTURES / name).open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def read_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    db.init_schema(c)
    return c


@pytest.fixture
def loaded(conn: sqlite3.Connection) -> sqlite3.Connection:
    """A database loaded from real sample rows of all three Voteview files."""
    loaders.insert_rollcalls(conn, iter(read_csv("rollcalls_sample.csv")))
    loaders.insert_members(conn, iter(read_csv("members_sample.csv")))
    loaders.insert_votes(conn, iter(read_csv("votes_sample.csv")))
    return conn


@pytest.fixture
def senate_vote_xml() -> str:
    """The real senate.gov roll call file for the 119th Congress, session 2, vote 231."""
    return read_text("vote_119_2_00231.xml")


@pytest.fixture
def legislators_sample() -> list[dict]:
    """A minimal congress-legislators payload covering the bridge shape.

    Includes a senator with no ``icpsr`` field, which is the real-world case
    that makes ``bioguide`` rather than ``icpsr`` the correct join spine.
    """
    return [
        {
            "id": {"bioguide": "S000033", "lis": "S313", "govtrack": 400357},
            "name": {"first": "Bernard", "last": "Sanders", "official_full": "Bernard Sanders"},
            "bio": {"birthday": "1941-09-08", "gender": "M"},
            "terms": [
                {"type": "rep", "start": "1991-01-03", "end": "1993-01-03", "state": "VT"},
                {
                    "type": "sen",
                    "start": "2025-01-03",
                    "end": "2031-01-03",
                    "state": "VT",
                    "class": 1,
                    "party": "Independent",
                },
            ],
        },
        {
            # No icpsr key at all: joining on icpsr would silently drop this one.
            "id": {"bioguide": "A000382", "lis": "S428"},
            "name": {
                "first": "Angela",
                "last": "Alsobrooks",
                "official_full": "Angela D. Alsobrooks",
            },
            "bio": {"birthday": "1971-02-23", "gender": "F"},
            "terms": [
                {
                    "type": "sen",
                    "start": "2025-01-03",
                    "end": "2031-01-03",
                    "state": "MD",
                    "class": 3,
                    "party": "Democrat",
                }
            ],
        },
        {
            # House-only: must not land in a Senate-scoped table.
            "id": {"bioguide": "H999999", "lis": "H001"},
            "name": {"first": "Test", "last": "Representative"},
            "bio": {},
            "terms": [{"type": "rep", "start": "2025-01-03", "end": "2027-01-03", "state": "TX"}],
        },
    ]


@pytest.fixture
def legislators_loaded(loaded: sqlite3.Connection, legislators_sample, monkeypatch):
    """Load the identity bridge without touching the network."""
    import httpx

    payload = json.dumps(legislators_sample)

    class FakeResponse:
        text = payload

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        def __init__(self, *a, **k) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a) -> None:
            return None

        def get(self, url):
            return FakeResponse()

    monkeypatch.setattr(httpx, "Client", FakeClient)
    loaders.load_legislators(loaded, "test-agent/0.0")
    return loaded
