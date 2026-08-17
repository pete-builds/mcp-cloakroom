"""The tool surface as a public contract: envelopes, docstrings, and names.

These assertions are about the shape callers depend on, not about the data.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

import pytest
from fastmcp import FastMCP

from clients.config import CloakroomSettings
from tools.analysis import register_analysis_tools
from tools.common import fail, ok, tool_guard
from tools.members import register_member_tools
from tools.schedule import register_schedule_tools
from tools.votes import register_vote_tools

EXPECTED_TOOLS = {
    "list_votes",
    "get_vote",
    "find_votes",
    "get_member_votes",
    "compare_members",
    "find_defectors",
    "find_unexpected_votes",
    "get_schedule",
}

ERROR_CODES = {
    "UPSTREAM_DOWN",
    "AUTH_FAILED",
    "INVALID_INPUT",
    "NOT_FOUND",
    "RATE_LIMITED",
    "INTERNAL",
}


@dataclass
class Ctx:
    settings: CloakroomSettings
    conn: object
    senate: object | None = None
    fetcher: object | None = None


@pytest.fixture
def mcp_and_ctx(loaded):
    settings = CloakroomSettings(_env_file=None)
    ctx = Ctx(settings=settings, conn=loaded)
    mcp = FastMCP("CloakroomTest")
    register_vote_tools(mcp, ctx)
    register_member_tools(mcp, ctx)
    register_analysis_tools(mcp, ctx)
    register_schedule_tools(mcp, ctx)
    return mcp, ctx


def _tools(mcp: FastMCP) -> dict:
    """Name -> FunctionTool, via the same listing a real client receives."""
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_the_expected_tool_set_is_registered(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    assert set(_tools(mcp)) == EXPECTED_TOOLS


def test_tool_count_stays_within_the_agreed_surface(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    assert 6 <= len(_tools(mcp)) <= 8


def test_every_tool_is_verb_named(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    verbs = ("list_", "get_", "find_", "compare_")
    for name in _tools(mcp):
        assert name.startswith(verbs), f"{name} does not read as an action"


def test_return_shape_is_documented_above_args(mcp_and_ctx) -> None:
    """FastMCP discards everything from ``Args:`` down.

    A ``Returns:`` section, or a shape described below ``Args:``, never reaches
    the model. The description the client actually receives must carry it.
    """
    mcp, _ = mcp_and_ctx
    for name, tool in _tools(mcp).items():
        desc = tool.description or ""
        assert "Returns JSON" in desc or "Returns the same shape" in desc, (
            f"{name} does not document its return shape in the visible description"
        )
        assert "Returns:" not in desc, f"{name} uses a Returns: section, which is truncated away"
        assert "Idempotent:" in desc, f"{name} does not state idempotency"
        assert "Example:" in desc, f"{name} has no example invocation"


def test_docstring_prose_precedes_args_in_source() -> None:
    """Structural check on the source, independent of FastMCP's parsing."""
    from pathlib import Path

    for path in Path(__file__).parent.parent.joinpath("tools").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for doc in re.findall(r'"""(.*?)"""', text, re.S):
            if "Args:" not in doc or "Returns JSON" not in doc:
                continue
            assert doc.index("Returns JSON") < doc.index("Args:"), (
                f"{path.name}: return shape documented below Args:, where it is discarded"
            )


def test_success_envelope_shape() -> None:
    payload = json.loads(ok({"x": 1}))
    assert set(payload) == {"data"}
    assert payload["data"] == {"x": 1}


def test_success_envelope_carries_provenance_when_given() -> None:
    payload = json.loads(ok({"x": 1}, {"sources": [{"name": "Voteview"}]}))
    assert payload["provenance"]["sources"][0]["name"] == "Voteview"


def test_failure_envelope_shape() -> None:
    payload = json.loads(fail("broke", "INVALID_INPUT", {"k": "v"}))
    assert payload["error"] == "broke"
    assert payload["code"] == "INVALID_INPUT"
    assert payload["details"] == {"k": "v"}


def test_tool_guard_converts_every_exception_class() -> None:
    """No exception may ever escape to the client."""
    from clients.http_polite import PoliteError
    from clients.queries import NotFound

    cases = [
        (PoliteError("down", "UPSTREAM_DOWN"), "UPSTREAM_DOWN"),
        (NotFound("gone"), "NOT_FOUND"),
        (ValueError("bad"), "INVALID_INPUT"),
        (RuntimeError("boom"), "INTERNAL"),
    ]
    for exc, expected in cases:

        @tool_guard
        async def boom(_e=exc):
            raise _e

        payload = json.loads(asyncio.run(boom()))
        assert payload["code"] == expected
        assert payload["code"] in ERROR_CODES
        assert "error" in payload


def test_tools_return_json_strings(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    out = asyncio.run(tools["list_votes"].fn(congress=119, limit=1))
    assert isinstance(out, str)
    payload = json.loads(out)
    assert "data" in payload
    assert payload["provenance"]["sources"][0]["citation"].startswith("Lewis")


def test_get_vote_accepts_both_addressing_schemes(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    by_roll = json.loads(asyncio.run(tools["get_vote"].fn(congress=119, rollnumber=890)))
    by_senate = json.loads(
        asyncio.run(tools["get_vote"].fn(congress=119, session=2, vote_number=231))
    )
    assert by_roll["data"]["vote"]["rollnumber"] == by_senate["data"]["vote"]["rollnumber"]
    assert len(by_roll["data"]["positions"]) == 100


def test_get_vote_can_omit_positions(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(
        asyncio.run(
            _tools(mcp)["get_vote"].fn(congress=119, rollnumber=890, include_positions=False)
        )
    )
    assert "positions" not in payload["data"]
    assert payload["data"]["party_breakdown"]


def test_find_votes_requires_a_filter(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(asyncio.run(_tools(mcp)["find_votes"].fn()))
    assert payload["code"] == "INVALID_INPUT"


def test_unknown_vote_returns_not_found_not_an_exception(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(asyncio.run(_tools(mcp)["get_vote"].fn(congress=119, rollnumber=7777)))
    assert payload["code"] == "NOT_FOUND"


def test_limits_are_clamped_not_rejected(mcp_and_ctx) -> None:
    """An oversized limit should degrade to the ceiling, not error."""
    mcp, _ = mcp_and_ctx
    payload = json.loads(asyncio.run(_tools(mcp)["list_votes"].fn(limit=100_000)))
    assert "data" in payload


def test_schedule_refuses_an_unknown_feed(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(asyncio.run(_tools(mcp)["get_schedule"].fn(feed="nonsense")))
    assert payload["code"] == "INVALID_INPUT"


def test_schedule_reports_disabled_feeds_clearly(loaded) -> None:
    settings = CloakroomSettings(_env_file=None, CLOAKROOM_SENATE_FEEDS="")
    ctx = Ctx(settings=settings, conn=loaded, senate=None)
    mcp = FastMCP("CloakroomTest")
    register_schedule_tools(mcp, ctx)
    payload = json.loads(asyncio.run(_tools(mcp)["get_schedule"].fn()))
    assert payload["code"] == "INVALID_INPUT"
    assert "disabled" in payload["error"]


def test_compare_members_rejects_comparing_a_senator_to_themselves(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(
        asyncio.run(_tools(mcp)["compare_members"].fn(member_a="S000033", member_b="S000033"))
    )
    assert payload["code"] == "INVALID_INPUT"


def test_find_defectors_works_in_both_modes(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    single = json.loads(asyncio.run(tools["find_defectors"].fn(congress=119, rollnumber=679)))
    assert "defectors" in single["data"]
    assert "vote" in single["data"]
    wide = json.loads(asyncio.run(tools["find_defectors"].fn(congress=119, min_votes=1)))
    assert "members" in wide["data"]


ANALYSIS_TOOLS = ["find_defectors", "find_unexpected_votes", "compare_members"]


@pytest.mark.parametrize("name", ANALYSIS_TOOLS)
def test_every_analysis_tool_returns_the_nominate_caveat(mcp_and_ctx, name: str) -> None:
    """A bare ideal point invites exactly the misreading this block prevents."""
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    if name == "compare_members":
        out = asyncio.run(tools[name].fn(member_a="S000033", member_b="K000383"))
    else:
        out = asyncio.run(tools[name].fn(congress=119))
    payload = json.loads(out)
    interp = payload["data"]["interpretation"]
    assert set(interp) >= {
        "what_it_is",
        "what_it_is_not",
        "on_defection",
        "on_uncertainty",
        "selection_effect",
    }
    assert "not a measure of ideology" in interp["what_it_is_not"]
    assert "not a claim about motive" in interp["on_defection"]


@pytest.mark.parametrize("name", ANALYSIS_TOOLS)
def test_every_analysis_tool_cites_voteview(mcp_and_ctx, name: str) -> None:
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    if name == "compare_members":
        out = asyncio.run(tools[name].fn(member_a="S000033", member_b="K000383"))
    else:
        out = asyncio.run(tools[name].fn(congress=119))
    citations = " ".join(s["citation"] for s in json.loads(out)["provenance"]["sources"])
    assert "Lewis" in citations and "Voteview" in citations


def test_analysis_output_carries_no_editorial_language(mcp_and_ctx) -> None:
    """Report the measure; do not characterize the legislator.

    Scoped to the per-legislator records, not to the `definitions` and
    `interpretation` blocks. Those blocks legitimately contain words like
    "betrayal" precisely in order to disclaim them, and scanning them would
    punish the honesty this test exists to protect.
    """
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    banned = [
        "betray",
        "disloyal",
        "rebel",
        "maverick",
        "extremist",
        "extreme",
        "moderate_crossover",
        "crossover_moderate",
        "against_own_wing",
        "surprise",
        "surprising",
        "shocking",
        "principled",
        "courageous",
        "loyal",
        "rogue",
        "traitor",
    ]

    records: list[dict] = []
    single = json.loads(asyncio.run(tools["find_defectors"].fn(congress=119, rollnumber=679)))
    records += single["data"]["defectors"] + single["data"]["parties"]
    wide = json.loads(asyncio.run(tools["find_defectors"].fn(congress=119, min_votes=1)))
    records += wide["data"]["members"]
    unexpected = json.loads(asyncio.run(tools["find_unexpected_votes"].fn(congress=119)))
    records += unexpected["data"]["votes"]
    assert records, "nothing was scanned; the test proved nothing"
    assert single["data"]["defectors"], (
        "the fixture roll call has no defectors, so the scan would be vacuous"
    )
    assert unexpected["data"]["votes"], "no unexpected votes to scan"

    blob = json.dumps(records).lower()
    for word in banned:
        assert word not in blob, f"editorial term {word!r} reached a per-legislator record"


def test_analysis_keys_are_descriptive_not_evaluative(mcp_and_ctx) -> None:
    """Field names carry judgement too, and they are harder to notice."""
    mcp, _ = mcp_and_ctx
    payload = json.loads(
        asyncio.run(_tools(mcp)["find_defectors"].fn(congress=119, rollnumber=679))
    )
    expected = {
        "position",
        "party_majority_position",
        "distance_from_party_median",
        "side_of_party_median",
        "model_probability",
        "model_expected_this_vote",
    }
    assert payload["data"]["defectors"], "fixture must contain defectors"
    for defector in payload["data"]["defectors"]:
        assert expected <= set(defector)
        assert "surprise" not in defector
        assert "defection_type" not in defector


def test_find_unexpected_votes_is_distinct_from_defection(mcp_and_ctx) -> None:
    """The two tools answer different questions and must not be aliases."""
    mcp, _ = mcp_and_ctx
    tools = _tools(mcp)
    unexpected = json.loads(asyncio.run(tools["find_unexpected_votes"].fn(congress=119)))
    assert "votes_considered" in unexpected["data"]
    assert "max_probability" in unexpected["data"]
    for v in unexpected["data"]["votes"]:
        assert v["model_probability"] < 0.5
        assert "fit" in v


def test_find_unexpected_votes_rejects_an_impossible_threshold(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    for bad in (0.0, -0.1, 1.5):
        payload = json.loads(
            asyncio.run(_tools(mcp)["find_unexpected_votes"].fn(congress=119, max_probability=bad))
        )
        assert payload["code"] == "INVALID_INPUT"


def test_find_unexpected_votes_can_scope_to_one_member(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(
        asyncio.run(_tools(mcp)["find_unexpected_votes"].fn(congress=119, member="S000033"))
    )
    assert payload["data"]["member"]["bioguide_id"] == "S000033"
    for v in payload["data"]["votes"]:
        assert v["bioguide_id"] == "S000033"


def test_compare_members_reports_ideological_context(mcp_and_ctx) -> None:
    mcp, _ = mcp_and_ctx
    payload = json.loads(
        asyncio.run(_tools(mcp)["compare_members"].fn(member_a="S000033", member_b="K000383"))
    )
    ctxb = payload["data"]["ideological_context"]
    assert "first_dimension_distance" in ctxb
    assert "same_party" in ctxb
    # Context, never a prediction dressed up as a statistic.
    assert "not as a prediction" in ctxb["note"]
    assert "expected_agreement_rate" not in ctxb


# --------------------------------------------- partial address rejection


@pytest.mark.parametrize("tool_name", ["find_defectors", "find_unexpected_votes"])
@pytest.mark.parametrize("kwargs", [{"session": 2}, {"vote_number": 231}])
def test_analysis_tools_reject_half_a_senate_address(mcp_and_ctx, tool_name, kwargs) -> None:
    """session and vote_number only mean anything together.

    Passing one alone used to be dropped silently, so
    find_defectors(congress=119, session=2) returned whole-congress rankings
    while looking like it had answered a question about session 2. get_vote
    already rejected this; these now agree with it.
    """
    mcp, _ = mcp_and_ctx
    payload = json.loads(asyncio.run(_tools(mcp)[tool_name].fn(congress=119, **kwargs)))
    assert payload["code"] == "INVALID_INPUT"
    assert "session" in payload["error"] and "vote_number" in payload["error"]


@pytest.mark.parametrize("tool_name", ["find_defectors", "find_unexpected_votes"])
def test_analysis_tools_still_accept_a_complete_or_absent_address(mcp_and_ctx, tool_name) -> None:
    """Positive control: the guard must not reject valid calls."""
    mcp, _ = mcp_and_ctx
    both = json.loads(
        asyncio.run(_tools(mcp)[tool_name].fn(congress=119, session=2, vote_number=231))
    )
    assert "data" in both
    neither = json.loads(asyncio.run(_tools(mcp)[tool_name].fn(congress=119)))
    assert "data" in neither
