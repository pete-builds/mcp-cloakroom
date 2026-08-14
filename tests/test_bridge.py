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


def test_clerk_index_rejects_a_duplicate_mapping(loaded) -> None:
    """The bridge index is UNIQUE, so an ambiguous mapping fails loudly.

    A second row claiming the same (congress, session, clerk_rollnumber) would
    make cross-referencing silently non-deterministic. The database refuses it.
    """
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        loaded.execute(
            "INSERT INTO rollcalls (congress, rollnumber, chamber, session, clerk_rollnumber) "
            "VALUES (?, ?, 'Senate', ?, ?)",
            (CONGRESS, 99999, SESSION, SENATE_VOTE_NUMBER),
        )
        loaded.commit()
