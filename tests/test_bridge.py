"""The clerk_rollnumber bridge, pinned against a real fixture pair.

This is the highest-value test in the repository. Voteview numbers roll calls
continuously across a Congress; senate.gov restarts numbering every session. The
119th Congress reached Voteview ``rollnumber`` 890, which is senate.gov
``vote_number`` 231 of session 2. Nothing about a mismatch here is loud: swap
the two and every cross-reference returns a real, plausible, wrong vote.

The fixtures are the actual published rows and the actual published XML file,
so this test fails if either the mapping logic or the ingest coercion drifts.
"""

from __future__ import annotations

import re

import pytest

from clients import queries
from clients.queries import NotFound

# The pair, verified against both sources on 2026-08-14.
CONGRESS = 119
SESSION = 2
SENATE_VOTE_NUMBER = 231
VOTEVIEW_ROLLNUMBER = 890


def test_the_two_numbering_schemes_are_not_the_same_number() -> None:
    """Guards the premise. If these ever coincide, the fixture is not exercising the bridge."""
    assert SENATE_VOTE_NUMBER != VOTEVIEW_ROLLNUMBER


def test_senate_vote_number_resolves_to_voteview_rollnumber(loaded) -> None:
    got = queries.resolve_rollnumber(
        loaded, CONGRESS, session=SESSION, vote_number=SENATE_VOTE_NUMBER
    )
    assert got == VOTEVIEW_ROLLNUMBER


def test_rollnumber_passes_through_untouched(loaded) -> None:
    got = queries.resolve_rollnumber(loaded, CONGRESS, rollnumber=VOTEVIEW_ROLLNUMBER)
    assert got == VOTEVIEW_ROLLNUMBER


def test_both_addressing_schemes_return_the_same_vote(loaded) -> None:
    by_roll = queries.rollcall_row(
        loaded,
        CONGRESS,
        queries.resolve_rollnumber(loaded, CONGRESS, rollnumber=VOTEVIEW_ROLLNUMBER),
    )
    by_senate = queries.rollcall_row(
        loaded,
        CONGRESS,
        queries.resolve_rollnumber(
            loaded, CONGRESS, session=SESSION, vote_number=SENATE_VOTE_NUMBER
        ),
    )
    assert by_roll["rollnumber"] == by_senate["rollnumber"]
    assert by_roll["date"] == by_senate["date"]


def test_bridge_matches_the_real_senate_gov_file(loaded, senate_vote_xml: str) -> None:
    """Cross-source check: the Voteview row and the senate.gov file must agree.

    Same vote, two independent publishers. The tallies and the date are the
    observable facts that prove the join landed on the right row rather than
    merely on *a* row.
    """
    roll = queries.resolve_rollnumber(
        loaded, CONGRESS, session=SESSION, vote_number=SENATE_VOTE_NUMBER
    )
    row = queries.rollcall_row(loaded, CONGRESS, roll)

    def field(tag: str) -> str:
        m = re.search(rf"<{tag}>(.*?)</{tag}>", senate_vote_xml, re.S)
        assert m, f"fixture is missing <{tag}>"
        return m.group(1).strip()

    assert int(field("vote_number")) == SENATE_VOTE_NUMBER
    assert int(field("congress")) == CONGRESS
    assert int(field("session")) == SESSION

    counts = re.search(r"<count>(.*?)</count>", senate_vote_xml, re.S).group(1)
    yeas = int(re.search(r"<yeas>(\d+)</yeas>", counts).group(1))
    nays = int(re.search(r"<nays>(\d+)</nays>", counts).group(1))
    assert row["yea_count"] == yeas
    assert row["nay_count"] == nays

    # senate.gov: "August 8, 2026,  04:36 AM"; Voteview: "2026-08-08".
    assert row["date"] == "2026-08-08"
    assert "August 8, 2026" in field("vote_date")


def test_missing_vote_number_raises_not_found(loaded) -> None:
    with pytest.raises(NotFound):
        queries.resolve_rollnumber(loaded, CONGRESS, session=SESSION, vote_number=998)


def test_partial_senate_address_is_rejected(loaded) -> None:
    """session without vote_number is a caller error, not an empty result."""
    with pytest.raises(ValueError):
        queries.resolve_rollnumber(loaded, CONGRESS, session=SESSION)
    with pytest.raises(ValueError):
        queries.resolve_rollnumber(loaded, CONGRESS)


def test_pre_101st_congress_votes_have_no_clerk_number(loaded) -> None:
    """Historical votes are addressable only by Voteview rollnumber.

    senate.gov publishes roll calls from the 101st Congress forward, so the
    bridge column is genuinely absent before then. It must be null rather than
    zero or a guess, and it must not block retrieval by rollnumber.
    """
    row = queries.rollcall_row(loaded, 1, 1)
    assert row["clerk_rollnumber"] is None
    assert row["date"] == "1789-07-17"


def test_ingest_rejects_a_duplicate_clerk_mapping(loaded) -> None:
    """The uniqueness guarantee, tested through the code that actually ships.

    This previously asserted IntegrityError against a hand-written plain
    INSERT. The ingest used INSERT OR REPLACE, which resolves a unique-index
    violation by *deleting* the conflicting row, so the guarantee the schema
    comment described was never enforced on any path a user could reach. The
    test passed and the promise was empty: the fourth instance in this project
    of a green test that never touched the shipping code.

    Drives clients.loaders.insert_rollcalls, the real ingest entry point.
    """
    import sqlite3

    from clients import loaders

    before = loaded.execute(
        "SELECT rollnumber FROM rollcalls WHERE congress=? AND session=? AND clerk_rollnumber=?",
        (CONGRESS, SESSION, SENATE_VOTE_NUMBER),
    ).fetchone()["rollnumber"]
    assert before == VOTEVIEW_ROLLNUMBER

    votes_before = loaded.execute(
        "SELECT COUNT(*) AS n FROM votes WHERE congress=? AND rollnumber=?",
        (CONGRESS, VOTEVIEW_ROLLNUMBER),
    ).fetchone()["n"]
    assert votes_before > 0, "fixture must carry member votes for the target roll call"

    collision = {
        "congress": str(CONGRESS),
        "chamber": "Senate",
        "rollnumber": "99999",
        "session": str(SESSION),
        "clerk_rollnumber": str(SENATE_VOTE_NUMBER),
        "date": "2026-08-08",
        "yea_count": "1",
        "nay_count": "1",
    }
    with pytest.raises(sqlite3.IntegrityError) as exc:
        loaders.insert_rollcalls(loaded, iter([collision]))

    message = str(exc.value)
    assert "clerk_rollnumber" in message
    assert str(VOTEVIEW_ROLLNUMBER) in message, "the error should name the existing roll call"

    # The original row survives and nothing is orphaned.
    after = loaded.execute(
        "SELECT rollnumber FROM rollcalls WHERE congress=? AND session=? AND clerk_rollnumber=?",
        (CONGRESS, SESSION, SENATE_VOTE_NUMBER),
    ).fetchone()["rollnumber"]
    assert after == VOTEVIEW_ROLLNUMBER, "the conflicting write replaced the real roll call"
    orphans = loaded.execute(
        "SELECT COUNT(*) AS n FROM votes v "
        "LEFT JOIN rollcalls r ON r.congress = v.congress AND r.rollnumber = v.rollnumber "
        "WHERE r.rollnumber IS NULL"
    ).fetchone()["n"]
    assert orphans == 0, "member votes were orphaned by the rejected write"


def test_reingesting_the_same_rollcalls_is_idempotent(loaded) -> None:
    """The upsert must not turn 'fail loudly on collision' into 'fail on rerun'.

    Refreshing from newly published Voteview data re-inserts every existing roll
    call, so a naive plain INSERT would make routine ingest impossible.
    """
    from clients import loaders
    from tests.conftest import read_csv

    before = loaded.execute("SELECT COUNT(*) AS n FROM rollcalls").fetchone()["n"]
    loaders.insert_rollcalls(loaded, iter(read_csv("rollcalls_sample.csv")))
    loaders.insert_rollcalls(loaded, iter(read_csv("rollcalls_sample.csv")))
    after = loaded.execute("SELECT COUNT(*) AS n FROM rollcalls").fetchone()["n"]
    assert before == after
    assert loaded.execute(
        "SELECT 1 FROM rollcalls WHERE congress=? AND rollnumber=?",
        (CONGRESS, VOTEVIEW_ROLLNUMBER),
    ).fetchone()


def test_upsert_refreshes_values_in_place(loaded) -> None:
    """A re-ingest must update the row, not leave stale values behind."""
    from clients import loaders

    loaded.execute(
        "UPDATE rollcalls SET yea_count = 0 WHERE congress=? AND rollnumber=?",
        (CONGRESS, VOTEVIEW_ROLLNUMBER),
    )
    loaded.commit()
    row = {
        "congress": str(CONGRESS),
        "chamber": "Senate",
        "rollnumber": str(VOTEVIEW_ROLLNUMBER),
        "session": str(SESSION),
        "clerk_rollnumber": str(SENATE_VOTE_NUMBER),
        "date": "2026-08-08",
        "yea_count": "52",
        "nay_count": "46",
    }
    loaders.insert_rollcalls(loaded, iter([row]))
    refreshed = loaded.execute(
        "SELECT yea_count FROM rollcalls WHERE congress=? AND rollnumber=?",
        (CONGRESS, VOTEVIEW_ROLLNUMBER),
    ).fetchone()["yea_count"]
    assert refreshed == 52
