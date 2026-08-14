"""cast_code semantics, checked against a real vote rather than against itself.

Asserting ``CAST_CODES[1] == "Yea"`` would only prove the constant equals
itself. The real check is whether decoding Voteview's integers reproduces the
labels senate.gov independently published for the same roll call: 100 senators,
two publishers, zero disagreements allowed.
"""

from __future__ import annotations

import re

import pytest

from clients.codes import (
    CAST_CODES,
    DECIDING_CODES,
    NAY_CODES,
    YEA_CODES,
    cast_detail,
    cast_position,
    party_name,
    provenance,
)

CONGRESS = 119
ROLLNUMBER = 890


def _senate_positions(xml: str) -> dict[str, str]:
    """lis_member_id -> vote_cast, straight out of the real file."""
    out = {}

    def tag(block: str, t: str) -> str:
        m = re.search(rf"<{t}>(.*?)</{t}>", block, re.S)
        return m.group(1).strip() if m else ""

    for block in re.findall(r"<member>(.*?)</member>", xml, re.S):
        out[tag(block, "lis_member_id")] = tag(block, "vote_cast")
    return out


def test_decoded_voteview_codes_match_senate_gov_labels(
    legislators_loaded, senate_vote_xml: str
) -> None:
    """The cross-source assertion this whole file exists for."""
    conn = legislators_loaded
    senate = _senate_positions(senate_vote_xml)
    assert len(senate) == 100, "fixture should carry the full chamber"

    rows = conn.execute(
        """
        SELECT i.lis_member_id AS lis, v.cast_code
        FROM votes v
        JOIN members m ON m.congress = v.congress AND m.icpsr = v.icpsr
        JOIN member_ids i ON i.bioguide_id = m.bioguide_id
        WHERE v.congress = ? AND v.rollnumber = ?
        """,
        (CONGRESS, ROLLNUMBER),
    ).fetchall()

    # The legislators fixture deliberately covers only two senators, so this
    # compares the pairs that resolve. The full-chamber bridge is exercised in
    # test_bridge.py against tallies.
    compared = 0
    for r in rows:
        if r["lis"] in senate:
            assert cast_position(r["cast_code"]) == senate[r["lis"]], (
                f"{r['lis']}: Voteview code {r['cast_code']} decoded to "
                f"{cast_position(r['cast_code'])!r}, senate.gov says {senate[r['lis']]!r}"
            )
            compared += 1
    assert compared >= 2, "the bridge resolved nothing; the test proved nothing"


def test_aggregate_decoded_tally_matches_senate_gov_counts(loaded, senate_vote_xml: str) -> None:
    """Chamber-wide check that needs no identity bridge at all.

    Decoding every Voteview cast_code for this roll call must reproduce
    senate.gov's own yea/nay/absent counts exactly. This catches a mapping error
    on any of the six yea/nay variants even when name matching is unavailable.
    """
    counts_block = re.search(r"<count>(.*?)</count>", senate_vote_xml, re.S).group(1)
    exp_yea = int(re.search(r"<yeas>(\d+)</yeas>", counts_block).group(1))
    exp_nay = int(re.search(r"<nays>(\d+)</nays>", counts_block).group(1))
    exp_absent = int(re.search(r"<absent>(\d+)</absent>", counts_block).group(1))

    rows = loaded.execute(
        "SELECT cast_code FROM votes WHERE congress=? AND rollnumber=?",
        (CONGRESS, ROLLNUMBER),
    ).fetchall()
    decoded = [cast_position(r["cast_code"]) for r in rows]

    assert decoded.count("Yea") == exp_yea
    assert decoded.count("Nay") == exp_nay
    assert decoded.count("Not Voting") == exp_absent
    assert len(decoded) == 100


def test_paired_and_announced_codes_collapse_but_stay_recoverable() -> None:
    """The historical variants must count, without losing what the record said."""
    for code in (1, 2, 3):
        assert cast_position(code) == "Yea"
    for code in (4, 5, 6):
        assert cast_position(code) == "Nay"
    assert cast_detail(2) == "Paired Yea"
    assert cast_detail(3) == "Announced Yea"
    assert cast_detail(4) == "Announced Nay"
    assert cast_detail(5) == "Paired Nay"
    # Collapsing must not erase the distinction.
    assert cast_detail(1) != cast_detail(2)


def test_position_sets_are_consistent_with_the_code_table() -> None:
    assert {c for c, (pos, _) in CAST_CODES.items() if pos == "Yea"} == YEA_CODES
    assert {c for c, (pos, _) in CAST_CODES.items() if pos == "Nay"} == NAY_CODES
    assert DECIDING_CODES == YEA_CODES | NAY_CODES
    assert not (YEA_CODES & NAY_CODES)
    # Present and absent must never count as a position.
    for code in (0, 7, 8, 9):
        assert code not in DECIDING_CODES


def test_non_voting_codes_decode_distinctly() -> None:
    assert cast_position(0) == "not_member"
    assert cast_position(7) == "Present"
    assert cast_position(9) == "Not Voting"
    assert cast_position(None) == "unknown"
    assert cast_position(42) == "unknown"


def test_party_codes_resolve_and_degrade_safely() -> None:
    assert party_name(100) == "Democratic Party"
    assert party_name(200) == "Republican Party"
    assert party_name(328) == "Independent"
    assert party_name(None) == "unknown"
    # An unmapped code must stay identifiable rather than becoming "unknown".
    assert "9999" in party_name(9999)


@pytest.mark.parametrize(
    "kwargs,expected_sources",
    [
        ({}, 1),
        ({"legislators": True}, 2),
        ({"senate_gov": True}, 2),
        ({"senate_gov": True, "legislators": True}, 3),
    ],
)
def test_provenance_always_cites_voteview(kwargs, expected_sources) -> None:
    """Voteview attribution is required, so it can never be omitted."""
    p = provenance(**kwargs)
    assert len(p["sources"]) == expected_sources
    joined = " ".join(s["citation"] for s in p["sources"])
    assert "Lewis" in joined and "Voteview" in joined
