"""Health check script for Docker HEALTHCHECK.

Wraps ``pete_mcp_core.healthcheck.check`` so the probe targets ``/healthz``,
never ``/mcp``.

A bare ``GET /mcp`` makes the MCP SDK create a transport session *before*
returning 406, and nothing reaps it: roughly 40 KB leaked per request. At a 30s
interval that is 2,880 probes a day, about 115 MiB/day of permanently leaked
memory on a server meant to run unattended for years. The HTTP verb is not a
workaround, because the session is created before method dispatch, so HEAD and
OPTIONS leak slightly worse than GET. Only staying off the ``/mcp`` mount avoids
it. ``/healthz`` is a plain custom route with no session cost.

503 is treated as alive on purpose. The server returns it when it is running
against a database that has not been populated yet, which happens when the
automatic load is disabled and ingest.py has not been run. That is a state the
operator has to resolve, not one a restart can fix, so failing here would only
produce a restart loop. (Note the first-run bulk load itself happens in the
entrypoint before the server binds, so during that window the probe gets a
connection error rather than a 503; the healthcheck's long start period is what
covers it.) 401 is alive for the same class of reason: with MCP_AUTH_REQUIRED
set, an unauthenticated probe is rejected by a server that is otherwise fine.
500 stays out, so a real fault still fails.

Env precedence for the port matches ``pete_mcp_core.serve`` exactly
(``FASTMCP_PORT`` > ``MCP_PORT`` > default) so the probe can never target a
different port than the server.
"""

from __future__ import annotations

import os
import sys

from pete_mcp_core.healthcheck import DEFAULT_HEALTHY_CODES, check

DEFAULT_PORT = 3728

# "Responds at all" == alive. See the module docstring before narrowing this.
HEALTHY_CODES = frozenset(DEFAULT_HEALTHY_CODES | {401, 503})


def main(default_port: int = DEFAULT_PORT) -> int:
    port_str = os.getenv("FASTMCP_PORT") or os.getenv("MCP_PORT") or str(default_port)
    path = os.getenv("MCP_HEALTH_PATH", "/healthz")
    try:
        port = int(port_str)
    except ValueError:
        return 1
    return check(port, path=path, healthy_codes=HEALTHY_CODES)


if __name__ == "__main__":
    sys.exit(main())
