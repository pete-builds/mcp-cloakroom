"""mcp-cloakroom — Senate roll call votes over MCP.

FastMCP wiring only. Tool bodies live in ``tools/``; data access, HTTP, and
analysis live in ``clients/``.

The federal government publishes no Senate roll call vote API: Congress.gov
ships House votes only, and govinfo's bulk collections contain no votes.
senate.gov XML is the only official machine-readable source, covering the 101st
Congress forward, and Voteview is the only bulk historical archive, reaching
back to 1789. Third-party APIs such as LegiScan do serve Senate roll calls.

What this server adds is the layer above the record: DW-NOMINATE ideal points
joined to the votes, which is what `find_defectors` and `find_unexpected_votes`
are built on. Assembling that from published bulk data is the point; wrapping
the vote record alone is not.

No credentials are needed for any upstream, and none should ever be added.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from dotenv import load_dotenv
from fastmcp import FastMCP
from pete_mcp_core import build_auth_provider, configure_logging, run_server
from pydantic import ValidationError

from clients import db, loaders
from clients.config import DEFAULT_PORT, VERSION, CloakroomSettings
from clients.http_polite import PoliteFetcher, SqliteHttpCache
from clients.senate_gov import SenateGovClient
from tools.analysis import register_analysis_tools
from tools.health import register_health_route
from tools.members import register_member_tools
from tools.schedule import register_schedule_tools
from tools.votes import register_vote_tools

load_dotenv()

try:
    settings = CloakroomSettings()
except ValidationError as exc:
    print(f"FATAL: invalid configuration: {exc}", file=sys.stderr)
    sys.exit(1)

configure_logging(settings.log_level, settings.log_format)
log = logging.getLogger("mcp-cloakroom")


@dataclass
class Context:
    """What the tool modules need, assembled once."""

    settings: CloakroomSettings
    conn: object
    senate: SenateGovClient | None
    fetcher: PoliteFetcher | None


def build_context(s: CloakroomSettings) -> Context:
    conn = db.connect(s.cloakroom_db_path)
    db.init_schema(conn)

    if s.cloakroom_auto_ingest and not loaders.is_populated(conn):
        log.info(
            "Database is empty. Running first-time bulk ingest from published data. "
            "This downloads about 140 MB and usually takes a few minutes."
        )
        loaders.run_ingest(conn, version=VERSION, contact_url=s.cloakroom_contact_url)

    fetcher = senate = None
    if s.enabled_feeds:
        fetcher = PoliteFetcher(
            version=VERSION,
            min_interval=s.cloakroom_min_request_interval,
            timeout=s.cloakroom_http_timeout,
            cache=SqliteHttpCache(conn),
            contact_url=s.cloakroom_contact_url,
            user_agent_override=s.cloakroom_user_agent,
            refresh_seconds=s.cloakroom_refresh_hours * 3600.0,
        )
        senate = SenateGovClient(
            fetcher,
            conn,
            current_congress=s.cloakroom_current_congress,
            current_session=s.cloakroom_current_session,
            # The toggle is enforced inside the client, so every caller is
            # covered rather than only the ones that remember to check.
            enabled_feeds=s.enabled_feeds,
        )
    else:
        log.info("All senate.gov feeds disabled; serving entirely from bulk data.")

    return Context(settings=s, conn=conn, senate=senate, fetcher=fetcher)


def build_app(s: CloakroomSettings | None = None) -> tuple[FastMCP, Context]:
    """Construct the server. Every side effect in this module happens here.

    Deliberately a function rather than module-level code. Building the context
    opens the database and, on an empty one, runs the full bulk ingest, so doing
    it at import time meant that merely importing this module downloaded ~140 MB
    from voteview.com and wrote a database into whatever directory the importer
    happened to be in. A test that imported the module for introspection paid
    that cost, which is how it went unnoticed: the import-smoke step set
    CLOAKROOM_AUTO_INGEST=false, so the one place anybody looked was the one
    place it could not happen.

    Importing this module must stay free of I/O. ``tests/test_import_purity.py``
    enforces that.
    """
    ctx = build_context(s or settings)

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            if ctx.fetcher is not None:
                await ctx.fetcher.close()

    mcp = FastMCP(
        "Cloakroom",
        lifespan=lifespan,
        auth=build_auth_provider(
            ctx.settings.auth_token,
            client_id="cloakroom",
            required=ctx.settings.auth_required,
            logger=log,
        ),
    )

    register_vote_tools(mcp, ctx)
    register_member_tools(mcp, ctx)
    register_analysis_tools(mcp, ctx)
    register_schedule_tools(mcp, ctx)
    # Plain HTTP, not a tool: an uptime monitor polls status codes.
    register_health_route(mcp, ctx, version=VERSION)
    return mcp, ctx


def main() -> None:
    # default_host is "0.0.0.0" on purpose. MCP SDK 2.0's server defaults to
    # loopback, which serves CI and localhost perfectly while returning 421 to
    # every client on the network. Binding explicitly here, and pinning it in
    # tests/test_config.py, is what keeps that failure from shipping silently.
    mcp, _ = build_app()
    run_server(
        mcp,
        default_port=DEFAULT_PORT,
        default_transport="streamable-http",
        default_host="0.0.0.0",
    )


if __name__ == "__main__":
    main()
