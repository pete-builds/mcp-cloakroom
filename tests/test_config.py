"""Config defaults must be publishable, and the bind host must not regress.

Both properties are the kind that stay true right up until someone adds a
convenient default. Pinning them here is what lets the repository be public at
every commit rather than after a sanitization pass.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

import server
from clients.config import DEFAULT_PORT, CloakroomSettings
from clients.http_polite import (
    DEFAULT_CONTACT_URL,
    LEGISLATORS_FILES,
    SENATE_STATIC_FEEDS,
    VOTEVIEW_FILES,
    user_agent,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Anything matching these must never appear in a default value or in tracked
# source. Private ranges cover the RFC 1918 space a homelab deployment uses.
FORBIDDEN_PATTERNS = [
    (r"\b10\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "RFC1918 10/8 address"),
    (r"\b192\.168\.\d{1,3}\.\d{1,3}\b", "RFC1918 192.168/16 address"),
    (r"\b172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b", "RFC1918 172.16/12 address"),
    (r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b", "CGNAT/Tailscale address"),
    (r"\b[\w.+-]+@[\w-]+\.[\w.]+\b", "email address"),
    (r"\.local\b", "internal hostname suffix"),
    (r"\.ts\.net\b", "Tailscale hostname"),
]


def _settings() -> CloakroomSettings:
    """Defaults only: ignore any .env or ambient environment."""
    return CloakroomSettings(_env_file=None)


def test_default_user_agent_is_generic_and_has_a_contact_url() -> None:
    ua = user_agent("0.1.0")
    assert ua.startswith("mcp-cloakroom/")
    assert "+https://" in ua, "the UA must carry a contact URL"
    for pattern, label in FORBIDDEN_PATTERNS:
        assert not re.search(pattern, ua), f"default UA leaks a {label}"


def test_default_contact_url_is_a_project_repository() -> None:
    assert DEFAULT_CONTACT_URL.startswith("https://github.com/")
    for pattern, label in FORBIDDEN_PATTERNS:
        assert not re.search(pattern, DEFAULT_CONTACT_URL), f"contact URL leaks a {label}"


def test_no_default_setting_contains_anything_deployment_specific() -> None:
    s = _settings()
    for name, value in s.model_dump().items():
        if not isinstance(value, str):
            continue
        for pattern, label in FORBIDDEN_PATTERNS:
            assert not re.search(pattern, value), f"default for {name!r} leaks a {label}: {value!r}"


def test_defaults_are_runnable_without_any_configuration() -> None:
    """A stranger cloning the repo must get a working configuration."""
    s = _settings()
    assert s.cloakroom_db_path
    assert s.cloakroom_auto_ingest is True
    assert s.cloakroom_min_request_interval > 0
    assert s.enabled_feeds == {"hearings", "members", "floor", "votes"}
    assert s.auth_token in (None, "")
    assert s.auth_required is False


def test_upstream_urls_are_all_public_https() -> None:
    for url in (
        *SENATE_STATIC_FEEDS.values(),
        *VOTEVIEW_FILES.values(),
        *LEGISLATORS_FILES.values(),
    ):
        assert url.startswith("https://"), f"{url} is not https"


def test_feeds_can_be_disabled_entirely() -> None:
    """Running with zero senate.gov traffic must be a supported configuration."""
    s = CloakroomSettings(_env_file=None, CLOAKROOM_SENATE_FEEDS="")
    assert s.enabled_feeds == set()
    assert not s.feed_enabled("votes")


def test_feed_selection_is_case_and_space_insensitive() -> None:
    s = CloakroomSettings(_env_file=None, CLOAKROOM_SENATE_FEEDS=" Hearings , FLOOR ")
    assert s.enabled_feeds == {"hearings", "floor"}
    assert s.feed_enabled("HEARINGS")
    assert not s.feed_enabled("votes")


# ------------------------------------------------------- the loopback trap


def test_server_binds_all_interfaces_by_default() -> None:
    """MCP SDK 2.0 defaults to loopback, which 421s every LAN client.

    CI and localhost stay green either way, so the only thing standing between
    that default and a broken deployment is this assertion.
    """
    src = inspect.getsource(server.main)
    assert 'default_host="0.0.0.0"' in src, "server.main must bind 0.0.0.0 explicitly"


def test_server_uses_streamable_http_not_sse() -> None:
    src = inspect.getsource(server.main)
    assert 'default_transport="streamable-http"' in src
    assert "sse" not in src.lower()


def test_server_runs_on_the_documented_port() -> None:
    assert DEFAULT_PORT == 3728
    assert f"default_port={DEFAULT_PORT}" in inspect.getsource(server.main) or (
        "default_port=DEFAULT_PORT" in inspect.getsource(server.main)
    )


# --------------------------------------------------- repository publishability


EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "data",
}


def _tracked_text_files() -> list[Path]:
    """Every non-binary file in the tree, by exclusion rather than allowlist.

    This deliberately does NOT filter on a set of known extensions. An earlier
    version did, and a positive control showed it silently skipped
    ``docker-entrypoint.sh``, ``LICENSE``, and every ``.csv``/``.xml`` test
    fixture: exactly the files where something unintended is most likely to ride
    along unnoticed, since nobody re-reads a checked-in data sample. Any new file
    type added to this repository is now covered automatically rather than
    needing someone to remember to extend a list.
    """
    out = []
    for p in REPO_ROOT.rglob("*"):
        if not p.is_file():
            continue
        if set(p.relative_to(REPO_ROOT).parts) & EXCLUDED_DIRS:
            continue
        try:
            chunk = p.read_bytes()[:4096]
        except OSError:
            continue
        if b"\x00" in chunk:
            continue  # binary
        out.append(p)
    return out


def test_the_scanner_actually_covers_the_repository() -> None:
    """Guards the guard.

    The leak test is only as good as the file set it walks. These are the exact
    carriers the previous extension allowlist missed, so they are asserted by
    name rather than trusted to a glob.
    """
    scanned = {p.relative_to(REPO_ROOT).as_posix() for p in _tracked_text_files()}
    for required in (
        "docker-entrypoint.sh",
        "LICENSE",
        "README.md",
        "Dockerfile",
        ".env.example",
        "requirements.lock",
        "tests/fixtures/rollcalls_sample.csv",
        "tests/fixtures/hearings_sample.xml",
        "tests/fixtures/vote_119_2_00231.xml",
        ".github/workflows/release.yml",
    ):
        assert required in scanned, f"leak scanner does not cover {required}"
    assert len(scanned) > 35, f"scanner covers only {len(scanned)} files; expected the whole tree"


@pytest.mark.parametrize("pattern,label", FORBIDDEN_PATTERNS)
def test_no_tracked_file_leaks_deployment_details(pattern: str, label: str) -> None:
    """Publishable at every commit, checked mechanically rather than by review."""
    offenders = []
    for path in _tracked_text_files():
        if path.name == "test_config.py":
            continue  # this file defines the patterns
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(pattern, text):
            offenders.append(f"{path.relative_to(REPO_ROOT)}: {match.group(0)}")
    assert not offenders, f"tracked files leak a {label}: {offenders[:5]}"


def test_no_secret_is_required_to_run() -> None:
    """This project authenticates to nothing upstream; keep it that way."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    for line in example.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        assert value.strip() == "", f"{key} ships with a value in .env.example"
