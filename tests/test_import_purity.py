"""Importing the server must do no I/O.

This exists because it did. ``build_context`` used to run at module scope, so
``import server`` opened the database and, finding it empty, downloaded roughly
140 MB from voteview.com and wrote a ~180 MB SQLite file into whatever directory
the importer was run from. A single test that imported the module for source
introspection was enough to trigger it on every CI run and every Dependabot PR.

It hid because the only step anyone thought to check, the CI import smoke test,
set ``CLOAKROOM_AUTO_INGEST=false``. The one place it was exercised was the one
place it could not fire.

These tests run the import in a subprocess with a poisoned socket layer, so they
fail on *any* outbound connection attempt rather than on a specific URL, and they
run in a scratch directory so a stray database has somewhere visible to land.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Poison the socket layer before importing, so the failure is "any network at
# all" rather than a guess about which host. Also poisons sqlite3.connect, since
# a database file appearing in a contributor's checkout is its own bug.
PROBE = """
import os, socket, sqlite3, sys, pathlib
sys.path.insert(0, {repo!r})
os.chdir({workdir!r})

def _no_net(*a, **k):
    raise AssertionError("NETWORK: importing server opened a socket")

socket.socket.connect = _no_net
socket.create_connection = _no_net

_real_sqlite = sqlite3.connect
def _no_db(*a, **k):
    raise AssertionError(f"DATABASE: importing server called sqlite3.connect{{a[:1]}}")
sqlite3.connect = _no_db

import server

assert hasattr(server, "build_app"), "build_app missing"
assert hasattr(server, "main"), "main missing"

stray = [str(p) for p in pathlib.Path({workdir!r}).rglob("*.db")]
assert not stray, f"DATABASE FILE CREATED: {{stray}}"
print("IMPORT_CLEAN")
"""


def _run_probe(tmp_path: Path) -> subprocess.CompletedProcess:
    code = PROBE.format(repo=str(REPO_ROOT), workdir=str(tmp_path))
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )


def test_importing_server_makes_no_network_call_and_creates_no_database(tmp_path) -> None:
    """The regression guard. Must hold with no environment variables set.

    Notably this does NOT set CLOAKROOM_AUTO_INGEST=false. Passing only with
    ingest disabled is exactly the blind spot that let the original bug ship.
    """
    result = _run_probe(tmp_path)
    combined = result.stdout + result.stderr
    assert "IMPORT_CLEAN" in combined, (
        f"importing server was not side-effect free.\n"
        f"exit={result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert result.returncode == 0


def test_the_probe_can_actually_fail(tmp_path) -> None:
    """Positive control for the guard above.

    A probe that cannot fail proves nothing. This runs the same poisoned
    environment against a module that deliberately opens a socket, and asserts
    the probe catches it. Without this, a typo in the probe would make the
    regression test permanently, silently green.
    """
    canary = tmp_path / "canary_module.py"
    canary.write_text("import socket\nsocket.create_connection(('example.com', 80))\n")
    code = PROBE.format(repo=str(REPO_ROOT), workdir=str(tmp_path)).replace(
        "import server", "import canary_module"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(tmp_path),
    )
    assert "IMPORT_CLEAN" not in (result.stdout + result.stderr)
    assert "NETWORK" in result.stderr, (
        f"the probe failed to detect a deliberate socket open: {result.stderr}"
    )


def test_build_app_is_the_only_entry_point_that_does_io() -> None:
    """Module scope must not call build_context or build_app."""
    source = (REPO_ROOT / "server.py").read_text(encoding="utf-8")
    body = []
    for line in source.splitlines():
        if line and not line[0].isspace() and not line.startswith(("def ", "class ", "@")):
            body.append(line)
    joined = "\n".join(body)
    assert "build_context(" not in joined, "build_context is called at module scope"
    assert "build_app(" not in joined, "build_app is called at module scope"
