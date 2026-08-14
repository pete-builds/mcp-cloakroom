"""The ``GET /healthz`` route.

Not an MCP tool. An HTTP monitor polls status codes and has no way to call an
MCP tool, so this registers a plain Starlette route on FastMCP's app.

Deliberately cheap and local: it reports process liveness and how much data is
loaded, and never touches an upstream. A healthcheck that reaches a third party
turns someone else's outage into a restart loop, and at a 30s interval it would
also be thousands of unpaid requests a day against a public service.

Returns 503 while the database is still empty, which is the honest answer
during the first-run bulk ingest.
"""

from __future__ import annotations

import sqlite3

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse


def register_health_route(mcp: FastMCP, ctx, *, version: str) -> None:
    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(_request: Request) -> JSONResponse:
        body: dict[str, object] = {
            "status": "ok",
            "service": "mcp-cloakroom",
            "version": version,
            "senate_feeds_enabled": sorted(ctx.settings.enabled_feeds),
        }
        try:
            row = ctx.conn.execute(
                "SELECT (SELECT COUNT(*) FROM rollcalls) AS rollcalls, "
                "(SELECT COUNT(*) FROM votes) AS votes, "
                "(SELECT COUNT(*) FROM members) AS members"
            ).fetchone()
            body["rollcalls"] = row["rollcalls"]
            body["votes"] = row["votes"]
            body["members"] = row["members"]
            ingest = ctx.conn.execute(
                "SELECT value FROM meta WHERE key = 'last_ingest_completed'"
            ).fetchone()
            body["last_ingest_completed"] = ingest["value"] if ingest else None
            if not row["votes"]:
                body["status"] = "loading"
                body["detail"] = "bulk ingest has not completed yet"
                return JSONResponse(body, status_code=503)
        except sqlite3.Error as exc:
            body["status"] = "degraded"
            body["detail"] = f"database not readable: {exc}"
            return JSONResponse(body, status_code=503)
        return JSONResponse(body, status_code=200)
