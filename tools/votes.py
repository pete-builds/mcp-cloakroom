"""Roll call vote tools: the index, one full vote, and search.

Docstring layout note: FastMCP uses the prose *above* ``Args:`` as the tool
description and folds ``Args:`` into the parameter schema, discarding anything
below it. So the return shape and the example live in the prose, and ``Args:``
comes last. A ``Returns:`` section would never reach the model.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients import queries
from clients.codes import provenance
from tools.common import clamp, ok, tool_guard


def register_vote_tools(mcp: FastMCP, ctx) -> None:
    conn = ctx.conn

    @mcp.tool()
    @tool_guard
    async def list_votes(
        congress: int | None = None,
        session: int | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """List Senate roll call votes, newest first, with optional filters.

        Covers every Senate roll call from the 1st Congress (1789) to the
        present, sourced from Voteview's published bulk archive. The official
        senate.gov feeds only reach back to the 101st Congress.

        Returns JSON with `data.total_matching` (the full count before
        paging), `data.returned`, `data.offset`, and `data.votes[]`. Each vote
        carries `congress`, `rollnumber` (Voteview's continuous numbering within
        a Congress), `session`, `vote_number` (senate.gov's per-session
        numbering, null before the 101st Congress), `date`, `bill_number`,
        `question`, `result`, `description`, `yea_count`, and `nay_count`.

        Both numbering schemes are returned because they are different: in the
        119th Congress, `rollnumber` 890 and `vote_number` 231 are the same
        vote. Pass whichever you have to `get_vote`.

        Idempotent: yes, read-only.

        Example: list_votes(congress=119, session=2, limit=5)

        Args:
            congress: Congress number, e.g. 119. Defaults to all congresses.
            session: Session within the congress, 1 or 2. Defaults to both.
            start_date: Earliest vote date, ISO `YYYY-MM-DD`. Defaults to no bound.
            end_date: Latest vote date, ISO `YYYY-MM-DD`. Defaults to no bound.
            limit: Maximum votes to return, 1-500. Defaults to 50.
            offset: Rows to skip, for paging. Defaults to 0.
        """
        data = queries.list_votes(
            conn,
            congress=congress,
            session=session,
            start_date=start_date,
            end_date=end_date,
            limit=clamp(limit, 1, 500),
            offset=max(0, offset),
        )
        return ok(data, provenance())

    @mcp.tool()
    @tool_guard
    async def get_vote(
        congress: int,
        rollnumber: int | None = None,
        session: int | None = None,
        vote_number: int | None = None,
        include_positions: bool = True,
        include_senate_detail: bool = False,
    ) -> str:
        """Get one roll call vote in full, including how every senator voted.

        Address the vote either by Voteview `rollnumber`, or by the senate.gov
        pair `session` + `vote_number`. The two numbering schemes are bridged
        internally, so either works and both return the same vote.

        Returns JSON with `data.vote` (the summary fields, both numbering
        schemes, and the DW-NOMINATE cutting-line coordinates for the vote),
        `data.party_breakdown[]` with yea/nay/other counts per party, and, when
        `include_positions` is true, `data.positions[]` with one row per
        senator: `name`, `state`, `party`, `bioguide_id`, `icpsr`,
        `lis_member_id`, `position` ("Yea"/"Nay"/"Present"/"Not Voting"),
        `position_detail` (preserves "Paired Yea", "Announced Nay", which the
        collapsed `position` does not), and `nominate_dim1`.

        With `include_senate_detail` true, `data.senate_detail` adds the
        verbatim question text, vote title, majority requirement, amendment
        purpose, and tie-breaker fields. That is the only part of this tool that
        makes a network request, it covers the 101st Congress forward, and the
        result is cached permanently.

        Idempotent: yes, read-only.

        Example: get_vote(congress=119, session=2, vote_number=231)

        Args:
            congress: Congress number, e.g. 119. Required.
            rollnumber: Voteview roll call number, continuous within a congress.
            session: Session number, 1 or 2. Use with vote_number.
            vote_number: senate.gov vote number, resets each session. Use with session.
            include_positions: Include every senator's position. Defaults to True.
            include_senate_detail: Also fetch verbatim text from senate.gov.
                Defaults to False.
        """
        roll = queries.resolve_rollnumber(
            conn, congress, rollnumber=rollnumber, session=session, vote_number=vote_number
        )
        row = queries.rollcall_row(conn, congress, roll)
        vote = queries.rollcall_summary(row)
        vote["nominate_mid_1"] = row["nominate_mid_1"]
        vote["nominate_spread_1"] = row["nominate_spread_1"]

        data: dict = {"vote": vote}
        positions = queries.vote_positions(conn, congress, roll)
        data["party_breakdown"] = queries.party_breakdown(positions)
        if include_positions:
            data["positions"] = positions

        used_senate = False
        if include_senate_detail:
            sess = row["session"]
            vnum = row["clerk_rollnumber"]
            if sess is None or vnum is None:
                data["senate_detail"] = {
                    "unavailable": "senate.gov publishes roll call detail from the "
                    "101st Congress forward; this vote predates that."
                }
            elif ctx.senate is None or not ctx.settings.feed_enabled("votes"):
                # Checked here for a clear message, and enforced again inside
                # SenateGovClient so a future caller cannot skip it.
                data["senate_detail"] = {
                    "unavailable": "the senate.gov votes feed is disabled by "
                    "configuration; no request was made."
                }
            else:
                data["senate_detail"] = await ctx.senate.vote_detail(congress, sess, vnum)
                used_senate = True

        return ok(data, provenance(senate_gov=used_senate, legislators=True))

    @mcp.tool()
    @tool_guard
    async def find_votes(
        query: str | None = None,
        bill_number: str | None = None,
        question: str | None = None,
        result: str | None = None,
        congress: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> str:
        """Search Senate roll call votes by text, bill, question type, or result.

        Searches the full 1789-present archive. `query` matches across the vote
        description, bill number, and question. Bill numbers are matched
        loosely, so "S. 5271", "S.5271", and "S5271" all find the same votes.

        Returns the same shape as `list_votes`: `data.total_matching`,
        `data.returned`, `data.offset`, and `data.votes[]`.

        Idempotent: yes, read-only.

        Example: find_votes(query="judicial courts", congress=1)

        Args:
            query: Free-text fragment matched against description, bill, and question.
            bill_number: Bill identifier, e.g. "S. 5271". Punctuation and spacing ignored.
            question: Question-type fragment, e.g. "On Passage" or "On the Nomination".
            result: Result fragment, e.g. "Agreed to", "Rejected", "Confirmed".
            congress: Restrict to one congress. Defaults to all.
            limit: Maximum votes to return, 1-500. Defaults to 50.
            offset: Rows to skip, for paging. Defaults to 0.
        """
        if not any([query, bill_number, question, result, congress]):
            raise ValueError(
                "pass at least one of query, bill_number, question, result, or congress"
            )
        data = queries.find_votes(
            conn,
            query=query,
            bill_number=bill_number,
            question=question,
            result=result,
            congress=congress,
            limit=clamp(limit, 1, 500),
            offset=max(0, offset),
        )
        return ok(data, provenance())
