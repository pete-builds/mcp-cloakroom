"""SQLite store: schema, connection, and the coercions the source data needs.

Read-only at serve time. Every write happens in ``ingest.py``, which runs as a
separate one-shot process, so the served database is never half-written.

The coercion helpers are not decoration. Voteview's CSVs are pandas exports, so
integer columns arrive float-formatted whenever the column ever held a null:
``party_code`` appears in Sall_members.csv as both ``200`` and ``200.0`` across
roughly 300 Senate rows. Reading those as strings, or with a naive ``int()``,
silently drops members from every party filter. Same story for ``born``/``died``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS rollcalls (
    congress            INTEGER NOT NULL,
    rollnumber          INTEGER NOT NULL,
    chamber             TEXT    NOT NULL,
    date                TEXT,
    session             INTEGER,
    clerk_rollnumber    INTEGER,
    yea_count           INTEGER,
    nay_count           INTEGER,
    nominate_mid_1      REAL,
    nominate_mid_2      REAL,
    nominate_spread_1   REAL,
    nominate_spread_2   REAL,
    nominate_log_likelihood REAL,
    bill_number         TEXT,
    vote_result         TEXT,
    vote_desc           TEXT,
    vote_question       TEXT,
    dtl_desc            TEXT,
    PRIMARY KEY (congress, rollnumber)
);

-- The clerk_rollnumber bridge. senate.gov numbers votes per session and resets
-- each session; Voteview numbers them continuously across a Congress. This
-- index is the join, and it is UNIQUE so a duplicate can never make a
-- cross-reference ambiguous rather than loudly wrong.
CREATE UNIQUE INDEX IF NOT EXISTS idx_rollcalls_clerk
    ON rollcalls (congress, session, clerk_rollnumber)
    WHERE clerk_rollnumber IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_rollcalls_date ON rollcalls (date);
CREATE INDEX IF NOT EXISTS idx_rollcalls_bill ON rollcalls (bill_number);

CREATE TABLE IF NOT EXISTS votes (
    congress    INTEGER NOT NULL,
    rollnumber  INTEGER NOT NULL,
    icpsr       INTEGER NOT NULL,
    cast_code   INTEGER,
    prob        REAL,
    PRIMARY KEY (congress, rollnumber, icpsr)
) WITHOUT ROWID;

CREATE INDEX IF NOT EXISTS idx_votes_member ON votes (icpsr, congress);

CREATE TABLE IF NOT EXISTS members (
    congress        INTEGER NOT NULL,
    icpsr           INTEGER NOT NULL,
    chamber         TEXT,
    state_abbrev    TEXT,
    party_code      INTEGER,
    bioname         TEXT,
    bioguide_id     TEXT,
    born            INTEGER,
    died            INTEGER,
    nominate_dim1   REAL,
    nominate_dim2   REAL,
    -- Model-fit columns. These are what make it possible to report an ideal
    -- point with its uncertainty instead of as a bare coordinate: how often the
    -- model classified this member's votes correctly, and over how many votes.
    nominate_geo_mean_probability REAL,
    nominate_number_of_votes INTEGER,
    nominate_number_of_errors INTEGER,
    PRIMARY KEY (congress, icpsr)
);

CREATE INDEX IF NOT EXISTS idx_members_bioguide ON members (bioguide_id);
CREATE INDEX IF NOT EXISTS idx_members_name ON members (bioname);
CREATE INDEX IF NOT EXISTS idx_members_icpsr ON members (icpsr);

-- Identity bridge from congress-legislators (CC0). bioguide_id is the spine:
-- Voteview's own icpsr column in congress-legislators is missing for 218 of 537
-- current legislators, so lis -> bioguide -> icpsr is the only path that
-- resolves all 100 sitting senators. Verified 100/100 on 2026-08-14.
CREATE TABLE IF NOT EXISTS member_ids (
    bioguide_id     TEXT PRIMARY KEY,
    lis_member_id   TEXT,
    govtrack_id     TEXT,
    wikipedia       TEXT,
    full_name       TEXT,
    birthday        TEXT,
    gender          TEXT,
    current_party   TEXT,
    current_state   TEXT,
    senate_class    INTEGER,
    term_start      TEXT,
    term_end        TEXT,
    is_current      INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_member_ids_lis ON member_ids (lis_member_id);

-- senate.gov per-vote detail, fetched lazily one vote at a time and then kept
-- forever. A closed session's roll call is immutable, so a cached row is not a
-- staleness risk; it is the reason this server does not need to re-ask.
CREATE TABLE IF NOT EXISTS senate_vote_detail (
    congress            INTEGER NOT NULL,
    session             INTEGER NOT NULL,
    vote_number         INTEGER NOT NULL,
    vote_date           TEXT,
    modify_date         TEXT,
    vote_question_text  TEXT,
    vote_document_text  TEXT,
    vote_result_text    TEXT,
    question            TEXT,
    vote_title          TEXT,
    majority_requirement TEXT,
    vote_result         TEXT,
    document_name       TEXT,
    document_title      TEXT,
    amendment_purpose   TEXT,
    tie_breaker_by      TEXT,
    tie_breaker_vote    TEXT,
    count_yeas          INTEGER,
    count_nays          INTEGER,
    count_present       INTEGER,
    count_absent        INTEGER,
    fetched_at          TEXT,
    PRIMARY KEY (congress, session, vote_number)
);

-- Per-member positions from a senate.gov roll call file, stored alongside the
-- detail row so a cached vote returns byte-identical data to a freshly fetched
-- one. Without this, the first call returned ~100 positions and every later
-- call returned an empty list, which quietly broke the idempotency the tool
-- advertises.
CREATE TABLE IF NOT EXISTS senate_vote_positions (
    congress        INTEGER NOT NULL,
    session         INTEGER NOT NULL,
    vote_number     INTEGER NOT NULL,
    lis_member_id   TEXT,
    member_full     TEXT,
    last_name       TEXT,
    first_name      TEXT,
    party           TEXT,
    state           TEXT,
    vote_cast       TEXT,
    ordinal         INTEGER NOT NULL,
    PRIMARY KEY (congress, session, vote_number, ordinal)
);

-- Conditional-GET bookkeeping for senate.gov. Storing the validators is what
-- lets a daily poll cost a 304 instead of a download.
CREATE TABLE IF NOT EXISTS http_cache (
    url             TEXT PRIMARY KEY,
    etag            TEXT,
    last_modified   TEXT,
    body            TEXT,
    fetched_at      TEXT,
    immutable       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""


def connect(path: str | Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open the store. ``read_only`` opens a URI connection that cannot write."""
    p = Path(path)
    if read_only:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def to_int(value: object) -> int | None:
    """Parse an int that may arrive as ``''``, ``'200'``, or ``'200.0'``.

    Voteview exports from pandas, so any integer column that ever held a null
    is float-formatted for every row in the file. ``int('200.0')`` raises, so a
    naive parse drops those rows silently. This is the single most likely place
    for a quiet data-loss bug in this project.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL"}:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def to_float(value: object) -> float | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.upper() in {"NA", "NAN", "NULL"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def to_text(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
