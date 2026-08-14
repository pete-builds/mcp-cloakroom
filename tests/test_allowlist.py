"""The "no crawling" guarantee, tested as a runtime property.

Positive and negative controls both matter here. A suite that only asserts the
allowlisted URLs pass would still be green if ``is_allowed`` were replaced with
``return True``, which is exactly the failure this file exists to catch. Every
negative case below is a URL on senate.gov's own host, so the test cannot be
satisfied by a naive host check either.
"""

from __future__ import annotations

import asyncio

import pytest

from clients.http_polite import (
    LEGISLATORS_FILES,
    SENATE_STATIC_FEEDS,
    VOTEVIEW_FILES,
    PoliteError,
    PoliteFetcher,
    assert_allowed,
    is_allowed,
    senate_menu_url,
    senate_vote_url,
)

# ---------------------------------------------------------------- positive

ALLOWED = [
    *SENATE_STATIC_FEEDS.values(),
    *VOTEVIEW_FILES.values(),
    *LEGISLATORS_FILES.values(),
    senate_menu_url(119, 2),
    senate_vote_url(119, 2, 231),
    senate_vote_url(101, 1, 1),
]


@pytest.mark.parametrize("url", ALLOWED)
def test_allowlisted_urls_pass(url: str) -> None:
    assert is_allowed(url) is True
    assert_allowed(url)  # must not raise


# ---------------------------------------------------------------- negative

# Every entry is a plausible URL that must still be refused. The senate.gov ones
# are the important half: they prove the check is a real allowlist and not a
# host comparison.
REFUSED = [
    # Same host, paths we never declared.
    "https://www.senate.gov/",
    "https://www.senate.gov/robots.txt",
    "https://www.senate.gov/general/XML.htm",
    "https://www.senate.gov/legislative/LIS/roll_call_lists/",
    "https://www.senate.gov/legislative/LIS/roll_call_lists/index.html",
    # Right directory, wrong file.
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/",
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00231.json",
    # Path traversal and suffix-append attempts against the anchored patterns.
    "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_2.xml/../../etc",
    "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_2.xml?x=1",
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_119_2_00231.xml.bak",
    # Mismatched congress/session between directory and filename.
    "https://www.senate.gov/legislative/LIS/roll_call_votes/vote1192/vote_118_1_00001.xml",
    # Out of the published range.
    "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_099_1.xml",
    # Scheme downgrade and host lookalikes.
    "http://www.senate.gov/general/committee_schedules/hearings.xml",
    "https://senate.gov/general/committee_schedules/hearings.xml",
    "https://www.senate.gov.evil.example/general/committee_schedules/hearings.xml",
    # Unrelated third parties.
    "https://example.com/",
    "https://voteview.com/static/data/out/rollcalls/H117_rollcalls.csv",
    "https://169.254.169.254/latest/meta-data/",
]


@pytest.mark.parametrize("url", REFUSED)
def test_non_allowlisted_urls_are_refused(url: str) -> None:
    assert is_allowed(url) is False
    with pytest.raises(PoliteError) as exc:
        assert_allowed(url)
    assert exc.value.code == "INVALID_INPUT"


def test_fetcher_refuses_before_opening_a_connection() -> None:
    """The check runs inside get_text, so no socket is opened for a bad URL.

    If the allowlist were only applied in the URL builders, this call would
    attempt a real request. It must raise instead.
    """
    fetcher = PoliteFetcher(version="0.0.0", min_interval=0)

    async def run() -> None:
        with pytest.raises(PoliteError) as exc:
            await fetcher.get_text("https://www.senate.gov/some/unvetted/path.xml")
        assert exc.value.code == "INVALID_INPUT"
        await fetcher.close()

    asyncio.run(run())


def test_url_builders_reject_out_of_range_arguments() -> None:
    with pytest.raises(PoliteError):
        senate_menu_url(99, 1)  # before senate.gov's published range
    with pytest.raises(PoliteError):
        senate_menu_url(119, 3)  # only sessions 1 and 2 exist
    with pytest.raises(PoliteError):
        senate_vote_url(119, 2, 0)  # vote numbers start at 1
    with pytest.raises(PoliteError):
        senate_vote_url(119, 2, 10_000)  # above the per-session ceiling


def test_builders_only_ever_emit_allowlisted_urls() -> None:
    """Anything the builders produce must satisfy the allowlist by construction."""
    for congress in (101, 119, 200):
        for session in (1, 2):
            assert is_allowed(senate_menu_url(congress, session))
            for vote in (1, 231, 999):
                assert is_allowed(senate_vote_url(congress, session, vote))
