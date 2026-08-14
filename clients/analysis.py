"""Ideological analysis over DW-NOMINATE estimates.

This is the layer the project exists for. Vote records are available from
several places; the ideal points joined to them are not, and the questions worth
asking live in the join.

Two different notions of "went against expectation" are kept strictly apart,
because collapsing them is the most common way to be confidently wrong here:

**Party defection** is observable and model-free. The member voted against the
position most of their party took. No estimate is involved.

**A model-unexpected vote** is probabilistic. Voteview publishes ``prob``, the
estimated probability that the member cast the vote they are recorded as
casting, given the fitted model. A low value means the model did not predict
that vote. This says nothing about party.

The two come apart constantly, and the gap is the interesting part. A moderate
crossing party lines is often a defection the model predicts perfectly well. A
member voting with their party can be the least predictable vote of the day.
Reporting only one of them, and calling it "breaking ranks", loses that.

Nothing here infers motive. See ``codes.NOMINATE_CAVEAT``.
"""

from __future__ import annotations

import sqlite3
from statistics import median

from clients.codes import DECIDING_CODES, NAY_CODES, YEA_CODES, fit_quality, party_name

# Parties with fewer than this many voting members have no meaningful "party
# line" to defect from; a two-member caucus median is noise, not a center.
MIN_PARTY_SIZE = 5

# Below this model probability a recorded vote is reported as unexpected. 0.5 is
# the natural cut: the model assigned the observed vote less weight than its
# alternative. It is a threshold on a continuous quantity, not a discovery, and
# the underlying probability is always returned alongside the flag.
UNEXPECTED_PROB = 0.5


def _prob(raw: float | None) -> float | None:
    """Normalize Voteview's ``prob`` column to a 0-1 probability.

    The published file stores it on a 0-100 scale. Treating it as already
    fractional would silently classify every vote as expected, so the scale is
    normalized in one place rather than at each call site.
    """
    if raw is None:
        return None
    p = float(raw)
    if p > 1.0:
        p = p / 100.0
    return round(min(max(p, 0.0), 1.0), 4)


def _members_on_rollcall(conn: sqlite3.Connection, congress: int, rollnumber: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT v.icpsr, v.cast_code, v.prob, m.bioname, m.bioguide_id,
               m.state_abbrev, m.party_code, m.nominate_dim1, m.nominate_dim2,
               m.nominate_geo_mean_probability, m.nominate_number_of_votes,
               m.nominate_number_of_errors, i.full_name
        FROM votes v
        JOIN members m ON m.congress = v.congress AND m.icpsr = v.icpsr
        LEFT JOIN member_ids i ON i.bioguide_id = m.bioguide_id
        WHERE v.congress = ? AND v.rollnumber = ? AND m.chamber = 'Senate'
        """,
        (congress, rollnumber),
    ).fetchall()
    return [dict(r) for r in rows]


def _party_lines(voting: list[dict]) -> tuple[dict[int, str | None], dict[int, float], list[dict]]:
    """Per party: the majority position, the median ideal point, and the counts."""
    by_party: dict[int, list[dict]] = {}
    for m in voting:
        if m["party_code"] is not None:
            by_party.setdefault(m["party_code"], []).append(m)

    centers: dict[int, float] = {}
    for pc, group in by_party.items():
        dims = [g["nominate_dim1"] for g in group if g["nominate_dim1"] is not None]
        if dims:
            centers[pc] = median(dims)

    lines: dict[int, str | None] = {}
    stats: list[dict] = []
    for pc, group in by_party.items():
        yeas = sum(1 for g in group if g["cast_code"] in YEA_CODES)
        nays = sum(1 for g in group if g["cast_code"] in NAY_CODES)
        # Too small to have a line, or split exactly evenly, means there is no
        # majority position to be inconsistent with. Reported as null rather
        # than resolved by a tiebreak.
        line = (
            None
            if (len(group) < MIN_PARTY_SIZE or yeas == nays)
            else ("Yea" if yeas > nays else "Nay")
        )
        lines[pc] = line
        stats.append(
            {
                "party": party_name(pc),
                "party_code": pc,
                "voting_members": len(group),
                "yea": yeas,
                "nay": nays,
                "majority_position": line,
                "median_dim1": round(centers[pc], 3) if pc in centers else None,
                "unanimous": yeas == 0 or nays == 0,
            }
        )
    stats.sort(key=lambda p: -p["voting_members"])
    return lines, centers, stats


def defectors_for_rollcall(conn: sqlite3.Connection, congress: int, rollnumber: int) -> dict:
    """Members whose vote differed from their party's majority position.

    Each defector is reported with the model's own probability for that vote, so
    a caller can see whether the defection was predictable. Both facts are
    returned because they answer different questions.
    """
    members = _members_on_rollcall(conn, congress, rollnumber)
    voting = [m for m in members if m["cast_code"] in DECIDING_CODES]
    lines, centers, party_stats = _party_lines(voting)

    scored = [pc for pc, line in lines.items() if line]

    defectors: list[dict] = []
    for m in voting:
        pc = m["party_code"]
        line = lines.get(pc)
        if not line:
            continue
        position = "Yea" if m["cast_code"] in YEA_CODES else "Nay"
        if position == line:
            continue

        dim1 = m["nominate_dim1"]
        center = centers.get(pc)
        distance = side = None
        if dim1 is not None and center is not None:
            distance = round(dim1 - center, 3)
            others = [centers[o] for o in scored if o != pc and o in centers]
            if others:
                opposing = sum(others) / len(others)
                toward = (dim1 - center) * (1.0 if opposing > center else -1.0)
                # Purely geometric: which side of their party's median the
                # member sits on, relative to the other party. No label about
                # what kind of politician that makes them.
                side = "toward_opposing_party" if toward > 0 else "away_from_opposing_party"

        p = _prob(m["prob"])
        defectors.append(
            {
                "icpsr": m["icpsr"],
                "bioguide_id": m["bioguide_id"],
                "name": m["full_name"] or m["bioname"],
                "state": m["state_abbrev"],
                "party": party_name(pc),
                "position": position,
                "party_majority_position": line,
                "nominate_dim1": dim1,
                "party_median_dim1": round(center, 3) if center is not None else None,
                "distance_from_party_median": distance,
                "side_of_party_median": side,
                "model_probability": p,
                "model_expected_this_vote": None if p is None else p >= UNEXPECTED_PROB,
                "fit": fit_quality(
                    m["nominate_geo_mean_probability"],
                    m["nominate_number_of_votes"],
                    m["nominate_number_of_errors"],
                ),
            }
        )

    # Least-predicted first: the ones the model handles worst are the ones a
    # caller most likely wants to look at, and it avoids ranking by a
    # value-laden score.
    defectors.sort(key=lambda d: (d["model_probability"] is None, d["model_probability"]))

    predicted = sum(1 for d in defectors if d["model_expected_this_vote"] is True)
    return {
        "congress": congress,
        "rollnumber": rollnumber,
        "defector_count": len(defectors),
        "defections_the_model_predicted": predicted,
        "defections_the_model_did_not_predict": len(defectors) - predicted,
        "parties": party_stats,
        "defectors": defectors,
        "definitions": {
            "defection": (
                "A recorded Yea/Nay differing from the member's party's majority "
                "position on this vote. Observable, not modelled."
            ),
            "model_probability": (
                "Voteview's estimated probability that this member cast the vote "
                "recorded, given the fitted DW-NOMINATE model. Low values mean the "
                "model did not predict the vote, not that the vote was improper."
            ),
            "side_of_party_median": (
                "Geometry only: whether the member's first-dimension estimate sits "
                "on the side of their party's median nearer to, or further from, "
                "the other party's median."
            ),
            "excluded": (
                f"Parties with fewer than {MIN_PARTY_SIZE} voting members, and "
                "parties split exactly evenly, have no majority position and are "
                "not scored. Absences, Present votes, and undirected pairs are "
                "excluded throughout."
            ),
        },
    }


def defection_rates(
    conn: sqlite3.Connection, congress: int, *, min_votes: int = 20, limit: int = 25
) -> dict:
    """How often each member voted against their party's majority, over a congress.

    Computed per roll call in Python because each vote's party line has to be
    established before anyone can be scored against it.
    """
    rows = conn.execute(
        """
        SELECT v.rollnumber, v.icpsr, v.cast_code, v.prob, m.party_code, m.bioname,
               m.bioguide_id, m.state_abbrev, m.nominate_dim1,
               m.nominate_geo_mean_probability, m.nominate_number_of_votes,
               m.nominate_number_of_errors, i.full_name
        FROM votes v
        JOIN members m ON m.congress = v.congress AND m.icpsr = v.icpsr
        LEFT JOIN member_ids i ON i.bioguide_id = m.bioguide_id
        WHERE v.congress = ? AND m.chamber = 'Senate'
        """,
        (congress,),
    ).fetchall()

    per_roll: dict[int, list] = {}
    for r in rows:
        if r["cast_code"] in DECIDING_CODES and r["party_code"] is not None:
            per_roll.setdefault(r["rollnumber"], []).append(r)

    tally: dict[int, dict] = {}
    for group in per_roll.values():
        by_party: dict[int, list] = {}
        for g in group:
            by_party.setdefault(g["party_code"], []).append(g)
        lines = {}
        for pc, members in by_party.items():
            if len(members) < MIN_PARTY_SIZE:
                continue
            yeas = sum(1 for m in members if m["cast_code"] in YEA_CODES)
            nays = len(members) - yeas
            if yeas != nays:
                lines[pc] = "Yea" if yeas > nays else "Nay"
        for g in group:
            line = lines.get(g["party_code"])
            if not line:
                continue
            t = tally.setdefault(
                g["icpsr"],
                {
                    "icpsr": g["icpsr"],
                    "bioguide_id": g["bioguide_id"],
                    "name": g["full_name"] or g["bioname"],
                    "state": g["state_abbrev"],
                    "party": party_name(g["party_code"]),
                    "nominate_dim1": g["nominate_dim1"],
                    "eligible_votes": 0,
                    "defections": 0,
                    "unpredicted_votes": 0,
                    "_fit": (
                        g["nominate_geo_mean_probability"],
                        g["nominate_number_of_votes"],
                        g["nominate_number_of_errors"],
                    ),
                },
            )
            t["eligible_votes"] += 1
            position = "Yea" if g["cast_code"] in YEA_CODES else "Nay"
            if position != line:
                t["defections"] += 1
            p = _prob(g["prob"])
            if p is not None and p < UNEXPECTED_PROB:
                t["unpredicted_votes"] += 1

    out: list[dict] = []
    for t in tally.values():
        if t["eligible_votes"] < min_votes:
            continue
        t["defection_rate"] = round(t["defections"] / t["eligible_votes"], 4)
        t["unpredicted_rate"] = round(t["unpredicted_votes"] / t["eligible_votes"], 4)
        t["fit"] = fit_quality(*t.pop("_fit"))
        out.append(t)
    out.sort(key=lambda x: -x["defection_rate"])
    return {
        "congress": congress,
        "rollcalls_analyzed": len(per_roll),
        "members_ranked": len(out),
        "min_votes": min_votes,
        "members": out[:limit],
        "definitions": {
            "defection_rate": (
                "Share of the member's Yea/Nay votes cast against their party's "
                "majority position, over roll calls where their party had at least "
                f"{MIN_PARTY_SIZE} voting members and was not split evenly."
            ),
            "unpredicted_rate": (
                "Share of the same votes the DW-NOMINATE model assigned a "
                f"probability below {UNEXPECTED_PROB}. Independent of party: a "
                "member can have a high defection rate and a low unpredicted rate "
                "if their crossings are exactly what their estimated position "
                "implies."
            ),
            "ranking": (
                "Ordered by defection_rate. Check each member's `fit` before "
                "comparing: a member with few votes or a high classification error "
                "rate has a poorly identified position."
            ),
        },
    }


def unexpected_votes(
    conn: sqlite3.Connection,
    congress: int,
    *,
    icpsr: int | None = None,
    rollnumber: int | None = None,
    max_probability: float = UNEXPECTED_PROB,
    limit: int = 25,
) -> dict:
    """Recorded votes the fitted model did not predict.

    Scoped to one member, one roll call, or a whole congress. This is the
    model-relative counterpart to ``defectors_for_rollcall``: it asks which
    votes are hard to reconcile with a member's estimated position, whether or
    not they broke with their party.
    """
    where = [
        "v.congress = ?",
        "m.chamber = 'Senate'",
        "v.prob IS NOT NULL",
        f"v.cast_code IN ({','.join(str(c) for c in sorted(DECIDING_CODES))})",
    ]
    params: list[object] = [congress]
    if icpsr is not None:
        where.append("v.icpsr = ?")
        params.append(icpsr)
    if rollnumber is not None:
        where.append("v.rollnumber = ?")
        params.append(rollnumber)

    rows = conn.execute(
        f"""
        SELECT v.rollnumber, v.icpsr, v.cast_code, v.prob, m.bioname, m.bioguide_id,
               m.state_abbrev, m.party_code, m.nominate_dim1,
               m.nominate_geo_mean_probability, m.nominate_number_of_votes,
               m.nominate_number_of_errors, i.full_name,
               r.date, r.bill_number, r.vote_question, r.vote_result, r.vote_desc,
               r.dtl_desc, r.session, r.clerk_rollnumber
        FROM votes v
        JOIN members m ON m.congress = v.congress AND m.icpsr = v.icpsr
        JOIN rollcalls r ON r.congress = v.congress AND r.rollnumber = v.rollnumber
        LEFT JOIN member_ids i ON i.bioguide_id = m.bioguide_id
        WHERE {" AND ".join(where)}
        """,
        params,
    ).fetchall()

    considered = 0
    found: list[dict] = []
    for r in rows:
        considered += 1
        p = _prob(r["prob"])
        if p is None or p >= max_probability:
            continue
        found.append(
            {
                "congress": congress,
                "rollnumber": r["rollnumber"],
                "session": r["session"],
                "vote_number": r["clerk_rollnumber"],
                "date": r["date"],
                "bill_number": r["bill_number"],
                "question": r["vote_question"],
                "result": r["vote_result"],
                "description": r["vote_desc"] or r["dtl_desc"],
                "icpsr": r["icpsr"],
                "bioguide_id": r["bioguide_id"],
                "name": r["full_name"] or r["bioname"],
                "state": r["state_abbrev"],
                "party": party_name(r["party_code"]),
                "position": "Yea" if r["cast_code"] in YEA_CODES else "Nay",
                "nominate_dim1": r["nominate_dim1"],
                "model_probability": p,
                "fit": fit_quality(
                    r["nominate_geo_mean_probability"],
                    r["nominate_number_of_votes"],
                    r["nominate_number_of_errors"],
                ),
            }
        )

    found.sort(key=lambda d: d["model_probability"])
    return {
        "congress": congress,
        "votes_considered": considered,
        "unexpected_found": len(found),
        "max_probability": max_probability,
        "votes": found[:limit],
        "definitions": {
            "model_probability": (
                "Voteview's estimated probability that the member cast the vote "
                "recorded, given the fitted DW-NOMINATE model."
            ),
            "unexpected": (
                f"Reported when model_probability is below {max_probability}. This "
                "is a threshold on a continuous quantity; a vote at 0.49 and one at "
                "0.51 are not meaningfully different, and the probability is "
                "returned so the cut can be judged rather than trusted."
            ),
            "interpretation": (
                "A low probability means the model, fitted to this member's overall "
                "voting record, does not account for this particular vote. It is "
                "not evidence of inconsistency, dishonesty, or a change of "
                "position. Procedural votes, local interests, absences from the "
                "pattern, and simple model error all look identical here."
            ),
        },
    }
