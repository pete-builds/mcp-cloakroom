"""Coded values from the source data, and the citation every response carries.

Nothing here is guessed. The cast codes and party codes are transcribed from
Voteview's published codebooks (``voteview.com/articles/data_help_votes`` and
``/data_help_parties``, both read 2026-08-14), and the cast-code mapping was
additionally cross-validated against senate.gov's own roll call XML for the
119th Congress, 2nd session, vote 231: 100 senators, 100 agreements, zero
disagreements. See ``tests/test_codes.py``.
"""

from __future__ import annotations

from typing import Final

# Voteview cast_code. The three "Yea"-family and three "Nay"-family codes exist
# because the Senate historically recorded paired and announced positions, which
# are real expressions of position but not live votes on the floor. `position`
# collapses them for counting; `detail` preserves what the record actually said.
CAST_CODES: Final[dict[int, tuple[str, str]]] = {
    0: ("not_member", "Not a member of the chamber when this vote was taken"),
    1: ("Yea", "Yea"),
    2: ("Yea", "Paired Yea"),
    3: ("Yea", "Announced Yea"),
    4: ("Nay", "Announced Nay"),
    5: ("Nay", "Paired Nay"),
    6: ("Nay", "Nay"),
    7: ("Present", "Present"),
    8: ("Present", "Present"),
    9: ("Not Voting", "Not Voting (Abstention)"),
}

# Only these count as a position for or against. Used by every agreement,
# defection, and tally computation so the definition can never drift between
# tools.
YEA_CODES: Final[frozenset[int]] = frozenset({1, 2, 3})
NAY_CODES: Final[frozenset[int]] = frozenset({4, 5, 6})
DECIDING_CODES: Final[frozenset[int]] = YEA_CODES | NAY_CODES

PARTY_CODES: Final[dict[int, str]] = {
    1: "Federalist Party",
    13: "Democratic-Republican Party",
    22: "Adams Party",
    26: "Anti Masonic Party",
    29: "Whig Party",
    37: "Constitutional Unionist Party",
    44: "Nullifier Party",
    46: "States Rights Party",
    100: "Democratic Party",
    108: "Anti-Lecompton Democrats",
    112: "Conservative Party",
    114: "Readjuster Party",
    117: "Readjuster Democrats",
    200: "Republican Party",
    203: "Unconditional Unionist Party",
    206: "Unionist Party",
    208: "Liberal Republican Party",
    213: "Progressive Republican Party",
    300: "Free Soil Party",
    310: "American Party",
    326: "National Greenbacker Party",
    328: "Independent",
    329: "Independent Democrat",
    331: "Independent Republican",
    340: "Populist Party",
    347: "Prohibitionist Party",
    354: "Silver Republican Party",
    355: "Union Labor Party",
    356: "Union Labor Party",
    370: "Progressive Party",
    380: "Socialist Party",
    402: "Liberal Party",
    403: "Law and Order Party",
    522: "American Labor Party",
    537: "Farmer-Labor Party",
    555: "Jackson Party",
    603: "Independent Whig",
    1060: "Silver Party",
    1111: "Liberty Party",
    1275: "Anti-Jacksonians",
    1346: "Jackson Republican",
    3333: "Opposition Party",
    4000: "Anti-Administration Party",
    4444: "Constitutional Unionist Party",
    5000: "Pro-Administration Party",
    6000: "Crawford Federalist Party",
    7000: "Jackson Federalist Party",
    7777: "Crawford Republican Party",
    8000: "Adams-Clay Federalist Party",
    8888: "Adams-Clay Republican Party",
}

# Required by Voteview's terms. Attribution, not restriction — but it is not
# optional, so it is attached in code rather than left to a README nobody reads.
VOTEVIEW_CITATION: Final[str] = (
    "Lewis, Jeffrey B., Keith Poole, Howard Rosenthal, Adam Boche, Aaron Rudkin, "
    "and Luke Sonnet (2026). Voteview: Congressional Roll-Call Votes Database. "
    "https://voteview.com/"
)

LEGISLATORS_CITATION: Final[str] = (
    "unitedstates/congress-legislators (public domain, CC0). "
    "https://github.com/unitedstates/congress-legislators"
)

SENATE_GOV_CITATION: Final[str] = (
    "U.S. Senate roll call vote XML, https://www.senate.gov/ (public record)."
)


def cast_position(code: int | None) -> str:
    """Collapse a cast_code to Yea / Nay / Present / Not Voting / not_member."""
    if code is None:
        return "unknown"
    return CAST_CODES.get(int(code), ("unknown", "unknown"))[0]


def cast_detail(code: int | None) -> str:
    """The verbatim record wording for a cast_code (keeps Paired/Announced)."""
    if code is None:
        return "unknown"
    return CAST_CODES.get(int(code), ("unknown", "unknown"))[1]


def party_name(code: int | None) -> str:
    """Party name for an ICPSR party code, or a stable label if unmapped."""
    if code is None:
        return "unknown"
    return PARTY_CODES.get(int(code), f"Party code {int(code)}")


# Attached to every response that reports a NOMINATE-derived number.
#
# DW-NOMINATE is probably the most misread quantity in quantitative political
# science. It is routinely described as measuring "ideology" or "extremism",
# and the scores get read as statements about what legislators believe. They are
# not that. The estimates are recovered from roll call behaviour alone, and a
# member's position summarizes how they voted relative to everyone else, which
# is a different thing from what they think, intend, or campaigned on.
#
# This server surfaces the measure, states its limits, and stops there. Callers
# interpret; the tools do not.
NOMINATE_CAVEAT: Final[dict[str, str]] = {
    "what_it_is": (
        "DW-NOMINATE places each legislator at an estimated point in a "
        "low-dimensional space recovered from roll call voting behaviour. The "
        "first dimension typically tracks the dominant cleavage of its era, "
        "which for the modern era largely coincides with party."
    ),
    "what_it_is_not": (
        "It is not a measure of ideology, policy positions, beliefs, intent, or "
        "quality of representation, and it is not derived from anything a "
        "legislator said. It is estimated from votes and describes votes."
    ),
    "on_defection": (
        "A 'defection' here means a recorded vote inconsistent with the party's "
        "majority position or with the model's prediction. It is a statistical "
        "description, not a claim about motive, betrayal, party discipline, or "
        "principle. Agenda control, procedural strategy, pairing, local "
        "interests, and vote timing all produce the same signature."
    ),
    "on_uncertainty": (
        "Ideal points are estimates with error, not measurements. Where the "
        "source supplies a fit statistic it is reported alongside the estimate; "
        "a member with few votes or poor classification has a correspondingly "
        "unreliable position, and small differences between members are usually "
        "not meaningful."
    ),
    "selection_effect": (
        "Only recorded roll call votes exist in this data. Most legislative "
        "activity, including everything settled by voice vote, unanimous "
        "consent, or never brought to the floor, leaves no trace here."
    ),
}


def nominate_interpretation() -> dict:
    """The caveat block, returned by every tool that reports a NOMINATE figure."""
    return dict(NOMINATE_CAVEAT)


def fit_quality(
    geo_mean_probability: float | None,
    number_of_votes: int | None,
    number_of_errors: int | None,
) -> dict:
    """Report how well the model explains one member, without grading them.

    ``classification_error_rate`` is the share of the member's votes the model
    got wrong. ``geo_mean_probability`` is Voteview's own summary of fit. Both
    are reported as-is; a caller comparing two members should check these before
    treating a difference in ideal points as real.
    """
    error_rate = None
    if number_of_votes and number_of_errors is not None:
        error_rate = round(number_of_errors / number_of_votes, 4)
    return {
        "votes_used_by_model": number_of_votes,
        "classification_errors": number_of_errors,
        "classification_error_rate": error_rate,
        "geo_mean_probability": geo_mean_probability,
        "note": (
            "Estimated from this member's roll call record. Few votes or a high "
            "error rate means the ideal point is poorly identified and should "
            "not be compared closely against another member's."
        ),
    }


def provenance(*, senate_gov: bool = False, legislators: bool = False) -> dict:
    """The provenance block attached to every tool response.

    Voteview attribution is always present because every tool in this server
    reads Voteview-derived data.
    """
    sources = [{"name": "Voteview (UCLA)", "citation": VOTEVIEW_CITATION}]
    if legislators:
        sources.append(
            {"name": "unitedstates/congress-legislators", "citation": LEGISLATORS_CITATION}
        )
    if senate_gov:
        sources.append({"name": "senate.gov", "citation": SENATE_GOV_CITATION})
    return {"sources": sources}
