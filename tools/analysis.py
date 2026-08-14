"""The DW-NOMINATE analysis tools.

Docstring layout note: the return shape sits above ``Args:`` because FastMCP
discards everything from ``Args:`` down when building the tool description.

Every tool here returns a ``interpretation`` block stating what the measure does
and does not support, and cites Voteview in its provenance. That is not
boilerplate: ideal points are routinely read as claims about belief and intent,
and a tool that hands a model an unqualified number invites exactly that error.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import analysis, queries
from clients.codes import nominate_interpretation, provenance
from tools.common import clamp, ok, tool_guard


def register_analysis_tools(mcp: FastMCP, ctx) -> None:
    conn = ctx.conn

    @mcp.tool()
    @tool_guard
    async def find_defectors(
        congress: int,
        rollnumber: int | None = None,
        session: int | None = None,
        vote_number: int | None = None,
        min_votes: int = 20,
        limit: int = 25,
    ) -> str:
        """Find senators who voted against their party's majority position.

        Two modes. Give a specific vote (by `rollnumber`, or by `session` plus
        `vote_number`) to see who broke with their party on it. Give only
        `congress` to rank the chamber by how often each senator did so.

        Party defection is observable and needs no model. What DW-NOMINATE adds
        here is whether the defection was *predictable*: each defector carries
        `model_probability`, Voteview's estimated probability that they cast the
        vote recorded. A moderate crossing party lines is often a defection the
        model predicts easily, which is a different event from a defection it
        does not.

        Returns JSON. Single-vote mode gives `data.defector_count`,
        `data.defections_the_model_predicted`,
        `data.defections_the_model_did_not_predict`, `data.parties[]` (per
        party: `voting_members`, `yea`, `nay`, `majority_position`,
        `median_dim1`, `unanimous`), and `data.defectors[]` sorted
        least-predicted first, each with `name`, `state`, `party`, `position`,
        `party_majority_position`, `nominate_dim1`, `party_median_dim1`,
        `distance_from_party_median`, `side_of_party_median`
        (`toward_opposing_party` or `away_from_opposing_party`, geometry only),
        `model_probability`, `model_expected_this_vote`, and `fit`.

        Congress-wide mode gives `data.rollcalls_analyzed`,
        `data.members_ranked`, and `data.members[]` sorted by `defection_rate`,
        each also carrying `unpredicted_rate` and `fit`.

        Both modes include `data.definitions` and `data.interpretation`. Read
        `fit` before comparing two members: a senator with few votes or a high
        classification error rate has a poorly identified position.

        Idempotent: yes, read-only.

        Example: find_defectors(congress=119, session=2, vote_number=231)

        Args:
            congress: Congress number, e.g. 119. Required.
            rollnumber: Voteview roll call number, for single-vote mode.
            session: Session number, 1 or 2, paired with vote_number.
            vote_number: senate.gov vote number, paired with session.
            min_votes: Congress-wide mode only. Minimum eligible votes before a
                senator is ranked, which keeps partial tenures off the top of
                the list. Defaults to 20.
            limit: Congress-wide mode only. Maximum senators returned, 1-100.
                Defaults to 25.
        """
        single = rollnumber is not None or (session is not None and vote_number is not None)
        if single:
            roll = queries.resolve_rollnumber(
                conn, congress, rollnumber=rollnumber, session=session, vote_number=vote_number
            )
            row = queries.rollcall_row(conn, congress, roll)
            data = analysis.defectors_for_rollcall(conn, congress, roll)
            data["vote"] = queries.rollcall_summary(row)
        else:
            data = analysis.defection_rates(
                conn, congress, min_votes=max(1, min_votes), limit=clamp(limit, 1, 100)
            )
        data["interpretation"] = nominate_interpretation()
        return ok(data, provenance(legislators=True))

    @mcp.tool()
    @tool_guard
    async def find_unexpected_votes(
        congress: int,
        member: str | None = None,
        rollnumber: int | None = None,
        session: int | None = None,
        vote_number: int | None = None,
        max_probability: float = 0.5,
        limit: int = 25,
    ) -> str:
        """Find votes the DW-NOMINATE model did not predict.

        The model-relative counterpart to `find_defectors`. That tool asks who
        broke with their party, which is observable. This one asks which votes
        are hard to reconcile with a senator's own estimated position, whether
        or not a party line was crossed.

        The two frequently disagree, and the disagreement is the point. A
        centrist crossing over can be entirely predicted; a senator voting with
        their party can be the least predicted vote of the day.

        Scope it to one senator (`member`), one roll call (`rollnumber`, or
        `session` plus `vote_number`), or leave both off for the whole congress.

        Returns JSON with `data.votes_considered`, `data.unexpected_found`,
        `data.max_probability`, and `data.votes[]` sorted lowest-probability
        first. Each carries the roll call identity (`rollnumber`, `session`,
        `vote_number`, `date`, `bill_number`, `question`, `result`,
        `description`), the senator (`name`, `state`, `party`, `bioguide_id`,
        `nominate_dim1`), the `position` cast, `model_probability`, and `fit`.
        `data.definitions` and `data.interpretation` are always included.

        A low probability means the fitted model does not account for that vote.
        It is not evidence of inconsistency or a change of position: procedural
        votes, local interests, and plain model error all look the same here.

        Idempotent: yes, read-only.

        Example: find_unexpected_votes(congress=119, member="COLLINS", limit=5)

        Args:
            congress: Congress number, e.g. 119. Required.
            member: Senator as a name fragment, bioguide id, or ICPSR number.
                Defaults to every senator.
            rollnumber: Restrict to one Voteview roll call number.
            session: Session number, 1 or 2, paired with vote_number.
            vote_number: senate.gov vote number, paired with session.
            max_probability: Report votes below this model probability, 0-1.
                Defaults to 0.5, the point where the model favoured the other
                outcome.
            limit: Maximum votes to return, 1-200. Defaults to 25.
        """
        if not 0.0 < max_probability <= 1.0:
            raise ValueError("max_probability must be greater than 0 and at most 1")

        icpsr = None
        member_block = None
        if member:
            from tools.members import resolve_any

            member_block = resolve_any(conn, member)
            icpsr = member_block["icpsr"]

        roll = None
        if rollnumber is not None or (session is not None and vote_number is not None):
            roll = queries.resolve_rollnumber(
                conn, congress, rollnumber=rollnumber, session=session, vote_number=vote_number
            )

        data = analysis.unexpected_votes(
            conn,
            congress,
            icpsr=icpsr,
            rollnumber=roll,
            max_probability=max_probability,
            limit=clamp(limit, 1, 200),
        )
        if member_block:
            data["member"] = member_block
        data["interpretation"] = nominate_interpretation()
        return ok(data, provenance(legislators=True))
