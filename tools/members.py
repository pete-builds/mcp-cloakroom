"""Senator-centric tools: one member's record, and pairwise comparison.

Docstring layout note: the return shape sits above ``Args:`` because FastMCP
discards everything from ``Args:`` down when building the tool description.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import queries
from clients.codes import nominate_interpretation, provenance
from tools.common import clamp, ok, tool_guard


def register_member_tools(mcp: FastMCP, ctx) -> None:
    conn = ctx.conn

    @mcp.tool()
    @tool_guard
    async def get_member_votes(
        name: str | None = None,
        bioguide_id: str | None = None,
        icpsr: int | None = None,
        congress: int | None = None,
        position: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> str:
        """Get one senator's voting record, across a career or one congress.

        Identify the senator by `name` (a fragment, matched case-insensitively
        against Voteview's "LAST, First Middle" form), or precisely by
        `bioguide_id` or `icpsr`. An ambiguous name returns an INVALID_INPUT
        error listing the candidates rather than silently picking one.

        Returns JSON with `data.member` (`name`, `state`, `party`,
        `bioguide_id`, `icpsr`, `lis_member_id`, `nominate_dim1` and
        `nominate_dim2` ideal points, `congresses` served, `is_current`) and
        `data.record` with `total_matching`, `returned`, `offset`, and
        `votes[]`. Each vote carries the roll call summary plus this member's
        `position` and `position_detail`.

        Idempotent: yes, read-only.

        Example: get_member_votes(name="SANDERS", congress=119, limit=10)

        Args:
            name: Surname or name fragment, e.g. "SANDERS" or "Warren".
            bioguide_id: Exact Biographical Directory id, e.g. "S000033".
            icpsr: Exact Voteview/ICPSR member number, e.g. 29147.
            congress: Restrict to one congress. Defaults to the whole career.
            position: Filter to "yea" or "nay" only. Defaults to all positions.
            limit: Maximum votes to return, 1-500. Defaults to 100.
            offset: Rows to skip, for paging. Defaults to 0.
        """
        member = queries.resolve_member(conn, bioguide_id=bioguide_id, icpsr=icpsr, name=name)
        record = queries.member_votes(
            conn,
            member["icpsr"],
            congress=congress,
            position=position,
            limit=clamp(limit, 1, 500),
            offset=max(0, offset),
        )
        return ok({"member": member, "record": record}, provenance(legislators=True))

    @mcp.tool()
    @tool_guard
    async def compare_members(
        member_a: str,
        member_b: str,
        congress: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        """Compare two senators: how often they voted the same way.

        Each member is a name fragment, a bioguide id, or an ICPSR number; the
        form is detected automatically. Only roll calls where *both* cast a
        Yea or Nay count toward the rate. Votes where either was absent, voted
        Present, or was paired without a direction are excluded and reported
        separately, so the denominator is never ambiguous.

        Returns JSON with `data.member_a` and `data.member_b` (each the member
        block from `get_member_votes`), `data.comparison` with
        `votes_compared`, `agreements`, `disagreements`, `agreement_rate` (0-1,
        null when they never voted together), `excluded_no_position`, and up to
        25 `sample_disagreements[]` showing each side's position, plus
        `data.ideological_context` with `same_party`, each member's
        `nominate_dim1`, `first_dimension_distance`, and
        `second_dimension_distance`.

        The distance is context for the rate, not a prediction of it. Same-party
        pairs agree at high rates almost regardless of distance, so 85% within a
        party and 85% across the aisle are very different findings.
        `data.interpretation` states the limits of the measure.

        Idempotent: yes, read-only.

        Example: compare_members(member_a="COLLINS", member_b="MURKOWSKI", congress=119)

        Args:
            member_a: First senator: name fragment, bioguide id, or ICPSR number.
            member_b: Second senator, same forms as member_a.
            congress: Restrict to one congress. Defaults to every congress they overlap.
            start_date: Earliest vote date, ISO `YYYY-MM-DD`. Defaults to no bound.
            end_date: Latest vote date, ISO `YYYY-MM-DD`. Defaults to no bound.
        """
        a = resolve_any(conn, member_a)
        b = resolve_any(conn, member_b)
        if a["icpsr"] == b["icpsr"]:
            raise ValueError("member_a and member_b resolved to the same senator")
        comparison = queries.compare(
            conn,
            a["icpsr"],
            b["icpsr"],
            congress=congress,
            start_date=start_date,
            end_date=end_date,
        )
        return ok(
            {
                "member_a": a,
                "member_b": b,
                "comparison": comparison,
                "ideological_context": queries.ideological_context(
                    a, b, comparison["agreement_rate"]
                ),
                "interpretation": nominate_interpretation(),
            },
            provenance(legislators=True),
        )


def resolve_any(conn, token: str) -> dict:
    """Accept a bioguide id, an ICPSR number, or a name in one argument.

    Bioguide ids are a letter followed by six digits, which cannot collide with
    either an all-digit ICPSR number or a real surname.
    """
    t = token.strip()
    if len(t) == 7 and t[0].isalpha() and t[1:].isdigit():
        return queries.resolve_member(conn, bioguide_id=t.upper())
    if t.isdigit():
        return queries.resolve_member(conn, icpsr=int(t))
    return queries.resolve_member(conn, name=t)
