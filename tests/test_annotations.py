"""Every tool declares itself read-only, and that claim is checked.

Eight tools reporting how members of Congress voted, and not one writes
anything anywhere. That is worth declaring rather than leaving to be inferred:
an unannotated read-only server and an unannotated server full of delete tools
are indistinguishable in the manifest, so a client trying to be careful has to
be careful about everything -- which in practice means being careful about
nothing.

This registers the tools onto a bare FastMCP with a stand-in context rather
than calling build_app(). build_app opens the database and, on an empty one,
runs a ~140 MB bulk ingest, which tests/test_import_purity.py exists to keep
out of import paths. Registration only stashes ctx for the tool bodies to use
later, so nothing here needs a real connection: the manifest is built from the
decorators, and no tool is invoked.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from tools.analysis import register_analysis_tools
from tools.members import register_member_tools
from tools.schedule import register_schedule_tools
from tools.votes import register_vote_tools

EXPECTED = {
    "get_member_votes", "compare_members", "find_defectors",
    "find_unexpected_votes", "get_schedule", "list_votes", "get_vote",
    "find_votes",
}


@pytest.fixture(scope="module")
def tools():
    """The live manifest, not the source. What a client would receive."""
    mcp = FastMCP("Cloakroom-test")
    ctx = SimpleNamespace(conn=None, fetcher=None, settings=None)
    for register in (register_vote_tools, register_member_tools,
                     register_analysis_tools, register_schedule_tools):
        register(mcp, ctx)
    return {t.name: t for t in asyncio.run(mcp.list_tools())}


def test_the_expected_eight_are_present(tools):
    """Guards the guard: an empty manifest would pass everything below."""
    assert set(tools) == EXPECTED


def test_every_tool_is_annotated(tools):
    assert sorted(n for n, t in tools.items() if t.annotations is None) == []


def test_every_tool_is_read_only(tools):
    """The whole surface. A write tool added later fails here first.

    The failure is a prompt to classify the new tool deliberately, not an
    obstacle to adding one.
    """
    assert sorted(n for n, t in tools.items() if not t.annotations.readOnlyHint) == []


def test_nothing_claims_to_be_destructive(tools):
    assert sorted(n for n, t in tools.items() if t.annotations.destructiveHint) == []


def test_every_tool_is_open_world_and_idempotent(tools):
    """Both at once, deliberately.

    An answer can change between two identical calls because a new vote was
    recorded upstream, which is a different thing from the call having changed
    something.
    """
    assert sorted(n for n, t in tools.items() if not t.annotations.openWorldHint) == []
    assert sorted(n for n, t in tools.items() if not t.annotations.idempotentHint) == []
