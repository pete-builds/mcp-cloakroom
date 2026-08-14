"""Query and ingest-coercion behaviour over the real sample rows."""

from __future__ import annotations

import pytest

from clients import analysis, loaders, queries
from clients.queries import NotFound
from tests.conftest import read_csv

CONGRESS = 119
ROLLNUMBER = 890

# A second, deliberately different roll call. 890 is perfectly party-line, which
# makes it the right fixture for tally and bridge checks and a useless one for
# analysis: with zero defectors and zero low-probability votes, every assertion
# about defection passes vacuously. 679 (2026-01-30, 71-29) carries 27 party
# defections and 24 votes the model scored below 0.5.
CONTESTED_ROLLNUMBER = 679


# ------------------------------------------------------------ ingest coercion


def test_float_formatted_party_codes_are_normalized(loaded) -> None:
    """Voteview exports ints as floats once a column has ever held a null.

    ``party_code`` ships as both "200" and "200.0" in the same file. Stored
    verbatim, every party filter silently misses the float-formatted rows.
    """
    rows = loaded.execute("SELECT DISTINCT party_code FROM members").fetchall()
    codes = [r["party_code"] for r in rows if r["party_code"] is not None]
    assert codes, "no party codes loaded"
    for code in codes:
        assert isinstance(code, int)
    # The fixture deliberately includes float-formatted source rows.
    assert (
        loaded.execute(
            "SELECT COUNT(*) AS n FROM members WHERE party_code IN (100, 200, 328)"
        ).fetchone()["n"]
        > 0
    )


def test_birth_years_survive_float_formatting(loaded) -> None:
    row = loaded.execute("SELECT born FROM members WHERE bioguide_id = 'S000033'").fetchone()
    assert row["born"] == 1941


def test_only_senate_rows_are_ingested(loaded) -> None:
    chambers = {
        r["chamber"] for r in loaded.execute("SELECT DISTINCT chamber FROM members").fetchall()
    }
    assert chambers == {"Senate"}


def test_ingest_is_idempotent(loaded) -> None:
    """Re-running must converge, not duplicate."""
    before = loaded.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
    loaders.insert_votes(loaded, iter(read_csv("votes_sample.csv")))
    after = loaded.execute("SELECT COUNT(*) AS n FROM votes").fetchone()["n"]
    assert before == after == 200


def test_legislators_load_skips_house_only_members(legislators_loaded) -> None:
    rows = legislators_loaded.execute("SELECT bioguide_id FROM member_ids").fetchall()
    ids = {r["bioguide_id"] for r in rows}
    assert "S000033" in ids
    assert "A000382" in ids
    assert "H999999" not in ids, "a House-only member reached a Senate table"


def test_bridge_resolves_a_member_with_no_icpsr_in_the_source(legislators_loaded) -> None:
    """The case that makes bioguide the correct join spine."""
    row = legislators_loaded.execute(
        "SELECT lis_member_id FROM member_ids WHERE bioguide_id = 'A000382'"
    ).fetchone()
    assert row["lis_member_id"] == "S428"
    joined = legislators_loaded.execute(
        "SELECT m.icpsr FROM member_ids i JOIN members m ON m.bioguide_id = i.bioguide_id "
        "WHERE i.lis_member_id = 'S428'"
    ).fetchone()
    assert joined is not None, "lis -> bioguide -> icpsr failed to resolve"


# ------------------------------------------------------------------- queries


def test_list_votes_paginates_and_reports_the_full_total(loaded) -> None:
    page = queries.list_votes(loaded, congress=CONGRESS, limit=2)
    assert page["returned"] == 2
    assert page["total_matching"] == 4
    assert page["votes"][0]["rollnumber"] == 890
    second = queries.list_votes(loaded, congress=CONGRESS, limit=2, offset=2)
    assert second["returned"] == 2
    assert second["offset"] == 2


def test_list_votes_exposes_both_numbering_schemes(loaded) -> None:
    v = queries.list_votes(loaded, congress=CONGRESS, limit=1)["votes"][0]
    assert v["rollnumber"] == 890
    assert v["vote_number"] == 231
    assert v["session"] == 2


def test_find_votes_normalizes_bill_number_punctuation(loaded) -> None:
    for form in ("S5271", "S. 5271", "s.5271", " S 5271 "):
        got = queries.find_votes(loaded, bill_number=form)
        assert got["total_matching"] >= 1, f"{form!r} matched nothing"
        assert got["votes"][0]["bill_number"] == "S5271"


def test_find_votes_searches_historical_descriptions(loaded) -> None:
    got = queries.find_votes(loaded, query="JUDICIAL COURTS", congress=1)
    assert got["total_matching"] == 1
    assert got["votes"][0]["date"] == "1789-07-17"


def test_find_votes_filters_by_result(loaded) -> None:
    got = queries.find_votes(loaded, result="Rejected", congress=CONGRESS)
    assert all("Rejected" in v["result"] for v in got["votes"])


def test_vote_positions_returns_the_whole_chamber(loaded) -> None:
    pos = queries.vote_positions(loaded, CONGRESS, ROLLNUMBER)
    assert len(pos) == 100
    assert {p["position"] for p in pos} <= {"Yea", "Nay", "Present", "Not Voting"}
    assert all(p["bioguide_id"] for p in pos)


def test_party_breakdown_totals_match_the_chamber(loaded) -> None:
    pos = queries.vote_positions(loaded, CONGRESS, ROLLNUMBER)
    breakdown = queries.party_breakdown(pos)
    assert sum(b["yea"] + b["nay"] + b["other"] for b in breakdown) == 100
    assert sum(b["yea"] for b in breakdown) == 52
    assert sum(b["nay"] for b in breakdown) == 46


def test_resolve_member_by_each_identifier(loaded) -> None:
    by_name = queries.resolve_member(loaded, name="SANDERS")
    by_bioguide = queries.resolve_member(loaded, bioguide_id="S000033")
    by_icpsr = queries.resolve_member(loaded, icpsr=by_name["icpsr"])
    assert by_name["icpsr"] == by_bioguide["icpsr"] == by_icpsr["icpsr"]
    assert by_name["state"] == "VT"
    assert by_name["party"] == "Independent"


def test_ambiguous_name_refuses_rather_than_guessing(loaded) -> None:
    """Silently picking one of several senators would be a correctness bug."""
    with pytest.raises(ValueError) as exc:
        queries.resolve_member(loaded, name="A")
    assert "matched" in str(exc.value)
    assert "icpsr" in str(exc.value)


def test_unknown_member_raises_not_found(loaded) -> None:
    with pytest.raises(NotFound):
        queries.resolve_member(loaded, bioguide_id="Z999999")


def test_member_votes_filters_by_position(loaded) -> None:
    m = queries.resolve_member(loaded, bioguide_id="S000033")
    all_votes = queries.member_votes(loaded, m["icpsr"], congress=CONGRESS)
    yeas = queries.member_votes(loaded, m["icpsr"], congress=CONGRESS, position="yea")
    nays = queries.member_votes(loaded, m["icpsr"], congress=CONGRESS, position="nay")
    assert yeas["total_matching"] + nays["total_matching"] <= all_votes["total_matching"]
    assert all(v["position"] == "Yea" for v in yeas["votes"])


def test_member_votes_rejects_a_bad_position(loaded) -> None:
    m = queries.resolve_member(loaded, bioguide_id="S000033")
    with pytest.raises(ValueError):
        queries.member_votes(loaded, m["icpsr"], position="maybe")


def test_compare_excludes_non_positions_from_the_denominator(loaded) -> None:
    """Absences must not be scored as disagreement."""
    voters = loaded.execute(
        "SELECT icpsr, cast_code FROM votes WHERE congress=? AND rollnumber=?",
        (CONGRESS, ROLLNUMBER),
    ).fetchall()
    yea = next(r["icpsr"] for r in voters if r["cast_code"] in (1, 2, 3))
    absent = next((r["icpsr"] for r in voters if r["cast_code"] == 9), None)
    assert absent is not None, "fixture should contain an absence"

    result = queries.compare(loaded, yea, absent, congress=CONGRESS)
    assert result["excluded_no_position"] >= 1
    assert result["votes_compared"] == result["agreements"] + result["disagreements"]


def test_compare_agreement_rate_is_a_fraction(loaded) -> None:
    voters = loaded.execute(
        "SELECT icpsr, cast_code FROM votes WHERE congress=? AND rollnumber=? "
        "AND cast_code IN (1,6)",
        (CONGRESS, ROLLNUMBER),
    ).fetchall()
    a, b = voters[0]["icpsr"], voters[1]["icpsr"]
    r = queries.compare(loaded, a, b, congress=CONGRESS)
    if r["votes_compared"]:
        assert 0.0 <= r["agreement_rate"] <= 1.0


def test_compare_with_no_overlap_returns_null_rate(loaded) -> None:
    r = queries.compare(loaded, 99999, 99998, congress=CONGRESS)
    assert r["votes_compared"] == 0
    assert r["agreement_rate"] is None


# ------------------------------------------------------------------ analysis


def test_defectors_identifies_party_line_breaks(loaded) -> None:
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, CONTESTED_ROLLNUMBER)
    assert d["defector_count"] > 0, "fixture must actually contain defections"
    assert d["parties"], "party breakdown missing"
    for p in d["parties"]:
        assert p["yea"] + p["nay"] == p["voting_members"]
    for defector in d["defectors"]:
        assert defector["position"] != defector["party_majority_position"]
        assert defector["side_of_party_median"] in {
            None,
            "toward_opposing_party",
            "away_from_opposing_party",
        }


def test_defectors_never_counts_absences(loaded) -> None:
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, ROLLNUMBER)
    total_voting = sum(p["voting_members"] for p in d["parties"])
    assert total_voting == 98, "the two absences must be excluded"


def test_a_party_line_vote_yields_no_defectors(loaded) -> None:
    """The negative case, so a defector-finder that always fires is caught.

    Roll call 890 split perfectly along party lines. Anything reporting
    defections here is reporting an artifact.
    """
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, ROLLNUMBER)
    assert d["defector_count"] == 0
    assert d["defectors"] == []


def test_defection_rates_rank_the_chamber(loaded) -> None:
    r = analysis.defection_rates(loaded, CONGRESS, min_votes=1, limit=10)
    assert r["rollcalls_analyzed"] >= 1
    assert len(r["members"]) <= 10
    rates = [m["defection_rate"] for m in r["members"]]
    assert rates == sorted(rates, reverse=True), "results must be ranked"
    for m in r["members"]:
        assert 0.0 <= m["defection_rate"] <= 1.0
        assert m["defections"] <= m["eligible_votes"]


def test_defection_rates_respects_the_min_votes_floor(loaded) -> None:
    r = analysis.defection_rates(loaded, CONGRESS, min_votes=10_000)
    assert r["members"] == []


def test_analysis_states_its_definitions(loaded) -> None:
    """A ranking without stated definitions is not interpretable."""
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, CONTESTED_ROLLNUMBER)
    r = analysis.defection_rates(loaded, CONGRESS, min_votes=1)
    assert {"defection", "model_probability", "side_of_party_median", "excluded"} <= set(
        d["definitions"]
    )
    assert {"defection_rate", "unpredicted_rate", "ranking"} <= set(r["definitions"])
    u = analysis.unexpected_votes(loaded, CONGRESS)
    assert "interpretation" in u["definitions"]
    assert "not evidence of inconsistency" in u["definitions"]["interpretation"]


# ------------------------------------------- model probability and its scale


def test_prob_column_is_normalized_to_a_probability(loaded) -> None:
    """Voteview publishes `prob` on a 0-100 scale.

    Read as though it were already fractional, every value lands above 1.0, the
    unexpected-vote threshold never fires, and the tool silently reports that
    nothing was ever surprising. The bug looks like a finding.
    """
    raw = loaded.execute(
        "SELECT prob FROM votes WHERE congress=119 AND rollnumber=890 AND prob IS NOT NULL"
    ).fetchall()
    assert raw, "fixture carries no probabilities"
    assert any(r["prob"] > 1.0 for r in raw), "fixture no longer exercises the 0-100 scale"

    for r in raw:
        p = analysis._prob(r["prob"])
        assert 0.0 <= p <= 1.0


def test_prob_normalization_handles_both_scales() -> None:
    assert analysis._prob(97.27) == 0.9727
    assert analysis._prob(0.9727) == 0.9727
    assert analysis._prob(None) is None
    assert analysis._prob(0) == 0.0
    assert analysis._prob(100) == 1.0


def test_defectors_report_model_probability_and_fit(loaded) -> None:
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, CONTESTED_ROLLNUMBER)
    assert d["defector_count"] > 0
    assert (
        d["defections_the_model_predicted"] + d["defections_the_model_did_not_predict"]
        == (d["defector_count"])
    )
    for defector in d["defectors"]:
        assert defector["model_probability"] is None or 0.0 <= defector["model_probability"] <= 1.0
        assert "fit" in defector
        assert defector["side_of_party_median"] in (
            None,
            "toward_opposing_party",
            "away_from_opposing_party",
        )


def test_defectors_are_sorted_least_predicted_first(loaded) -> None:
    d = analysis.defectors_for_rollcall(loaded, CONGRESS, CONTESTED_ROLLNUMBER)
    probs = [x["model_probability"] for x in d["defectors"] if x["model_probability"] is not None]
    assert probs == sorted(probs)


def test_unexpected_votes_respects_the_threshold(loaded) -> None:
    strict = analysis.unexpected_votes(loaded, CONGRESS, max_probability=0.5)
    assert strict["unexpected_found"] > 0, "fixture must contain low-probability votes"
    loose = analysis.unexpected_votes(loaded, CONGRESS, max_probability=0.99)
    assert loose["unexpected_found"] >= strict["unexpected_found"]
    for v in strict["votes"]:
        assert v["model_probability"] < 0.5


def test_unexpected_votes_scopes_to_a_member(loaded) -> None:
    icpsr = loaded.execute("SELECT icpsr FROM votes LIMIT 1").fetchone()["icpsr"]
    scoped = analysis.unexpected_votes(loaded, CONGRESS, icpsr=icpsr, max_probability=1.0)
    assert scoped["votes_considered"] >= 1
    for v in scoped["votes"]:
        assert v["icpsr"] == icpsr


def test_fit_quality_computes_an_error_rate() -> None:
    from clients.codes import fit_quality

    f = fit_quality(0.96267, 829, 9)
    assert f["classification_error_rate"] == round(9 / 829, 4)
    assert f["votes_used_by_model"] == 829
    # Missing inputs must degrade to null, not to a fabricated zero.
    assert fit_quality(None, None, None)["classification_error_rate"] is None
    assert fit_quality(None, 0, 0)["classification_error_rate"] is None


def test_defection_and_unpredicted_rates_are_independent(loaded) -> None:
    """They measure different things and must be reported separately."""
    r = analysis.defection_rates(loaded, CONGRESS, min_votes=1, limit=100)
    for m in r["members"]:
        assert 0.0 <= m["defection_rate"] <= 1.0
        assert 0.0 <= m["unpredicted_rate"] <= 1.0
        assert "fit" in m
    assert "unpredicted_rate" in r["definitions"]
