"""Standard Error Contract helpers shared by every tool module."""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pete_mcp_core import format_response

from clients.http_polite import PoliteError
from clients.queries import NotFound

log = logging.getLogger("mcp-cloakroom.tools")

# --- Tool annotations ---
# This server is entirely read-only: eight tools reporting how members of
# Congress voted, and not one writes anything anywhere. That is worth
# DECLARING rather than leaving to be inferred, because an unannotated
# read-only server and an unannotated server full of delete tools are
# indistinguishable in the manifest. Saying "these eight are safe" is what
# makes "that one is not", elsewhere in the fleet, mean something.
#
# openWorldHint is True: the tools read a public upstream, so an answer can
# change between two identical calls because a new vote was recorded, not
# because the call changed anything.

#: Reads only. Safe to repeat, safe to call speculatively.
READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


def ok(data: Any, provenance: dict | None = None) -> str:
    """Success envelope: ``{"data": ...}``, with source attribution attached."""
    payload: dict[str, Any] = {"data": data}
    if provenance:
        payload["provenance"] = provenance
    return format_response(payload)


def fail(message: str, code: str, details: dict | None = None) -> str:
    """Failure envelope: ``{"error", "code", "details"}``."""
    payload: dict[str, Any] = {"error": message, "code": code}
    if details:
        payload["details"] = details
    return format_response(payload)


def tool_guard(func: Callable[..., Awaitable[str]]) -> Callable[..., Awaitable[str]]:
    """Turn any escaping exception into the Standard Error Contract.

    ``ValueError`` maps to ``INVALID_INPUT`` because every ValueError raised in
    this codebase is a caller mistake with an actionable message (an ambiguous
    member name, a missing argument pair). No exception ever reaches the client.
    """

    @functools.wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await func(*args, **kwargs)
        except PoliteError as exc:
            log.warning("tool %s failed (%s): %s", func.__name__, exc.code, exc)
            return fail(str(exc), exc.code, exc.details)
        except NotFound as exc:
            return fail(str(exc), "NOT_FOUND")
        except ValueError as exc:
            return fail(str(exc), "INVALID_INPUT")
        except Exception as exc:
            log.error("tool %s raised %s: %s", func.__name__, type(exc).__name__, exc)
            return fail(
                f"Unexpected failure in {func.__name__}: {exc}",
                "INTERNAL",
                {"exception": type(exc).__name__},
            )

    return wrapper


def clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))
