"""Configuration. Every deployment-specific value comes from the environment.

The defaults are the ones a stranger cloning this repo should get: a database
under ``./data``, all feeds enabled, a polite rate limit, and a User-Agent that
identifies the project rather than any individual. Nothing here needs editing
to run the server; ``.env`` exists for people who want to change something.

There are no credentials in this project. The upstreams are published bulk data
files and public XML feeds, none of which authenticate. That is deliberate and
worth preserving: adding a secret here would be a regression.
"""

from __future__ import annotations

from pete_mcp_core.settings import BaseCoreSettings
from pydantic import AliasChoices, Field

from clients.http_polite import DEFAULT_CONTACT_URL

DEFAULT_PORT = 3728
VERSION = "0.1.1"


def _alias(*names: str) -> AliasChoices:
    return AliasChoices(*names)


class CloakroomSettings(BaseCoreSettings):
    """Runtime configuration, all overridable by environment variable."""

    # Where the SQLite store lives. In the published image this is a volume so
    # the multi-minute first ingest survives container replacement.
    cloakroom_db_path: str = Field(
        default="./data/cloakroom.db",
        validation_alias=_alias("CLOAKROOM_DB_PATH", "MCP_CLOAKROOM_DB_PATH"),
    )

    # Identifies this software to the servers it fetches from. Point it at your
    # own fork or contact page if you run a modified deployment.
    cloakroom_contact_url: str = Field(
        default=DEFAULT_CONTACT_URL,
        validation_alias=_alias("CLOAKROOM_CONTACT_URL"),
    )
    cloakroom_user_agent: str | None = Field(
        default=None,
        validation_alias=_alias("CLOAKROOM_USER_AGENT"),
    )

    # Minimum seconds between two senate.gov requests. Raise it freely; the
    # server is designed to work fine at any value because the historical
    # record never comes from there.
    cloakroom_min_request_interval: float = Field(
        default=2.0,
        validation_alias=_alias("CLOAKROOM_MIN_REQUEST_INTERVAL"),
    )

    # How long a cached current-session feed is reused before revalidating.
    # Revalidation is a conditional GET, so the usual cost is a 304.
    cloakroom_refresh_hours: float = Field(
        default=24.0,
        validation_alias=_alias("CLOAKROOM_REFRESH_HOURS"),
    )

    # Which senate.gov feeds are enabled, comma separated. Set to an empty
    # string to run entirely from bulk data with no senate.gov traffic at all.
    cloakroom_senate_feeds: str = Field(
        default="hearings,members,floor,votes",
        validation_alias=_alias("CLOAKROOM_SENATE_FEEDS"),
    )

    # Run the bulk load automatically when the database is empty.
    cloakroom_auto_ingest: bool = Field(
        default=True,
        validation_alias=_alias("CLOAKROOM_AUTO_INGEST"),
    )

    # The session treated as "in progress". Everything strictly before it is
    # immutable and cached permanently.
    cloakroom_current_congress: int = Field(
        default=119, validation_alias=_alias("CLOAKROOM_CURRENT_CONGRESS")
    )
    cloakroom_current_session: int = Field(
        default=2, validation_alias=_alias("CLOAKROOM_CURRENT_SESSION")
    )

    cloakroom_http_timeout: float = Field(
        default=60.0, validation_alias=_alias("CLOAKROOM_HTTP_TIMEOUT")
    )

    @property
    def enabled_feeds(self) -> set[str]:
        return {f.strip().lower() for f in self.cloakroom_senate_feeds.split(",") if f.strip()}

    def feed_enabled(self, name: str) -> bool:
        return name.lower() in self.enabled_feeds
