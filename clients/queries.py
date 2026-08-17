"""Read queries over the store. No HTTP, no writes.

Every function returns plain dicts already shaped for a tool response, so the
tool modules stay thin and the SQL lives in one place.
"""

from __future__ import annotations

import sqlite3

from clients.codes import (
    DECIDING_CODES,
    NAY_CODES,
    YEA_CODES,
    cast_detail,
    cast_position,
    fit_quality,
    party_name,
)

# SQLite LIKE treats % and _ as wildcards, so a user searching for the literal
# string "100%" was asking for "100 followed by anything" and a search for "%"
# alone matched every row in the table. Parameterization prevents injection but
# does nothing about this: the wildcards live in the *value*, not the SQL. Every
# LIKE in this module escapes its pattern and declares an ESCAPE character.
LIKE_ESCAPE = "\\"


def like_contains(value: str) -> str:
    """Build a contains-pattern that matches ``value`` literally."""
    escaped = (
        value.replace(LIKE_ESCAPE, LIKE_ESCAPE + LIKE_ESCAPE)
        .replace("%", LIKE_ESCAPE + "%")
        .replace("_", LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


class NotFound(LookupError):
    pass


def rollcall_row(conn: sqlite3.Connection, congress: int, rollnumber: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM rollcalls WHERE congress=? AND rollnumber=?", (congress, rollnumber)
    ).fetchone()
    if not row:
        raise NotFound(f"no roll call {rollnumber} in the {congress}th Congress")
    return row


def resolve_rollnumber(
    conn: sqlite3.Connection,
    congress: int,
    *,
    rollnumber: int | None = None,
    session: int | None = None,
    vote_number: int | None = None,
) -> int:
    """Turn either addressing scheme into a Voteview rollnumber.

    This is the ``clerk_rollnumber`` bridge, and it is the single most
    dangerous join in this project. Voteview's ``rollnumber`` counts
    continuously across a Congress (the 119th reached 890); senate.gov's
    ``vote_number`` restarts at 1 each session (the 119th's 2nd session is at
    231). Treating them as interchangeable silently returns the wrong vote
    rather than failing, so the conversion happens here and only here.
    """
    if rollnumber is not None:
        return rollnumber
    if session is None or vote_number is None:
        raise ValueError("pass either rollnumber, or both session and vote_number")
    row = conn.execute(
        "SELECT rollnumber FROM rollcalls WHERE congress=? AND session=? AND clerk_rollnumber=?",
        (congress, session, vote_number),
    ).fetchone()
    if not row:
        raise NotFound(f"no vote {vote_number} in session {session} of the {congress}th Congress")
    return int(row["rollnumber"])


def rollcall_summary(row: sqlite3.Row) -> dict:
    return {
        "congress": row["congress"],
        "rollnumber": row["rollnumber"],
        "session": row["session"],
        "vote_number": row["clerk_rollnumber"],
        "date": row["date"],
        "bill_number": row["bill_number"],
        "question": row["vote_question"],
        "result": row["vote_result"],
        "description": row["vote_desc"] or row["dtl_desc"],
        "yea_count": row["yea_count"],
        "nay_count": row["nay_count"],
    }


def list_votes(
    conn: sqlite3.Connection,
    *,
    congress: int | None = None,
    session: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where: list[str] = ["chamber = 'Senate'"]
    params: list[object] = []
    if congress is not None:
        where.append("congress = ?")
        params.append(congress)
    if session is not None:
        where.append("session = ?")
        params.append(session)
    if start_date:
        where.append("date >= ?")
        params.append(start_date)
    if end_date:
        where.append("date <= ?")
        params.append(end_date)
    clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) AS n FROM rollcalls WHERE {clause}", params).fetchone()[
        "n"
    ]
    rows = conn.execute(
        f"SELECT * FROM rollcalls WHERE {clause} "
        f"ORDER BY congress DESC, rollnumber DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "total_matching": total,
        "returned": len(rows),
        "offset": offset,
        "votes": [rollcall_summary(r) for r in rows],
    }


def find_votes(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    bill_number: str | None = None,
    question: str | None = None,
    result: str | None = None,
    congress: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where: list[str] = ["chamber = 'Senate'"]
    params: list[object] = []
    if query:
        where.append(
            "(vote_desc LIKE ? ESCAPE '\\' OR dtl_desc LIKE ? ESCAPE '\\' "
            "OR bill_number LIKE ? ESCAPE '\\' OR vote_question LIKE ? ESCAPE '\\')"
        )
        like = like_contains(query)
        params.extend([like, like, like, like])
    if bill_number:
        # Voteview stores bill numbers unspaced ("S5271"); accept "S. 5271" too.
        norm = bill_number.replace(" ", "").replace(".", "").upper()
        where.append("REPLACE(REPLACE(UPPER(bill_number),' ',''),'.','') = ?")
        params.append(norm)
    if question:
        where.append("vote_question LIKE ? ESCAPE '\\'")
        params.append(like_contains(question))
    if result:
        where.append("vote_result LIKE ? ESCAPE '\\'")
        params.append(like_contains(result))
    if congress is not None:
        where.append("congress = ?")
        params.append(congress)
    clause = " AND ".join(where)

    total = conn.execute(f"SELECT COUNT(*) AS n FROM rollcalls WHERE {clause}", params).fetchone()[
        "n"
    ]
    rows = conn.execute(
        f"SELECT * FROM rollcalls WHERE {clause} "
        f"ORDER BY congress DESC, rollnumber DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "total_matching": total,
        "returned": len(rows),
        "offset": offset,
        "votes": [rollcall_summary(r) for r in rows],
    }


def vote_positions(conn: sqlite3.Connection, congress: int, rollnumber: int) -> list[dict]:
    """Every member's position on one roll call, joined to identity."""
    rows = conn.execute(
        """
        SELECT v.icpsr, v.cast_code, v.prob,
               m.bioname, m.bioguide_id, m.state_abbrev, m.party_code,
               m.nominate_dim1, m.nominate_dim2,
               i.lis_member_id, i.full_name
        FROM votes v
        LEFT JOIN members m ON m.congress = v.congress AND m.icpsr = v.icpsr
        LEFT JOIN member_ids i ON i.bioguide_id = m.bioguide_id
        WHERE v.congress = ? AND v.rollnumber = ?
        ORDER BY m.state_abbrev, m.bioname
        """,
        (congress, rollnumber),
    ).fetchall()
    return [
        {
            "icpsr": r["icpsr"],
            "bioguide_id": r["bioguide_id"],
            "lis_member_id": r["lis_member_id"],
            "name": r["full_name"] or r["bioname"],
            "state": r["state_abbrev"],
            "party": party_name(r["party_code"]),
            "party_code": r["party_code"],
            "position": cast_position(r["cast_code"]),
            "position_detail": cast_detail(r["cast_code"]),
            "nominate_dim1": r["nominate_dim1"],
        }
        for r in rows
    ]


def party_breakdown(positions: list[dict]) -> list[dict]:
    """Yea/Nay/other counts per party for one roll call."""
    agg: dict[str, dict] = {}
    for p in positions:
        b = agg.setdefault(p["party"], {"party": p["party"], "yea": 0, "nay": 0, "other": 0})
        if p["position"] == "Yea":
            b["yea"] += 1
        elif p["position"] == "Nay":
            b["nay"] += 1
        else:
            b["other"] += 1
    return sorted(agg.values(), key=lambda x: -(x["yea"] + x["nay"]))


def resolve_member(
    conn: sqlite3.Connection,
    *,
    bioguide_id: str | None = None,
    icpsr: int | None = None,
    name: str | None = None,
) -> dict:
    """Find one senator by bioguide id, ICPSR number, or name fragment.

    Raises ``NotFound`` when nothing matches and ``ValueError`` listing the
    candidates when a name is ambiguous — an ambiguous match must never be
    silently resolved to whichever row sorted first.
    """
    if bioguide_id:
        rows = conn.execute(
            "SELECT * FROM members WHERE bioguide_id = ? AND chamber='Senate' "
            "ORDER BY congress DESC",
            (bioguide_id,),
        ).fetchall()
    elif icpsr is not None:
        rows = conn.execute(
            "SELECT * FROM members WHERE icpsr = ? AND chamber='Senate' ORDER BY congress DESC",
            (icpsr,),
        ).fetchall()
    elif name:
        rows = conn.execute(
            "SELECT * FROM members WHERE bioname LIKE ? ESCAPE '\\' "
            "AND chamber='Senate' ORDER BY congress DESC",
            (like_contains(name.upper()),),
        ).fetchall()
    else:
        raise ValueError("pass one of bioguide_id, icpsr, or name")

    if not rows:
        raise NotFound(f"no senator matched {bioguide_id or icpsr or name!r}")

    distinct = {r["icpsr"] for r in rows}
    if len(distinct) > 1:
        seen, names = set(), []
        for r in rows:
            if r["icpsr"] in seen:
                continue
            seen.add(r["icpsr"])
            names.append(f"{r['bioname']} (icpsr {r['icpsr']}, {r['state_abbrev']})")
        raise ValueError(
            f"{name!r} matched {len(distinct)} senators: {'; '.join(names[:10])}. "
            "Re-run with bioguide_id or icpsr."
        )

    latest = rows[0]
    ids = conn.execute(
        "SELECT * FROM member_ids WHERE bioguide_id = ?", (latest["bioguide_id"],)
    ).fetchone()
    return {
        "icpsr": latest["icpsr"],
        "bioguide_id": latest["bioguide_id"],
        "lis_member_id": ids["lis_member_id"] if ids else None,
        "name": (ids["full_name"] if ids else None) or latest["bioname"],
        "bioname": latest["bioname"],
        "state": latest["state_abbrev"],
        "party": party_name(latest["party_code"]),
        "party_code": latest["party_code"],
        "nominate_dim1": latest["nominate_dim1"],
        "nominate_dim2": latest["nominate_dim2"],
        "fit": fit_quality(
            latest["nominate_geo_mean_probability"],
            latest["nominate_number_of_votes"],
            latest["nominate_number_of_errors"],
        ),
        "congresses": sorted({r["congress"] for r in rows}),
        "is_current": bool(ids["is_current"]) if ids else False,
    }


def member_votes(
    conn: sqlite3.Connection,
    icpsr: int,
    *,
    congress: int | None = None,
    position: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict:
    where: list[str] = ["v.icpsr = ?", "r.chamber = 'Senate'"]
    params: list[object] = [icpsr]
    if congress is not None:
        where.append("v.congress = ?")
        params.append(congress)
    codes = None
    if position:
        p = position.strip().lower()
        if p == "yea":
            codes = YEA_CODES
        elif p == "nay":
            codes = NAY_CODES
        else:
            raise ValueError("position must be 'yea' or 'nay'")
        where.append(f"v.cast_code IN ({','.join('?' * len(codes))})")
        params.extend(sorted(codes))
    clause = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM votes v "
        f"JOIN rollcalls r ON r.congress=v.congress AND r.rollnumber=v.rollnumber "
        f"WHERE {clause}",
        params,
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT r.*, v.cast_code FROM votes v "
        f"JOIN rollcalls r ON r.congress=v.congress AND r.rollnumber=v.rollnumber "
        f"WHERE {clause} ORDER BY r.date DESC, r.rollnumber DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    votes = []
    for r in rows:
        s = rollcall_summary(r)
        s["position"] = cast_position(r["cast_code"])
        s["position_detail"] = cast_detail(r["cast_code"])
        votes.append(s)
    return {"total_matching": total, "returned": len(votes), "offset": offset, "votes": votes}


def compare(
    conn: sqlite3.Connection,
    icpsr_a: int,
    icpsr_b: int,
    *,
    congress: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict:
    """Pairwise agreement over every roll call both members voted on.

    Only Yea/Nay pairs count. A vote where either member was absent, paired
    without a direction, or voted Present is excluded rather than scored as
    disagreement, and the excluded count is reported so the denominator is
    never a mystery.
    """
    where = [
        "a.congress = b.congress",
        "a.rollnumber = b.rollnumber",
        "a.icpsr = ?",
        "b.icpsr = ?",
        "r.chamber = 'Senate'",
    ]
    params: list[object] = [icpsr_a, icpsr_b]
    if congress is not None:
        where.append("a.congress = ?")
        params.append(congress)
    if start_date:
        where.append("r.date >= ?")
        params.append(start_date)
    if end_date:
        where.append("r.date <= ?")
        params.append(end_date)

    rows = conn.execute(
        f"SELECT a.cast_code AS ca, b.cast_code AS cb, r.congress, r.rollnumber, "
        f"r.date, r.bill_number, r.vote_question, r.vote_desc, r.dtl_desc, "
        f"r.session, r.clerk_rollnumber, r.vote_result, r.yea_count, r.nay_count "
        f"FROM votes a JOIN votes b ON a.congress=b.congress AND a.rollnumber=b.rollnumber "
        f"JOIN rollcalls r ON r.congress=a.congress AND r.rollnumber=a.rollnumber "
        f"WHERE {' AND '.join(where)}",
        params,
    ).fetchall()

    agree = disagree = skipped = 0
    disagreements: list[dict] = []
    for r in rows:
        ca, cb = r["ca"], r["cb"]
        if ca not in DECIDING_CODES or cb not in DECIDING_CODES:
            skipped += 1
            continue
        same = (ca in YEA_CODES) == (cb in YEA_CODES)
        if same:
            agree += 1
        else:
            disagree += 1
            if len(disagreements) < 25:
                d = rollcall_summary(r)
                d["position_a"] = cast_position(ca)
                d["position_b"] = cast_position(cb)
                disagreements.append(d)
    compared = agree + disagree
    return {
        "votes_compared": compared,
        "agreements": agree,
        "disagreements": disagree,
        "agreement_rate": round(agree / compared, 4) if compared else None,
        "excluded_no_position": skipped,
        "sample_disagreements": disagreements,
    }


def ideological_context(member_a: dict, member_b: dict, agreement_rate: float | None) -> dict:
    """Place a pairwise agreement rate next to the two members' ideal points.

    An agreement rate on its own is hard to read: 85% between two senators of
    the same party is unremarkable, while 85% across the aisle is not. Reporting
    the distance between their estimated positions, and whether they share a
    party, gives the number the context it needs without interpreting it.

    Deliberately does not predict an expected agreement rate. Mapping ideal-point
    distance onto a predicted rate needs the cutting-line geometry of the
    specific votes compared, and inventing a formula here would dress a guess up
    as a statistic.
    """
    a1, b1 = member_a.get("nominate_dim1"), member_b.get("nominate_dim1")
    a2, b2 = member_a.get("nominate_dim2"), member_b.get("nominate_dim2")
    d1 = round(abs(a1 - b1), 3) if a1 is not None and b1 is not None else None
    d2 = round(abs(a2 - b2), 3) if a2 is not None and b2 is not None else None
    same_party = (
        member_a.get("party_code") == member_b.get("party_code")
        if member_a.get("party_code") is not None and member_b.get("party_code") is not None
        else None
    )
    return {
        "same_party": same_party,
        "member_a_dim1": a1,
        "member_b_dim1": b1,
        "first_dimension_distance": d1,
        "second_dimension_distance": d2,
        "agreement_rate": agreement_rate,
        "note": (
            "First-dimension distance is the gap between the two members' "
            "DW-NOMINATE estimates, on the same scale as the ideal points "
            "themselves (roughly -1 to 1). It is offered as context for the "
            "agreement rate, not as a prediction of it: agreement also depends on "
            "which votes were held, and same-party pairs agree at high rates "
            "almost regardless of distance. Check each member's `fit` before "
            "treating a small distance as meaningful."
        ),
    }
