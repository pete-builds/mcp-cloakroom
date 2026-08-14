"""The schedule tool: three differently-shaped feeds, one normalized response.

Docstring layout note: the return shape sits above ``Args:`` because FastMCP
discards everything from ``Args:`` down when building the tool description.
"""

from __future__ import annotations

from fastmcp import FastMCP

from clients.codes import provenance
from tools.common import ok, tool_guard

FEEDS = ("hearings", "members", "floor", "votes")


def register_schedule_tools(mcp: FastMCP, ctx) -> None:
    @mcp.tool()
    @tool_guard
    async def get_schedule(
        feed: str = "all", congress: int | None = None, session: int | None = None
    ) -> str:
        """Get the Senate's current schedule: hearings, floor days, and recent votes.

        Normalizes feeds that publish in three different shapes into one
        response: a flat meeting list, an attribute-keyed roster, and a nested
        convening calendar all come back with consistent ISO dates and field
        names.

        Returns JSON keyed by the feeds requested. `data.hearings[]` carries
        `committee`, `committee_code`, `type`, `date`, `time`, `day_of_week`,
        `room`, `matter`, `video_url`, and `placeholder` (true for status rows
        that are not actual meetings). `data.floor` carries `congress`,
        `session`, and `days[]` with `legislative_day`, `convene`, `adjourn`,
        `adjourn_type`, `next_convene`. `data.members[]` carries each current
        senator's `lis_member_id`, `bioguide_id`, name parts, `party`, `state`,
        `state_rank`, `office`, and `committees[]`. `data.votes[]` is the
        current session's roll call index with `vote_number`, `vote_date`,
        `congress_year`, `issue`, `question`, `result`, `yeas`, `nays`, `title`.

        Each requested feed that is disabled or unavailable comes back as an
        object with an `unavailable` key rather than being silently omitted.

        This is the one tool that always makes a network request. Responses use
        conditional GETs, so a repeat call within the refresh window normally
        costs a 304 with no body.

        Idempotent: yes, read-only.

        Example: get_schedule(feed="floor")

        Args:
            feed: Which feed: "all", "hearings", "floor", "members", or "votes".
                Defaults to "all".
            congress: Congress for the votes feed. Defaults to the configured
                current congress.
            session: Session for the votes feed, 1 or 2. Defaults to the
                configured current session.
        """
        want = FEEDS if feed == "all" else (feed,)
        for w in want:
            if w not in FEEDS:
                raise ValueError(f"feed must be 'all' or one of {', '.join(FEEDS)}; got {feed!r}")

        if ctx.senate is None:
            raise ValueError(
                "senate.gov feeds are disabled; set CLOAKROOM_SENATE_FEEDS to enable them"
            )

        data: dict = {}
        for name in want:
            if not ctx.settings.feed_enabled(name):
                data[name] = {"unavailable": f"the {name} feed is disabled by configuration"}
                continue
            if name == "hearings":
                data["hearings"] = await ctx.senate.hearings()
            elif name == "floor":
                data["floor"] = await ctx.senate.floor_schedule()
            elif name == "members":
                data["members"] = await ctx.senate.senators()
            elif name == "votes":
                data["votes"] = await ctx.senate.vote_menu(
                    congress or ctx.settings.cloakroom_current_congress,
                    session or ctx.settings.cloakroom_current_session,
                )
        return ok(data, provenance(senate_gov=True))
