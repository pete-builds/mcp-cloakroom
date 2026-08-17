"""The only way this server is allowed to talk to the public internet.

The traffic posture toward senate.gov is a design constraint, not a policy
document, so it is enforced here in code:

* **Fixed allowlist, no crawling.** Every senate.gov URL is either a literal
  from ``SENATE_STATIC_FEEDS`` or is built by ``senate_vote_url`` from validated
  integers. No URL is ever taken from a link, a redirect target, or a tool
  argument. There is no code path that fetches an arbitrary URL.
* **Identifying User-Agent with a contact URL.** An operator who wants this
  traffic to stop can see who to contact without doing any forensics.
* **Conditional GETs.** Stored ETag / Last-Modified turn a daily poll into a
  304 with no body.
* **Immutable caching.** A closed session's roll call cannot change, so once
  cached it is never re-requested.
* **Serialized and rate limited.** One request at a time, with a floor on the
  gap between them.

Explicitly and permanently out of scope: stack fingerprinting, port scanning,
vulnerability probing, fuzzing, auth surfaces, staging hosts, admin paths. This
module cannot express any of those, which is the point.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Final

import httpx

log = logging.getLogger("mcp-cloakroom.http")

# Default contact is this project's repository, so an operator seeing the
# traffic can find the source. Override with CLOAKROOM_CONTACT_URL when running
# your own deployment.
DEFAULT_CONTACT_URL: Final[str] = "https://github.com/pete-builds/mcp-cloakroom"

SENATE_HOST: Final[str] = "https://www.senate.gov"

# Literal feeds. Each is small, already public, and published for reuse.
SENATE_STATIC_FEEDS: Final[dict[str, str]] = {
    "hearings": f"{SENATE_HOST}/general/committee_schedules/hearings.xml",
    "members": f"{SENATE_HOST}/legislative/LIS_MEMBER/cvc_member_data.xml",
    "floor": f"{SENATE_HOST}/legislative/schedule/floor_schedule.xml",
}

# Voteview and congress-legislators bulk files. Static hosting, built for bulk
# download, and the entire reason this project never needs to scrape anything.
VOTEVIEW_FILES: Final[dict[str, str]] = {
    "rollcalls": "https://voteview.com/static/data/out/rollcalls/Sall_rollcalls.csv",
    "votes": "https://voteview.com/static/data/out/votes/Sall_votes.csv",
    "members": "https://voteview.com/static/data/out/members/Sall_members.csv",
}

LEGISLATORS_FILES: Final[dict[str, str]] = {
    "current": "https://unitedstates.github.io/congress-legislators/legislators-current.json",
    "historical": "https://unitedstates.github.io/congress-legislators/legislators-historical.json",
}

# senate.gov publishes roll call votes from the 101st Congress forward. Bounds
# exist so a bad argument fails here rather than becoming a request.
MIN_CONGRESS: Final[int] = 101
MAX_CONGRESS: Final[int] = 200
MAX_VOTE_NUMBER: Final[int] = 999


# Pattern allowlist for the two parameterized senate.gov paths. Anchored at both
# ends so nothing can be appended, and the numeric groups are bounded, so these
# cannot be widened into a crawl of the host.
_MENU_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://www\.senate\.gov/legislative/LIS/roll_call_lists/"
    r"vote_menu_(\d{3})_([12])\.xml$"
)
_VOTE_RE: Final[re.Pattern[str]] = re.compile(
    r"^https://www\.senate\.gov/legislative/LIS/roll_call_votes/"
    r"vote(\d{3})([12])/vote_(\d{3})_([12])_(\d{5})\.xml$"
)


def is_allowed(url: str) -> bool:
    """Whether a URL is on the fixed allowlist.

    This is the "no crawling" guarantee expressed as a runtime check rather
    than a convention. A URL qualifies only by being a literal from one of the
    published-file tables, or by matching one of the two anchored senate.gov
    patterns whose numeric fields are bounded. There is no wildcard, no
    host-prefix rule, and no way for a link, a redirect target, or a tool
    argument to reach the network without passing through here.
    """
    if url in SENATE_STATIC_FEEDS.values():
        return True
    if url in VOTEVIEW_FILES.values() or url in LEGISLATORS_FILES.values():
        return True
    m = _MENU_RE.match(url)
    if m:
        return MIN_CONGRESS <= int(m.group(1)) <= MAX_CONGRESS
    m = _VOTE_RE.match(url)
    if m:
        congress, session, c2, s2, vote = (
            int(m.group(1)),
            int(m.group(2)),
            int(m.group(3)),
            int(m.group(4)),
            int(m.group(5)),
        )
        return (
            congress == c2
            and session == s2
            and MIN_CONGRESS <= congress <= MAX_CONGRESS
            and 1 <= vote <= MAX_VOTE_NUMBER
        )
    return False


def assert_allowed(url: str) -> None:
    """Raise unless ``url`` is on the allowlist. Called before every request."""
    if not is_allowed(url):
        raise PoliteError(
            "refusing to fetch a URL that is not on the fixed allowlist",
            "INVALID_INPUT",
            {"url": url},
        )


class PoliteError(RuntimeError):
    """A fetch that failed, carrying an error-contract code."""

    def __init__(self, message: str, code: str = "UPSTREAM_DOWN", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def user_agent(version: str, contact_url: str | None = None) -> str:
    """Identifying UA. A contact URL is part of being a good citizen."""
    return f"mcp-cloakroom/{version} (+{contact_url or DEFAULT_CONTACT_URL})"


def _check_bounds(congress: int, session: int, vote_number: int | None = None) -> None:
    if not MIN_CONGRESS <= congress <= MAX_CONGRESS:
        raise PoliteError(
            f"congress {congress} is outside the range senate.gov publishes "
            f"({MIN_CONGRESS}-{MAX_CONGRESS}); historical votes come from Voteview instead",
            "INVALID_INPUT",
            {"congress": congress},
        )
    if session not in (1, 2):
        raise PoliteError(
            f"session must be 1 or 2, got {session}", "INVALID_INPUT", {"session": session}
        )
    if vote_number is not None and not 1 <= vote_number <= MAX_VOTE_NUMBER:
        raise PoliteError(
            f"vote_number must be 1-{MAX_VOTE_NUMBER}, got {vote_number}",
            "INVALID_INPUT",
            {"vote_number": vote_number},
        )


def senate_menu_url(congress: int, session: int) -> str:
    """Per-session vote index.

    The index lives under ``roll_call_lists``; ``roll_call_votes`` is the
    directory for individual vote files. Both are used, for different things.
    """
    _check_bounds(congress, session)
    return f"{SENATE_HOST}/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml"


def senate_vote_url(congress: int, session: int, vote_number: int) -> str:
    """One roll call's detail file. Built from validated ints, never from a link."""
    _check_bounds(congress, session, vote_number)
    return (
        f"{SENATE_HOST}/legislative/LIS/roll_call_votes/"
        f"vote{congress}{session}/vote_{congress}_{session}_{vote_number:05d}.xml"
    )


class PoliteFetcher:
    """Serialized, rate-limited, conditional-GET HTTP for a fixed set of URLs."""

    def __init__(
        self,
        *,
        version: str,
        min_interval: float = 1.0,
        timeout: float = 60.0,
        cache=None,
        contact_url: str | None = None,
        user_agent_override: str | None = None,
        refresh_seconds: float = 0.0,
    ):
        self._ua = user_agent_override or user_agent(version, contact_url)
        self._min_interval = min_interval
        # Within this window a cached body is served with no request at all.
        # Outside it, the stored validators still make the refetch a conditional
        # GET, so the usual cost is a 304 with no body.
        self._refresh_seconds = refresh_seconds
        self._last_request = 0.0
        self._lock = asyncio.Lock()
        self._cache = cache
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # a redirect target is a URL we did not vet
            headers={"User-Agent": self._ua, "Accept": "*/*"},
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self) -> None:
        await self._client.aclose()

    def _within_refresh_window(self, cached: dict) -> bool:
        """Whether a cached entry is young enough to serve without asking again.

        Returns False when the window is zero (the default), which restores the
        always-revalidate behaviour.
        """
        if self._refresh_seconds <= 0:
            return False
        stamp = cached.get("fetched_at")
        if not stamp:
            return False
        try:
            fetched = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - fetched).total_seconds()
        return age < self._refresh_seconds

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self._min_interval:
            await asyncio.sleep(self._min_interval - elapsed)
        self._last_request = time.monotonic()

    async def get_text(self, url: str, *, immutable: bool = False) -> str:
        """Fetch a URL's body, using and updating the conditional-GET cache.

        ``immutable=True`` marks the entry as never needing revalidation. Use it
        only for closed sessions, whose roll calls cannot change.
        """
        assert_allowed(url)
        cached = self._cache.get(url) if self._cache else None
        if cached and cached.get("immutable") and cached.get("body"):
            log.debug("cache hit (immutable): %s", url)
            return cached["body"]

        if cached and cached.get("body") and self._within_refresh_window(cached):
            log.debug("cache hit (within refresh window): %s", url)
            return cached["body"]

        headers: dict[str, str] = {}
        if cached:
            if cached.get("etag"):
                headers["If-None-Match"] = cached["etag"]
            if cached.get("last_modified"):
                headers["If-Modified-Since"] = cached["last_modified"]

        async with self._lock:
            await self._throttle()
            try:
                resp = await self._client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                if cached and cached.get("body"):
                    log.warning("fetch failed (%s), serving cached body: %s", exc, url)
                    return cached["body"]
                raise PoliteError(
                    f"could not reach {url}: {exc}", "UPSTREAM_DOWN", {"url": url}
                ) from exc

        if resp.status_code == 304 and cached and cached.get("body"):
            log.info("304 not modified: %s", url)
            self._store(
                url, cached.get("etag"), cached.get("last_modified"), cached["body"], immutable
            )
            return cached["body"]

        if resp.status_code in (301, 302, 303, 307, 308):
            # Never chased. A redirect points somewhere the allowlist did not vet.
            raise PoliteError(
                f"{url} redirected to an un-vetted location; refusing to follow",
                "NOT_FOUND",
                {"url": url, "status": resp.status_code},
            )

        if resp.status_code == 404:
            raise PoliteError(f"{url} not found", "NOT_FOUND", {"url": url, "status": 404})

        if resp.status_code == 429:
            raise PoliteError(
                "upstream asked us to slow down",
                "RATE_LIMITED",
                {"url": url, "retry_after": resp.headers.get("Retry-After")},
            )

        if resp.status_code >= 400:
            raise PoliteError(
                f"{url} returned HTTP {resp.status_code}",
                "INTERNAL" if resp.status_code >= 500 else "INVALID_INPUT",
                {"url": url, "status": resp.status_code},
            )

        body = resp.text
        self._store(
            url, resp.headers.get("ETag"), resp.headers.get("Last-Modified"), body, immutable
        )
        return body

    def _store(
        self, url: str, etag: str | None, last_modified: str | None, body: str, immutable: bool
    ) -> None:
        if self._cache is None:
            return
        self._cache.put(
            url=url,
            etag=etag,
            last_modified=last_modified,
            body=body,
            fetched_at=datetime.now(UTC).isoformat(),
            immutable=immutable,
        )


class SqliteHttpCache:
    """Conditional-GET cache backed by the ``http_cache`` table."""

    def __init__(self, conn):
        self._conn = conn

    def get(self, url: str) -> dict | None:
        row = self._conn.execute(
            "SELECT url, etag, last_modified, body, fetched_at, immutable "
            "FROM http_cache WHERE url = ?",
            (url,),
        ).fetchone()
        return dict(row) if row else None

    def put(
        self,
        *,
        url: str,
        etag: str | None,
        last_modified: str | None,
        body: str,
        fetched_at: str,
        immutable: bool,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO http_cache "
            "(url, etag, last_modified, body, fetched_at, immutable) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (url, etag, last_modified, body, fetched_at, 1 if immutable else 0),
        )
        self._conn.commit()
