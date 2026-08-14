"""Bulk ingest from published data files.

The whole design rests on this module being cheap and boring. Two of the three
sources are static bulk files built for exactly this purpose, so a complete
historical load, every Senate roll call from 1789 to the present, costs five
HTTP requests:

    3 to Voteview   (rollcalls, members, member-level votes)
    2 to congress-legislators (current + historical rosters)

senate.gov is not touched by ingest at all. Current-session freshness comes
from one small index feed, requested on demand, and individual roll call files
are fetched one at a time only when a caller actually asks for one.

Every load is ``INSERT OR REPLACE`` keyed on the natural primary key, so
re-running ingest is safe and converges. There is no partial-state to clean up.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import sqlite3
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from clients.db import to_float, to_int, to_text
from clients.http_polite import LEGISLATORS_FILES, VOTEVIEW_FILES, user_agent

log = logging.getLogger("mcp-cloakroom.ingest")

BATCH = 50_000


def _stream_csv(url: str, ua: str, timeout: float = 300.0) -> Iterator[dict]:
    """Download a remote CSV to a temp file, then yield it row by row.

    Deliberately not parsed straight off the socket. Voteview's roll call file
    contains quoted vote descriptions with embedded newlines, and a chunk-wise
    parser has to reassemble those by hand; getting it subtly wrong corrupts
    rows in a way that looks like real data. Buffering to disk costs a few
    seconds and one temp file, and hands the whole problem to ``csv``, which
    already solves it. The file is streamed to disk, so peak memory stays flat
    regardless of source size.
    """
    with tempfile.NamedTemporaryFile(mode="w+b", suffix=".csv", delete=True) as tmp:
        with (
            httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": ua}) as c,
            c.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            size = 0
            for chunk in resp.iter_bytes(chunk_size=1 << 20):
                tmp.write(chunk)
                size += len(chunk)
        tmp.flush()
        tmp.seek(0)
        log.info("  downloaded %.1f MB from %s", size / 1e6, url.rsplit("/", 1)[-1])
        text = io.TextIOWrapper(tmp, encoding="utf-8", errors="replace", newline="")
        yield from csv.DictReader(text)


def _progress(label: str, n: int, every: int = 250_000) -> None:
    if n % every == 0:
        log.info("  %s: %s rows", label, f"{n:,}")


def load_rollcalls(conn: sqlite3.Connection, ua: str) -> int:
    log.info("Loading roll calls from Voteview ...")
    return insert_rollcalls(conn, _stream_csv(VOTEVIEW_FILES["rollcalls"], ua))


def insert_rollcalls(conn: sqlite3.Connection, rows: Iterator[dict]) -> int:
    """Insert roll call rows. Split from the fetch so tests drive it offline."""
    cols = (
        "congress",
        "rollnumber",
        "chamber",
        "date",
        "session",
        "clerk_rollnumber",
        "yea_count",
        "nay_count",
        "nominate_mid_1",
        "nominate_mid_2",
        "nominate_spread_1",
        "nominate_spread_2",
        "nominate_log_likelihood",
        "bill_number",
        "vote_result",
        "vote_desc",
        "vote_question",
        "dtl_desc",
    )
    sql = (
        f"INSERT OR REPLACE INTO rollcalls ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    )
    batch, total = [], 0
    for r in rows:
        if r.get("chamber") != "Senate":
            continue
        batch.append(
            (
                to_int(r.get("congress")),
                to_int(r.get("rollnumber")),
                to_text(r.get("chamber")),
                to_text(r.get("date")),
                to_int(r.get("session")),
                to_int(r.get("clerk_rollnumber")),
                to_int(r.get("yea_count")),
                to_int(r.get("nay_count")),
                to_float(r.get("nominate_mid_1")),
                to_float(r.get("nominate_mid_2")),
                to_float(r.get("nominate_spread_1")),
                to_float(r.get("nominate_spread_2")),
                to_float(r.get("nominate_log_likelihood")),
                to_text(r.get("bill_number")),
                to_text(r.get("vote_result")),
                to_text(r.get("vote_desc")),
                to_text(r.get("vote_question")),
                to_text(r.get("dtl_desc")),
            )
        )
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch.clear()
            _progress("roll calls", total, 25_000)
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    log.info("  roll calls: %s rows loaded", f"{total:,}")
    return total


def load_members(conn: sqlite3.Connection, ua: str) -> int:
    log.info("Loading members from Voteview ...")
    return insert_members(conn, _stream_csv(VOTEVIEW_FILES["members"], ua))


def insert_members(conn: sqlite3.Connection, rows: Iterator[dict]) -> int:
    """Insert member rows. Split from the fetch so tests drive it offline."""
    cols = (
        "congress",
        "icpsr",
        "chamber",
        "state_abbrev",
        "party_code",
        "bioname",
        "bioguide_id",
        "born",
        "died",
        "nominate_dim1",
        "nominate_dim2",
        "nominate_geo_mean_probability",
        "nominate_number_of_votes",
        "nominate_number_of_errors",
    )
    sql = f"INSERT OR REPLACE INTO members ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    batch, total = [], 0
    for r in rows:
        if r.get("chamber") != "Senate":
            continue
        batch.append(
            (
                to_int(r.get("congress")),
                to_int(r.get("icpsr")),
                to_text(r.get("chamber")),
                to_text(r.get("state_abbrev")),
                # party_code arrives as both "200" and "200.0" in this file; to_int
                # normalizes both, and skipping that silently drops members.
                to_int(r.get("party_code")),
                to_text(r.get("bioname")),
                to_text(r.get("bioguide_id")),
                to_int(r.get("born")),
                to_int(r.get("died")),
                to_float(r.get("nominate_dim1")),
                to_float(r.get("nominate_dim2")),
                to_float(r.get("nominate_geo_mean_probability")),
                to_int(r.get("nominate_number_of_votes")),
                to_int(r.get("nominate_number_of_errors")),
            )
        )
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    log.info("  members: %s rows loaded", f"{total:,}")
    return total


def load_votes(conn: sqlite3.Connection, ua: str) -> int:
    log.info("Loading member-level votes from Voteview (the big one, ~126 MB) ...")
    return insert_votes(conn, _stream_csv(VOTEVIEW_FILES["votes"], ua))


def insert_votes(conn: sqlite3.Connection, rows: Iterator[dict]) -> int:
    """Insert member-level vote rows. Split from the fetch so tests drive it offline."""
    sql = (
        "INSERT OR REPLACE INTO votes "
        "(congress, rollnumber, icpsr, cast_code, prob) VALUES (?,?,?,?,?)"
    )
    batch, total = [], 0
    for r in rows:
        if r.get("chamber") != "Senate":
            continue
        batch.append(
            (
                to_int(r.get("congress")),
                to_int(r.get("rollnumber")),
                to_int(r.get("icpsr")),
                to_int(r.get("cast_code")),
                to_float(r.get("prob")),
            )
        )
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            batch.clear()
            _progress("votes", total)
    if batch:
        conn.executemany(sql, batch)
        conn.commit()
        total += len(batch)
    log.info("  votes: %s rows loaded", f"{total:,}")
    return total


def load_legislators(conn: sqlite3.Connection, ua: str) -> int:
    """Identity bridge from congress-legislators (CC0).

    ``bioguide_id`` is the spine on purpose. That file also carries an ``icpsr``
    field, but it is absent for a large share of current legislators, so joining
    on it drops sitting senators. Going lis -> bioguide -> icpsr (with icpsr
    supplied by Voteview's own members file) resolves the full chamber.
    """
    log.info("Loading identity bridge from congress-legislators ...")
    cols = (
        "bioguide_id",
        "lis_member_id",
        "govtrack_id",
        "wikipedia",
        "full_name",
        "birthday",
        "gender",
        "current_party",
        "current_state",
        "senate_class",
        "term_start",
        "term_end",
        "is_current",
    )
    sql = (
        f"INSERT OR REPLACE INTO member_ids ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
    )
    total = 0
    with httpx.Client(timeout=120.0, follow_redirects=True, headers={"User-Agent": ua}) as c:
        for kind, url in LEGISLATORS_FILES.items():
            resp = c.get(url)
            resp.raise_for_status()
            people = json.loads(resp.text)
            batch = []
            for p in people:
                ids, name, bio = p.get("id", {}), p.get("name", {}), p.get("bio", {})
                bioguide = ids.get("bioguide")
                if not bioguide:
                    continue
                terms = p.get("terms") or []
                sen = [t for t in terms if t.get("type") == "sen"]
                if not sen:
                    continue  # House-only members are out of scope for this server
                last = sen[-1]
                full = name.get("official_full") or " ".join(
                    x for x in (name.get("first"), name.get("last")) if x
                )
                batch.append(
                    (
                        bioguide,
                        ids.get("lis"),
                        str(ids["govtrack"]) if ids.get("govtrack") else None,
                        ids.get("wikipedia"),
                        full,
                        bio.get("birthday"),
                        bio.get("gender"),
                        last.get("party"),
                        last.get("state"),
                        to_int(last.get("class")),
                        last.get("start"),
                        last.get("end"),
                        1 if kind == "current" else 0,
                    )
                )
            conn.executemany(sql, batch)
            conn.commit()
            total += len(batch)
            log.info("  %s: %s senators", kind, f"{len(batch):,}")
    return total


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def is_populated(conn: sqlite3.Connection) -> bool:
    """True when a previous ingest finished cleanly."""
    try:
        if get_meta(conn, "last_ingest_completed") is None:
            return False
        n = conn.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
        return n > 0
    except sqlite3.Error:
        return False


def run_ingest(conn: sqlite3.Connection, *, version: str, contact_url: str | None = None) -> dict:
    """Full idempotent load. Safe to re-run; converges on the published data."""
    ua = user_agent(version, contact_url)
    started = datetime.now(UTC)
    log.info("Starting bulk ingest. This takes a few minutes on first run.")
    counts = {
        "rollcalls": load_rollcalls(conn, ua),
        "members": load_members(conn, ua),
        "votes": load_votes(conn, ua),
        "legislator_ids": load_legislators(conn, ua),
    }
    log.info("Optimizing database ...")
    conn.execute("ANALYZE")
    conn.commit()
    finished = datetime.now(UTC)
    set_meta(conn, "last_ingest_completed", finished.isoformat())
    set_meta(conn, "last_ingest_counts", json.dumps(counts))
    elapsed = (finished - started).total_seconds()
    log.info(
        "Ingest complete in %.0fs: %s roll calls, %s votes, %s member-congress rows.",
        elapsed,
        f"{counts['rollcalls']:,}",
        f"{counts['votes']:,}",
        f"{counts['members']:,}",
    )
    return {
        "counts": counts,
        "elapsed_seconds": round(elapsed, 1),
        "completed_at": finished.isoformat(),
    }
